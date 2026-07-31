import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent.analysis_preparation as prep  # noqa: E402
from agent.analysis_preparation import (  # noqa: E402
    BusinessContextContract,
    BusinessRelationshipContract,
    DatasourceSelectionContract,
    IntentContract,
    PreparationResult,
    compact_datasource_snapshot,
    enrich_selected_datasource_schemas,
    infer_query_capabilities,
    prepare_analysis,
    validate_business_context,
)


def _snapshot():
    return {
        "selected_datasource_ids": [11, 12],
        "selected_datasource_snapshot": [
            {
                "datasource_id": 11,
                "datasource_name": "合同",
                "datasource_type": "table",
                "datasource_identifier": "dw.contracts",
                "metadata_schema": {
                    "fields": [
                        {"name": "contract_no", "type": "string"},
                        {"name": "customer_id", "type": "string"},
                        {"name": "amount", "type": "number"},
                        {"name": "status", "type": "string"},
                    ]
                },
            },
            {
                "datasource_id": 12,
                "datasource_name": "客户",
                "datasource_type": "table",
                "datasource_identifier": "dw.customers",
                "metadata_schema": {
                    "fields": [
                        {"name": "id", "type": "string"},
                        {"name": "customer_name", "type": "string"},
                    ]
                },
            },
        ],
    }


def _intent():
    return IntentContract(
        business_goal="了解合同整体情况",
        business_subjects=["合同"],
        business_questions=["合同当前呈现出哪些值得关注的业务特征"],
    )


def _selection():
    return DatasourceSelectionContract(
        selected_datasource_ids=[11, 12],
        selection_reason="合同提供主事实，客户提供关系维度",
    )


def _business_context():
    return BusinessContextContract(
        relationships=[
            BusinessRelationshipContract(
                relationship_id="contract_customer",
                left_datasource_id=11,
                right_datasource_id=12,
                relationship_type="dimension_enrichment",
                joinability="unknown",
                left_fields=["customer_id"],
                right_fields=["id"],
                business_meaning="合同可按客户补充名称",
                evidence_basis="unresolved",
            )
        ],
        unresolved_relationships=["合同与客户的真实覆盖率需要按需 Probe"],
    )


def test_intent_contract_does_not_preplan_analysis_semantics():
    schema = IntentContract.model_json_schema()
    properties = set(schema["properties"])
    assert properties == {
        "business_goal",
        "business_subjects",
        "business_questions",
    }
    assert "request_kind" not in properties
    assert "analysis_modes" not in properties
    assert "explicit_constraints" not in properties


def test_selection_contract_does_not_own_metrics_or_probe_plan():
    properties = set(DatasourceSelectionContract.model_json_schema()["properties"])
    assert "answer_coverage" not in properties
    assert "probe_requirements" not in properties
    assert set(_selection().selected_datasource_ids) == {11, 12}


def test_business_context_only_models_datasource_relationships():
    properties = set(BusinessContextContract.model_json_schema()["properties"])
    assert "analytical_facets" not in properties
    assert "scope_boundaries" not in properties
    assert properties == {
        "relationships",
        "unresolved_relationships",
    }


def test_compact_datasource_snapshot_preserves_complete_field_semantics():
    snapshot = {
        "selected_datasource_ids": [31],
        "selected_datasource_snapshot": [{
            "datasource_id": 31,
            "datasource_name": "设备事件",
            "datasource_type": "table",
            "datasource_identifier": "ops.device_event",
            "metadata_schema": {
                "name": "设备事件",
                "description": "设备运行与停机事件",
                "business_domain": "设备运营",
                "properties": {
                    "event_time": {
                        "name": "事件时间",
                        "property_type": "datetime",
                        "format": "yyyy-MM-dd HH:mm:ss",
                        "description": "事件发生时间",
                    },
                    "status_code": {
                        "name": "运行状态",
                        "property_type": "string",
                        "enum": ["RUNNING", "STOPPED", "MAINTENANCE"],
                        "unit": "状态码",
                    },
                },
                "required": ["event_time"],
            },
        }],
    }

    compact = compact_datasource_snapshot(snapshot)
    schema = compact["selected_datasource_snapshot"][0]["metadata_schema"]

    assert schema["business_domain"] == "设备运营"
    assert schema["required"] == ["event_time"]
    assert schema["properties"]["event_time"] == {
        "name": "事件时间",
        "property_type": "datetime",
        "format": "yyyy-MM-dd HH:mm:ss",
        "description": "事件发生时间",
    }
    assert schema["properties"]["status_code"]["name"] == "运行状态"
    assert schema["properties"]["status_code"]["enum"] == [
        "RUNNING",
        "STOPPED",
        "MAINTENANCE",
    ]
    assert schema["properties"]["status_code"]["unit"] == "状态码"
    assert set(
        compact["selected_datasource_snapshot"][0]["field_capabilities"][
            "queryable_fields"
        ]
    ) == {"event_time", "status_code"}


def test_compact_datasource_snapshot_does_not_invent_required_fields():
    snapshot = {
        "selected_datasource_ids": [31],
        "selected_datasource_snapshot": [{
            "datasource_id": 31,
            "metadata_schema": {
                "fields": [{
                    "name": "optional_status",
                    "property_type": "string",
                    "nullable": True,
                }],
            },
        }],
    }

    compact = compact_datasource_snapshot(snapshot)
    schema = compact["selected_datasource_snapshot"][0]["metadata_schema"]

    assert "required" not in schema
    assert schema["fields"][0]["nullable"] is True
    assert schema["properties"]["optional_status"]["nullable"] is True


def test_business_context_reference_validation_accepts_real_schema_fields():
    snapshot = compact_datasource_snapshot(_snapshot())
    validate_business_context(
        _business_context(),
        snapshot["selected_datasource_snapshot"],
        selection=_selection(),
    )


def test_business_context_reference_validation_rejects_unknown_fields():
    context = _business_context()
    broken = context.model_copy(update={
        "relationships": [
            context.relationships[0].model_copy(
                update={"left_fields": ["missing_field"]}
            )
        ]
    })
    snapshot = compact_datasource_snapshot(_snapshot())
    with pytest.raises(ValueError, match="unavailable fields"):
        validate_business_context(
            broken,
            snapshot["selected_datasource_snapshot"],
            selection=_selection(),
        )


def test_business_context_schema_evidence_requires_explicit_relationship_metadata():
    context = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "schema",
            })
        ],
        "unresolved_relationships": [],
    })
    snapshot = compact_datasource_snapshot(_snapshot())
    with pytest.raises(ValueError, match="explicit relationship metadata"):
        validate_business_context(
            context,
            snapshot["selected_datasource_snapshot"],
            selection=_selection(),
        )

    declared_snapshot = _snapshot()
    declared_snapshot["selected_datasource_snapshot"][0]["metadata_schema"][
        "foreign_keys"
    ] = [{
        "columns": ["customer_id"],
        "referenced_datasource_id": 12,
        "referenced_columns": ["id"],
    }]
    compact = compact_datasource_snapshot(declared_snapshot)
    validate_business_context(
        context,
        compact["selected_datasource_snapshot"],
        selection=_selection(),
    )


def test_business_context_rejects_mismatched_relationship_key_arity():
    context = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "left_fields": ["customer_id", "contract_no"],
                "right_fields": ["id"],
            })
        ],
    })
    with pytest.raises(ValueError, match="same number"):
        validate_business_context(
            context,
            compact_datasource_snapshot(_snapshot())[
                "selected_datasource_snapshot"
            ],
            selection=_selection(),
        )


def test_preparation_result_exposes_only_enriched_selected_schema_to_formal_analysis():
    enriched_snapshot = enrich_selected_datasource_schemas(
        _snapshot(),
        selection=_selection(),
        business_context=_business_context(),
    )
    result = PreparationResult(
        intent=_intent(),
        selection=_selection(),
        business_context=_business_context(),
        selected_snapshot=enriched_snapshot,
        query_capabilities=("sql",),
    )
    prompt_snapshot = result.prompt_snapshot()
    assert "preparation_envelope" not in prompt_snapshot
    assert "intent_context" not in str(prompt_snapshot)
    assert "datasource_context" not in str(prompt_snapshot)
    fields = {
        item["datasource_id"]: set(item["field_capabilities"]["queryable_fields"])
        for item in prompt_snapshot["selected_datasource_snapshot"]
    }
    assert fields[11] == {"contract_no", "customer_id", "amount", "status"}
    assert fields[12] == {"id", "customer_name"}
    relationships = {
        item["datasource_id"]: item["metadata_schema"]["relationships"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    }
    assert relationships[11][0]["target_datasource_id"] == 12
    assert relationships[11][0]["local_fields"] == ["customer_id"]
    assert relationships[12][0]["target_datasource_id"] == 11
    assert relationships[12][0]["local_fields"] == ["id"]
    schemas = {
        item["datasource_id"]: item["metadata_schema"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    }
    assert "analysis_role" not in schemas[11]
    assert schemas[11]["properties"]["customer_id"]["relationship_ids"] == [
        "contract_customer"
    ]
    assert schemas[12]["properties"]["id"]["relationship_ids"] == [
        "contract_customer"
    ]
    assert all(
        "declared_relationships" not in item
        for item in prompt_snapshot["selected_datasource_snapshot"]
    )


def test_schema_enrichment_preserves_explicit_fk_without_parallel_context():
    snapshot = _snapshot()
    snapshot["selected_datasource_snapshot"][0]["metadata_schema"]["foreign_keys"] = [{
        "columns": ["customer_id"],
        "referenced_datasource_id": 12,
        "referenced_columns": ["id"],
    }]
    fallback_context = prep.AnalysisPreparationWorkflow._fallback_business_context(
        _selection()
    )

    enriched = enrich_selected_datasource_schemas(
        snapshot,
        selection=_selection(),
        business_context=fallback_context,
    )
    contract_schema = enriched["selected_datasource_snapshot"][0]["metadata_schema"]

    assert contract_schema["foreign_keys"] == [{
        "columns": ["customer_id"],
        "referenced_datasource_id": 12,
        "referenced_columns": ["id"],
    }]
    assert contract_schema["relationships"] == [{
        "relationship_id": "schema_relationship_1",
        "target_datasource_id": 12,
        "target_datasource_name": "客户",
        "local_fields": ["customer_id"],
        "target_fields": ["id"],
        "relationship_type": "same_entity",
        "joinability": "joinable",
        "business_meaning": "数据源 Schema 显式声明的字段关系",
        "evidence_basis": "schema",
    }]


def test_prepare_analysis_runs_advisory_flow_without_failure_route(monkeypatch):
    outputs = {
        prep.IntentContract: _intent(),
        prep.DatasourceSelectionContract: _selection(),
        prep.BusinessContextContract: _business_context(),
    }
    stages = []

    def fake_invoke_contract(*, stage, output_model, **kwargs):
        stages.append(stage)
        return outputs[output_model]

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)
    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )
    assert stages == ["intent_analysis", "select_datasources", "business_context"]
    prompt_snapshot = result.prompt_snapshot()
    assert prompt_snapshot["selected_datasource_ids"] == [11, 12]
    assert "preparation_envelope" not in prompt_snapshot
    assert (
        prompt_snapshot["selected_datasource_snapshot"][0]["metadata_schema"][
            "relationships"
        ][0]["target_datasource_id"]
        == 12
    )


def test_business_context_rejects_fabricated_probe_relationship():
    context = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "probe",
                "observation_id": "probe:not_registered",
            })
        ]
    })

    with pytest.raises(ValueError, match="unavailable Probe observation"):
        validate_business_context(
            context,
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            selection=_selection(),
            probe_observations=[],
        )


def test_business_context_semantic_rejection_is_repaired_in_stage(monkeypatch):
    invalid_schema_claim = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "schema",
            })
        ],
    })
    def fake_invoke_contract(*, output_model, **kwargs):
        if output_model is prep.IntentContract:
            return _intent()
        if output_model is prep.DatasourceSelectionContract:
            return _selection()
        return invalid_schema_claim

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)

    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )

    assert result.business_context.relationships[0].evidence_basis == "unresolved"
    assert result.business_context.relationships[0].joinability == "unknown"
    assert result.probe_observations == ()


def test_single_datasource_skips_selection_and_business_context_llm(monkeypatch):
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"] = [11]
    snapshot["selected_datasource_snapshot"] = snapshot["selected_datasource_snapshot"][:1]
    selection = _selection().model_copy(update={
        "selected_datasource_ids": [11],
    })
    outputs = {prep.IntentContract: _intent()}
    stages = []

    def fake_invoke_contract(*, stage, output_model, **kwargs):
        stages.append(stage)
        return outputs[output_model]

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)
    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
    )

    assert stages == ["intent_analysis"]
    assert result.selection.selected_datasource_ids == [11]
    assert result.business_context.relationships == []


def test_discovery_catalog_preserves_all_candidates_before_selection():
    catalog = [
        {
            "datasource_id": index,
            "datasource_name": "合同明细" if index == 7 else f"无关来源{index}",
            "description": "合同金额与合同状态" if index == 7 else "其他主题",
            "fields": [{"name": "contract_amount" if index == 7 else f"field_{index}"}],
        }
        for index in range(1, 41)
    ]

    shortlisted = prep.build_datasource_discovery_catalog(
        catalog,
        search_text="分析合同金额",
    )

    assert len(shortlisted) == 40
    assert shortlisted[6]["datasource_id"] == 7


def test_selection_is_not_rejected_by_formal_context_capacity():
    result = DatasourceSelectionContract(
        selected_datasource_ids=[11, 12, 13],
        selection_reason="test",
    )

    assert result.selected_datasource_ids == [11, 12, 13]


def test_shortlist_trigger_uses_discovery_payload_size(monkeypatch):
    monkeypatch.setattr(
        prep.Config,
        "DATASOURCE_SELECTION_SHORTLIST_TRIGGER_COUNT",
        8,
    )
    monkeypatch.setattr(
        prep.Config,
        "DATASOURCE_SELECTION_SHORTLIST_TRIGGER_CHARS",
        1000,
    )

    assert prep._requires_datasource_shortlist([
        {"datasource_id": index, "description": "small"}
        for index in range(9)
    ])
    assert prep._requires_datasource_shortlist([
        {"datasource_id": 1, "description": "x" * 1200}
    ])
    assert not prep._requires_datasource_shortlist([
        {"datasource_id": index, "description": "small"}
        for index in range(3)
    ])


def test_preparation_stage_failure_carries_uncertainty_to_formal_analysis(monkeypatch):
    def fail_contract(**kwargs):
        raise ValueError("synthetic contract failure")

    monkeypatch.setattr(prep, "_invoke_contract", fail_contract)
    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )
    assert "preparation_envelope" not in result.prompt_snapshot()
    assert set(result.selection.selected_datasource_ids) == {11, 12}
    assert result.business_context.unresolved_relationships


def test_query_capabilities_are_loaded_from_selected_source_types():
    assert infer_query_capabilities(_snapshot()) == ("sql",)
