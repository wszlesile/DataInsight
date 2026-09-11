import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END

from agent import invoker
from agent.context_engineering_runtime import CustomContext
from agent.runtime_driver_state import (
    RUNTIME_WORK_PACKET_MEMORY_KEY,
    RUNTIME_WORK_PACKET_PAYLOAD_KEY,
    new_runtime_work_packet,
    reduce_runtime_tool_result,
    runtime_work_packet_from_dict,
)
from agent.tools import (
    _build_analysis_clarification_payload,
    request_analysis_clarification,
)


def _runtime_for_persisted_clarifications(*, is_rerun: bool):
    return SimpleNamespace(
        is_rerun=is_rerun,
        short_term_memory={},
        conversation=SimpleNamespace(id=66, insight_namespace_id=19),
        turn=SimpleNamespace(id=99, user_query="分析七月批次质量"),
        active_datasource_snapshot={},
        analysis_task_id=None,
    )


def _request_for_persisted_clarifications():
    return invoker.AgentRequest(
        username="tester",
        namespace_id=19,
        user_message="分析七月批次质量",
        conversation_id=66,
    )


def test_rerun_restores_raw_clarification_answers_without_compilation(monkeypatch):
    persisted = [{
        "clarification_id": "clarify-1",
        "clarification_item_id": "time-attribution",
        "question": "OOS批次按哪个时间归属？",
        "semantic_scope": {
            "subject": "OOS批次率",
            "ambiguity_kind": "time_attribution",
        },
        "answer_text": "按批次开始时间",
        "resolved_value": "按批次开始时间归属。",
    }]
    monkeypatch.setattr(invoker, "session_scope", lambda **_kwargs: nullcontext(object()))
    monkeypatch.setattr(
        invoker,
        "ConversationContextService",
        lambda _session: SimpleNamespace(
            get_turn_clarification_answers=lambda _turn_id: persisted
        ),
    )
    runtime = _runtime_for_persisted_clarifications(is_rerun=True)
    request = _request_for_persisted_clarifications()

    invoker._hydrate_persisted_runtime_clarifications(runtime, request)
    context = invoker._build_agent_context(request, runtime)
    instruction = invoker._build_runtime_clarification_answer_instruction(
        request.clarification_context
    )

    assert request.clarification_context["user_clarifications"] == persisted
    assert "authoritative_business_facts" not in CustomContext.model_fields
    assert not hasattr(context, "authoritative_business_facts")
    assert '"item_id":"time-attribution"' in instruction
    assert '"resolved_value":"按批次开始时间归属。"' in instruction
    assert "semantic_decision" not in instruction
    assert "probe_requirement_specs" not in instruction


def test_new_analysis_does_not_load_persisted_clarifications(monkeypatch):
    def unexpected_session_scope(**_kwargs):
        raise AssertionError("fresh analysis must not query persisted clarification history")

    monkeypatch.setattr(invoker, "session_scope", unexpected_session_scope)
    runtime = _runtime_for_persisted_clarifications(is_rerun=False)
    request = _request_for_persisted_clarifications()

    invoker._hydrate_persisted_runtime_clarifications(runtime, request)

    assert request.clarification_context is None
    assert runtime.short_term_memory["persisted_clarifications_loaded"] is True


def test_current_answer_replaces_only_the_same_clarification_item(monkeypatch):
    persisted = [{
        "clarification_id": "clarify-1",
        "clarification_item_id": "metric-definition",
        "question": "一次合格率如何定义？",
        "answer_text": "按检测记录计算。",
        "resolved_value": "按检测记录计算。",
        "semantic_scope": {
            "subject": "一次合格率",
            "ambiguity_kind": "metric_definition",
        },
    }, {
        "clarification_id": "clarify-1",
        "clarification_item_id": "population-definition",
        "question": "一次合格率总体如何定义？",
        "answer_text": "按生产批次计算。",
        "resolved_value": "按生产批次计算。",
        "semantic_scope": {
            "subject": "一次合格率",
            "ambiguity_kind": "metric_definition",
        },
    }]
    current = {
        **persisted[0],
        "answer_text": "按批次首次非复检记录计算。",
        "resolved_value": "按批次首次非复检记录计算。",
    }
    monkeypatch.setattr(invoker, "session_scope", lambda **_kwargs: nullcontext(object()))
    monkeypatch.setattr(
        invoker,
        "ConversationContextService",
        lambda _session: SimpleNamespace(
            get_turn_clarification_answers=lambda _turn_id: persisted
        ),
    )
    runtime = _runtime_for_persisted_clarifications(is_rerun=True)
    request = _request_for_persisted_clarifications()
    request.clarification_context = {"user_clarifications": [current]}

    invoker._hydrate_persisted_runtime_clarifications(runtime, request)

    merged = request.clarification_context["user_clarifications"]
    assert [item["clarification_item_id"] for item in merged] == [
        "population-definition",
        "metric-definition",
    ]
    assert merged[-1]["resolved_value"] == "按批次首次非复检记录计算。"


def test_clarification_payload_supports_single_item_and_free_text():
    payload = _build_analysis_clarification_payload([{
        "ambiguity_kind": "metric_definition",
        "subject": "批次统计周期归属",
        "question": "批次按开始时间还是结束时间归属统计周期？",
        "options": [],
        "allow_free_text": True,
    }])

    assert payload["question"] == "批次按开始时间还是结束时间归属统计周期？"
    assert payload["allow_free_text"] is True
    assert payload["semantic_scope"] == {
        "subject": "批次统计周期归属",
        "ambiguity_kind": "metric_definition",
    }
    assert len(payload["items"]) == 1
    assert payload["item_id"]


def test_clarification_payload_batches_distinct_business_ambiguities():
    payload = _build_analysis_clarification_payload([
        {
            "subject": "统计总体",
            "question": "统计总体按合同还是合同明细？",
            "options": [
                {"id": "contract", "label": "按合同", "value": "每份合同计一次"},
                {"id": "detail", "label": "按明细", "value": "每条合同明细计一次"},
            ],
            "allow_free_text": True,
        },
        {
            "subject": "时间归属",
            "question": "按签订时间还是生效时间归属？",
            "options": [],
            "allow_free_text": True,
        },
    ])

    assert len(payload["items"]) == 2
    assert payload["semantic_scope"]["subjects"] == ["统计总体", "时间归属"]


def test_clarification_payload_keeps_distinct_kinds_for_one_subject():
    payload = _build_analysis_clarification_payload([
        {
            "ambiguity_kind": "metric_definition",
            "subject": "OOS批次率",
            "question": "OOS批次率的分母按什么总体？",
            "options": [],
            "allow_free_text": True,
        },
        {
            "ambiguity_kind": "time_attribution",
            "subject": "OOS批次率",
            "question": "OOS批次按开始时间还是结束时间归属？",
            "options": [],
            "allow_free_text": True,
        },
    ])

    assert len(payload["items"]) == 2
    assert {
        item["semantic_scope"]["ambiguity_kind"] for item in payload["items"]
    } == {"metric_definition", "time_attribution"}


def test_clarification_payload_keeps_complementary_items_for_same_subject_and_kind():
    payload = _build_analysis_clarification_payload([
        {
            "item_id": "population",
            "ambiguity_kind": "metric_definition",
            "subject": "一次合格率",
            "question": "一次合格率按批次还是检测记录统计？",
            "options": [],
            "allow_free_text": True,
        },
        {
            "item_id": "qualification",
            "ambiguity_kind": "metric_definition",
            "subject": "一次合格率",
            "question": "复检记录是否计入合格判定？",
            "options": [],
            "allow_free_text": True,
        },
    ])

    assert [item["item_id"] for item in payload["items"]] == [
        "population",
        "qualification",
    ]


def test_clarification_payload_rejects_duplicate_item_ids():
    with pytest.raises(ValueError, match="item_id 必须唯一"):
        _build_analysis_clarification_payload([
            {
                "item_id": "definition",
                "subject": "统计总体",
                "question": "总体按合同还是明细？",
                "options": [],
                "allow_free_text": True,
            },
            {
                "item_id": "definition",
                "subject": "统计资格",
                "question": "终止合同是否计入？",
                "options": [],
                "allow_free_text": True,
            },
        ])


def test_clarification_without_any_answer_path_is_rejected():
    with pytest.raises(ValueError, match="必须提供快捷选项"):
        _build_analysis_clarification_payload([{
            "subject": "统计总体",
            "question": "按哪个总体统计？",
            "options": [],
            "allow_free_text": False,
        }])


def test_clarification_rejects_duplicate_option_ids():
    with pytest.raises(ValueError, match="选项 id 必须唯一"):
        _build_analysis_clarification_payload([{
            "subject": "统计总体",
            "question": "按哪个总体统计？",
            "options": [
                {"id": "population", "label": "按合同", "value": "每份合同计一次"},
                {"id": "population", "label": "按明细", "value": "每条明细计一次"},
            ],
            "allow_free_text": True,
        }])


def test_clarification_tool_terminates_the_runtime_graph():
    command = request_analysis_clarification.func(
        runtime=SimpleNamespace(
            tool_call_id="call-1",
            context=SimpleNamespace(analysis_task_id=123),
        ),
        items=[{
            "subject": "统计周期归属",
            "question": "批次按开始时间还是结束时间归属？",
            "options": [],
            "allow_free_text": True,
        }],
    )

    assert command.goto == END
    payload = json.loads(command.update["messages"][0].content)
    assert payload["status"] == "clarification_required"


def test_clarification_tool_does_not_suspend_legacy_sync_invocation():
    command = request_analysis_clarification.func(
        runtime=SimpleNamespace(
            tool_call_id="call-1",
            context=SimpleNamespace(analysis_task_id=None),
        ),
        items=[{
            "subject": "统计周期归属",
            "question": "批次按开始时间还是结束时间归属？",
            "options": [],
            "allow_free_text": True,
        }],
    )

    assert command.goto != END
    payload = json.loads(command.update["messages"][0].content)
    assert payload["status"] == "clarification_unavailable"


def test_invoker_extracts_terminal_runtime_clarification():
    clarification = {
        "question": "按哪个业务时间归属？",
        "items": [{"item_id": "time", "question": "按哪个业务时间归属？"}],
    }
    message = ToolMessage(
        tool_call_id="call-1",
        content=json.dumps({
            "tool": "request_analysis_clarification",
            "status": "clarification_required",
            "clarification": clarification,
        }, ensure_ascii=False),
    )

    assert invoker._extract_runtime_clarification([message]) == clarification


def test_user_clarification_answers_are_injected_once_into_react_history():
    request = SimpleNamespace(
        user_message="分析批次率",
        clarification_context={
            "user_clarifications": [{
                "clarification_id": "clarify-1",
                "clarification_item_id": "time",
                "question": "批次按哪个时间归属？",
                "answer_text": "按批次开始时间",
                "resolved_value": "批次按开始时间归属统计周期",
                "semantic_scope": {"subject": "时间归属"},
            }],
        },
    )
    runtime = SimpleNamespace(
        conversation=SimpleNamespace(insight_namespace_id=1, id=2),
        history_turn_limit=5,
        active_datasource_snapshot={},
        context_mode="fresh_analysis",
    )
    prior = [HumanMessage("分析批次率"), AIMessage("需要确认时间归属")]

    first = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request,
        runtime=runtime,
        prior_messages=prior,
    )
    second = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request,
        runtime=runtime,
        prior_messages=first.messages,
    )

    merged = "\n".join(str(message.content) for message in second.messages)
    assert merged.count("<user-clarification-answers>") == 1
    assert "批次按开始时间归属统计周期" in merged
    assert "保留每条回答的完整含义并映射到完整 Schema" in merged
    assert "技术未知合并到一次有界 Probe" in merged
    assert isinstance(first.messages[-1], HumanMessage)


def test_canonical_clarification_facts_preserve_all_items_and_raw_fields():
    answers = [{
        "clarification_id": "clarify-many",
        "clarification_item_id": f"item-{index}",
        "question": f"问题 {index}",
        "answer_text": f"回答 {index}",
        "resolved_value": f"完整业务含义 {index}",
        "option_id": "",
        "semantic_scope": {
            "subject": "同一主题",
            "ambiguity_kind": "metric_definition",
        },
    } for index in range(10)]

    facts = invoker._canonical_runtime_clarification_facts(answers)

    assert len(facts) == 10
    assert [item["item_id"] for item in facts] == [f"item-{index}" for index in range(10)]
    assert facts[-1]["resolved_value"] == "完整业务含义 9"
    assert facts[-1]["semantic_scope"] == answers[-1]["semantic_scope"]


def test_clarification_resume_context_keeps_probe_observations(monkeypatch):
    waiting = reduce_runtime_tool_result(
        new_runtime_work_packet(workspace_id='66:99:7', focus='Analyze quality'),
        tool_name='request_analysis_clarification',
        tool_call_id='clarify',
        arguments={},
        result={'status': 'clarification_required'},
    )
    runtime = SimpleNamespace(
        context_mode="fresh_analysis",
        short_term_memory={RUNTIME_WORK_PACKET_MEMORY_KEY: waiting.to_dict()},
    )
    executions = [
        SimpleNamespace(
            execution_status="failed",
            error_message="probe completed",
            result_payload_json=json.dumps({
                "error_type": "probe_feedback",
                "diagnostics": {"quality_status_candidates": ["pass", "near_limit"]},
            }),
        ),
        SimpleNamespace(
            execution_status="failed",
            error_message="contract failed",
            result_payload_json=json.dumps({"error_type": "contract_error"}),
        ),
    ]
    monkeypatch.setattr(invoker, "_load_turn_executions_fresh", lambda _runtime: executions)

    context = invoker._build_clarification_resume_context(
        preparation=None,
        runtime=runtime,
    )

    assert len(context["probe_observations"]) == 1
    assert context["probe_observations"][0]["retry_type"] == "probe_feedback"
    assert context[RUNTIME_WORK_PACKET_MEMORY_KEY] == waiting.to_dict()
    instruction = invoker._build_resumed_probe_observation_instruction(context)
    assert "quality_status_candidates" in instruction


def test_tool_payload_persists_runtime_work_packet_without_changing_existing_memory():
    runtime = SimpleNamespace(short_term_memory={'probe_fact_ledger': [{'fact_id': 'f1'}]})
    packet = new_runtime_work_packet(workspace_id='66:99:7', focus='Analyze quality')

    invoker._update_short_term_memory_from_tool_payload(runtime, {
        'tool': 'select_runtime_work',
        'status': 'success',
        RUNTIME_WORK_PACKET_PAYLOAD_KEY: packet.to_dict(),
    })

    restored = runtime_work_packet_from_dict(
        runtime.short_term_memory[RUNTIME_WORK_PACKET_MEMORY_KEY]
    )
    assert restored == packet
    assert runtime.short_term_memory['probe_fact_ledger'] == [{'fact_id': 'f1'}]


def test_resume_restores_tool_calls_and_accepts_a_second_clarification(monkeypatch):
    monkeypatch.setattr(invoker, '_load_turn_executions_fresh', lambda _: [])
    transcript = [
        SystemMessage('Full schema and contracts'),
        HumanMessage('Analyze'),
        AIMessage(content='Inspect the date format', tool_calls=[{
            'name': 'probe_analysis_data', 'args': {'code': 'print("observed")'},
            'id': 'probe-1', 'type': 'tool_call',
        }]),
        ToolMessage(content='observed dates', tool_call_id='probe-1'),
    ]
    context = invoker._build_clarification_resume_context(
        preparation=None, runtime=SimpleNamespace(context_mode='fresh_analysis'), transcript=transcript)
    context['user_clarifications'] = [{'clarification_id': 'one', 'answer_text': 'Use July'}]
    request = SimpleNamespace(clarification_context=context)
    def unexpected_input(*args, **kwargs):
        raise AssertionError('Resume must not rebuild or shorten the previous input')
    monkeypatch.setattr(invoker, 'get_input', unexpected_input)
    resumed = invoker._build_agent_input_with_runtime_instruction(agent_request=request, runtime=SimpleNamespace())
    assert [message.content for message in resumed.messages[:4]] == [message.content for message in transcript]
    assert resumed.messages[2].tool_calls == transcript[2].tool_calls
    assert resumed.messages[3].tool_call_id == 'probe-1'
    assert 'Use July' in resumed.messages[-1].content
    assert len(resumed.messages) == 5
    repeated = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request, runtime=SimpleNamespace(), prior_messages=resumed.messages)
    assert len(repeated.messages) == 5
    context['user_clarifications'].append({'clarification_id': 'two', 'answer_text': 'Count unique batches'})
    continued = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request, runtime=SimpleNamespace(), prior_messages=resumed.messages)
    assert len(continued.messages) == 6
    assert 'Count unique batches' in continued.messages[-1].content


def test_resumed_probe_observations_are_injected_once_into_react_history():
    request = SimpleNamespace(
        user_message="分析批次率",
        clarification_context={
            "probe_observations": [{
                "retry_type": "probe_feedback",
                "diagnostics": {"quality_status_candidates": ["pass", "near_limit"]},
            }],
        },
    )
    runtime = SimpleNamespace(
        conversation=SimpleNamespace(insight_namespace_id=1, id=2),
        history_turn_limit=5,
        active_datasource_snapshot={},
        context_mode="fresh_analysis",
    )
    prior = [HumanMessage("分析批次率")]

    first = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request,
        runtime=runtime,
        prior_messages=prior,
    )
    second = invoker._build_agent_input_with_runtime_instruction(
        agent_request=request,
        runtime=runtime,
        prior_messages=first.messages,
    )

    merged = "\n".join(str(message.content) for message in second.messages)
    assert merged.count("<resumed-probe-observations>") == 1
    assert "quality_status_candidates" in merged


def test_continued_react_recovery_uses_bounded_backend_feedback():
    instruction = invoker._build_continued_react_recovery_instruction()

    assert "后端归纳" in instruction
    assert "完整工具转录" in instruction
    assert "diagnostics" in instruction
    assert "submit_analysis_program" in instruction
    assert len(instruction) < 420


def test_continued_react_recovery_preserves_probe_for_new_technical_unknowns():
    instruction = invoker._build_continued_react_recovery_instruction()

    assert "已有观察足以实现分析时直接调用 submit_analysis_program" in instruction
    assert "probe_analysis_data" in instruction
    assert "不重复已确认的观察" in instruction
    assert "Probe 阶段已经完成" not in instruction


@pytest.mark.parametrize("clarification_mode", ["same_turn", "disabled"])
def test_probe_capability_is_independent_of_clarification_policy(monkeypatch, clarification_mode):
    import agent

    monkeypatch.setattr(agent, "create_data_insight_model", lambda _: object())
    monkeypatch.setattr(agent, "create_agent", lambda **kwargs: kwargs)
    configured = agent.create_data_insight_agent(clarification_mode=clarification_mode)

    tool_names = {tool.name for tool in configured["tools"]}
    assert {"probe_analysis_data", "submit_analysis_program"} <= tool_names
    assert ("request_analysis_clarification" in tool_names) == (clarification_mode == "same_turn")


def test_runtime_prompt_keeps_clarification_inside_unified_runtime():
    runtime_root = Path(invoker.__file__).resolve().parent.joinpath("prompts", "runtime")
    prompt = "\n".join([
        runtime_root.joinpath("sys_prompt.md").read_text(encoding="utf-8"),
        runtime_root.joinpath("contracts", "interaction/clarification.md").read_text(encoding="utf-8"),
        runtime_root.joinpath("contracts", "execution/core.md").read_text(encoding="utf-8"),
    ])

    assert "request_analysis_clarification" in prompt
    assert "不生成独立指标计划或报告模板" in prompt
    assert 'execution_intent = "probe"' in prompt
    assert "于当前 ReAct 内完成必要的口径落地" in prompt
    assert "父实体定义统计总体时，先形成父实体范围" in prompt
    assert "不再增加一次语义确认工具回合" in prompt


def test_runtime_path_has_no_secondary_semantic_compiler_or_reviewer():
    source = Path(invoker.__file__).read_text(encoding="utf-8")
    tools_source = Path(invoker.__file__).with_name("tools.py").read_text(encoding="utf-8")

    assert "compile_authoritative_semantic_decisions" not in source
    assert "review_authoritative_business_fact_adherence" not in tools_source
    assert "required_semantic_probe_observations" not in tools_source
