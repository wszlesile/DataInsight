import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import (  # noqa: E402
    CURRENT_DATASOURCE_NUMERIC_FIELDS,
    StructuredResult,
    _build_execution_namespace,
    _extract_dataframe_reduction_clauses,
    _inspect_preparation_relationship,
    _inspect_preparation_schema_fact,
    _inspect_sql_preparation_relationship,
    _normalize_chart_items,
    _validate_analysis_relationship_contract,
    _validate_generated_code_contract,
    _validate_structured_result_contract,
    load_data_with_sql,
    validate_query_constraints,
)
from utils.chart_document_utils import build_chart_result, normalize_chart_result_item  # noqa: E402


class ChartContractValidationTestCase(unittest.TestCase):
    @patch("agent.tools._relationship_key_counts")
    def test_preparation_relationship_probe_reports_coverage_and_cardinality(
        self,
        key_counts,
    ) -> None:
        key_counts.side_effect = [
            {("A",): 3, ("B",): 2},
            {("A",): 1, ("B",): 1, ("C",): 1},
        ]
        relationship = {
            "relationship_id": "contract_customer",
            "left_datasource_id": 11,
            "right_datasource_id": 12,
            "left_fields": ["customer_id"],
            "right_fields": ["id"],
        }

        observation = _inspect_preparation_relationship(
            relationship,
            {11: {"datasource_id": 11}, 12: {"datasource_id": 12}},
        )

        self.assertEqual(observation["status"], "verified")
        self.assertEqual(observation["facts"]["cardinality"], "many_to_one")
        self.assertEqual(observation["facts"]["left_match_coverage_ratio"], 1.0)
        self.assertTrue(observation["facts"]["left_measure_aggregation_safe"])

    @patch("agent.tools.load_data_with_sql")
    def test_sql_relationship_probe_aggregates_at_source(self, load_sql) -> None:
        load_sql.return_value = pd.DataFrame([{
            "left_distinct_key_count": 12000,
            "right_distinct_key_count": 10000,
            "matched_left_key_count": 9000,
            "matched_right_key_count": 9000,
            "left_max_rows_per_key": 12,
            "right_max_rows_per_key": 1,
        }])

        observation = _inspect_sql_preparation_relationship(
            relationship={"relationship_id": "revenue_product"},
            left_source={"datasource_identifier": "dw.revenue"},
            right_source={"datasource_identifier": "dw.products"},
            left_fields=["code"],
            right_fields=["code"],
        )

        sql = load_sql.call_args.args[0]
        self.assertIn("GROUP BY code", sql)
        self.assertNotIn("LIMIT", sql.upper())
        self.assertEqual(observation["facts"]["cardinality"], "many_to_one")
        self.assertEqual(observation["facts"]["left_match_coverage_ratio"], 0.75)

    @patch("agent.tools.load_data_with_sql")
    def test_schema_fact_probe_returns_bounded_enum_summary(self, load_sql) -> None:
        load_sql.return_value = pd.DataFrame([
            {"probed_value": "软件产品", "frequency": 12},
            {"probed_value": "项目服务", "frequency": 7},
        ])

        observation = _inspect_preparation_schema_fact(
            {
                "request_id": "revenue_type_values",
                "datasource_id": 192,
                "field": "revenue_type",
                "probe_kind": "distinct_values",
                "purpose": "确认收入类型是否表达产品线",
                "top_n": 20,
            },
            {
                "datasource_id": 192,
                "datasource_type": "table",
                "datasource_identifier": "dw.revenue",
            },
        )

        sql = load_sql.call_args.args[0]
        self.assertIn("GROUP BY revenue_type", sql)
        self.assertIn("LIMIT 20", sql)
        self.assertNotIn("SELECT *", sql.upper())
        self.assertEqual(observation["status"], "verified")
        self.assertEqual(observation["facts"]["values"][0]["value"], "软件产品")

    def test_dataframe_reduction_contract_ignores_unused_temporary_slice(self) -> None:
        code = """
top_rows = data.head(2)
report = f"total={data['amount'].sum()}"
"""

        self.assertEqual(_extract_dataframe_reduction_clauses(code), [])

    def test_dataframe_reduction_contract_keeps_consumed_slice(self) -> None:
        code = """
top_rows = data.head(2)
charts = [build_chart_result(data=top_rows)]
"""

        self.assertEqual(_extract_dataframe_reduction_clauses(code), ["data.head(2)"])

    def test_dataframe_reduction_contract_does_not_treat_scalar_iloc_as_slice(self) -> None:
        code = "value = data['amount'].iloc[0]\nreport = f'value={value}'"

        self.assertEqual(_extract_dataframe_reduction_clauses(code), [])

    def test_topn_artifact_title_discloses_dataframe_reduction(self) -> None:
        result = StructuredResult(
            analysis_report="## 产品结构\n\n主要产品贡献较为集中。",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[{"product": "A", "amount": 100}],
                title="产品营收 Top10",
                category_field="product",
                value_field="amount",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code="top_rows = data.head(10)\ncharts = [build_chart_result(data=top_rows)]",
        )

        self.assertIsNone(retry)

    def test_single_relative_window_discloses_dataframe_reduction(self) -> None:
        result = StructuredResult(
            analysis_report="## Trend\n\nThe full history covers 75 months; the latest 6 months are shown.",
            charts=[build_chart_result(
                chart_kind="line",
                data=[{"month": "2026-01", "amount": 100}],
                title="Latest 6 months",
                category_field="month",
                value_field="amount",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code="recent = data.tail(6)\ncharts = [build_chart_result(data=recent)]",
        )

        self.assertIsNone(retry)

    def test_relative_window_does_not_hide_a_second_undisclosed_reduction(self) -> None:
        result = StructuredResult(
            analysis_report="## Trend\n\nThe latest 6 months are shown.",
            charts=[build_chart_result(
                chart_kind="line",
                data=[{"month": "2026-01", "amount": 100}],
                title="Latest 6 months",
                category_field="month",
                value_field="amount",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code=(
                "recent = data.tail(6)\n"
                "top_types = shipping_data.head(3)\n"
                "charts = [build_chart_result(data=recent), build_chart_result(data=top_types)]"
            ),
        )

        self.assertIsNotNone(retry)

    def test_sql_loader_coerces_only_explicit_numeric_result_columns(self) -> None:
        raw = pd.DataFrame([{
            "business_id": "00123",
            "record_count": "7",
            "total_amount": "12.50",
        }])
        with patch("agent.tools._load_data_with_fed_query", return_value=raw):
            data = load_data_with_sql(
                "SELECT business_id, COUNT(*) AS record_count, SUM(amount) AS total_amount FROM t",
                numeric_columns=["record_count", "total_amount"],
            )

        self.assertEqual(data.loc[0, "business_id"], "00123")
        self.assertEqual(data.loc[0, "record_count"], 7)
        self.assertEqual(data.loc[0, "total_amount"], 12.5)

    def test_sql_loader_infers_unambiguous_numeric_aggregate_aliases(self) -> None:
        raw = pd.DataFrame([{
            "business_id": "00123",
            "record_count": "7",
            "total_amount": "12.50",
            "first_business_id": "00001",
        }])
        with patch("agent.tools._load_data_with_fed_query", return_value=raw):
            data = load_data_with_sql(
                "SELECT business_id, COUNT(*) AS record_count, "
                "SUM(amount) AS total_amount, MIN(business_id) AS first_business_id "
                "FROM t GROUP BY business_id"
            )

        self.assertEqual(data.loc[0, "business_id"], "00123")
        self.assertEqual(data.loc[0, "record_count"], 7)
        self.assertEqual(data.loc[0, "total_amount"], 12.5)
        self.assertEqual(data.loc[0, "first_business_id"], "00001")

    def test_sql_loader_infers_generated_time_part_as_numeric(self) -> None:
        raw = pd.DataFrame([{
            "month": "7",
            "monthly_amount": "12.50",
        }])
        with patch("agent.tools._load_data_with_fed_query", return_value=raw):
            data = load_data_with_sql(
                "SELECT WM_EXTRA_TS_occurred_at_MONTH AS month, "
                "SUM(amount) AS monthly_amount FROM t "
                "GROUP BY WM_EXTRA_TS_occurred_at_MONTH"
            )

        self.assertEqual(data.loc[0, "month"], 7)
        self.assertEqual(data.loc[0, "monthly_amount"], 12.5)

    def test_sql_loader_restores_direct_numeric_fields_from_selected_schema(self) -> None:
        raw = pd.DataFrame([{
            "contract_id": "00123",
            "self_owned_product_amount": "12000.50",
            "contract_amount": "32000.75",
        }])
        token = CURRENT_DATASOURCE_NUMERIC_FIELDS.set([{
            "identifiers": ["dw.contract_amount_unit", "contract_amount_unit"],
            "fields": ["self_owned_product_amount", "amount"],
        }])
        try:
            with patch("agent.tools._load_data_with_fed_query", return_value=raw):
                data = load_data_with_sql(
                    "SELECT contract_id, self_owned_product_amount, amount AS contract_amount "
                    "FROM dw.contract_amount_unit"
                )
        finally:
            CURRENT_DATASOURCE_NUMERIC_FIELDS.reset(token)

        self.assertEqual(data.loc[0, "contract_id"], "00123")
        self.assertEqual(data.loc[0, "self_owned_product_amount"], 12000.5)
        self.assertEqual(data.loc[0, "contract_amount"], 32000.75)

    def test_no_data_branch_does_not_disclose_dormant_topn_code(self) -> None:
        result = StructuredResult(
            analysis_report="## 产品结构\n\n当前范围内无产品明细数据。",
            charts=[],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code="top_rows = data.head(10)\ncharts = build_charts(top_rows)",
            allow_empty_artifact_no_data=True,
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "missing_structured_artifact")

    def test_chart_contract_retains_runtime_query_origin(self) -> None:
        data = pd.DataFrame([{"department": "Sales", "amount": 12.5}])
        data.attrs["data_insight_query_key"] = "q_department"

        normalized = normalize_chart_result_item(build_chart_result(
            chart_kind="bar",
            data=data,
            title="Department amount",
            category_field="department",
            value_field="amount",
        ))

        self.assertEqual(normalized["chart_data_contract"]["source_query_key"], "q_department")

    def test_chart_suite_can_be_composed_with_individual_charts(self) -> None:
        first = build_chart_result(
            chart_kind="bar",
            data=[{"department": "Sales", "amount": 12.5}],
            title="Department amount",
            category_field="department",
            value_field="amount",
        )
        second = build_chart_result(
            chart_kind="line",
            data=[{"month": "2026-01", "amount": 12.5}],
            title="Monthly amount",
            category_field="month",
            value_field="amount",
        )

        normalized = _normalize_chart_items([[first], second])

        self.assertEqual(len(normalized), 2)
        self.assertEqual([item["title"] for item in normalized], ["Department amount", "Monthly amount"])

    def test_normalized_chart_retains_backend_field_mapping(self) -> None:
        chart = build_chart_result(
            chart_kind="bar",
            data=[{"department": "Sales", "amount_wan": 12.5}],
            title="Sales amount",
            category_field="department",
            value_field="amount_wan",
        )

        normalized = normalize_chart_result_item(chart)

        self.assertEqual(
            normalized["chart_data_contract"]["encoding"]["value_field"],
            "amount_wan",
        )
        self.assertNotIn("dataset", normalized["chart_data_contract"])

    def test_chart_series_null_data_is_contract_error(self) -> None:
        result = StructuredResult(
            analysis_report="## 销量统计\n\n总销量为 100。",
            charts=[{
                "title": "销量统计",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["WS1"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": [None]}],
                },
            }],
            tables=[],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "chart_contract_error")
        self.assertIn("图表数据", retry.message)
        self.assertEqual(retry.diagnostics["invalid_chart_points"][0]["series_index"], 0)

    def test_line_chart_allows_null_gaps_when_other_points_are_renderable(self) -> None:
        result = StructuredResult(
            analysis_report="## Monthly trend\n\nThe series contains a missing month.",
            charts=[{
                "title": "Monthly trend",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["Jan", "Feb", "Mar"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "line", "data": [10, None, 12]}],
                },
            }],
            tables=[],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_bar_chart_allows_null_gaps_when_other_points_are_renderable(self) -> None:
        result = StructuredResult(
            analysis_report="## Monthly comparison\n\nOne department has no value in February.",
            charts=[{
                "title": "Monthly comparison",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["Jan", "Feb", "Mar"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": [10, None, 12]}],
                },
            }],
            tables=[],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_line_chart_rejects_an_all_null_series(self) -> None:
        result = StructuredResult(
            analysis_report="## Monthly trend\n\nNo renderable values.",
            charts=[{
                "title": "Monthly trend",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["Jan", "Feb"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "line", "data": [None, None]}],
                },
            }],
            tables=[],
        )

        retry = _validate_structured_result_contract(result)
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "chart_contract_error")

    def test_report_datasource_reference_does_not_imply_data_access(self) -> None:
        result = StructuredResult(
            analysis_report="当前结果来自收入表；产品明细表可用于后续补充产品分类。",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[{"year": "2026", "amount": 100}],
                title="收入",
                category_field="year",
                value_field="amount",
            )],
            query_snapshots={
                "revenue": {
                    "implementation": {"sql": "SELECT SUM(amount) FROM dw.revenue"},
                    "execution": {"status": "success"},
                },
            },
        )

        retry = _validate_structured_result_contract(
            result,
            used_datasource_ids=[12],
            selected_datasource_snapshot=[
                {"datasource_id": 12, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 13, "datasource_identifier": "dw.product_detail"},
            ],
        )

        self.assertIsNone(retry)

    def test_query_snapshot_requires_all_accessed_datasources_to_be_declared(self) -> None:
        result = StructuredResult(
            analysis_report="收入按产品分类汇总。",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[{"product": "A", "amount": 100}],
                title="产品收入",
                category_field="product",
                value_field="amount",
            )],
            query_snapshots={
                "product_revenue": {
                    "implementation": {
                        "sql": (
                            "SELECT p.product, SUM(r.amount) FROM dw.revenue r "
                            "JOIN dw.product_detail p ON p.code = r.code GROUP BY p.product"
                        ),
                    },
                    "execution": {"status": "success"},
                },
            },
        )

        retry = _validate_structured_result_contract(
            result,
            used_datasource_ids=[12],
            selected_datasource_snapshot=[
                {"datasource_id": 12, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 13, "datasource_identifier": "dw.product_detail"},
            ],
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "result_contract_error")
        self.assertEqual(retry.diagnostics["untracked_datasource_accesses"][0]["datasource_id"], 13)

    def test_opaque_profile_field_cannot_be_a_user_facing_chart_dimension(self) -> None:
        data = pd.DataFrame([{"revenue_type": "-4634325740024215503", "amount": 100}])
        data.attrs["data_insight_query_key"] = "revenue_by_type"
        result = StructuredResult(
            analysis_report="收入按类型汇总。",
            charts=[build_chart_result(
                chart_kind="pie",
                data=data,
                title="收入类型构成",
                category_field="revenue_type",
                value_field="amount",
            )],
            query_snapshots={
                "revenue_by_type": {
                    "implementation": {
                        "sql": (
                            "SELECT revenue_type, SUM(amount) AS amount "
                            "FROM dw.revenue WHERE occurred_at >= %s GROUP BY revenue_type"
                        ),
                    },
                    "execution": {"status": "success"},
                },
            },
        )

        retry = _validate_structured_result_contract(
            result,
            selected_datasource_snapshot=[{
                "datasource_id": 12,
                "datasource_identifier": "dw.revenue",
                "metadata_schema": {
                    "properties": {
                        "revenue_type": {
                            "type": "string",
                            "observed_value_profile": {
                                "availability": "non_empty",
                                "readability": "opaque_identifier",
                            },
                        },
                    },
                },
            }],
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "opaque_identifier_as_business_dimension",
        )

    def test_structured_opaque_categories_are_rejected_without_a_schema_profile(self) -> None:
        result = StructuredResult(
            analysis_report="## Shipping type\n\nThe distribution is concentrated.",
            charts=[build_chart_result(
                chart_kind="pie",
                data=[
                    {"shipping_type": "-4634325740024215503", "count": 12},
                    {"shipping_type": "5876448292014431002", "count": 8},
                ],
                title="Shipping type distribution",
                category_field="shipping_type",
                value_field="count",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            selected_datasource_snapshot=[{
                "datasource_id": 12,
                "datasource_identifier": "dw.shipping",
                "metadata_schema": {"properties": {"shipping_type": {"type": "string"}}},
            }],
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["opaque_dimension_artifacts"][0]["reason"],
            "all_structured_category_values_are_opaque_identifiers",
        )

    def test_execution_namespace_exposes_pandas_alias(self) -> None:
        namespace = _build_execution_namespace()
        self.assertIs(namespace["pd"], pd)

    def test_validate_query_constraints_accepts_sql_params_alias(self) -> None:
        retry = validate_query_constraints(
            query_key="q_revenue",
            query_constraints={
                "time_range": {
                    "start": "2026-01-01",
                    "end": "2027-01-01",
                    "end_exclusive": True,
                },
                "target_filters": [],
                "aggregation": {"grain": "year", "metric": "revenue"},
            },
            sql="SELECT SUM(amount) FROM revenue WHERE occurred_at >= %s AND occurred_at < %s",
            sql_params=["2026-01-01", "2027-01-01"],
            aggregation_hints=["year", "revenue"],
        )

        self.assertIsNone(retry)

    def test_validate_query_constraints_rejects_conflicting_parameter_aliases(self) -> None:
        retry = validate_query_constraints(
            query_constraints={
                "time_range": {},
                "target_filters": [],
                "aggregation": {"grain": "overall", "metric": "revenue"},
            },
            sql="SELECT SUM(amount) FROM revenue WHERE year = %s",
            query_params=[2026],
            sql_params=[2025],
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_constraint"],
            "conflicting_query_parameter_aliases",
        )

    def test_month_bucket_rejects_nonportable_double_pipe_concatenation(self) -> None:
        retry = validate_query_constraints(
            query_constraints={
                "time_range": {},
                "target_filters": [],
                "aggregation": {"grain": "month", "metric": "revenue"},
            },
            sql=(
                "SELECT CAST(year_no AS VARCHAR) || '-' || LPAD(month_no, 2, '0') AS month, "
                "SUM(amount) FROM revenue GROUP BY year_no, month_no"
            ),
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_constraint"],
            "nonportable_time_bucket_concat",
        )

    def test_generated_code_rejects_pyecharts_when_helper_supports_chart_kind(self) -> None:
        code = '''
import json
from pyecharts.charts import Boxplot

execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "outlier"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT * FROM t")
if validation is not None:
    result = validation
else:
    chart = Boxplot()
    chart.add_xaxis(["Speed", "Torque"])
    chart.add_yaxis("Value", [[1, 2, 3, 4, 5], [2, 3, 4, 5, 6]])
    chart_spec = json.loads(chart.dump_options_with_quotes())
    charts = [{
        "title": "Distribution",
        "chart_type": "echarts",
        "description": "Outlier check",
        "chart_spec": chart_spec,
    }]
    result = save_analysis_result(analysis_report="ok", charts=charts, tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "chart_contract_error")
        self.assertIn("Boxplot", retry.diagnostics["blocked_chart_apis"])

    def test_generated_code_rejects_handwritten_chart_spec_when_helper_supports_chart_kind(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT * FROM t")
if validation is not None:
    result = validation
else:
    charts = [{
        "title": "Count",
        "chart_type": "echarts",
        "description": "Count by type",
        "chart_spec": {
            "xAxis": {"type": "category", "data": ["A"]},
            "yAxis": {"type": "value"},
            "series": [{"type": "bar", "data": [1]}],
        },
    }]
    result = save_analysis_result(analysis_report="ok", charts=charts, tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "chart_contract_error")
        self.assertIn("bar", retry.diagnostics["supported_chart_spec_kinds"])

    def test_generated_code_allows_handwritten_chart_spec_for_unsupported_chart_kind(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "density"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT * FROM t")
if validation is not None:
    result = validation
else:
    charts = [{
        "title": "Density",
        "chart_type": "echarts",
        "description": "Unsupported helper chart",
        "chart_spec": {
            "xAxis": {"type": "category", "data": ["A"]},
            "yAxis": {"type": "category", "data": ["B"]},
            "series": [{"type": "heatmap", "data": [[0, 0, 1]]}],
        },
    }]
    result = save_analysis_result(analysis_report="ok", charts=charts, tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNone(retry)

    def test_generated_code_rejects_silent_zero_for_missing_query_dimension(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "revenue"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT year, revenue FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT year, revenue FROM t")
    values = data.set_index("year")["revenue"].to_dict()
    current_value = values.get(year, 0)
    result = save_analysis_result(analysis_report=f"value={current_value}", charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "query_dimension_missing_is_not_zero",
        )

    def test_analysis_sql_cannot_join_an_unresolved_business_relationship(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 12, "datasource_identifier": "dw.products"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "revenue_product",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "dimension_enrichment",
                    "joinability": "unknown",
                }]
            },
        })
        code = '''
execution_intent = "analysis"
sql = "SELECT * FROM dw.revenue r JOIN dw.products p ON r.code = p.code"
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unresolved_business_lineage_join",
        )

    def test_analysis_dataframe_cannot_merge_an_unresolved_business_relationship(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 12, "datasource_identifier": "dw.products"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "revenue_product",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "dimension_enrichment",
                    "joinability": "unknown",
                }]
            },
        })
        code = '''
execution_intent = "analysis"
revenue_sql = "SELECT code, amount FROM dw.revenue"
product_sql = "SELECT code, name FROM dw.products"
revenue = load_data_with_sql(revenue_sql)
products = load_data_with_sql(product_sql)
prepared_revenue = revenue.copy()
joined = prepared_revenue.merge(products, on="code", how="left")
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unresolved_business_lineage_join",
        )

    def test_analysis_dataframe_can_merge_a_confirmed_business_relationship(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 12, "datasource_identifier": "dw.products"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "revenue_product",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "dimension_enrichment",
                    "joinability": "joinable",
                }]
            },
        })
        code = '''
execution_intent = "analysis"
revenue = load_data_with_sql("SELECT code, amount FROM dw.revenue")
products = load_data_with_sql("SELECT code, name FROM dw.products")
joined = revenue.merge(products, on="code", how="left")
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_sql_rejects_aggregate_from_fanout_side_after_join(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.products"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_products",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "cardinality": "one_to_many",
                    "aggregation_safety": {
                        "left_measure_after_join": {"status": "unsafe_repeated_by_join"},
                        "right_measure_after_join": {"status": "safe"},
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
sql = """
SELECT p.category, SUM(c.contract_amount) AS revenue
FROM dw.contracts c
JOIN dw.products p ON c.code = p.code
WHERE c.accepted_at >= %s AND c.accepted_at < %s
GROUP BY p.category
"""
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "relationship_fanout_aggregation",
        )

    def test_analysis_sql_allows_aggregate_from_safe_side_after_join(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.products"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_products",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "cardinality": "one_to_many",
                    "aggregation_safety": {
                        "left_measure_after_join": {"status": "unsafe_repeated_by_join"},
                        "right_measure_after_join": {"status": "safe"},
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
sql = """
SELECT p.category, SUM(p.sales_price) AS revenue
FROM dw.contracts c
JOIN dw.products p ON c.code = p.code
GROUP BY p.category
"""
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))



if __name__ == "__main__":
    unittest.main()
