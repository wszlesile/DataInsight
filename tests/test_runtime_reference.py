import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field

import agent
from agent import invoker, tools
from agent.runtime_contract_registry import (
    load_runtime_contract_pack,
    load_runtime_contract_reference,
    runtime_contract_pack_ids,
)
from agent.runtime_prompt_loader import load_runtime_prompt
from agent.runtime_reference import read_runtime_reference


def reference_runtime(output_type="standard", clarification_mode="same_turn"):
    return SimpleNamespace(context=agent.CustomContext(
        username="test", output_contract={"type": output_type},
        clarification_mode=clarification_mode,
    ))


def test_complete_references_are_reachable_and_have_live_api_signatures():
    for contract_id in runtime_contract_pack_ids():
        text = load_runtime_contract_reference(contract_id)
        assert text.startswith(f"# {contract_id}")
        assert "<!-- runtime-helper-reference -->" not in text
        assert "<!-- runtime-prompt-omit" not in text
        # A reference includes its ordinary contract plus authoring examples.
        ordinary = load_runtime_contract_pack(contract_id)
        assert all(line in text for line in ordinary.splitlines() if line.strip())

    text = load_runtime_contract_reference("query.relational")
    assert "canonical-runtime-example: relational" in text
    assert "query_data_with_sql(*," in text
    assert "save_analysis_result(" in text
    assert "canonical-runtime-example" not in load_runtime_prompt(("sql",))


def test_batch_lookup_keeps_valid_results_and_never_reads_arbitrary_paths():
    result = read_runtime_reference.func(
        reference_runtime(), ["query.relational", "../../.env", "query.relational", "query.time_series"],
    )
    assert result["status"] == "partial"
    assert result["unavailable_contract_ids"] == ["../../.env"]
    assert [item["contract_id"] for item in result["references"]] == ["query.relational", "query.time_series"]
    for item in result["references"]:
        assert item["sha256"] == hashlib.sha256(item["content"].encode()).hexdigest()
    assert read_runtime_reference.func(reference_runtime(), ["unknown"])["status"] == "unavailable"


@pytest.mark.parametrize("output_type,result_id", [
    ("standard", "result.artifacts"), ("text", "result.text"), ("json", "result.json"),
])
@pytest.mark.parametrize("clarification_mode", ["same_turn", "disabled"])
def test_reference_does_not_introduce_a_conflicting_invocation_policy(output_type, result_id, clarification_mode):
    result = read_runtime_reference.func(reference_runtime(output_type, clarification_mode), [
        "result.artifacts", "result.text", "result.json", "interaction.clarification", "interaction.no_clarification",
    ])
    interaction_id = "interaction.clarification" if clarification_mode == "same_turn" else "interaction.no_clarification"
    assert {item["contract_id"] for item in result["references"]} == {result_id, interaction_id}
    assert "context.followup" in result["available_contract_ids"]


@pytest.mark.parametrize("output_type", ["text", "json"])
def test_query_reference_does_not_reintroduce_standard_output_through_an_example(output_type):
    result = read_runtime_reference.func(reference_runtime(output_type, "disabled"), [
        "query.relational", "query.local_file", "result." + output_type,
    ])
    assert result["status"] == "success"
    text = "\n".join(item["content"] for item in result["references"])
    assert "canonical-runtime-example" not in text
    assert "charts=charts" not in text
    assert "query_data_with_sql(*," in text
    assert "query_local_file(*," in text


@pytest.mark.parametrize("clarification_mode", ["same_turn", "disabled"])
def test_reference_observation_stays_in_real_react_with_probe_and_repair(monkeypatch, clarification_mode):
    class ScriptedModel(FakeMessagesListChatModel):
        seen: list = Field(default_factory=list)

        def bind_tools(self, tool_list, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            self.seen.append(list(messages))
            return super()._generate(messages, **kwargs)

    calls = [
        ("read_runtime_reference", {"contract_ids": ["query.relational", "execution.core"]}),
        ("probe_analysis_data", {"code": "result = None", "used_datasource_ids": [1]}),
        ("submit_analysis_program", {"code": "result = None", "used_datasource_ids": [1]}),
        ("submit_analysis_program", {"code": "result = None", "used_datasource_ids": [1]}),
    ]
    llm = ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
        "id": str(index), "name": name, "args": args,
    }]) for index, (name, args) in enumerate(calls)])
    monkeypatch.setattr(agent, "create_data_insight_model", lambda _: llm)
    observed = []

    def execute(*, runtime, execution_mode, **kwargs):
        observed.append(execution_mode)
        msg = ToolMessage(content=f"execution-{len(observed)}", tool_call_id=runtime.tool_call_id)
        return tools._terminal_tool_success(msg) if len(observed) == 3 else msg

    monkeypatch.setattr(tools.execute_python, "func", execute)
    graph = agent.create_data_insight_agent(clarification_mode=clarification_mode)
    result = graph.invoke({"messages": [HumanMessage("Analyze with the supplied business definitions")]},
                          context=reference_runtime(clarification_mode=clarification_mode).context)
    assert observed == ["probe", "analysis", "analysis"]
    assert len(llm.seen) == 4
    last_observations = [msg for msg in llm.seen[-1] if isinstance(msg, ToolMessage)]
    assert [msg.tool_call_id for msg in last_observations] == ["0", "1", "2"]
    assert json.loads(last_observations[0].content)["tool"] == "read_runtime_reference"
    assert "save_analysis_result(" in last_observations[0].content
    assert result["analysis_complete"] is True
    assert len([msg for msg in result["messages"] if isinstance(msg, HumanMessage)]) == 1


def test_reference_does_not_pollute_execution_feedback_or_frontend_progress():
    runtime = SimpleNamespace(short_term_memory={})
    invoker._update_short_term_memory_from_tool_payload(runtime, read_runtime_reference.func(
        reference_runtime(), ["query.relational"],
    ))
    assert runtime.short_term_memory == {}
    progress = invoker._format_tool_call_message({"name": "read_runtime_reference"})
    assert "用法说明" in progress
    assert "分析代码" not in progress
    reference_message = ToolMessage(content=json.dumps(read_runtime_reference.func(
        reference_runtime(), ["context.followup"],
    )), tool_call_id="reference")
    assert invoker._extract_invoke_result([reference_message]) == ("", "", [], [], False)
    previous = [HumanMessage("Use the supplied business definitions"), reference_message]
    merged = invoker._merge_agent_round_messages(previous, previous)
    assert merged == previous


def test_core_and_complete_reference_explain_the_actual_field_scope_projection():
    for contract_id in ("context.analysis", "query.relational"):
        for loader in (load_runtime_contract_pack, load_runtime_contract_reference):
            text = loader(contract_id)
            assert "allowed_fields_ref=metadata_schema.properties" in text
            assert "query_contract.allowed_fields" in text


@pytest.mark.parametrize("is_analysis", [True, False])
def test_rerun_instruction_consumes_routing_instead_of_historical_success(monkeypatch, is_analysis):
    history_query = Mock(side_effect=AssertionError("Routing must not depend on previous success"))
    monkeypatch.setattr(invoker, "_rerun_has_prior_analysis_result", history_query)
    runtime = SimpleNamespace(
        is_rerun=True, turn=SimpleNamespace(user_query="Analyze the current data"),
        context_mode="fresh_analysis", conversation=SimpleNamespace(id=1),
    )
    instruction = invoker._build_rerun_instruction(None, runtime, analysis_request_expected=is_analysis)
    assert ("submit_analysis_program" in instruction) == is_analysis
    assert ("已分流为普通问答" in instruction) == (not is_analysis)
    assert "自行判断是否需要调用工具" not in instruction
    history_query.assert_not_called()


def test_non_rerun_instruction_is_empty():
    assert invoker._build_rerun_instruction(
        None, SimpleNamespace(is_rerun=False), analysis_request_expected=True,
    ) == ""
