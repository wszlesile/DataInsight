from types import SimpleNamespace

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


def test_rerun_is_always_an_analysis_request_even_when_text_classifier_is_uncertain():
    runtime = _runtime(_snapshot(11))
    runtime.is_rerun = True
    runtime.context_mode = "general"

    assert invoker._is_analysis_request_expected(runtime, "历史问题文本")
