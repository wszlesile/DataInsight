from service.insight_ns_conversation_service import InsightNsConversationService
from service.post_analysis_evidence_service import PostAnalysisEvidenceCompiler


def _formula_operations() -> dict[str, dict]:
    return {
        "group": {
            "operation_node_id": "group",
            "line": 1,
            "semantic_kind": "aggregation",
            "source": (
                'monthly = data.groupby("month").agg('
                'profit=("gross_profit", "sum"), '
                'revenue=("sales_revenue", "sum"))'
            ),
        },
        "profit": {
            "operation_node_id": "profit",
            "line": 2,
            "semantic_kind": "aggregation",
            "source": 'total_profit = monthly["profit"].sum()',
        },
        "sales": {
            "operation_node_id": "sales",
            "line": 3,
            "semantic_kind": "aggregation",
            "source": 'total_sales = monthly["revenue"].sum()',
        },
        "margin": {
            "operation_node_id": "margin",
            "line": 4,
            "semantic_kind": "formula",
            "source": (
                "gross_margin = total_profit / total_sales * 100 "
                "if total_sales else 0.0"
            ),
        },
    }


def test_metric_fields_are_presented_as_an_unordered_field_set() -> None:
    items = InsightNsConversationService._calculation_items(
        {
            "metric_fields": ["销售收入_元", "毛利_元"],
            "aggregation": "",
        },
        [],
    )

    assert {"label": "参与计算字段", "value": "销售收入_元、毛利_元"} in items
    assert all("销售收入_元 / 毛利_元" not in str(item.get("value")) for item in items)


def test_read_data_step_uses_the_same_metric_field_semantics() -> None:
    items = InsightNsConversationService._source_items(
        [{"datasource_name": "产品月度利润_公司产线", "datasource_type": "local_file"}],
        ["销售收入_元", "毛利_元"],
    )

    assert {"label": "参与计算字段", "value": "销售收入_元、毛利_元"} in items


def test_aggregation_description_does_not_render_fields_as_division() -> None:
    description = InsightNsConversationService._describe_calculation_method(
        {
            "metric_fields": ["销售收入_元", "毛利_元"],
            "aggregation": "sum",
        },
        {},
    )

    assert "销售收入_元、毛利_元" in description
    assert "销售收入_元 / 毛利_元" not in description


def test_verified_operation_graph_materializes_a_readable_formula() -> None:
    operations = _formula_operations()

    formula = PostAnalysisEvidenceCompiler._operation_formula(
        list(operations),
        operations,
    )

    assert formula == "(SUM(gross_profit) ÷ SUM(sales_revenue)) × 100"


def test_upstream_formula_is_not_materialized_for_a_ranking_boundary() -> None:
    operations = _formula_operations()
    operations["ranking"] = {
        "operation_node_id": "ranking",
        "line": 5,
        "semantic_kind": "ranking",
        "source": 'peak = monthly.loc[monthly["profit"].idxmax()]',
    }

    formula = PostAnalysisEvidenceCompiler._operation_formula(
        list(operations),
        operations,
        boundary_operation_ids=["ranking"],
    )

    assert formula == ""


def test_unsupported_formula_is_not_exposed_as_python_source() -> None:
    operations = {
        "formula": {
            "operation_node_id": "formula",
            "line": 1,
            "semantic_kind": "formula",
            "source": "score = custom_business_function(raw_value)",
        },
    }

    formula = PostAnalysisEvidenceCompiler._operation_formula(
        ["formula"],
        operations,
        boundary_operation_ids=["formula"],
    )

    assert formula == ""
