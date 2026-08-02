import json

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
    assert message.content.index('"datasources"') < message.content.index('"analysis_semantic_context"')
    assert "正式分析不再重新选源" in message.content
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
    assert datasource["metadata_schema"]["properties"] == properties
    assert datasource["metadata_schema"]["all_properties_required"] is True
    assert "required" not in datasource["metadata_schema"]
    assert datasource["field_capabilities"]["all_properties_queryable"] is True
    assert "queryable_fields" not in datasource["field_capabilities"]
