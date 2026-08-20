from types import SimpleNamespace

from agent import intent_classifier


def test_datasource_selection_state_query_skips_analysis_classifier(monkeypatch):
    runtime = SimpleNamespace()
    payload = {
        "current_user_message": "现在选中了多少个数据源",
        "recent_messages": [],
        "selected_datasource_summary": {
            "selected_count": 276,
            "datasource_type_counts": {"local_file": 2, "table": 274},
            "selection_origin_counts": {"external": 2, "uns": 274},
        },
        "selected_datasources": [],
    }
    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(
        intent_classifier,
        "create_data_insight_model",
        lambda: (_ for _ in ()).throw(AssertionError("conversation state must not call the LLM classifier")),
    )

    result = intent_classifier.resolve_context_mode_with_llm(
        runtime,
        payload["current_user_message"],
    )

    assert result.mode == "general"
    assert result.source == "conversation_state"
    assert result.confidence == 1.0


def test_elliptical_datasource_state_followup_remains_general(monkeypatch):
    runtime = SimpleNamespace()
    payload = {
        "current_user_message": "现在呢",
        "recent_messages": [
            {"turn_no": 1, "role": "user", "content": "我选中了多少个数据源"},
            {"turn_no": 1, "role": "assistant", "content": "当前选中 114 个数据源"},
        ],
        "selected_datasource_summary": {"selected_count": 226},
        "selected_datasources": [],
    }
    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)

    result = intent_classifier.resolve_context_mode_with_llm(runtime, "现在呢")

    assert result.mode == "general"
    assert result.source == "conversation_state"


def test_analysis_followup_is_not_mistaken_for_conversation_state():
    payload = {
        "current_user_message": "现在按华东区域重新计算销售额",
        "recent_messages": [
            {"turn_no": 1, "role": "user", "content": "分析销售趋势"},
        ],
    }

    assert not intent_classifier._is_conversation_state_request(payload)


def test_analysis_of_current_selected_datasources_is_not_treated_as_state_query():
    payload = {
        "current_user_message": "分析当前已选数据源",
        "recent_messages": [],
    }

    assert not intent_classifier._is_conversation_state_request(payload)
