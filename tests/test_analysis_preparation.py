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
    ScopeClarificationResult,
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
            subject_scope="question_defined_scope",
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
            subject_scope="question_defined_scope",
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
        "subject_scope",
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
            subject_scope="question_defined_scope",
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


def test_source_free_intent_preserves_current_datasource_set_scope():
    intent = IntentContract(
        business_goal="查看当前数据源趋势",
        business_subjects=["当前数据源"],
        analytical_facets=["趋势"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="当前数据源近十分钟的历史趋势如何",
            subject_scope="all_conversation_datasources",
            required_business_facts=[BusinessFactRequirementContract(
                fact_id="question_1_fact_1",
                business_fact="当前数据源近十分钟的历史趋势",
                semantic_definition="当前会话中各个匹配数据源的独立时间序列趋势",
            )],
        )],
    )

    canonical = prep._canonicalize_intent_selection_facts(intent)

    assert canonical.business_questions[0].subject_scope == "all_conversation_datasources"


@pytest.mark.parametrize(
    "user_query",
    [
        "年费合同情况如何",
        "分析所有合同的状态构成",
        "对比产品生产成本与产品月度利润两个数据源",
        "分析主要数据源",
        "展示近10分钟历史趋势",
    ],
)
def test_full_scope_requires_explicit_conversation_datasource_evidence(user_query):
    intent = _intent().model_copy(update={
        "business_questions": [
            _intent().business_questions[0].model_copy(update={
                "subject_scope": "all_conversation_datasources",
            })
        ],
    })

    grounded = prep._ground_intent_subject_scope(
        intent,
        user_query=user_query,
        conversation_context={},
    )

    assert grounded.business_questions[0].subject_scope == "question_defined_scope"


@pytest.mark.parametrize(
    "user_query",
    [
        "分析下数据源",
        "分析当前已选数据源",
        "当前选中的数据源分别有什么异常",
        "对比各数据源的核心指标",
        "把这批数据源逐个分析",
    ],
)
def test_explicit_conversation_datasource_scope_remains_full(user_query):
    intent = _intent()

    grounded = prep._ground_intent_subject_scope(
        intent,
        user_query=user_query,
        conversation_context={},
    )

    assert grounded.business_questions[0].subject_scope == "all_conversation_datasources"


def test_elliptical_full_scope_is_grounded_by_selected_datasource_context():
    intent = _intent().model_copy(update={
        "business_questions": [
            _intent().business_questions[0].model_copy(update={
                "subject_scope": "all_conversation_datasources",
            })
        ],
    })

    grounded = prep._ground_intent_subject_scope(
        intent,
        user_query="分别看一下",
        conversation_context={
            "recent_messages": [
                {"role": "user", "content": "我已经选择了两个时序数据源"},
            ],
        },
    )

    assert grounded.business_questions[0].subject_scope == "all_conversation_datasources"


def test_subject_scope_is_required_in_intent_contract():
    with pytest.raises(ValueError):
        BusinessQuestionContract.model_validate({
            "question_id": "question_1",
            "business_question": "分析下数据源",
            "required_business_facts": [{
                "fact_id": "question_1_fact_1",
                "business_fact": "分析下数据源",
                "semantic_definition": "当前问题要求理解的业务事实",
            }],
        })


def test_full_scope_capacity_keeps_every_small_bound_datasource(monkeypatch):
    workflow = prep.AnalysisPreparationWorkflow(
        user_query="分析下这些数据源",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
        runtime_config=None,
        execution_context={},
        cancelled=None,
        stage_callback=None,
    )
    intent = _intent().model_copy(update={
        "business_questions": [
            _intent().business_questions[0].model_copy(update={
                "subject_scope": "all_conversation_datasources",
            })
        ],
    })
    monkeypatch.setattr(
        prep.Config,
        "FULL_SCOPE_DIRECT_ANALYSIS_MAX_DATASOURCES",
        5,
    )

    state = workflow.scope_capacity(
        {"intent": intent, "catalog": prep.build_datasource_catalog(_snapshot())},
        provider="local",
    )

    assert state["scope_route"] == "full_scope"
    assert state["selection"].selected_datasource_ids == [11, 12]
    assert state["selected_snapshot"]["selected_datasource_ids"] == [11, 12]


def test_full_scope_capacity_requests_narrowing_above_limit(monkeypatch):
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"] = list(range(1, 7))
    snapshot["selected_datasource_snapshot"] = [
        {
            "datasource_id": datasource_id,
            "datasource_name": f"source-{datasource_id}",
            "datasource_type": "table",
            "datasource_identifier": f"dw.source_{datasource_id}",
            "metadata_schema": {"fields": [{"name": "value", "type": "number"}]},
        }
        for datasource_id in range(1, 7)
    ]
    workflow = prep.AnalysisPreparationWorkflow(
        user_query="分析下数据源",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
        runtime_config=None,
        execution_context={},
        cancelled=None,
        stage_callback=None,
    )
    question = _intent().business_questions[0].model_copy(update={
        "subject_scope": "all_conversation_datasources",
    })
    intent = _intent().model_copy(update={"business_questions": [question]})
    monkeypatch.setattr(
        prep.Config,
        "FULL_SCOPE_DIRECT_ANALYSIS_MAX_DATASOURCES",
        5,
    )

    state = workflow.scope_capacity(
        {"intent": intent, "catalog": prep.build_datasource_catalog(snapshot)},
        provider="local",
    )

    assert state["scope_route"] == "clarification"
    clarification = state["scope_clarification"]
    assert isinstance(clarification, ScopeClarificationResult)
    assert clarification.datasource_count == 6
    assert "6 个数据源" in clarification.assistant_message()


def test_full_scope_without_bound_datasource_stops_before_semantic_retrieval():
    workflow = prep.AnalysisPreparationWorkflow(
        user_query="分析全部数据源",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot={
            "selected_datasource_ids": [],
            "selected_datasource_snapshot": [],
        },
        conversation_context={},
        runtime_config=None,
        execution_context={},
        cancelled=None,
        stage_callback=None,
    )
    question = _intent().business_questions[0].model_copy(update={
        "subject_scope": "all_conversation_datasources",
    })
    intent = _intent().model_copy(update={"business_questions": [question]})

    state = workflow.scope_capacity(
        {"intent": intent, "catalog": []},
        provider="semantic_center",
    )

    assert state["scope_route"] == "clarification"
    assert (
        state["scope_clarification"].reason_code
        == "full_scope_without_bound_datasource"
    )


def test_fact_recall_keeps_dimension_source_omitted_by_llm_shortlist():
    intent = IntentContract(
        business_goal="分析收入结构",
        business_subjects=["收入", "产品"],
        business_questions=[BusinessQuestionContract(
            question_id="question_1",
            business_question="各产品线收入贡献如何",
            subject_scope="question_defined_scope",
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


def test_selection_keeps_definition_sources_out_of_physical_question_scope():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="question_1_fact_1",
                coverage_status="direct",
                datasource_ids=[11],
                supporting_datasource_ids=[12],
                semantic_reference_datasource_ids=[13, 13, 11],
                coverage_reason="事实、标签和指标定义共同支持问题",
            )],
            coverage_reason="事实范围完整，定义来源只提供口径",
        )],
        selection_reason="保留事实、标签和定义",
    )

    fact = selection.question_coverage[0].fact_coverage[0]
    assert fact.semantic_reference_datasource_ids == [13]
    assert selection.question_coverage[0].datasource_ids == [11, 12]
    assert selection.selected_datasource_ids == [11, 12, 13]


def test_prompt_snapshot_keeps_semantic_reference_without_er_binding():
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"].append(13)
    snapshot["selected_datasource_snapshot"].append({
        "datasource_id": 13,
        "datasource_name": "合同指标口径",
        "description": "提供合同状态与金额指标定义，不承载合同明细事实",
        "datasource_type": "table",
        "datasource_identifier": "dw.contract_metric_rules",
        "metadata_schema": {
            "fields": [{"name": "metric_definition", "type": "string"}],
        },
    })
    selection_payload = _selection().model_dump()
    selection_payload["question_coverage"][0]["fact_coverage"][0][
        "semantic_reference_datasource_ids"
    ] = [13]
    selection = DatasourceSelectionContract.model_validate(selection_payload)

    prompt_snapshot = PreparationResult(
        intent=_intent(),
        selection=selection,
        business_context=_business_context(),
        selected_snapshot=snapshot,
        query_capabilities=("sql",),
    ).prompt_snapshot()

    assert prompt_snapshot["selected_datasource_ids"] == [11, 12]
    assert {
        item["datasource_id"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    } == {11, 12}
    assert prompt_snapshot["analysis_semantic_context"]["semantic_references"] == [{
        "datasource_id": 13,
        "datasource_name": "合同指标口径",
        "description": "提供合同状态与金额指标定义，不承载合同明细事实",
        "metadata_schema": compact_datasource_snapshot(snapshot)[
            "selected_datasource_snapshot"
        ][-1]["metadata_schema"],
    }]
    assert all(
        relationship["left_datasource_id"] != 13
        and relationship["right_datasource_id"] != 13
        for relationship in prompt_snapshot["analysis_semantic_context"]["relationships"]
    )


@pytest.mark.parametrize("schema_shape", ["properties", "fields"])
def test_semantic_reference_definitions_reach_model_and_survive_resume(monkeypatch, schema_shape):
    import copy
    import agent
    import agent.context_engineering_runtime as runtime_context

    definition = {
        "type": "number",
        "description": "Net rate uses total net amount divided by total base amount.",
        "formula": "sum(net_amount) / sum(base_amount)",
        "unit": "ratio",
        "applicable_objects": ["settled_transaction"],
    }
    schema = {"description": "Business definitions", "rules": [{
        "name": "period", "field": "settled_at", "boundary": "[start,end)",
    }]}
    if schema_shape == "properties":
        schema["properties"] = {"net_rate": definition}
    else:
        schema["fields"] = [{"name": "net_rate", **definition}]
    snapshot = _snapshot()
    snapshot["selected_datasource_ids"].append(13)
    snapshot["selected_datasource_snapshot"].append({
        "datasource_id": 13, "datasource_name": "Business definitions",
        "datasource_type": "table", "datasource_identifier": "rules.definitions",
        "metadata_schema": schema,
    })
    original = copy.deepcopy(snapshot)
    selection_payload = _selection().model_dump()
    selection_payload["question_coverage"][0]["fact_coverage"][0][
        "semantic_reference_datasource_ids"
    ] = [13]
    prepared = PreparationResult(
        intent=_intent(),
        selection=DatasourceSelectionContract.model_validate(selection_payload),
        business_context=_business_context(), selected_snapshot=snapshot,
        query_capabilities=("sql",),
    )
    restored = PreparationResult.from_resume_payload(prepared.resume_payload())
    assert restored is not None
    prompt_snapshot = restored.prompt_snapshot()
    reference = prompt_snapshot["analysis_semantic_context"]["semantic_references"][0]
    for key, value in schema.items():
        assert reference["metadata_schema"][key] == value
    assert prompt_snapshot["selected_datasource_ids"] == [11, 12]
    assert 13 not in {item["datasource_id"] for item in prompt_snapshot["selected_datasource_snapshot"]}

    monkeypatch.setattr(runtime_context, "_load_conversation", lambda _: None)
    monkeypatch.setattr(agent, "_build_conversation_prompt_message", lambda *_: None)
    monkeypatch.setattr(agent, "get_memory_messages", lambda *_, **__: [])
    monkeypatch.setattr(agent, "get_history_messages", lambda *_, **__: [])
    messages = agent.build_prompt_messages(
        "Analyze the settled business", namespace_id=1, conversation_id=1,
        datasource_snapshot_override=prompt_snapshot, context_mode="fresh_analysis",
    )
    model_text = "\n".join(message.content for message in messages)
    assert definition["description"] in model_text
    assert definition["formula"] in model_text
    assert "settled_at" in model_text
    assert snapshot == original
    reference["metadata_schema"]["description"] = "changed output only"
    assert snapshot == original


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


def test_analytical_recommendation_inherits_its_factual_scope():
    selection = DatasourceSelectionContract(
        question_coverage=[
            BusinessQuestionCoverageContract(
                question_id="root_cause",
                fact_coverage=[BusinessFactCoverageContract(
                    fact_id="root_cause_evidence",
                    coverage_status="direct",
                    datasource_ids=[11, 12],
                    coverage_reason="process and equipment observations",
                )],
                coverage_reason="root-cause evidence is available",
            ),
            BusinessQuestionCoverageContract(
                question_id="evidence_gap",
                fact_coverage=[BusinessFactCoverageContract(
                    fact_id="confirmation_evidence_needed",
                    coverage_status="direct",
                    derived_from_fact_ids=["root_cause_evidence"],
                    coverage_reason="assess confirmation gaps from observed evidence",
                )],
                coverage_reason="the recommendation is derived from observed evidence",
            ),
        ],
        selection_reason="facts and their analytical synthesis",
    )

    recommendation = selection.question_coverage[1].fact_coverage[0]
    assert recommendation.coverage_status == "direct"
    assert recommendation.datasource_ids == [11, 12]
    assert selection.selected_datasource_ids == [11, 12]


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


def test_analysis_work_is_not_preserved_as_an_unresolved_business_fact():
    selection = DatasourceSelectionContract(
        question_coverage=[BusinessQuestionCoverageContract(
            question_id="question_1",
            fact_coverage=[BusinessFactCoverageContract(
                fact_id="root_cause_priority",
                coverage_status="direct",
                datasource_ids=[11],
                coverage_reason="event facts are available for analysis",
            )],
            coverage_reason="the selected event source covers the question",
        )],
        selection_reason="selected event source",
    )
    candidate = BusinessContextContract(insight_lineage=[InsightLineageContract(
        question_id="question_1",
        business_question="rank root-cause candidates",
        lineage_status="partial",
        datasource_ids=[11],
        fact_bindings=[BusinessFactBindingContract(
            fact_id="root_cause_priority",
            datasource_id=11,
            fields=["event_type", "event_count"],
        )],
        unresolved_business_facts=[UnresolvedBusinessFactContract(
            fact_id="root_cause_priority",
            reason="ranking requires analysis of the selected event facts",
        )],
        synthesis_path="compare the selected event facts and rank candidates",
    )])

    completed = prep.AnalysisPreparationWorkflow._complete_derived_fact_lineage(
        candidate,
        selection,
    )

    lineage = completed.insight_lineage[0]
    assert lineage.lineage_status == "direct"
    assert lineage.unresolved_business_facts == []


def test_unsupported_candidate_does_not_override_a_grounded_fact_binding():
    candidate = _business_context().model_copy(update={
        "relationships": [
            _business_context().relationships[0].model_copy(update={
                "joinability": "joinable",
                "evidence_basis": "schema",
            })
        ],
    })

    completed = prep.AnalysisPreparationWorkflow._complete_derived_fact_lineage(
        candidate,
        _selection(),
    )

    lineage = completed.insight_lineage[0]
    assert lineage.lineage_status == "direct"
    assert lineage.unresolved_business_facts == []


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


def test_business_context_allows_auxiliary_selected_source_without_fact_binding():
    context = _business_context()
    lineage = context.insight_lineage[0].model_copy(update={
        "lineage_status": "direct",
        "relationship_ids": [],
        "fact_bindings": [
            BusinessFactBindingContract(
                fact_id=f"question_1_fact_{index}",
                datasource_id=11,
                fields=["amount"],
            )
            for index in range(1, 4)
        ],
        "unresolved_business_facts": [],
    })

    validate_business_context(
        context.model_copy(update={"insight_lineage": [lineage]}),
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
        intent=_intent(),
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


def test_business_lineage_field_normalization_preserves_grounded_fields():
    context = _business_context()
    lineage = context.insight_lineage[0]
    candidate = context.model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "fact_bindings": [
                lineage.fact_bindings[0].model_copy(update={
                    "fields": ["amount", "invented_metric_field"],
                }),
                *lineage.fact_bindings[1:],
            ],
        })],
    })

    normalized = prep.AnalysisPreparationWorkflow._normalize_fact_binding_fields(
        candidate,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
    )

    assert normalized.insight_lineage[0].fact_bindings[0].fields == ["amount"]
    validate_business_context(
        normalized,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        selection=_selection(),
        intent=_intent(),
    )


def test_business_lineage_field_normalization_marks_ungrounded_fact_partial():
    context = _business_context()
    lineage = context.insight_lineage[0]
    candidate = context.model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "lineage_status": "direct",
            "fact_bindings": [
                lineage.fact_bindings[0].model_copy(update={
                    "fact_id": "question_1_fact_4",
                    "fields": ["invented_metric_field"],
                }),
            ],
            "unresolved_business_facts": [],
        })],
    })

    normalized = prep.AnalysisPreparationWorkflow._normalize_fact_binding_fields(
        candidate,
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
    )

    result = normalized.insight_lineage[0]
    assert result.fact_bindings == []
    assert result.lineage_status == "partial"
    assert [item.fact_id for item in result.unresolved_business_facts] == [
        "question_1_fact_4"
    ]

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


def test_prompt_snapshot_keeps_selected_datasource_when_lineage_binding_is_unsupported():
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

    assert prompt_snapshot["selected_datasource_ids"] == [11, 12, 13]
    assert {
        item["datasource_id"]
        for item in prompt_snapshot["selected_datasource_snapshot"]
    } == {11, 12, 13}
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
            "left_distinct_key_count": 12050,
            "right_distinct_key_count": 900,
            "matched_distinct_key_count": 875,
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
    assert left["observed_key_cardinality"] == {
        "local_distinct_key_count": 12050,
        "target_distinct_key_count": 900,
        "matched_distinct_key_count": 875,
    }
    assert right["cardinality"] == "many_to_one"
    assert right["observed_key_cardinality"] == {
        "local_distinct_key_count": 900,
        "target_distinct_key_count": 12050,
        "matched_distinct_key_count": 875,
    }
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
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: [],
    )
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
    relationships = prompt_snapshot["analysis_semantic_context"]["relationships"]
    assert len(relationships) == 1
    assert relationships[0]["joinability"] == "unknown"


def test_business_context_validation_failure_preserves_normalized_candidate(monkeypatch):
    outputs = {
        prep.IntentContract: _intent(),
        prep.DatasourceSelectionContract: _canonical_flow_selection(),
        prep.BusinessContextDraftContract: prep.BusinessContextDraftContract.model_validate(
            _canonical_flow_context().model_dump(exclude={"semantic_resolutions"})
        ),
    }
    original_validate = prep.validate_business_context
    validation_calls = 0

    def fake_invoke_contract(*, output_model, **kwargs):
        return outputs[output_model]

    def fail_once(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            raise ValueError("one relationship candidate requires local normalization")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)
    monkeypatch.setattr(prep, "validate_business_context", fail_once)
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: [],
    )

    result = prepare_analysis(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )

    semantic_context = result.prompt_snapshot()["analysis_semantic_context"]
    assert validation_calls == 2
    assert semantic_context["relationships"]
    assert semantic_context["insight_lineage"]
    assert semantic_context["relationships"][0]["joinability"] == "unknown"


def test_semantic_selection_normalizes_to_canonical_preparation_state(monkeypatch):
    from service.semantic_datasource_selection_service import (
        SemanticDatasourceSelectionResult,
    )

    snapshot = _snapshot()
    workflow = prep.AnalysisPreparationWorkflow(
        user_query='分析合同情况',
        context_mode='fresh_analysis',
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
        runtime_config=None,
        execution_context={
            'token': 'Bearer test',
            'namespace_id': 6,
            'database_conn_info': {},
        },
        cancelled=None,
        stage_callback=None,
    )
    semantic_result = SemanticDatasourceSelectionResult(
        datasource_ids=(11, 12),
        datasource_snapshot=snapshot,
        relationship_hints=({
            'source_datasource_id': 11,
            'target_datasource_id': 12,
            'source_fields': ['customer_id'],
            'target_fields': ['id'],
            'description': '合同关联客户',
            'source': 'semantic_center',
            'verification_status': 'provider_asserted',
        },),
    )
    monkeypatch.setattr(
        prep.DatasourceSelectionProviderRouter,
        'resolve',
        lambda snapshot=None: 'semantic_center',
    )
    retrieve_call = {}

    def retrieve(self, **kwargs):
        retrieve_call.update(kwargs)
        return semantic_result

    monkeypatch.setattr(
        prep.SemanticDatasourceSelectionService,
        'retrieve',
        retrieve,
    )
    captured = {}

    def fake_invoke_contract(*, stage, payload, **kwargs):
        captured['stage'] = stage
        captured['payload'] = payload
        return _selection()

    monkeypatch.setattr(prep, '_invoke_contract', fake_invoke_contract)

    result = workflow.semantic_selection({'intent': _intent()})

    assert result['selection'].selected_datasource_ids == [11, 12]
    selected_rows = result['selected_snapshot']['selected_datasource_snapshot']
    relation = selected_rows[0]['metadata_schema']['relationships'][0]
    assert relation['target_datasource_id'] == 12
    assert relation['source_fields'] == ['customer_id']
    assert relation['target_fields'] == ['id']
    assert 'relationship_hints' not in result
    assert captured['stage'] == 'semantic_selection'
    assert 'semantic_candidate_catalog' in captured['payload']
    assert captured['payload']['intent_context'] == _intent().model_dump()
    assert 'user_query' not in captured['payload']
    assert captured['payload']['relationship_hints'] == list(
        semantic_result.relationship_hints
    )
    assert _intent().business_goal in retrieve_call['query']
    assert _intent().business_questions[0].business_question in retrieve_call['query']


def test_semantic_selection_seeds_an_empty_conversation_snapshot(monkeypatch):
    from service.semantic_datasource_selection_service import (
        SemanticDatasourceSelectionResult,
    )

    discovered_snapshot = _snapshot()
    workflow = prep.AnalysisPreparationWorkflow(
        user_query='鍒嗘瀽鍚堝悓鎯呭喌',
        context_mode='fresh_analysis',
        expected_time_range={},
        datasource_snapshot={},
        conversation_context={},
        runtime_config=None,
        execution_context={
            'token': 'Bearer test',
            'namespace_id': 6,
            'database_conn_info': {},
        },
        cancelled=None,
        stage_callback=None,
    )
    monkeypatch.setattr(
        prep.SemanticDatasourceSelectionService,
        'retrieve',
        lambda self, **kwargs: SemanticDatasourceSelectionResult(
            datasource_ids=(11, 12),
            datasource_snapshot=discovered_snapshot,
            relationship_hints=(),
        ),
    )
    monkeypatch.setattr(
        prep,
        '_invoke_contract',
        lambda **kwargs: _selection(),
    )

    result = workflow.semantic_selection({
        'intent': _intent(),
        'catalog': [],
    })

    assert result['selection'].selected_datasource_ids == [11, 12]
    assert result['selected_snapshot']['selected_datasource_ids'] == [11, 12]
    assert result['query_capabilities'] == ('sql',)


def test_semantic_relationship_is_schema_checked_and_skips_relationship_probe(
    monkeypatch,
):
    hints = ({
        "source_datasource_id": 11,
        "target_datasource_id": 12,
        "source_fields": ["customer_id"],
        "target_fields": ["id"],
        "description": "合同通过客户标识关联客户",
        "source": "semantic_center",
        "verification_status": "provider_asserted",
    },)
    enriched = prep.enrich_snapshot_with_semantic_relationships(_snapshot(), hints)
    schemas = compact_datasource_snapshot(enriched)[
        "selected_datasource_snapshot"
    ]
    candidate = prep.AnalysisPreparationWorkflow._normalize_unverified_schema_relationships(
        _business_context(), schemas
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
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: pytest.fail("trusted semantic relationship must not be probed"),
    )

    resolved, observations = workflow._probe_relationship_candidates(
        candidate=candidate,
        schemas=schemas,
        selection=_selection(),
    )

    assert observations == []
    assert resolved.relationships[0].joinability == "joinable"
    assert resolved.relationships[0].evidence_basis == "schema"


def test_semantic_relationship_with_stale_field_is_not_trusted():
    hints = ({
        "source_datasource_id": 11,
        "target_datasource_id": 12,
        "source_fields": ["missing_customer_id"],
        "target_fields": ["id"],
        "source": "semantic_center",
        "verification_status": "provider_asserted",
    },)
    schemas = compact_datasource_snapshot(_snapshot())[
        "selected_datasource_snapshot"
    ]

    assert prep.validate_semantic_relationship_hints(hints, schemas) == ()


def test_business_context_consumes_trusted_semantic_relationship_without_probe(
    monkeypatch,
):
    snapshot = _snapshot()
    selected_snapshot = prep.enrich_snapshot_with_semantic_relationships(
        prep.filter_datasource_snapshot(snapshot, [11, 12]),
        ({
            "source_datasource_id": 11,
            "target_datasource_id": 12,
            "source_fields": ["customer_id"],
            "target_fields": ["id"],
            "description": "合同通过客户标识关联客户",
            "source": "semantic_center",
            "verification_status": "provider_asserted",
        },),
    )
    schemas = compact_datasource_snapshot(selected_snapshot)[
        "selected_datasource_snapshot"
    ]
    workflow = prep.AnalysisPreparationWorkflow(
        user_query="分析合同情况",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
        runtime_config=None,
        execution_context={},
        cancelled=None,
        stage_callback=None,
    )
    monkeypatch.setattr(
        prep,
        "_invoke_contract",
        lambda **kwargs: _business_context(),
    )
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: pytest.fail("trusted semantic relationship must not be probed"),
    )

    result = workflow.business_context({
        "intent": _intent(),
        "selection": _selection(),
        "selected_snapshot": selected_snapshot,
        "selected_schemas": schemas,
        "query_capabilities": ("sql",),
    })

    relationship = result["business_context"].relationships[0]
    assert relationship.joinability == "joinable"
    assert relationship.evidence_basis == "schema"
    enriched_rows = result["selected_snapshot"]["selected_datasource_snapshot"]
    schema_relationships = [
        item
        for row in enriched_rows
        for item in (row.get("metadata_schema") or {}).get("relationships") or []
        if item.get("relationship_id") == relationship.relationship_id
    ]
    assert schema_relationships
    assert all(item["joinability"] == "joinable" for item in schema_relationships)


def test_semantic_provider_reuses_canonical_intent_before_selection(monkeypatch):
    snapshot = _snapshot()
    workflow = prep.AnalysisPreparationWorkflow(
        user_query='分析合同情况',
        context_mode='fresh_analysis',
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
        runtime_config=None,
        execution_context={'token': 'Bearer test', 'namespace_id': 6},
        cancelled=None,
        stage_callback=None,
    )
    intent = _intent()
    selection = _selection()
    calls = []

    monkeypatch.setattr(
        prep.DatasourceSelectionProviderRouter,
        'resolve',
        lambda snapshot=None: 'semantic_center',
    )

    def intent_node(state):
        calls.append('intent_analysis')
        return {'intent': intent}

    def semantic_node(state):
        calls.append('semantic_selection')
        assert state['intent'] is intent
        return {
            'selection': selection,
            'selected_snapshot': snapshot,
            'selected_schemas': prep.compact_datasource_snapshot(snapshot)[
                'selected_datasource_snapshot'
            ],
            'query_capabilities': ('sql',),
        }

    def business_node(state):
        calls.append('business_context')
        return {
            'business_context': workflow._fallback_business_context(selection),
            'selected_snapshot': snapshot,
        }

    monkeypatch.setattr(workflow, 'intent_analysis', intent_node)
    monkeypatch.setattr(workflow, 'semantic_selection', semantic_node)
    monkeypatch.setattr(workflow, 'business_context', business_node)

    result = workflow.run()

    assert calls == ['intent_analysis', 'semantic_selection', 'business_context']
    assert result.selection.selected_datasource_ids == [11, 12]


def test_semantic_selection_failure_runs_complete_local_preparation(monkeypatch):
    snapshot = _snapshot()
    workflow = prep.AnalysisPreparationWorkflow(
        user_query='分析合同情况',
        context_mode='fresh_analysis',
        expected_time_range={},
        datasource_snapshot=snapshot,
        conversation_context={},
        runtime_config=None,
        execution_context={'token': 'Bearer test', 'namespace_id': 6},
        cancelled=None,
        stage_callback=None,
    )
    monkeypatch.setattr(
        prep.DatasourceSelectionProviderRouter,
        'resolve',
        lambda snapshot=None: 'semantic_center',
    )

    def fail_semantic(self, **kwargs):
        raise prep.SemanticDatasourceSelectionError('gateway unavailable')

    monkeypatch.setattr(
        prep.SemanticDatasourceSelectionService,
        'retrieve',
        fail_semantic,
    )
    local_calls = []
    intent = prep._canonicalize_intent_selection_facts(_intent())
    selection = _canonical_flow_selection()

    monkeypatch.setattr(
        workflow,
        'intent_analysis',
        lambda state: local_calls.append('intent_analysis') or {'intent': intent},
    )
    monkeypatch.setattr(
        workflow,
        'select_datasources',
        lambda state: local_calls.append('select_datasources') or {
            'selection': selection,
            'selected_snapshot': snapshot,
            'selected_schemas': prep.compact_datasource_snapshot(snapshot)[
                'selected_datasource_snapshot'
            ],
            'query_capabilities': ('sql',),
        },
    )
    monkeypatch.setattr(
        workflow,
        'business_context',
        lambda state: local_calls.append('business_context') or {
            'business_context': workflow._fallback_business_context(selection),
            'selected_snapshot': snapshot,
        },
    )
    result = workflow.run()

    assert local_calls == ['intent_analysis', 'select_datasources', 'business_context']
    assert result.selection.selected_datasource_ids == [11, 12]


def test_empty_semantic_selection_failure_does_not_run_local_selection(monkeypatch):
    workflow = prep.AnalysisPreparationWorkflow(
        user_query='鍒嗘瀽鍚堝悓鎯呭喌',
        context_mode='fresh_analysis',
        expected_time_range={},
        datasource_snapshot={},
        conversation_context={},
        runtime_config=None,
        execution_context={'token': 'Bearer test', 'namespace_id': 6},
        cancelled=None,
        stage_callback=None,
    )
    monkeypatch.setattr(
        prep.DatasourceSelectionProviderRouter,
        'resolve',
        lambda snapshot=None: 'semantic_center',
    )
    monkeypatch.setattr(
        workflow,
        'intent_analysis',
        lambda state: {'intent': _intent()},
    )
    monkeypatch.setattr(
        prep.SemanticDatasourceSelectionService,
        'retrieve',
        lambda self, **kwargs: (_ for _ in ()).throw(
            prep.SemanticDatasourceSelectionError('gateway unavailable')
        ),
    )
    monkeypatch.setattr(
        workflow,
        'select_datasources',
        lambda state: pytest.fail('empty local selection must not be invoked'),
    )

    with pytest.raises(prep.SemanticDatasourceSelectionError):
        workflow.run()


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


def test_unverified_joinable_relationship_is_downgraded_for_local_probe():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "joinability": "joinable",
        "evidence_basis": "unresolved",
    })

    normalized = prep.AnalysisPreparationWorkflow._normalize_unverified_schema_relationships(
        context.model_copy(update={"relationships": [relationship]}),
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
    )

    assert normalized.relationships[0].joinability == "unknown"
    assert normalized.relationships[0].evidence_basis == "unresolved"


def test_locally_verified_probe_relationship_survives_final_normalization():
    context = _business_context()
    relationship = context.relationships[0].model_copy(update={
        "joinability": "joinable",
        "evidence_basis": "probe",
        "observation_id": "probe:contract_customer",
        "probe_facts": {
            "join_supported": True,
            "cardinality": "many_to_one",
        },
    })

    normalized = prep.AnalysisPreparationWorkflow._normalize_unverified_schema_relationships(
        context.model_copy(update={"relationships": [relationship]}),
        compact_datasource_snapshot(_snapshot())["selected_datasource_snapshot"],
        probe_observations=[{
            "observation_id": "probe:contract_customer",
            "kind": "relationship_coverage",
            "status": "verified",
            "complete": True,
        }],
    )

    assert normalized.relationships[0].joinability == "joinable"
    assert normalized.relationships[0].evidence_basis == "probe"
    assert normalized.relationships[0].observation_id == "probe:contract_customer"


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
    resolution = prep.BusinessContextProbeResolutionContract(
        fact_resolutions=[prep.BusinessFactProbeResolutionContract(
            fact_id="question_1_fact_1",
            resolution_status="unchanged",
        )]
    )
    outputs = iter([_intent(), _canonical_flow_selection(), draft, resolution])
    payloads = []
    stages = []

    def fake_invoke_contract(**kwargs):
        payloads.append(kwargs.get("payload") or {})
        stages.append(kwargs.get("stage"))
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

    assert stages[-1] == "business_context_probe_resolution"
    assert len(payloads[-1]["probe_observations"]) == 1
    assert payloads[-1]["affected_facts"][0]["fact_id"] == "question_1_fact_1"
    assert "selected_datasources" not in payloads[-1]
    assert "query_guidance" not in payloads[-1]
    assert "business_context_draft" not in payloads[-1]
    assert result.business_context.probe_requests == []
    assert result.business_context.relationships[0].joinability == "joinable"
    assert result.business_context.insight_lineage[0].lineage_status == "direct"


def test_business_context_only_refinalizes_consumable_probe_observations():
    workflow = prep.AnalysisPreparationWorkflow

    assert not workflow._has_consumable_probe_observations([])
    assert not workflow._has_consumable_probe_observations([{
        "observation_id": "probe:contract_customer",
        "status": "unavailable",
        "complete": False,
    }])
    assert not workflow._has_consumable_probe_observations([{
        "observation_id": "probe:contract_customer",
        "status": "verified",
        "complete": False,
    }])
    assert not workflow._has_consumable_probe_observations([{
        "observation_id": "probe:contract_customer",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": {"join_supported": True},
    }])
    assert workflow._has_consumable_probe_observations([{
        "observation_id": "probe:status_values",
        "kind": "distinct_values",
        "status": "verified",
        "complete": True,
        "facts": {"values": [{"value": "active", "count": 8}]},
    }])


def test_probe_fact_resolution_merges_supported_observation_locally():
    candidate = _canonical_flow_context()
    lineage = candidate.insight_lineage[0]
    candidate = candidate.model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "unresolved_business_facts": [prep.UnresolvedBusinessFactContract(
                fact_id="question_1_fact_1",
                reason="status values require verification",
            )],
            "lineage_status": "partial",
        })],
    })
    resolution = prep.BusinessContextProbeResolutionContract(
        fact_resolutions=[prep.BusinessFactProbeResolutionContract(
            fact_id="question_1_fact_1",
            resolution_status="supported",
            observation_ids=["probe:status_values"],
        )]
    )

    result = prep.AnalysisPreparationWorkflow._apply_probe_fact_resolutions(
        candidate,
        resolution,
        [{
            "observation_id": "probe:status_values",
            "datasource_id": 11,
            "field": "status",
            "related_fact_ids": ["question_1_fact_1"],
        }],
    )

    assert result.insight_lineage[0].lineage_status == "direct"
    assert result.insight_lineage[0].unresolved_business_facts == []
    assert any(
        binding.datasource_id == 11 and "status" in binding.fields
        for binding in result.insight_lineage[0].fact_bindings
    )


def test_probe_fact_resolution_removes_only_unsupported_observed_field():
    candidate = _canonical_flow_context()
    lineage = candidate.insight_lineage[0]
    candidate = candidate.model_copy(update={
        "insight_lineage": [lineage.model_copy(update={
            "fact_bindings": [prep.BusinessFactBindingContract(
                fact_id="question_1_fact_1",
                datasource_id=11,
                fields=["status", "contract_amount"],
            )],
        })],
    })
    resolution = prep.BusinessContextProbeResolutionContract(
        fact_resolutions=[prep.BusinessFactProbeResolutionContract(
            fact_id="question_1_fact_1",
            resolution_status="unsupported",
            observation_ids=["probe:status_values"],
            reason="observed values do not represent the requested status",
            business_impact="status segmentation remains unavailable",
        )]
    )

    result = prep.AnalysisPreparationWorkflow._apply_probe_fact_resolutions(
        candidate,
        resolution,
        [{
            "observation_id": "probe:status_values",
            "datasource_id": 11,
            "field": "status",
            "related_fact_ids": ["question_1_fact_1"],
        }],
    )

    assert result.insight_lineage[0].lineage_status == "partial"
    assert result.insight_lineage[0].fact_bindings[0].fields == ["contract_amount"]
    unresolved = result.insight_lineage[0].unresolved_business_facts[0]
    assert unresolved.fact_id == "question_1_fact_1"
    assert unresolved.unsupported_field_bindings[0].fields == ["status"]


def test_relation_only_probe_reconciles_lineage_without_second_llm(monkeypatch):
    resolved = _canonical_flow_context()
    draft = resolved.model_copy(update={
        "relationships": [resolved.relationships[0].model_copy(update={
            "joinability": "unknown",
            "evidence_basis": "unresolved",
            "observation_id": "",
            "probe_facts": {},
        })],
        "insight_lineage": [resolved.insight_lineage[0].model_copy(update={
            "lineage_status": "partial",
            "unresolved_business_facts": [UnresolvedBusinessFactContract(
                fact_id="question_1_fact_1",
                reason="the required relationship has not been verified",
            )],
        })],
        "unresolved_relationships": ["contract to customer is unverified"],
    })
    outputs = iter([_intent(), _canonical_flow_selection(), draft])
    calls = []

    def fake_invoke_contract(**kwargs):
        calls.append(kwargs["stage"])
        return next(outputs)

    monkeypatch.setattr(prep, "_invoke_contract", fake_invoke_contract)
    monkeypatch.setattr(
        "agent.tools.execute_preparation_relationship_probe",
        lambda **kwargs: [{
            "observation_id": "probe:contract_customer",
            "kind": "relationship_coverage",
            "status": "verified",
            "complete": True,
            "facts": {
                "join_supported": True,
                "cardinality": "many_to_one",
            },
        }],
    )

    result = prepare_analysis(
        user_query="analyze contracts",
        context_mode="fresh_analysis",
        expected_time_range={},
        datasource_snapshot=_snapshot(),
        conversation_context={},
    )

    assert calls == ["intent_analysis", "select_datasources", "business_context"]
    assert result.business_context.relationships[0].joinability == "joinable"
    assert result.business_context.insight_lineage[0].lineage_status == "direct"
    assert result.business_context.insight_lineage[0].unresolved_business_facts == []


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
