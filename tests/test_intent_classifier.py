from types import SimpleNamespace

from langchain_core.messages import AIMessage

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


def test_explicit_analysis_of_all_conversation_datasources_skips_llm_classifier(monkeypatch):
    payload = {
        "current_user_message": "分析下当前会话关联的所有数据源",
        "recent_messages": [],
        "selected_datasource_summary": {"selected_count": 0},
        "selected_datasources": [],
        "pending_scope_clarification": {},
    }

    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(
        intent_classifier,
        "create_data_insight_model",
        lambda: (_ for _ in ()).throw(AssertionError("explicit analysis must route locally")),
    )

    result = intent_classifier.resolve_context_mode_with_llm(
        SimpleNamespace(),
        payload["current_user_message"],
    )

    assert result.mode == "fresh_analysis"
    assert result.source == "explicit_analysis_request"
    assert result.clarification_relation == "none"


def test_broad_datasource_analysis_command_is_not_downgraded_to_general(monkeypatch):
    payload = {
        "current_user_message": "分析下数据源",
        "recent_messages": [],
        "selected_datasource_summary": {"selected_count": 279},
        "selected_datasources": [],
        "pending_scope_clarification": {},
    }
    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(
        intent_classifier,
        "create_data_insight_model",
        lambda: (_ for _ in ()).throw(AssertionError("explicit analysis must route locally")),
    )

    result = intent_classifier.resolve_context_mode_with_llm(
        SimpleNamespace(),
        payload["current_user_message"],
    )

    assert result.mode == "fresh_analysis"
    assert result.source == "explicit_analysis_request"


def test_datasource_capacity_question_is_not_an_explicit_analysis_request():
    assert not intent_classifier._is_explicit_datasource_analysis_request(
        "你最多支持一次性分析多少数据源"
    )


def test_datasource_concept_question_is_not_an_explicit_analysis_request():
    assert not intent_classifier._is_explicit_datasource_analysis_request(
        "分析数据源是什么意思"
    )


def test_pending_scope_answer_continues_as_followup(monkeypatch):
    payload = {
        "current_user_message": "只分析设备运行相关的数据源",
        "recent_messages": [],
        "selected_datasource_summary": {"selected_count": 279},
        "selected_datasources": [],
        "pending_scope_clarification": {
            "status": "pending",
            "original_user_query": "分析下数据源",
            "question": "请补充具体业务主题或数据源范围",
        },
    }

    class FakeModel:
        def invoke(self, _messages):
            return AIMessage(content=(
                '{"mode":"general","clarification_relation":"answer",'
                '"confidence":0.98,"reason":"用户补充了设备运行范围"}'
            ))

    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(intent_classifier, "create_data_insight_model", lambda: FakeModel())

    result = intent_classifier.resolve_context_mode_with_llm(
        SimpleNamespace(),
        payload["current_user_message"],
    )

    assert result.mode == "followup"
    assert result.clarification_relation == "answer"


def test_pending_scope_does_not_capture_independent_new_request(monkeypatch):
    payload = {
        "current_user_message": "分析今年的年费合同",
        "recent_messages": [],
        "selected_datasource_summary": {"selected_count": 279},
        "selected_datasources": [],
        "pending_scope_clarification": {
            "status": "pending",
            "original_user_query": "分析下数据源",
            "question": "请补充具体业务主题或数据源范围",
        },
    }

    class FakeModel:
        def invoke(self, _messages):
            return AIMessage(content=(
                '{"mode":"fresh_analysis","clarification_relation":"new_request",'
                '"confidence":0.97,"reason":"用户提出独立的年费合同分析"}'
            ))

    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(intent_classifier, "create_data_insight_model", lambda: FakeModel())

    result = intent_classifier.resolve_context_mode_with_llm(
        SimpleNamespace(),
        payload["current_user_message"],
    )

    assert result.mode == "fresh_analysis"
    assert result.clarification_relation == "new_request"


def test_pending_scope_missing_relation_stays_in_clarification(monkeypatch):
    payload = {
        "current_user_message": "还是分析全部",
        "recent_messages": [],
        "selected_datasource_summary": {"selected_count": 279},
        "selected_datasources": [],
        "pending_scope_clarification": {
            "status": "pending",
            "original_user_query": "分析下数据源",
        },
    }

    class FakeModel:
        def invoke(self, _messages):
            return AIMessage(content=(
                '{"mode":"fresh_analysis","confidence":0.7,'
                '"reason":"未返回澄清关系"}'
            ))

    monkeypatch.setattr(intent_classifier, "_build_classifier_payload", lambda *_: payload)
    monkeypatch.setattr(intent_classifier, "create_data_insight_model", lambda: FakeModel())

    result = intent_classifier.resolve_context_mode_with_llm(
        SimpleNamespace(),
        payload["current_user_message"],
    )

    assert result.mode == "general"
    assert result.clarification_relation == "uncertain"
