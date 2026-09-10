import json

from langchain_core.messages import SystemMessage

import agent
import agent.context_engineering_runtime as runtime_context


def _runtime_payload(message):
    content = message.content
    return json.loads(content[content.index("{"):content.rindex("}") + 1])


def test_runtime_contracts_follow_delivered_sources_when_snapshot_is_loaded_internally(monkeypatch):
    snapshot = {'selected_datasource_snapshot': [
        {'datasource_id': 1, 'datasource_type': 'table', 'table_engine': 'time_series',
         'ts_query_source': 'vxbase', 'datasource_identifier': 'sensor',
         'metadata_schema': {'properties': {'value': {'type': 'number', 'description': 'full sensor meaning'}}}},
        {'datasource_id': 2, 'datasource_type': 'local_file', 'datasource_identifier': '/data/cost.csv',
         'metadata_schema': {'properties': {'cost': {'type': 'number', 'description': 'full cost meaning'}}}},
    ]}
    monkeypatch.setattr(runtime_context, '_load_conversation', lambda _: None)
    original = runtime_context.get_datasource_message
    # The caller has no explicit snapshot; the datasource loader supplies it.
    monkeypatch.setattr(agent, 'get_datasource_message', lambda **_: original(1, 2, snapshot))
    monkeypatch.setattr(agent, '_build_conversation_prompt_message', lambda *_: None)
    monkeypatch.setattr(agent, 'get_memory_messages', lambda *_, **__: [])
    monkeypatch.setattr(agent, 'get_history_messages', lambda *_, **__: [])
    messages = agent.build_prompt_messages('Compare the available data', namespace_id=1, conversation_id=2)
    prompt = messages[0].content
    assert '# query.time_series' in prompt
    assert '# query.local_file' in prompt
    assert '# query.cross_source' in prompt
    assert '# query.relational' not in prompt
    assert 'full sensor meaning' in prompt and 'full cost meaning' in prompt


def test_datasource_context_keeps_full_snapshot_for_preparation_to_narrow(monkeypatch):
    datasource_rows = [
        {
            "datasource_id": index,
            "datasource_name": f"业务数据源{index}",
            "datasource_type": "table",
            "datasource_identifier": f"business.source_{index}",
            "metadata_schema": {
                "properties": {"business_key": {"type": "string"}}
            },
        }
        for index in range(1, 32)
    ]
    snapshot = {
        "selected_datasource_ids": list(range(1, 32)),
        "selected_datasource_snapshot": datasource_rows,
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override=snapshot,
    )

    assert message is not None
    payload = _runtime_payload(message)
    assert payload["selected_datasource_ids"] == list(range(1, 32))
    assert len(payload["datasources"]) == 31
    assert payload["runtime_capabilities"]["query_execution"] == {
        "max_rows_per_sql_result": 10000,
        "capacity_scope": "each physical load_data_with_sql execution",
        "relational_sql_dialect": "mysql_compatible",
        "relational_sql_profile": "fedquery_portable_subset",
        "operation_capabilities": {"string_aggregation": "unsupported"},
        "full_population_strategies": [
            "push filters, joins and final aggregation into the query",
            "use stable pagination and incrementally reduce each page",
        ],
        "truncation_policy": (
            "Do not apply an arbitrary LIMIT to a full-population metric. "
            "LIMIT is valid only for an intentional TopN, bounded detail or sample output."
        ),
    }
    assert "容量边界作用于每次物理查询" in message.content


def test_datasource_context_preserves_preparation_semantic_handoff(monkeypatch):
    semantic_context = {
        "analysis_goal": "分析营收及产品营收结构",
        "business_questions": [
            {
                "question_id": "q1",
                "business_question": "2026 年整体营收表现如何",
                "required_business_facts": [{
                    "fact_id": "q1_fact_1",
                    "business_fact": "实际营收",
                    "semantic_definition": "按营收确认事件形成的实际收入",
                }],
            }
        ],
        "question_coverage": [
            {
                "question_id": "q1",
                "coverage_status": "direct",
                "datasource_ids": [192],
            }
        ],
        "relationships": [],
        "insight_lineage": [
            {
                "question_id": "q1",
                "lineage_status": "direct",
                "datasource_ids": [192],
                "fact_bindings": [
                    {
                        "fact_id": "q1_fact_1",
                        "datasource_id": 192,
                        "fields": ["revenue_amount"],
                    }
                ],
            }
        ],
    }
    snapshot = {
        "selected_datasource_ids": [192],
        "selected_datasource_snapshot": [
            {
                "datasource_id": 192,
                "datasource_name": "营收实绩",
                "datasource_type": "table",
                "datasource_identifier": "fqe.revenue",
                "metadata_schema": {
                    "properties": {"revenue_amount": {"type": "number"}}
                },
            }
        ],
        "analysis_semantic_context": semantic_context,
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override=snapshot,
    )

    assert message is not None
    payload = _runtime_payload(message)
    assert payload["analysis_semantic_context"] == semantic_context
    assert message.content.index('"analysis_semantic_context"') < message.content.index('"datasources"')
    assert "正式分析不再重新选源" in message.content
    assert "逐项阅读 `analysis_semantic_context.business_questions`" in message.content
    assert "开放式洞察方向" in message.content
    assert "不规定报告章节或结论" in message.content
    assert "已选候选范围" not in message.content
    assert "未声明未解决业务事实，可直接进入正式分析" not in message.content
    assert "带确认状态的准备信息" in message.content


def test_datasource_context_compacts_only_redundant_schema_indexes(monkeypatch):
    properties = {
        "amount": {"type": "number", "description": "合同金额"},
        "department": {"type": "string", "description": "业绩归属部门"},
    }
    snapshot = {
        "selected_datasource_ids": [78],
        "selected_datasource_snapshot": [{
            "datasource_id": 78,
            "datasource_name": "合同事实",
            "datasource_type": "table",
            "datasource_identifier": "dw.contract",
            "metadata_schema": {
                "properties": properties,
                "required": ["amount", "department"],
            },
            "field_capabilities": {
                "queryable_fields": ["amount", "department"],
                "descriptive_or_derived_fields": [],
            },
        }],
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override=snapshot,
    )

    payload = _runtime_payload(message)
    datasource = payload["datasources"][0]
    assert datasource["query_contract"] == {
        "datasource_id": 78,
        "datasource_identifier": "dw.contract",
        "allowed_fields_ref": "metadata_schema.properties",
        "all_schema_properties_allowed": True,
        "sql_dialect": "mysql_compatible",
        "sql_profile": "fedquery_portable_subset",
        "operation_capabilities": {"string_aggregation": "unsupported"},
    }
    assert datasource["metadata_schema"]["properties"] == properties
    assert datasource["metadata_schema"]["all_properties_required"] is True
    assert "required" not in datasource["metadata_schema"]
    assert datasource["field_capabilities"]["all_properties_queryable"] is True
    assert "queryable_fields" not in datasource["field_capabilities"]
    assert "字段边界" in message.content


def test_datasource_query_contract_localizes_verified_key_cardinality(monkeypatch):
    snapshot = {
        "selected_datasource_ids": [78],
        "selected_datasource_snapshot": [{
            "datasource_id": 78,
            "datasource_name": "quality facts",
            "datasource_type": "table",
            "datasource_identifier": "dw.quality",
            "metadata_schema": {
                "properties": {
                    "batch_id": {"type": "string"},
                    "deviation": {"type": "number"},
                },
                "relationships": [{
                    "relationship_id": "quality_batch",
                    "local_fields": ["batch_id"],
                    "observed_key_cardinality": {
                        "local_distinct_key_count": 18653,
                        "target_distinct_key_count": 19000,
                        "matched_distinct_key_count": 18500,
                    },
                }],
            },
        }],
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override=snapshot,
    )

    payload = _runtime_payload(message)
    assert payload["datasources"][0]["query_contract"]["known_key_cardinalities"] == [{
        "fields": ["batch_id"],
        "observed_distinct_count": 18653,
        "relationship_id": "quality_batch",
    }]
    assert payload["datasources"][0]["query_contract"]["capacity_risks"] == [{
        "fields": ["batch_id"],
        "observed_distinct_count": 18653,
        "max_rows_per_sql_result": 10000,
        "required_strategy": "query_side_final_aggregation_or_stable_pagination",
    }]


def test_general_datasource_context_keeps_schema_without_analysis_execution_contract(monkeypatch):
    properties = {
        "contract_amount": {
            "type": "number",
            "displayName": "合同金额",
            "description": "合同含税金额",
        },
        "customer_name": {
            "type": "string",
            "displayName": "客户名称",
        },
    }
    snapshot = {
        "selected_datasource_ids": [78],
        "selected_datasource_snapshot": [{
            "datasource_id": 78,
            "datasource_name": "合同事实",
            "datasource_type": "table",
            "datasource_identifier": "dw.contract",
            "metadata_schema": {
                "properties": properties,
                "required": ["contract_amount", "customer_name"],
            },
            "field_capabilities": {
                "queryable_fields": ["contract_amount", "customer_name"],
            },
        }],
        "analysis_semantic_context": {"analysis_goal": "分析合同情况"},
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override=snapshot,
        context_mode="general",
    )

    payload = json.loads(message.content[message.content.index("{"):])
    datasource = payload["datasources"][0]
    assert payload["datasource_selection_summary"] == {
        "selected_count": 1,
        "datasource_type_counts": {"table": 1},
        "selection_origin_counts": {"unknown": 1},
    }
    assert datasource["metadata_schema"]["properties"] == properties
    assert datasource["selected_for_current_context"] is True
    assert datasource["datasource_name"] == "合同事实"
    assert "runtime_capabilities" not in payload
    assert "analysis_semantic_context" not in payload
    assert "query_contract" not in datasource
    assert "field_capabilities" not in datasource
    assert "execute_python" not in message.content
    assert "Probe" not in message.content


def test_general_datasource_context_reports_large_selection_without_llm_counting(monkeypatch):
    snapshot_rows = [
        {
            "datasource_id": 1,
            "datasource_name": "成本文件",
            "datasource_type": "local_file",
            "selection_origin": "external",
            "metadata_schema": {"properties": {"cost": {"type": "number"}}},
        }
    ] + [
        {
            "datasource_id": datasource_id,
            "datasource_name": f"UNS-{datasource_id}",
            "datasource_type": "table",
            "selection_origin": "uns",
            "metadata_schema": {"properties": {"value": {"type": "number"}}},
        }
        for datasource_id in range(2, 277)
    ]
    snapshot = {
        "selected_datasource_ids": list(range(1, 277)),
        "selected_datasource_snapshot": snapshot_rows,
    }
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)

    message = runtime_context.get_datasource_message(
        namespace_id=1,
        conversation_id=5,
        snapshot_override=snapshot,
        context_mode="general",
    )

    payload = json.loads(message.content[message.content.index("{"):])
    assert payload["datasource_selection_summary"] == {
        "selected_count": 276,
        "datasource_type_counts": {"local_file": 1, "table": 275},
        "selection_origin_counts": {"external": 1, "uns": 275},
    }
    assert len(payload["datasources"]) == 276


def test_general_datasource_context_handles_empty_scope_without_analysis_instructions(monkeypatch):
    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)
    monkeypatch.setattr(runtime_context, "_load_conversation_datasources", lambda _: [])

    message = runtime_context.get_datasource_message(
        namespace_id=6,
        conversation_id=99,
        snapshot_override={"namespace_id": 6},
        context_mode="general",
    )

    assert "当前没有关联任何可用数据源" in message.content
    assert "execute_python" not in message.content
    assert "生成 Python" not in message.content


def test_general_memory_keeps_success_facts_without_failed_execution_noise():
    payload = {
        "recent_execution_summaries": [
            {
                "execution_id": 1,
                "execution_status": "failed",
                "analysis_report_preview": "失败结果",
            },
            {
                "execution_id": 2,
                "turn_id": 3,
                "title": "合同分析",
                "description": "分析合同情况",
                "execution_status": "success",
                "used_datasource_ids": [78],
                "analysis_report_preview": "合同总数为 149 份",
                "generated_code": "print('internal')",
            },
        ],
        "latest_execution": {
            "execution_id": 2,
            "turn_id": 3,
            "title": "合同分析",
            "description": "分析合同情况",
            "execution_status": "success",
            "used_datasource_ids": [78],
            "analysis_report_preview": "合同总数为 149 份",
            "generated_code": "print('internal')",
        },
    }

    pruned = runtime_context._prune_analysis_state_payload(payload, mode="general")

    assert [item["execution_id"] for item in pruned["recent_execution_summaries"]] == [2]
    assert pruned["latest_execution"]["analysis_report_preview"] == "合同总数为 149 份"
    assert "generated_code" not in pruned["latest_execution"]


def test_current_semantic_context_closes_merged_system_message(monkeypatch):
    prompt_call = {}

    def fake_load_semantic_prompt(
        query_capabilities=(),
        context_mode="fresh_analysis",
        output_format="standard",
        clarification_mode="same_turn",
    ):
        prompt_call["semantic"] = (
            query_capabilities,
            context_mode,
            output_format,
            clarification_mode,
        )
        return "CORE_RULES"

    def fake_load_contract_prompt(
        query_capabilities=(),
        context_mode="fresh_analysis",
        output_format="standard",
        clarification_mode="same_turn",
    ):
        prompt_call["contracts"] = (
            query_capabilities,
            context_mode,
            output_format,
            clarification_mode,
        )
        return "MECHANICAL_CONTRACTS"

    monkeypatch.setattr(agent, "load_runtime_semantic_prompt", fake_load_semantic_prompt)
    monkeypatch.setattr(agent, "load_runtime_initial_contract_prompt", fake_load_contract_prompt)
    monkeypatch.setattr(agent, "_build_conversation_prompt_message", lambda *_: None)
    monkeypatch.setattr(
        agent,
        "_build_runtime_environment_message",
        lambda: SystemMessage("RUNTIME_ENVIRONMENT"),
    )
    monkeypatch.setattr(agent, "_build_relative_date_hint_message", lambda *_: None)
    monkeypatch.setattr(
        agent,
        "get_datasource_message",
        lambda **_: SystemMessage("CURRENT_SCHEMA_AND_SEMANTIC_CONTEXT"),
    )
    monkeypatch.setattr(
        agent,
        "get_memory_messages",
        lambda *_, **__: [SystemMessage("HISTORICAL_MEMORY")],
    )
    monkeypatch.setattr(agent, "get_history_messages", lambda *_, **__: [])

    messages = agent.build_prompt_messages(
        "分析当前数据",
        namespace_id=6,
        conversation_id=20,
        extra_system_messages=["CURRENT_REPAIR_INSTRUCTION"],
        context_mode="followup",
    )

    system_text = messages[0].content
    assert system_text.index("HISTORICAL_MEMORY") < system_text.index(
        "CURRENT_SCHEMA_AND_SEMANTIC_CONTEXT"
    )
    assert system_text.index("CURRENT_SCHEMA_AND_SEMANTIC_CONTEXT") < system_text.index(
        "MECHANICAL_CONTRACTS"
    )
    assert system_text.index("MECHANICAL_CONTRACTS") < system_text.index(
        "CORE_RULES"
    )
    assert system_text.index("CORE_RULES") < system_text.index(
        "CURRENT_REPAIR_INSTRUCTION"
    )
    assert prompt_call == {
        "semantic": ((), "followup", "standard", "same_turn"),
        "contracts": ((), "followup", "standard", "same_turn"),
    }


def test_general_prompt_context_keeps_datasource_schema_and_analysis_memory(monkeypatch):
    datasource_call = {}

    def fake_get_datasource_message(**kwargs):
        datasource_call.update(kwargs)
        return SystemMessage("CURRENT_DATASOURCE_SCHEMA")

    monkeypatch.setattr(agent, "load_general_system_prompt", lambda: "GENERAL_RULES")
    monkeypatch.setattr(
        agent,
        "_build_runtime_environment_message",
        lambda: SystemMessage("RUNTIME_ENVIRONMENT"),
    )
    monkeypatch.setattr(
        agent,
        "_build_user_visible_language_message",
        lambda: SystemMessage("LANGUAGE_RULES"),
    )
    monkeypatch.setattr(
        agent,
        "get_datasource_message",
        fake_get_datasource_message,
    )
    monkeypatch.setattr(agent, "_build_conversation_prompt_message", lambda *_: None)
    monkeypatch.setattr(
        agent,
        "get_memory_messages",
        lambda *_, **__: [SystemMessage("RECENT_ANALYSIS_MEMORY")],
    )
    monkeypatch.setattr(agent, "get_history_messages", lambda *_, **__: [])

    messages = agent.build_general_prompt_messages(
        "你最多支持一次性分析多少数据源",
        conversation_id=15,
        history_turn_limit=2,
    )

    assert messages[0].content == (
        "GENERAL_RULES\n\nRUNTIME_ENVIRONMENT\n\nRECENT_ANALYSIS_MEMORY\n\n"
        "CURRENT_DATASOURCE_SCHEMA\n\nLANGUAGE_RULES"
    )
    assert messages[-1].content == "你最多支持一次性分析多少数据源"
    assert datasource_call["context_mode"] == "general"


def test_user_visible_language_contract_covers_sse_and_final_outputs():
    content = agent._build_user_visible_language_message().content

    assert "根据当前用户问题及最近对话自动判断本轮主要自然语言" in content
    assert "工具调用前的中间分析、思考或规划说明" in content
    assert "最终回答、分析报告、图表标题、表格标题和业务说明" in content
    assert "SSE" not in content
    assert "内部思维链" not in content


def test_analysis_prompt_does_not_repeat_general_conversation_routing():
    content = agent.load_system_prompt()

    assert "## 正式分析执行边界" in content
    assert "## 意图分流规则" not in content
    assert "### B. 不属于数据分析任务" not in content
    assert "无数据源例外" not in content
    assert "普通问答或产品咨询" in content
    assert "知识库上下文（预留）" not in content
    assert "{user_input}" not in content
    assert "## 技术栈参考" not in content
    assert "本文档版本" not in content
    assert "最后更新" not in content


def test_general_prompt_carries_ordinary_answer_capabilities_without_runtime_protocol():
    content = agent.load_general_system_prompt()

    assert "普通问答" in content
    assert "通用概念解释" in content
    assert "产品使用与能力咨询" in content
    assert "历史对话用于理解" in content
    assert "最近成功执行摘要" in content
    assert "完整 Schema" in content
    assert "已有结果" in content
    assert "MinIO 远程文件" in content
    assert "HTTP API 数据源" in content
    assert "内部类型 `table` 对外称为 UNS 文件" in content
    assert "折线图、柱状图、饼图、散点图和箱线图" in content
    assert "趋势、对比、异常、业务含义与可操作建议" in content
    assert "10000 行" in content
    assert "execute_python" not in content
    assert "load_data_with_sql" not in content
    assert "request_retry" not in content
    assert "QuerySnapshot" not in content
