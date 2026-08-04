import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent.analysis_preparation as prep  # noqa: E402
import agent.tools as runtime_tools  # noqa: E402
from agent.analysis_preparation import (  # noqa: E402
    BusinessFactCoverageContract,
    BusinessFactBindingContract,
    BusinessFactRequirementContract,
    BusinessContextContract,
    BusinessContextProbeRequest,
    BusinessQuestionContract,
    BusinessQuestionCoverageContract,
    BusinessRelationshipContract,
    DatasourceSelectionContract,
    IntentContract,
    InsightLineageContract,
    PreparationResult,
    UnsupportedBusinessFieldBindingContract,
    UnresolvedBusinessFactContract,
    compact_datasource_snapshot,
    enrich_selected_datasource_schemas,
    infer_query_capabilities,
    prepare_analysis,
    recall_datasources_for_business_facts,
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
        analytical_facets=["规模", "结构", "业务含义"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="合同当前呈现出哪些值得关注的业务特征",
        required_business_facts=[
            BusinessFactRequirementContract(
                fact_id=f"question_1_fact_{index}",
                business_fact=fact,
                semantic_definition=f"{fact}的业务语义",
            )
            for index, fact in enumerate(
                ["合同规模", "合同状态", "客户归属"],
                start=1,
            )
        ],
        )],
    )


def _selection():
    return DatasourceSelectionContract(
        selected_datasource_ids=[11, 12],
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            datasource_ids=[11, 12],
            fact_coverage=[
                BusinessFactCoverageContract(
                    fact_id=f"question_1_fact_{index}",
                    coverage_status="direct",
                    datasource_ids=[11, 12],
                    coverage_reason="合同与客户来源共同支撑该事实",
                )
                for index, fact in enumerate(
                    ["合同规模", "合同状态", "客户归属"],
                    start=1,
                )
            ],
            coverage_reason="合同提供事实，客户提供业务归属",
        )],
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
        insight_lineage=[InsightLineageContract(
            question_id="question_1",
            business_question="合同当前呈现出哪些值得关注的业务特征",
            lineage_status="partial",
            datasource_ids=[11, 12],
            fact_bindings=[
                BusinessFactBindingContract(
                    fact_id="question_1_fact_1",
                    datasource_id=11,
                    fields=["contract_no", "amount", "status", "customer_id"],
                ),
                BusinessFactBindingContract(
                    fact_id="question_1_fact_2",
                    datasource_id=11,
                    fields=["status"],
                ),
                BusinessFactBindingContract(
                    fact_id="question_1_fact_3",
                    datasource_id=12,
                    fields=["id", "customer_name"],
                ),
            ],
            relationship_ids=["contract_customer"],
            unresolved_business_facts=[UnresolvedBusinessFactContract(
                fact_id="question_1_fact_3",
                reason="合同与客户关系覆盖尚未确认",
                business_impact="当前数据无法可靠补充客户归属，因此不按客户分类下结论。",
                unsupported_field_bindings=[UnsupportedBusinessFieldBindingContract(
                    datasource_id=11,
                    fields=["customer_id"],
                    business_reason="客户标识尚不能可靠补充客户归属",
                )],
            )],
            synthesis_path="合同事实通过客户标识补充客户归属后形成整体判断",
        )],
        unresolved_relationships=["合同与客户的真实覆盖率需要按需 Probe"],
    )


def _canonical_flow_selection():
    return DatasourceSelectionContract(
        selected_datasource_ids=[11, 12],
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            datasource_ids=[11, 12],
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="direct",
                datasource_ids=[11, 12],
                coverage_reason="合同与客户来源共同支撑原业务问题",
            )],
            coverage_reason="合同提供事实，客户提供业务归属",
        )],
        selection_reason="合同提供主事实，客户提供关系维度",
    )


def _canonical_flow_context():
    base = _business_context()
    relationship = base.relationships[0].model_copy(update={
        "joinability": "joinable",
        "evidence_basis": "probe",
        "observation_id": "probe:contract_customer",
        "probe_facts": {
            "join_supported": True,
            "cardinality": "many_to_one",
        },
    })
    lineage = base.insight_lineage[0].model_copy(update={
        "lineage_status": "direct",
        "fact_bindings": [
            BusinessFactBindingContract(
                fact_id="question_1_fact_1",
                datasource_id=11,
                fields=["contract_no", "amount", "status", "customer_id"],
            ),
            BusinessFactBindingContract(
                fact_id="question_1_fact_1",
                datasource_id=12,
                fields=["id", "customer_name"],
            ),
        ],
        "unresolved_business_facts": [],
    })
    return base.model_copy(update={
        "relationships": [relationship],
        "insight_lineage": [lineage],
        "unresolved_relationships": [],
    })


def test_intent_contract_plans_business_facts_without_execution_semantics():
    schema = IntentContract.model_json_schema()
    properties = set(schema["properties"])
    assert properties == {
        "business_goal",
        "business_subjects",
        "analytical_facets",
        "business_questions",
    }
    assert "request_kind" not in properties
    assert "analysis_modes" not in properties
    assert "explicit_constraints" not in properties
    intent = IntentContract(
        business_goal="了解合同整体情况",
        business_subjects=["合同"],
        analytical_facets=["规模", "状态构成", "异常"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="合同总体情况如何",
            required_business_facts=[BusinessFactRequirementContract(
                fact_id="question_1_fact_1",
                business_fact="合同总体情况",
                semantic_definition="当前合同主体的总体业务状态",
            )],
        )],
    )
    assert intent.analytical_facets == ["规模", "状态构成", "异常"]
    question_properties = set(
        BusinessQuestionContract.model_json_schema()["properties"]
    )
    assert question_properties == {
        "question_id",
        "business_question",
        "required_business_facts",
    }


def test_source_free_intent_canonicalizes_selection_facts_to_explicit_questions():
    intent = IntentContract(
        business_goal="了解年费合同情况",
        business_subjects=["年费合同"],
        analytical_facets=["规模", "状态", "趋势"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="年费合同情况如何",
            required_business_facts=[
                BusinessFactRequirementContract(
                    fact_id="invented_amount",
                    business_fact="年费合同金额",
                    semantic_definition="模型在无 Schema 时自行扩写的金额事实",
                ),
                BusinessFactRequirementContract(
                    fact_id="invented_renewal",
                    business_fact="续约率",
                    semantic_definition="模型在无 Schema 时自行扩写的续约事实",
                ),
            ],
        )],
    )

    canonical = prep._canonicalize_intent_selection_facts(intent)

    facts = canonical.business_questions[0].required_business_facts
    assert len(facts) == 1
    assert facts[0].fact_id == "question_1_fact_1"
    assert facts[0].business_fact == "年费合同情况如何"
    assert canonical.analytical_facets == ["规模", "状态", "趋势"]


def test_fact_recall_keeps_dimension_source_omitted_by_llm_shortlist():
    intent = IntentContract(
        business_goal="分析收入结构",
        business_subjects=["收入", "产品"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="各产品线收入贡献如何",
            required_business_facts=[BusinessFactRequirementContract(
                fact_id="question_1_fact_1",
                business_fact="各产品线营业收入",
                semantic_definition="按产品分类拆分确认收入",
            )],
        )],
    )
    catalog = [
        {
            "datasource_id": 1,
            "datasource_name": "收入事实",
            "description": "确认收入",
            "field_details": [
                {"name": "revenue", "description": "营业收入"},
            ],
        },
        {
            "datasource_id": 2,
            "datasource_name": "产品目录",
            "description": "产品维度",
            "field_details": [
                {"name": "product_category", "description": "产品线分类"},
            ],
        },
        {
            "datasource_id": 3,
            "datasource_name": "员工目录",
            "description": "组织人员",
            "field_details": [
                {"name": "employee", "description": "员工姓名"},
            ],
        },
    ]

    recalled = recall_datasources_for_business_facts(catalog, intent, per_fact=2)

    assert set(recalled) == {1, 2}
    assert 3 not in recalled


def test_derived_fact_binding_accepts_null_as_logical_datasource():
    binding = BusinessFactBindingContract.model_validate({
        "fact_id": "growth_rate",
        "datasource_id": None,
        "fields": [],
        "derived_from_fact_ids": ["current_value", "baseline_value"],
    })

    assert binding.datasource_id == 0


def test_selection_includes_semantic_support_sources_in_canonical_scope():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="partial",
                datasource_ids=[11],
                supporting_datasource_ids=[12, 12, 11],
                coverage_reason="事实表提供金额，客户表提供业务标签",
            )],
            coverage_reason="度量与业务维度共同覆盖问题",
        )],
        selection_reason="保留完整事实与语义空间",
    )

    fact_coverage = selection.question_coverage[0].fact_coverage[0]
    assert fact_coverage.supporting_datasource_ids == [12]
    assert selection.question_coverage[0].datasource_ids == [11, 12]
    assert selection.selected_datasource_ids == [11, 12]


def test_derived_fact_inherits_sources_and_status_from_canonical_facts():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[
            BusinessFactCoverageContract(
                fact_id="current_revenue",
                coverage_status="direct",
                datasource_ids=[11],
                supporting_datasource_ids=[12],
                coverage_reason="本期收入事实",
            ),
            BusinessFactCoverageContract(
                fact_id="prior_revenue",
                coverage_status="direct",
                datasource_ids=[11],
                supporting_datasource_ids=[12],
                coverage_reason="同期收入事实",
            ),
            BusinessFactCoverageContract(
                fact_id="growth_rate",
                coverage_status="direct",
                derived_from_fact_ids=["current_revenue", "prior_revenue"],
                coverage_reason="由本期和同期收入确定性计算",
            ),
            ],
            coverage_reason="收入事实及其派生同比",
        )],
        selection_reason="年度收入事实图",
    )

    coverage = selection.question_coverage[0]
    growth = coverage.fact_coverage[2]
    assert growth.datasource_ids == [11]
    assert growth.supporting_datasource_ids == [12]
    assert coverage.coverage_status == "direct"


def test_derived_fact_can_depend_on_a_fact_from_another_business_question():
    selection = DatasourceSelectionContract(
        question_coverage=[
            BusinessQuestionCoverageContract(
                question_id="overall",
                fact_coverage=[BusinessFactCoverageContract(
                    fact_id="overall_revenue",
                    coverage_status="direct",
                    datasource_ids=[11],
                    coverage_reason="整体营收",
                )],
                coverage_reason="整体口径",
            ),
            BusinessQuestionCoverageContract(
                question_id="structure",
                fact_coverage=[
                    BusinessFactCoverageContract(
                        fact_id="product_revenue",
                        coverage_status="direct",
                        datasource_ids=[11],
                        supporting_datasource_ids=[12],
                        coverage_reason="产品营收",
                    ),
                    BusinessFactCoverageContract(
                        fact_id="product_share",
                        coverage_status="direct",
                        derived_from_fact_ids=[
                            "product_revenue",
                            "overall_revenue",
                        ],
                        coverage_reason="产品营收除以整体营收",
                    ),
                ],
                coverage_reason="产品结构",
            ),
        ],
        selection_reason="完整营收事实图",
    )

    share = selection.question_coverage[1].fact_coverage[1]
    assert share.datasource_ids == [11]
    assert share.supporting_datasource_ids == [12]


def test_business_fact_binding_supports_derived_lineage_without_fake_fields():
    binding = BusinessFactBindingContract(
        fact_id="growth_rate",
        datasource_id=11,
        derived_from_fact_ids=["current_revenue", "prior_revenue"],
    )

    assert binding.fields == []
    assert binding.datasource_id == 0
    assert binding.derived_from_fact_ids == ["current_revenue", "prior_revenue"]


def test_business_context_compiler_completes_selection_owned_derived_facts():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[
                BusinessFactCoverageContract(
                    fact_id="revenue",
                    coverage_status="direct",
                    datasource_ids=[11],
                    coverage_reason="revenue fact",
                ),
                BusinessFactCoverageContract(
                    fact_id="share",
                    coverage_status="direct",
                    derived_from_fact_ids=["revenue"],
                    coverage_reason="derived share",
                ),
            ],
            coverage_reason="question coverage",
        )],
        selection_reason="selected revenue source",
    )
    candidate = BusinessContextContract(insight_lineage=[InsightLineageContract(
        question_id="question_1",
        business_question="revenue structure",
        lineage_status="direct",
        datasource_ids=[11],
        fact_bindings=[BusinessFactBindingContract(
            fact_id="revenue",
            datasource_id=11,
            fields=["amount"],
        )],
        synthesis_path="derive structure from revenue",
    )])

    completed = prep.AnalysisPreparationWorkflow._complete_derived_fact_lineage(
        candidate,
        selection,
    )

    derived = completed.insight_lineage[0].fact_bindings[1]
    assert derived.fact_id == "share"
    assert derived.datasource_id == 0
    assert derived.fields == []
    assert derived.derived_from_fact_ids == ["revenue"]


def test_derived_fact_completion_promotes_fully_grounded_lineage_to_direct():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[
                BusinessFactCoverageContract(
                    fact_id="measured_fact",
                    coverage_status="direct",
                    datasource_ids=[11],
                    coverage_reason="measured directly",
                ),
                BusinessFactCoverageContract(
                    fact_id="derived_advice",
                    coverage_status="direct",
                    derived_from_fact_ids=["measured_fact"],
                    coverage_reason="derived from the measured fact",
                ),
            ],
            coverage_reason="complete",
        )],
        selection_reason="complete fact graph",
    )
    candidate = BusinessContextContract(insight_lineage=[InsightLineageContract(
        question_id="question_1",
        business_question="analyze and recommend",
        lineage_status="partial",
        datasource_ids=[11],
        fact_bindings=[BusinessFactBindingContract(
            fact_id="measured_fact",
            datasource_id=11,
            fields=["amount"],
        )],
        synthesis_path="derive recommendations from the measured fact",
    )])

    completed = prep.AnalysisPreparationWorkflow._complete_derived_fact_lineage(
        candidate,
        selection,
    )

    assert completed.insight_lineage[0].lineage_status == "direct"
    assert {
        item.fact_id for item in completed.insight_lineage[0].fact_bindings
    } == {"measured_fact", "derived_advice"}


def test_selection_contract_covers_questions_without_metrics_or_probe_plan():
    properties = set(DatasourceSelectionContract.model_json_schema()["properties"])
    assert "question_coverage" in properties
    assert "probe_requirements" not in properties
    assert set(_selection().selected_datasource_ids) == {11, 12}


def test_question_coverage_status_is_derived_from_each_business_fact():
    coverage = BusinessQuestionCoverageContract(
        question_id="question_1",
        coverage_status="direct",
        datasource_ids=[999],
        fact_coverage=[
            BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="direct",
                datasource_ids=[11],
                coverage_reason="合同表直接支撑",
            ),
            BusinessFactCoverageContract(
                fact_id="question_1_fact_3",
                coverage_status="unresolved",
                datasource_ids=[],
                coverage_reason="缺少可确认的客户关系",
            ),
        ],
        coverage_reason="只能覆盖部分事实",
    )

    assert coverage.coverage_status == "partial"
    assert coverage.datasource_ids == [11]


def test_selection_fact_coverage_must_match_intent_facts_exactly():
    selection = DatasourceSelectionContract(
        selected_datasource_ids=[11],
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            datasource_ids=[11],
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_unknown",
                coverage_status="direct",
                datasource_ids=[11],
                coverage_reason="错误替代",
            )],
            coverage_reason="错误覆盖",
        )],
        selection_reason="test",
    )

    with pytest.raises(ValueError, match="exactly match"):
        prep.AnalysisPreparationWorkflow._normalize_selection(
            selection,
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            _intent(),
        )


def test_selection_ids_are_derived_from_business_fact_coverage():
    selection = DatasourceSelectionContract(
        selected_datasource_ids=[11, 12],
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            datasource_ids=[999],
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="direct",
                datasource_ids=[11],
                coverage_reason="合同来源直接支撑",
            )],
            coverage_reason="只声明了合同来源",
        )],
        selection_reason="只有事实覆盖会进入最终集合",
    )

    assert selection.selected_datasource_ids == [11]


def test_selection_ignores_read_only_business_question_echo():
    coverage = BusinessQuestionCoverageContract.model_validate({
        "question_id": "question_1",
        "business_question": "合同情况如何",
        "fact_coverage": [{
            "fact_id": "question_1_fact_1",
            "coverage_status": "direct",
            "datasource_ids": [11],
            "coverage_reason": "合同表直接承载",
        }],
        "coverage_reason": "合同来源覆盖问题",
    })

    assert coverage.question_id == "question_1"
    assert not hasattr(coverage, "business_question")


def test_selection_normalizes_intent_fact_label_to_canonical_coverage():
    coverage = BusinessQuestionCoverageContract.model_validate({
        "question_id": "question_1",
        "required_business_facts": [{
            "fact_id": "question_1_fact_1",
            "coverage_status": "direct",
            "datasource_ids": [15],
            "coverage_reason": "The selected fact source directly supports the question.",
        }],
        "coverage_reason": "The selected sources cover the question.",
    })

    assert [item.fact_id for item in coverage.fact_coverage] == ["question_1_fact_1"]


def test_selection_normalizes_matching_fact_id_echo_alias():
    coverage = BusinessFactCoverageContract.model_validate({
        "fact_id": "question_1_fact_1",
        "coverage_id": "question_1_fact_1",
        "coverage_status": "direct",
        "datasource_ids": [11],
        "coverage_reason": "合同表直接承载",
    })

    assert coverage.fact_id == "question_1_fact_1"


def test_selection_discards_read_only_business_fact_echo():
    coverage = BusinessFactCoverageContract.model_validate({
        "fact_id": "question_1_fact_1",
        "business_fact": "年费合同情况如何",
        "semantic_definition": "上游事实的只读说明",
        "coverage_status": "direct",
        "datasource_ids": [15],
        "coverage_reason": "年费运营主表直接支撑",
    })

    assert coverage.fact_id == "question_1_fact_1"
    assert not hasattr(coverage, "business_fact")


def test_question_coverage_discards_read_only_business_fact_echo():
    coverage = BusinessQuestionCoverageContract.model_validate({
        "question_id": "question_1",
        "business_fact": "年费合同情况如何",
        "coverage_status": "direct",
        "datasource_ids": [15],
        "fact_coverage": [{
            "fact_id": "question_1_fact_1",
            "coverage_status": "direct",
            "datasource_ids": [15],
            "coverage_reason": "年费运营主表直接支撑",
        }],
        "coverage_reason": "年费主题域可回答原问题",
    })

    assert coverage.question_id == "question_1"
    assert not hasattr(coverage, "business_fact")


def test_lineage_merges_split_bindings_for_the_same_fact_and_datasource():
    lineage = InsightLineageContract.model_validate({
        "question_id": "question_1",
        "business_question": "合同情况如何",
        "lineage_status": "direct",
        "datasource_ids": [11],
        "fact_bindings": [
            {
                "fact_id": "question_1_fact_1",
                "datasource_id": 11,
                "fields": ["contract_id"],
            },
            {
                "fact_id": "question_1_fact_1",
                "datasource_id": 11,
                "fields": ["status", "contract_id"],
            },
        ],
        "relationship_ids": [],
        "unresolved_business_facts": [],
        "synthesis_path": "由合同事实形成整体判断",
    })

    assert len(lineage.fact_bindings) == 1
    assert lineage.fact_bindings[0].fields == ["contract_id", "status"]


def test_unresolved_fact_discards_empty_unsupported_field_binding():
    fact = UnresolvedBusinessFactContract.model_validate({
        "fact_id": "question_1_fact_1",
        "reason": "候选字段不能支撑业务事实",
        "business_impact": "该维度保持未解析",
        "unsupported_field_bindings": [{
            "datasource_id": 16,
            "fields": [],
            "business_reason": "没有实际字段边界",
        }],
    })

    assert fact.unsupported_field_bindings == []


def test_insight_lineage_discards_probe_result_echoes():
    lineage = InsightLineageContract.model_validate({
        "question_id": "question_1",
        "business_question": "合同情况如何",
        "lineage_status": "direct",
        "datasource_ids": [1],
        "fact_bindings": [{
            "fact_id": "question_1_fact_1",
            "datasource_id": 1,
            "fields": ["status"],
            "derived_from_fact_ids": [],
        }],
        "relationship_ids": [],
        "unresolved_business_facts": [],
        "synthesis_path": "合同状态字段支撑整体分析。",
        "verified_enums": {"status": {"values": ["有效"]}},
    })

    assert lineage.question_id == "question_1"


def test_business_context_models_question_lineage_without_report_plan():
    properties = set(BusinessContextContract.model_json_schema()["properties"])
    assert "analytical_facets" not in properties
    assert "scope_boundaries" not in properties
    assert properties == {
        "relationships",
        "insight_lineage",
        "unresolved_relationships",
        "probe_requests",
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


def test_business_lineage_must_cover_every_planned_question():
    with pytest.raises(ValueError, match="every planned business question"):
        validate_business_context(
            _business_context().model_copy(update={"insight_lineage": []}),
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            selection=_selection(),
            intent=_intent(),
        )


def test_business_lineage_rejects_unavailable_fact_fields():
    lineage = _business_context().insight_lineage[0]
    broken = _business_context().model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "fact_bindings": [
                lineage.fact_bindings[0].model_copy(update={
                    "fields": ["invented_metric_field"],
                }),
                lineage.fact_bindings[1],
            ],
        })],
    })
    with pytest.raises(ValueError, match="unavailable fact fields"):
        validate_business_context(
            broken,
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            selection=_selection(),
            intent=_intent(),
        )


def test_direct_lineage_cannot_depend_on_unresolved_cross_source_relationship():
    context = _business_context()
    direct_lineage = context.insight_lineage[0].model_copy(update={
        "lineage_status": "direct",
        "unresolved_business_facts": [],
    })
    with pytest.raises(ValueError, match="depends on unresolved cross-source"):
        validate_business_context(
            context.model_copy(update={"insight_lineage": [direct_lineage]}),
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            selection=_selection(),
            intent=_intent(),
        )

    partial_lineage = context.insight_lineage[0].model_copy(update={
        "lineage_status": "partial",
        "unresolved_business_facts": [UnresolvedBusinessFactContract(
            fact_id="question_1_fact_3",
            reason="合同与客户关系覆盖尚未确认",
        )],
    })
    validate_business_context(
        context.model_copy(update={"insight_lineage": [partial_lineage]}),
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
        intent=_intent(),
    )


def test_unresolved_relationship_downgrades_only_the_affected_lineage():
    context = _business_context()
    direct_lineage = context.insight_lineage[0].model_copy(update={
        "lineage_status": "direct",
        "unresolved_business_facts": [],
    })
    candidate = context.model_copy(update={"insight_lineage": [direct_lineage]})

    normalized = prep.AnalysisPreparationWorkflow._complete_derived_fact_lineage(
        candidate,
        _selection(),
    )

    lineage = normalized.insight_lineage[0]
    assert lineage.lineage_status == "partial"
    assert lineage.unresolved_business_facts
    assert normalized.relationships == candidate.relationships
    validate_business_context(
        normalized,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
        intent=_intent(),
    )


def test_unknown_relationship_cannot_reintroduce_schema_join_evidence():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "relationship_type": "unknown",
        "joinability": "alignable",
        "evidence_basis": "schema",
    })

    normalized = prep.AnalysisPreparationWorkflow._normalize_unverified_schema_relationships(
        context.model_copy(update={"relationships": [relationship]}),
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
    )

    assert normalized.relationships[0].joinability == "unknown"
    assert normalized.relationships[0].evidence_basis == "unresolved"


def test_comparable_relationship_does_not_require_physical_join_metadata():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "relationship_type": "comparable",
        "joinability": "alignable",
        "evidence_basis": "schema",
    })
    candidate = context.model_copy(update={"relationships": [relationship]})

    validate_business_context(
        candidate,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
        intent=_intent(),
    )


def test_lineage_relationship_scope_drops_relationships_outside_question_sources():
    context = _business_context()
    lineage = context.insight_lineage[0].model_copy(update={
        "lineage_status": "direct",
        "datasource_ids": [11],
        "fact_bindings": [
            BusinessFactBindingContract(
                fact_id=f"question_1_fact_{index}",
                datasource_id=11,
                fields=["amount"],
            )
            for index in range(1, 4)
        ],
        "relationship_ids": ["contract_customer"],
        "unresolved_business_facts": [],
    })

    normalized = prep.AnalysisPreparationWorkflow._scope_lineage_relationships(
        context.model_copy(update={"insight_lineage": [lineage]})
    )

    assert normalized.insight_lineage[0].relationship_ids == []
    assert normalized.relationships == []


def test_business_context_schema_probe_request_requires_selected_real_field():
    context = _business_context().model_copy(update={
        "probe_requests": [BusinessContextProbeRequest(
            request_id="status_values",
            datasource_id=11,
            field="missing_status",
            probe_kind="distinct_values",
            purpose="确认合同状态枚举",
            related_fact_ids=["question_1_fact_1"],
        )],
    })

    with pytest.raises(ValueError, match="unavailable field"):
        validate_business_context(
            context,
            compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
            selection=_selection(),
            intent=_intent(),
        )


@pytest.mark.parametrize("value", [None, 0, ""])
def test_business_context_probe_ignores_empty_top_n_for_non_enum_profiles(value):
    request = BusinessContextProbeRequest.model_validate({
        "request_id": "time_range",
        "datasource_id": 11,
        "field": "status",
        "probe_kind": "field_profile",
        "purpose": "确认关系键覆盖",
        "top_n": value,
    })

    assert request.top_n == 20


def test_business_context_probe_clamps_oversized_top_n():
    request = BusinessContextProbeRequest.model_validate({
        "request_id": "status_values",
        "datasource_id": 11,
        "field": "status",
        "probe_kind": "distinct_values",
        "purpose": "resolve lifecycle states",
        "top_n": 100,
    })

    assert request.top_n == 50


def test_business_context_probe_normalizes_null_optional_lists():
    request = BusinessContextProbeRequest.model_validate({
        "request_id": "status_values",
        "datasource_id": 11,
        "field": "status",
        "probe_kind": "distinct_values",
        "purpose": "resolve lifecycle states",
        "related_fact_ids": None,
        "expected_values": None,
    })

    assert request.related_fact_ids == []
    assert request.expected_values == []


def test_business_context_probe_normalizes_json_scalar_expected_values():
    request = BusinessContextProbeRequest.model_validate({
        "request_id": "month_values",
        "datasource_id": 11,
        "field": "month_key",
        "probe_kind": "distinct_values",
        "purpose": "confirm the stored month representation",
        "expected_values": [202606, 6.0, True, "06"],
    })

    assert request.expected_values == ["202606", "6.0", "True", "06"]


def test_business_context_removes_intrasource_er_edges():
    context = _business_context()
    self_edge = context.relationships[0].model_copy(update={
        "relationship_id": "contract_internal",
        "right_datasource_id": 11,
        "right_fields": ["contract_no"],
    })
    lineage = context.insight_lineage[0].model_copy(update={
        "relationship_ids": ["contract_internal"],
    })

    normalized = prep.AnalysisPreparationWorkflow._remove_intrasource_relationships(
        context.model_copy(update={
            "relationships": [self_edge],
            "insight_lineage": [lineage],
        })
    )

    assert normalized.relationships == []
    assert normalized.insight_lineage[0].relationship_ids == []


def test_business_context_can_refine_partial_selection_with_full_schema():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[
                BusinessFactCoverageContract(
                    fact_id=f"question_1_fact_{index}",
                    coverage_status="partial",
                    datasource_ids=[11],
                    coverage_reason="候选阶段尚未完成字段级确认",
                )
                for index in range(1, 4)
            ],
            coverage_reason="等待完整 Schema 形成最终业务血缘",
        )],
        selection_reason="高召回保留合同事实来源",
    )
    context = BusinessContextContract(insight_lineage=[InsightLineageContract(
        question_id="question_1",
        business_question=_intent().business_questions[0].business_question,
        lineage_status="direct",
        datasource_ids=[11],
        fact_bindings=[
            BusinessFactBindingContract(
                fact_id=f"question_1_fact_{index}",
                datasource_id=11,
                fields=["amount" if index == 1 else "status"],
            )
            for index in range(1, 4)
        ],
        synthesis_path="完整合同 Schema 已直接承载全部事实",
    )])

    validate_business_context(
        context,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=selection,
        intent=_intent(),
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
                "joinability": "joinable",
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


def test_business_context_keeps_unresolved_candidate_key_sets():
    context = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "left_fields": ["customer_id", "contract_no"],
                "right_fields": ["id"],
            })
        ],
    })

    validate_business_context(
        context,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
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
        probe_observations=({
            "observation_id": "probe:customer_id_values",
            "kind": "distinct_values",
            "status": "verified",
            "complete": True,
            "datasource_id": 11,
            "field": "customer_id",
            "facts": {
                "values": [
                    {"value": "123456789012345678", "count": 4},
                    {"value": "987654321098765432", "count": 2},
                ]
            },
        },),
    )
    prompt_snapshot = result.prompt_snapshot()
    assert "preparation_envelope" not in prompt_snapshot
    assert "intent_context" not in str(prompt_snapshot)
    assert "datasource_context" not in str(prompt_snapshot)
    assert prompt_snapshot["analysis_semantic_context"]["business_questions"][0][
        "question_id"
    ] == "question_1"
    assert prompt_snapshot["analysis_semantic_context"]["analytical_facets"] == [
        "规模",
        "结构",
        "业务含义",
    ]
    assert "question_coverage" not in prompt_snapshot["analysis_semantic_context"]
    assert prompt_snapshot["analysis_semantic_context"]["insight_lineage"][0][
        "relationship_ids"
    ] == ["contract_customer"]
    runtime_gap = prompt_snapshot["analysis_semantic_context"]["insight_lineage"][0][
        "unresolved_business_facts"
    ][0]
    assert runtime_gap == {
        "fact_id": "question_1_fact_3",
        "unsupported_field_bindings": [{
            "datasource_id": 11,
            "fields": ["customer_id"],
            "business_reason": "客户标识尚不能可靠补充客户归属",
        }],
        "business_impact": "当前数据无法可靠补充客户归属，因此不按客户分类下结论。",
    }
    runtime_bindings = prompt_snapshot["analysis_semantic_context"]["insight_lineage"][0][
        "fact_bindings"
    ]
    assert not any(
        binding["fact_id"] == "question_1_fact_3"
        and binding["datasource_id"] == 11
        for binding in runtime_bindings
    )
    semantic_text = json.dumps(
        prompt_snapshot["analysis_semantic_context"],
        ensure_ascii=False,
    )
    assert "probe_facts" not in semantic_text
    assert "observation_id" not in semantic_text
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
    assert schemas[11]["properties"]["customer_id"]["observed_value_profile"] == {
        "availability": "non_empty",
        "readability": "opaque_identifier",
        "observed_distinct_value_count": 2,
    }
    assert "123456789012345678" not in json.dumps(prompt_snapshot)
    assert all(
        "declared_relationships" not in item
        for item in prompt_snapshot["selected_datasource_snapshot"]
    )


def test_prompt_snapshot_removes_datasource_used_only_by_unsupported_binding():
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"].append(13)
    snapshot["selected_datasource_snapshot"].append({
        "datasource_id": 13,
        "datasource_name": "precomputed rate",
        "datasource_type": "table",
        "datasource_identifier": "dw.precomputed_rate",
        "metadata_schema": {
            "fields": [
                {"name": "rate", "type": "number"},
                {"name": "department", "type": "string"},
            ]
        },
    })
    selection = _selection().model_copy(update={
        "selected_datasource_ids": [11, 12, 13],
    })
    lineage = _business_context().insight_lineage[0]
    unsupported = lineage.unresolved_business_facts[0].model_copy(update={
        "unsupported_field_bindings": [
            *lineage.unresolved_business_facts[0].unsupported_field_bindings,
            UnsupportedBusinessFieldBindingContract(
                datasource_id=13,
                fields=["rate"],
                business_reason="precomputed rate uses an incompatible time scope",
            ),
        ],
    })
    context = _business_context().model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "datasource_ids": [11, 12, 13],
            "fact_bindings": [
                *lineage.fact_bindings,
                BusinessFactBindingContract(
                    fact_id="question_1_fact_3",
                    datasource_id=13,
                    fields=["rate"],
                ),
            ],
            "unresolved_business_facts": [unsupported],
        })],
    })

    prompt_snapshot = PreparationResult(
        intent=_intent(),
        selection=selection,
        business_context=context,
        selected_snapshot=snapshot,
        query_capabilities=("sql",),
    ).prompt_snapshot()

    assert prompt_snapshot["selected_datasource_ids"] == [11, 12]
    assert {
        item["datasource_id"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    } == {11, 12}
    assert 13 not in prompt_snapshot["analysis_semantic_context"][
        "insight_lineage"
    ][0]["datasource_ids"]


def test_prompt_snapshot_keeps_supported_fields_from_partially_unsupported_source():
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"].append(13)
    snapshot["selected_datasource_snapshot"].append({
        "datasource_id": 13,
        "datasource_name": "contract operations",
        "datasource_type": "table",
        "datasource_identifier": "dw.contract_operations",
        "metadata_schema": {
            "fields": [
                {"name": "status", "type": "string"},
                {"name": "customer", "type": "string"},
            ]
        },
    })
    selection = _selection().model_copy(update={
        "selected_datasource_ids": [11, 12, 13],
    })
    lineage = _business_context().insight_lineage[0]
    unsupported = lineage.unresolved_business_facts[0].model_copy(update={
        "unsupported_field_bindings": [
            UnsupportedBusinessFieldBindingContract(
                datasource_id=13,
                fields=["status"],
                business_reason="The status enum cannot answer one narrower sub-question.",
            ),
        ],
    })
    context = _business_context().model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "datasource_ids": [11, 12, 13],
            "fact_bindings": [
                *lineage.fact_bindings,
                BusinessFactBindingContract(
                    fact_id="question_1_fact_3",
                    datasource_id=13,
                    fields=["status", "customer"],
                ),
            ],
            "unresolved_business_facts": [unsupported],
        })],
    })

    prompt_snapshot = PreparationResult(
        intent=_intent(),
        selection=selection,
        business_context=context,
        selected_snapshot=snapshot,
        query_capabilities=("sql",),
    ).prompt_snapshot()

    assert prompt_snapshot["selected_datasource_ids"] == [11, 12, 13]
    binding = next(
        item
        for item in prompt_snapshot["analysis_semantic_context"]["insight_lineage"][0]["fact_bindings"]
        if item["datasource_id"] == 13
    )
    assert binding["fields"] == ["customer"]
    assert {
        item["datasource_id"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    } == {11, 12, 13}
    assert 13 in prompt_snapshot["analysis_semantic_context"][
        "insight_lineage"
    ][0]["datasource_ids"]


def test_readable_probe_enumerations_are_folded_into_field_schema():
    snapshot = {
        "selected_datasource_snapshot": [{
            "datasource_id": 11,
            "metadata_schema": {
                "properties": {"status": {"type": "string"}},
            },
        }],
    }
    observations = [{
        "kind": "distinct_values",
        "status": "verified",
        "complete": True,
        "datasource_id": 11,
        "field": "status",
        "facts": {
            "values": [
                {"value": "执行中", "count": 10},
                {"value": "已完成", "count": 8},
            ],
            "requested_value_resolutions": [{
                "requested_value": "进行中",
                "status": "similar",
                "observed_value": "执行中",
                "similarity": 0.8,
            }],
        },
    }]

    prep._apply_probe_field_profiles(snapshot, observations)

    profile = snapshot["selected_datasource_snapshot"][0]["metadata_schema"]["properties"]["status"]["observed_value_profile"]
    assert profile == {
        "availability": "non_empty",
        "readability": "categorical_values",
        "observed_distinct_value_count": 2,
        "observed_values": ["执行中", "已完成"],
        "requested_value_resolutions": [{
            "requested_value": "进行中",
            "status": "similar",
            "observed_value": "执行中",
        }],
    }


def test_schema_relationship_aggregation_safety_is_orientation_aware():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "probe_facts": {
            "cardinality": "one_to_many",
            "left_measure_aggregation_safe": False,
            "right_measure_aggregation_safe": True,
        }
    })
    enriched = prep.enrich_selected_datasource_schemas(
        _snapshot(),
        selection=_selection(),
        business_context=context.model_copy(update={"relationships": [relationship]}),
    )
    relations = {
        item["datasource_id"]: item["metadata_schema"]["relationships"][0]
        for item in enriched["selected_datasource_snapshot"]
    }

    left = relations[11]
    right = relations[12]
    assert left["cardinality"] == "one_to_many"
    assert left["aggregation_safety"]["local_measure_after_join"] == {
        "status": "unsafe_repeated_by_join",
        "strategy": "do_not_attribute_local_measure_to_the_other_side_dimensions_without_an_explicit_allocation_or_functional_dependency",
    }
    assert left["aggregation_safety"]["key_domain_coverage"]["numerator"] == (
        "distinct_local_keys_intersect_distinct_target_keys"
    )
    assert right["cardinality"] == "many_to_one"
    assert right["aggregation_safety"]["local_measure_after_join"] == {
        "status": "safe",
        "strategy": "may_aggregate_after_join",
    }


def test_prompt_snapshot_upgrades_resumed_probe_relationship_semantics():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "joinability": "alignable",
        "evidence_basis": "probe",
        "probe_facts": {
            "join_supported": True,
            "cardinality": "one_to_many",
            "left_measure_aggregation_safe": False,
            "right_measure_aggregation_safe": True,
        },
    })
    enriched = prep.enrich_selected_datasource_schemas(
        _snapshot(),
        selection=_selection(),
        business_context=context.model_copy(update={"relationships": [relationship]}),
    )
    result = PreparationResult(
        intent=_intent(),
        selection=_selection(),
        business_context=context.model_copy(update={"relationships": [relationship]}),
        selected_snapshot=enriched,
        query_capabilities=("sql",),
    )

    prompt_snapshot = result.prompt_snapshot()
    assert prompt_snapshot["analysis_semantic_context"]["relationships"][0][
        "joinability"
    ] == "joinable"
    schema_relationships = {
        item["datasource_id"]: item["metadata_schema"]["relationships"][0]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    }
    assert schema_relationships[11]["joinability"] == "joinable"
    assert schema_relationships[12]["joinability"] == "joinable"


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
        prep.DatasourceSelectionContract: _canonical_flow_selection(),
        prep.BusinessContextContract: _canonical_flow_context(),
        prep.BusinessContextDraftContract: prep.BusinessContextDraftContract.model_validate(
            _canonical_flow_context().model_dump(exclude={"semantic_resolutions"})
        ),
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
    assert prompt_snapshot["analysis_semantic_context"]["relationships"] == []


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


def test_business_context_relationship_probe_promotes_verified_candidate(monkeypatch):
    observation = {
        "observation_id": "probe:contract_customer",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": {
            "join_supported": True,
            "left_match_coverage_ratio": 1.0,
            "right_match_coverage_ratio": 1.0,
            "cardinality": "many_to_one",
            "left_measure_aggregation_safe": True,
            "right_measure_aggregation_safe": False,
        },
    }
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: [observation],
    )
    workflow = prep.AnalysisPreparationWorkflow(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
        runtime_config=None,
        execution_context={},
        cancelled=None,
        stage_callback=None,
    )

    resolved, observations = workflow._probe_relationship_candidates(
        candidate=_business_context(),
        schemas=compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
    )

    relationship = resolved.relationships[0]
    assert relationship.joinability == "joinable"
    assert relationship.evidence_basis == "probe"
    assert relationship.probe_facts["cardinality"] == "many_to_one"
    assert observations == [observation]


def test_verified_relationship_removes_redundant_key_value_probe():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "joinability": "joinable",
        "evidence_basis": "probe",
        "observation_id": "probe:contract_customer",
    })
    candidate = context.model_copy(update={
        "relationships": [relationship],
        "probe_requests": [
            BusinessContextProbeRequest(
                request_id="customer_id_values",
                datasource_id=11,
                field="customer_id",
                probe_kind="distinct_values",
                purpose="confirm customer labels",
            ),
            BusinessContextProbeRequest(
                request_id="status_values",
                datasource_id=11,
                field="status",
                probe_kind="distinct_values",
                purpose="confirm status values",
            ),
        ],
    })

    normalized = (
        prep.AnalysisPreparationWorkflow._remove_relationship_resolved_schema_probes(
            candidate
        )
    )

    assert [item.request_id for item in normalized.probe_requests] == [
        "status_values"
    ]


def test_relationship_probe_uses_only_field_pairs_present_on_both_schemas():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "left_fields": ["customer_id", "contract_no"],
        "right_fields": ["id", "missing_contract_no"],
        "joinability": "unknown",
        "evidence_basis": "unresolved",
    })
    candidate = context.model_copy(update={"relationships": [relationship]})

    normalized = prep.AnalysisPreparationWorkflow._normalize_unverified_schema_relationships(
        candidate,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
    )

    assert normalized.relationships[0].left_fields == ["customer_id"]
    assert normalized.relationships[0].right_fields == ["id"]
    assert normalized.relationships[0].joinability == "unknown"
    assert any("Schema 不完整" in item for item in normalized.unresolved_relationships)


def test_one_to_many_probe_is_joinable_with_separate_aggregation_safety(monkeypatch):
    observation = {
        "observation_id": "probe:contract_customer",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": {
            "join_supported": True,
            "cardinality": "one_to_many",
            "left_measure_aggregation_safe": False,
            "right_measure_aggregation_safe": True,
        },
    }
    candidate = _business_context()
    normalized = prep.AnalysisPreparationWorkflow._normalize_probe_backed_relationships(
        candidate,
        [observation],
    )

    assert normalized.relationships[0].joinability == "joinable"
    assert not normalized.relationships[0].probe_facts[
        "left_measure_aggregation_safe"
    ]


def test_business_context_finalization_cannot_rebind_or_invent_probe_relationship():
    draft = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "evidence_basis": "probe",
                "observation_id": "probe:contract_customer",
            })
        ],
    })
    invented = draft.relationships[0].model_copy(update={
        "relationship_id": "invented_relation",
        "observation_id": "probe:contract_customer",
    })
    final_lineage = draft.insight_lineage[0].model_copy(update={
        "relationship_ids": ["contract_customer", "invented_relation"],
    })

    reconciled = prep.AnalysisPreparationWorkflow._reconcile_finalized_relationships(
        draft.model_copy(update={
            "relationships": [
                draft.relationships[0].model_copy(update={
                    "observation_id": "probe:another_relation",
                }),
                invented,
            ],
            "insight_lineage": [final_lineage],
        }),
        draft,
    )

    assert [item.relationship_id for item in reconciled.relationships] == [
        "contract_customer"
    ]
    assert reconciled.relationships[0].observation_id == "probe:contract_customer"
    assert reconciled.insight_lineage[0].relationship_ids == ["contract_customer"]


def test_business_context_finalizes_lineage_after_bounded_probe(monkeypatch):
    draft = _canonical_flow_context().model_copy(update={
        "relationships": [
            _canonical_flow_context().relationships[0].model_copy(update={
                "joinability": "unknown",
                "evidence_basis": "unresolved",
                "observation_id": "",
                "probe_facts": {},
            })
        ],
        "probe_requests": [BusinessContextProbeRequest(
            request_id="contract_status_values",
            datasource_id=11,
            field="status",
            probe_kind="distinct_values",
            purpose="确认状态字段的业务枚举",
            related_fact_ids=["question_1_fact_1"],
        )],
    })
    finalized = _canonical_flow_context().model_copy(update={
        "relationships": [
            _canonical_flow_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "probe",
                "observation_id": "probe:contract_customer",
            })
        ],
        "insight_lineage": [
            _canonical_flow_context().insight_lineage[0].model_copy(update={
                "lineage_status": "direct",
                "unresolved_business_facts": [],
            })
        ],
        "unresolved_relationships": [],
        "probe_requests": [],
    })
    outputs = iter([_intent(), _canonical_flow_selection(), draft, finalized])
    payloads = []

    def fake_invoke_contract(**kwargs):
        payloads.append(kwargs.get("payload") or {})
        return next(outputs)

    relationship_observation = {
        "observation_id": "probe:contract_customer",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": {"join_supported": True, "cardinality": "many_to_one"},
    }
    schema_observation = {
        "observation_id": "probe:contract_status_values",
        "kind": "distinct_values",
        "status": "verified",
        "complete": True,
        "datasource_id": 11,
        "field": "status",
        "facts": {"values": [{"value": "执行中", "count": 8}]},
    }
    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: [relationship_observation],
    )
    monkeypatch.setattr(
        "agent.tools.execute_preparation_schema_probe",
        lambda **kwargs: [schema_observation],
    )

    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )

    assert payloads[-1]["resolution_phase"] == "finalize_after_probe"
    assert len(payloads[-1]["probe_observations"]) == 2
    assert "selected_datasources" not in payloads[-1]
    assert "query_guidance" not in payloads[-1]
    assert payloads[-1]["business_context_draft"]
    assert result.business_context.probe_requests == []
    assert result.business_context.relationships[0].joinability == "joinable"
    assert result.business_context.insight_lineage[0].lineage_status == "direct"


def test_business_context_normalizes_status_and_ignores_top_level_annotations():
    payload = _business_context().model_dump()
    payload["insight_lineage"][0]["lineage_status"] = "confirmed"
    payload["probe_resolution_notes"] = ["requested values were resolved"]

    result = BusinessContextContract.model_validate(payload)

    assert result.insight_lineage[0].lineage_status == "direct"
    assert not hasattr(result, "probe_resolution_notes")


def test_local_file_time_coverage_schema_probe_has_its_own_pandas_runtime(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(
        runtime_tools,
        "load_local_file",
        lambda _path: pd.DataFrame({
            "installed_at": ["2024-01-02", "2025-03-04", None],
        }),
    )

    observation = runtime_tools._inspect_preparation_schema_fact(
        {
            "request_id": "device_installation_coverage",
            "datasource_id": 12,
            "field": "installed_at",
            "probe_kind": "time_coverage",
        },
        {
            "datasource_id": 12,
            "datasource_type": "local_file",
            "datasource_identifier": "devices.xlsx",
        },
    )

    assert observation["status"] == "verified"
    assert observation["facts"]["total_count"] == 3
    assert observation["facts"]["non_null_count"] == 2
    assert observation["facts"]["min_value"].startswith("2024-01-02")
    assert observation["facts"]["max_value"].startswith("2025-03-04")


def test_business_context_semantic_rejection_preserves_advisory_lineage(monkeypatch):
    invalid_schema_claim = _canonical_flow_context().model_copy(update={
        "relationships": [
            _canonical_flow_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "schema",
            })
        ],
    })
    def fake_invoke_contract(*, output_model, **kwargs):
        if output_model is prep.IntentContract:
            return _intent()
        if output_model is prep.DatasourceSelectionContract:
            return _canonical_flow_selection()
        return invalid_schema_claim

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)

    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )

    assert len(result.business_context.relationships) == 1
    relationship = result.business_context.relationships[0]
    assert relationship.joinability == "unknown"
    assert relationship.evidence_basis == "unresolved"
    assert result.business_context.insight_lineage[0].lineage_status == "partial"
    assert result.business_context.insight_lineage[0].unresolved_business_facts
    assert result.probe_observations


def test_single_datasource_skips_selection_but_builds_business_lineage(monkeypatch):
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"] = [11]
    snapshot["selected_datasource_snapshot"] = snapshot["selected_datasource_snapshot"][:1]
    single_lineage = _business_context().model_copy(update={
        "relationships": [],
        "insight_lineage": [
                _business_context().insight_lineage[0].model_copy(update={
                    "lineage_status": "partial",
                    "datasource_ids": [11],
                    "fact_bindings": [
                        _business_context().insight_lineage[0].fact_bindings[0],
                        _business_context().insight_lineage[0].fact_bindings[1],
                    ],
                    "relationship_ids": [],
                    "unresolved_business_facts": [
                        UnresolvedBusinessFactContract(
                            fact_id="question_1_fact_3",
                            reason="唯一数据源不包含客户名称",
                        )
                    ],
                })
        ],
        "unresolved_relationships": [],
    })
    outputs = {
        prep.IntentContract: _intent(),
        prep.BusinessContextContract: single_lineage,
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
        datasource_snapshot=snapshot,
        conversation_context={},
    )

    assert stages == ["intent_analysis", "business_context"]
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
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            datasource_ids=[11, 12, 13],
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="direct",
                datasource_ids=[11, 12, 13],
                coverage_reason="共同覆盖",
            )],
            coverage_reason="共同覆盖",
        )],
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


def test_unresolved_fact_drops_only_its_redundant_empty_binding():
    lineage = InsightLineageContract.model_validate({
        "question_id": "q1",
        "business_question": "合同续约风险",
        "lineage_status": "partial",
        "datasource_ids": [11],
        "fact_bindings": [
            {"fact_id": "contract_count", "datasource_id": 11, "fields": ["contract_no"]},
            {"fact_id": "renewal_result", "datasource_id": 0, "fields": []},
        ],
        "unresolved_business_facts": [{
            "fact_id": "renewal_result",
            "reason": "当前 Schema 没有续约结果字段",
        }],
        "synthesis_path": "先说明规模，再披露续约结果缺口",
    })

    assert [item.fact_id for item in lineage.fact_bindings] == ["contract_count"]
    assert [item.fact_id for item in lineage.unresolved_business_facts] == ["renewal_result"]


def test_intent_ignores_echoed_input_context_annotations():
    payload = _intent().model_dump()
    payload["analysis_as_of_date"] = "2026-08-02"
    payload["expected_time_range"] = {}

    intent = IntentContract.model_validate(payload)

    assert intent.business_goal == _intent().business_goal
    assert not hasattr(intent, "analysis_as_of_date")


def test_unknown_relationship_type_remains_unresolved_without_guessing():
    relationship = BusinessRelationshipContract.model_validate({
        "relationship_id": "unknown_edge",
        "left_datasource_id": 11,
        "right_datasource_id": 12,
        "relationship_type": "unknown",
        "joinability": "unknown",
        "left_fields": [],
        "right_fields": [],
        "business_meaning": "Schema 暂不能确认两个对象的业务关系",
    })

    assert relationship.relationship_type == "unknown"
    with pytest.raises(ValueError, match="must keep joinability unknown"):
        BusinessRelationshipContract.model_validate(
            {**relationship.model_dump(), "joinability": "joinable"}
        )


def test_empty_fact_binding_without_matching_unresolved_fact_still_fails():
    with pytest.raises(ValueError, match="fact binding requires fields"):
        InsightLineageContract.model_validate({
            "question_id": "q1",
            "business_question": "合同规模",
            "datasource_ids": [11],
            "fact_bindings": [
                {"fact_id": "contract_count", "datasource_id": 0, "fields": []},
            ],
            "synthesis_path": "统计合同规模",
        })
