from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import agent.invoker as invoker
from agent.analysis_preparation import (
    BusinessFactCoverageContract,
    BusinessFactRequirementContract,
    BusinessContextContract,
    BusinessQuestionContract,
    BusinessQuestionCoverageContract,
    DatasourceSelectionContract,
    IntentContract,
    PreparationResult,
)


def _snapshot(*datasource_ids: int) -> dict:
    return {
        "selected_datasource_ids": list(datasource_ids),
        "selected_datasource_snapshot": [
            {
                "datasource_id": datasource_id,
                "datasource_name": f"source-{datasource_id}",
                "datasource_type": "table",
                "metadata_schema": {
                    "properties": {
                        "business_id": {"type": "string"},
                        "amount": {"type": "number"},
                    }
                },
            }
            for datasource_id in datasource_ids
        ],
    }


def _runtime(snapshot: dict) -> SimpleNamespace:
    return SimpleNamespace(
        conversation=SimpleNamespace(id=21, insight_namespace_id=6),
        turn=SimpleNamespace(id=31, turn_no=1, user_query="分析合同情况"),
        active_datasource_snapshot=snapshot,
        short_term_memory={},
        context_mode="fresh_analysis",
        analysis_task_id=0,
    )


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        user_message="分析合同情况",
        username="tester",
        auth_token="",
        database_conn_info={},
    )


def _preparation(snapshot: dict) -> PreparationResult:
    return PreparationResult(
        intent=IntentContract(
            business_goal="分析合同情况",
            business_subjects=["合同"],
            business_questions=[BusinessQuestionContract(
                question_id="question_1",
                business_question="合同情况如何",
                subject_scope="specific",
                required_business_facts=[BusinessFactRequirementContract(
                    fact_id="question_1_fact_1",
                    business_fact="合同事实",
                    semantic_definition="合同业务事实",
                )],
            )],
        ),
        selection=DatasourceSelectionContract(
            selected_datasource_ids=[11],
            question_coverage=[BusinessQuestionCoverageContract(
                question_id="question_1",
                datasource_ids=[11],
                fact_coverage=[BusinessFactCoverageContract(
                    fact_id="question_1_fact_1",
                    coverage_status="direct",
                    datasource_ids=[11],
                    coverage_reason="合同数据源直接覆盖",
                )],
                coverage_reason="合同数据源直接覆盖",
            )],
            selection_reason="合同事实来源",
        ),
        business_context=BusinessContextContract(),
        selected_snapshot=_snapshot(11),
        query_capabilities=("sql",),
    )


def test_formal_context_uses_prepared_snapshot_without_mutating_conversation_snapshot():
    original = _snapshot(11, 12)
    runtime = _runtime(original)
    preparation = _preparation(original)

    context = invoker._build_agent_context(
        _request(),
        runtime,
        preparation,
    )

    assert context.active_datasource_snapshot["selected_datasource_ids"] == [11]
    assert runtime.active_datasource_snapshot["selected_datasource_ids"] == [11, 12]


def test_preparation_failure_preserves_original_datasource_context(monkeypatch):
    original = _snapshot(11, 12)
    runtime = _runtime(original)

    monkeypatch.setattr(
        invoker,
        "prepare_analysis",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("temporary failure")),
    )

    result = invoker._prepare_analysis_context(
        runtime=runtime,
        agent_request=_request(),
        runtime_config=SimpleNamespace(),
    )

    assert result.selection.selected_datasource_ids == [11, 12]
    assert result.selected_snapshot == original
    assert runtime.active_datasource_snapshot == original
    assert runtime.short_term_memory["preparation_result"] is result


def test_semantic_provider_allows_analysis_without_bound_datasource(monkeypatch):
    runtime = _runtime(_snapshot())
    monkeypatch.setattr(
        invoker.DatasourceSelectionProviderRouter,
        "resolve",
        lambda snapshot=None: "semantic_center",
    )
    monkeypatch.setattr(
        invoker,
        "_is_analysis_request_expected",
        lambda runtime, user_message: True,
    )

    assert not invoker._should_finish_without_datasource(runtime, "分析合同情况")


def test_local_provider_still_requires_bound_datasource(monkeypatch):
    runtime = _runtime(_snapshot())
    monkeypatch.setattr(
        invoker.DatasourceSelectionProviderRouter,
        "resolve",
        lambda snapshot=None: "local",
    )
    monkeypatch.setattr(
        invoker,
        "_is_analysis_request_expected",
        lambda runtime, user_message: True,
    )

    assert invoker._should_finish_without_datasource(runtime, "分析合同情况")


def test_empty_semantic_preparation_failure_converges_to_no_datasource(monkeypatch):
    runtime = _runtime(_snapshot())
    monkeypatch.setattr(
        invoker,
        "prepare_analysis",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("semantic unavailable")),
    )

    with pytest.raises(invoker.NoDatasourceAvailableForAnalysis):
        invoker._prepare_analysis_context(
            runtime=runtime,
            agent_request=_request(),
            runtime_config=SimpleNamespace(),
        )


def test_preparation_probe_context_keeps_the_separate_fedquery_authorization(monkeypatch):
    runtime = _runtime(_snapshot(11))
    captured = {}
    request = _request()
    request.platform_authorization = "Bearer platform-ticket"
    request.fedquery_authorization = "Bearer fedquery-jwt"

    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return _preparation(_snapshot(11))

    monkeypatch.setattr(invoker, "prepare_analysis", fake_prepare)

    invoker._prepare_analysis_context(
        runtime=runtime,
        agent_request=request,
        runtime_config=SimpleNamespace(),
    )

    assert captured["execution_context"]["token"] == "Bearer platform-ticket"
    assert (
        captured["execution_context"]["fedquery_authorization"]
        == "Bearer fedquery-jwt"
    )


def test_streaming_preparation_forwards_each_stage_as_user_readable_status(monkeypatch):
    runtime = _runtime(_snapshot(11))
    expected = _preparation(_snapshot(11))

    def fake_prepare_context(*, stage_callback, **kwargs):
        for stage in (
            "intent_analysis",
            "semantic_selection",
            "select_datasources",
            "business_context",
        ):
            stage_callback(stage)
        return expected

    monkeypatch.setattr(invoker, "_prepare_analysis_context", fake_prepare_context)
    stream = invoker._stream_analysis_preparation(
        runtime=runtime,
        agent_request=_request(),
        runtime_config=SimpleNamespace(),
    )
    events = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as completed:
            result = completed.value
            break

    assert result is expected
    assert [event["stage"] for event in events] == [
        "preparation",
        "preparation:intent_analysis",
        "preparation:semantic_selection",
        "preparation:select_datasources",
        "preparation:business_context",
        "preparation:complete",
    ]
    assert all(event["message"] for event in events)


def test_streaming_general_conversation_skips_preparation(monkeypatch):
    runtime = _runtime(_snapshot())
    runtime.context_mode = "general"
    runtime.is_rerun = False
    runtime.history_turn_limit = None
    runtime.conversation.title = "普通对话"
    request = _request()
    request.user_message = "你好"
    request.selected_llm_model_id = ""

    class FakeAgent:
        def stream(self, *_args, **_kwargs):
            yield "updates", {
                "model": {"messages": [AIMessage(content="你好，请问想了解哪些数据？")]}
            }

    monkeypatch.setattr(invoker, "_resolve_agent_runtime_config", lambda _request: SimpleNamespace())
    monkeypatch.setattr(invoker, "get_general_conversation_agent", lambda _config: FakeAgent())
    monkeypatch.setattr(
        invoker,
        "get_data_insight_agent",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("general conversation must not use the analysis agent")
        ),
    )
    monkeypatch.setattr(
        invoker,
        "_stream_analysis_preparation",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("general conversation must skip Preparation")),
    )
    monkeypatch.setattr(invoker, "get_general_input", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        invoker,
        "_build_agent_input_with_runtime_instruction",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("general conversation must not build analysis context")
        ),
    )
    monkeypatch.setattr(invoker, "_build_agent_context", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        invoker,
        "_did_enter_analysis_flow",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("general conversation must not inspect analysis executions")
        ),
    )
    monkeypatch.setattr(
        invoker,
        "_resolve_analysis_outputs",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("general conversation must not resolve analysis artifacts")
        ),
    )
    monkeypatch.setattr(
        invoker,
        "_finalize_run_with_fresh_session",
        lambda **kwargs: invoker.AgentResponse(
            username=kwargs["username"],
            message=kwargs["assistant_message"],
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            charts=[],
            tables=[],
        ),
    )

    events = list(invoker._stream_with_runtime(None, runtime, request))

    assert any(
        event["type"] == "assistant" and "你好" in event["message"]
        for event in events
    )
    assert events[-1]["type"] == "done"
    assert not any(event["type"] == "error" for event in events)


def test_rerun_failed_analysis_activity_does_not_override_general_mode(monkeypatch):
    runtime = _runtime(_snapshot(11))
    runtime.is_rerun = True
    runtime.context_mode = "general"

    assert not invoker._is_analysis_request_expected(runtime, "历史问题文本")


def test_rerun_without_prior_analysis_activity_respects_general_mode(monkeypatch):
    runtime = _runtime(_snapshot())
    runtime.is_rerun = True
    runtime.context_mode = "general"

    assert not invoker._is_analysis_request_expected(runtime, "你好")


def test_rerun_without_prior_analysis_activity_uses_intent_classifier(monkeypatch):
    runtime = _runtime(_snapshot())
    runtime.is_rerun = True
    monkeypatch.setattr(
        invoker,
        "resolve_context_mode_with_llm",
        lambda *_args: SimpleNamespace(
            mode="general",
            source="llm",
            confidence=0.99,
            reason="普通问候",
        ),
    )

    assert invoker._resolve_runtime_context_mode(runtime, "你好") == "general"


def test_rerun_intent_classifier_is_authoritative_despite_prior_failed_activity(monkeypatch):
    runtime = _runtime(_snapshot(11))
    runtime.is_rerun = True
    monkeypatch.setattr(
        invoker,
        "resolve_context_mode_with_llm",
        lambda *_args: SimpleNamespace(
            mode="general",
            source="llm",
            confidence=0.98,
            reason="产品能力咨询",
        ),
    )

    assert invoker._resolve_runtime_context_mode(runtime, "你最多支持多少数据源") == "general"


def test_rerun_analysis_intent_still_uses_analysis_agent(monkeypatch):
    runtime = _runtime(_snapshot(11))
    runtime.is_rerun = True
    monkeypatch.setattr(
        invoker,
        "resolve_context_mode_with_llm",
        lambda *_args: SimpleNamespace(
            mode="fresh_analysis",
            source="llm",
            confidence=0.99,
            reason="请求分析合同情况",
        ),
    )

    assert invoker._resolve_runtime_context_mode(runtime, "年费合同情况如何") == "fresh_analysis"
    assert invoker._is_analysis_request_expected(runtime, "年费合同情况如何")


def test_scope_clarification_answer_is_consumed_only_after_preparation(monkeypatch):
    runtime = _runtime(_snapshot(11))
    cleared = []
    monkeypatch.setattr(
        invoker,
        "resolve_context_mode_with_llm",
        lambda *_args: SimpleNamespace(
            mode="followup",
            source="llm",
            confidence=0.99,
            reason="用户补充了具体范围",
            clarification_relation="answer",
        ),
    )
    monkeypatch.setattr(
        invoker,
        "_clear_pending_scope_clarification",
        lambda current_runtime: cleared.append(current_runtime.conversation.id),
    )

    assert invoker._resolve_runtime_context_mode(runtime, "只分析合同数据") == "followup"
    assert cleared == []

    monkeypatch.setattr(
        invoker,
        "prepare_analysis",
        lambda **_kwargs: _preparation(_snapshot(11)),
    )
    invoker._prepare_analysis_context(
        runtime=runtime,
        agent_request=_request(),
        runtime_config=SimpleNamespace(),
    )

    assert cleared == [runtime.conversation.id]
    assert runtime.short_term_memory["scope_clarification_relation"] == "none"


def test_independent_request_clears_pending_scope_before_preparation(monkeypatch):
    runtime = _runtime(_snapshot(11))
    cleared = []
    monkeypatch.setattr(
        invoker,
        "resolve_context_mode_with_llm",
        lambda *_args: SimpleNamespace(
            mode="fresh_analysis",
            source="llm",
            confidence=0.99,
            reason="用户提出了独立问题",
            clarification_relation="new_request",
        ),
    )
    monkeypatch.setattr(
        invoker,
        "_clear_pending_scope_clarification",
        lambda current_runtime: cleared.append(current_runtime.conversation.id),
    )

    assert invoker._resolve_runtime_context_mode(runtime, "分析年费合同") == "fresh_analysis"
    assert cleared == [runtime.conversation.id]


def test_general_rerun_does_not_require_prior_analysis_result_execution(monkeypatch):
    runtime = _runtime(_snapshot(11))
    runtime.is_rerun = True
    runtime.context_mode = "general"
    monkeypatch.setattr(
        invoker,
        "_rerun_has_prior_analysis_result",
        lambda *_args: True,
    )

    assert not invoker._should_require_rerun_analysis_execution(None, runtime, False)
