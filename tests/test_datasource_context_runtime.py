import json

from langchain_core.messages import SystemMessage

import agent
import agent.context_engineering_runtime as runtime_context


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
    payload = json.loads(message.content[message.content.index("{"):])
    assert payload["selected_datasource_ids"] == list(range(1, 32))
    assert len(payload["datasources"]) == 31
    assert payload["runtime_capabilities"]["query_execution"] == {
        "max_rows_per_sql_result": 10000,
        "capacity_scope": "each physical load_data_with_sql execution",
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
    payload = json.loads(message.content[message.content.index("{"):])
    assert payload["analysis_semantic_context"] == semantic_context
    assert message.content.index('"analysis_semantic_context"') < message.content.index('"datasources"')
    assert "正式分析不再重新选源" in message.content
    assert "逐项阅读 `analysis_semantic_context.business_questions`" in message.content
    assert "开放式洞察方向" in message.content
    assert "不规定报告章节或结论" in message.content
    assert "已选候选范围" not in message.content


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

    payload = json.loads(message.content[message.content.index("{"):])
    datasource = payload["datasources"][0]
    assert datasource["query_contract"] == {
        "datasource_id": 78,
        "datasource_identifier": "dw.contract",
        "allowed_fields": ["amount", "department"],
    }
    assert datasource["metadata_schema"]["properties"] == properties
    assert datasource["metadata_schema"]["all_properties_required"] is True
    assert "required" not in datasource["metadata_schema"]
    assert datasource["field_capabilities"]["all_properties_queryable"] is True
    assert "queryable_fields" not in datasource["field_capabilities"]
    assert "字段归属的执行边界" in message.content


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

    payload = json.loads(message.content[message.content.index("{"):])
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


def test_current_semantic_context_closes_merged_system_message(monkeypatch):
    monkeypatch.setattr(agent, "load_system_prompt", lambda: "CORE_RULES")
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
    )

    system_text = messages[0].content
    assert system_text.index("CORE_RULES") < system_text.index("HISTORICAL_MEMORY")
    assert system_text.index("HISTORICAL_MEMORY") < system_text.index(
        "CURRENT_SCHEMA_AND_SEMANTIC_CONTEXT"
    )
    assert system_text.index("CURRENT_SCHEMA_AND_SEMANTIC_CONTEXT") < system_text.index(
        "CURRENT_REPAIR_INSTRUCTION"
    )


def test_user_visible_language_contract_covers_sse_and_final_outputs():
    content = agent._build_user_visible_language_message().content

    assert "根据当前用户问题及最近对话自动判断本轮主要自然语言" in content
    assert "工具调用前的中间分析、思考或规划说明" in content
    assert "最终回答、分析报告、图表标题、表格标题和业务说明" in content
    assert "SSE" not in content
    assert "内部思维链" not in content
