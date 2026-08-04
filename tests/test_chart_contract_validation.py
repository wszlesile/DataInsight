import sys
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import (  # noqa: E402
    CURRENT_EXECUTION_INTENT,
    CURRENT_DATASOURCE_NUMERIC_FIELDS,
    StructuredResult,
    _build_generated_code_error_context,
    _build_execution_namespace,
    _canonicalize_used_datasource_ids,
    _extract_dataframe_reduction_clauses,
    _classify_execution_error,
    _inspect_preparation_relationship,
    _inspect_preparation_schema_fact,
    _inspect_sql_preparation_relationship,
    _normalize_chart_items,
    _restore_sql_projection_column_names,
    _terminal_tool_success,
    _tool_success_message,
    _validate_analysis_relationship_contract,
    _validate_generated_code_contract,
    _validate_structured_result_contract,
    load_data_with_sql,
    probe_data_with_sql,
    request_retry,
    save_analysis_result,
    validate_query_constraints,
)
from utils.chart_document_utils import build_chart_result, normalize_chart_result_item  # noqa: E402


class ChartContractValidationTestCase(unittest.TestCase):

    def test_syntax_error_feedback_contains_exact_generated_code_context(self) -> None:
        retry = _validate_generated_code_contract(
            'execution_intent = "analysis"\nreport = "用户期望的"过程波动"分类"\n'
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.diagnostics["violated_contract"], "invalid_python_syntax")
        context = retry.diagnostics["code_error_context"]
        self.assertEqual(context["line_no"], 2)
        self.assertIn('"过程波动"', context["error_line"])
        self.assertIn("第 2 行", retry.message)

    def test_query_snapshots_canonicalize_declared_datasource_ids(self) -> None:
        result = StructuredResult(
            analysis_report="收入分析。",
            query_snapshots={
                "revenue": {
                    "implementation": {"sql": "SELECT SUM(amount) FROM dw.revenue"},
                    "execution": {"status": "success"},
                },
            },
        )

        canonical = _canonicalize_used_datasource_ids(
            result,
            declared_datasource_ids=[12, 13],
            selected_datasource_snapshot=[
                {"datasource_id": 12, "datasource_identifier": "dw.revenue"},
                {"datasource_id": 13, "datasource_identifier": "dw.product_detail"},
            ],
        )

        self.assertEqual(canonical, [12])

    def test_valid_analysis_result_terminates_the_agent_graph(self) -> None:
        message = _tool_success_message(
            tool_call_id="call-1",
            analysis_report="# report",
            charts=[{"title": "trend"}],
        )

        command = _terminal_tool_success(message)

        self.assertEqual(command.goto, "__end__")
        self.assertEqual(command.update, {"messages": [message]})

    def test_probe_rejects_a_static_inspection_plan_without_data_access(self) -> None:
        code = '''
execution_intent = "probe"
sql_probe = "SELECT status FROM contracts LIMIT 20"
result = request_retry(
    retry_type="probe_feedback",
    message="inspect status values",
    diagnostics={"sql": sql_probe, "actions": ["inspect status"]},
)
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "probe_requires_observed_data",
        )

    def test_probe_accepts_bounded_observations_derived_from_loaded_data(self) -> None:
        code = '''
execution_intent = "probe"
sql_probe = "SELECT status FROM contracts LIMIT 20"
probe_data = load_data_with_sql(sql=sql_probe)
status_values = probe_distinct_values(probe_data, "status", top_n=10)
result = request_retry(
    retry_type="probe_feedback",
    message="observed status values",
    diagnostics={
        "row_count": len(probe_data),
        "status_values": status_values,
    },
)
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_partial_analysis_program_gets_state_boundary_repair_feedback(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"target_filters": []}
sql = "SELECT status FROM contracts"
validation = validate_query_constraints(
    query_key="q_contracts",
    query_constraints=query_constraints,
    sql=sql,
)
data = load_data_with_sql(sql=sql)
print("data loading complete; continue later")
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "contract_error")
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "incomplete_analysis_program",
        )
        self.assertTrue(any(
            "独立执行单元" in item
            for item in retry.repair_instructions
        ))

    def test_probe_accepts_observations_accumulated_with_list_append(self) -> None:
        code = '''
execution_intent = "probe"
observations = []
for sql in probe_queries:
    outcome = probe_data_with_sql(sql=sql)
    observations.append({
        "status": outcome["status"],
        "row_count": outcome["row_count"],
    })
result = request_retry(
    retry_type="probe_feedback",
    message="observed candidate queries",
    diagnostics={"observations": observations},
)
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_probe_accepts_observations_accumulated_by_dict_key(self) -> None:
        code = '''
execution_intent = "probe"
observations = {}
for name, sql in probe_queries.items():
    outcome = probe_data_with_sql(sql=sql)
    entry = {"status": outcome["status"], "row_count": outcome["row_count"]}
    observations[name] = entry
result = request_retry(
    retry_type="probe_feedback",
    message="observed candidate queries",
    diagnostics={"observations": observations},
)
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_probe_sql_wrapper_returns_structured_query_error(self) -> None:
        token = CURRENT_EXECUTION_INTENT.set("probe")
        try:
            with patch("agent.tools.load_data_with_sql", side_effect=ValueError("bad sql")):
                outcome = probe_data_with_sql("SELECT broken")
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)

        self.assertEqual(outcome["status"], "query_error")
        self.assertEqual(outcome["row_count"], 0)
        self.assertEqual(outcome["error_message"], "bad sql")
        self.assertIsNone(outcome["data"])

    def test_probe_sql_wrapper_distinguishes_empty_timeseries_path(self) -> None:
        token = CURRENT_EXECUTION_INTENT.set("probe")
        try:
            with patch(
                "agent.tools.load_data_with_sql",
                side_effect=RuntimeError("unknown tag"),
            ):
                outcome = probe_data_with_sql("SELECT broken")
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)

        self.assertEqual(outcome["status"], "no_data")
        self.assertEqual(outcome["error_type"], "no_data_found")

    def test_query_fetch_failure_is_not_misclassified_as_result_contract(self) -> None:
        error_message = 'IO Error: Failed to fetch result for query "SELECT *"'

        self.assertEqual(
            _classify_execution_error(RuntimeError(error_message), error_message),
            "data_source_unavailable",
        )

    def test_oversized_query_result_has_a_specific_repair_type(self) -> None:
        error_message = (
            "Query result exceeds 10000 rows, please update query conditions "
            "or set pagination"
        )

        self.assertEqual(
            _classify_execution_error(RuntimeError(error_message), error_message),
            "query_result_too_large",
        )

    def test_probe_feedback_adds_authoritative_outcome_summary(self) -> None:
        retry = request_retry(
            retry_type="probe_feedback",
            message="done",
            diagnostics={"query_observations": [
                {"datasource_id": 1, "datasource_name": "A", "status": "success", "row_count": 2},
                {"datasource_id": 2, "datasource_name": "B", "status": "no_data", "row_count": 0, "error_type": "no_data_found"},
                {"datasource_id": 3, "datasource_name": "C", "status": "query_error", "row_count": 0, "error_type": "data_source_unavailable"},
            ]},
        )

        summary = retry.diagnostics["_probe_outcome_summary"]
        self.assertEqual([item["datasource_id"] for item in summary["successful"]], [1])
        self.assertEqual([item["datasource_id"] for item in summary["no_data"]], [2])
        self.assertEqual([item["datasource_id"] for item in summary["unavailable"]], [3])

    def test_probe_broad_sql_exception_feedback_preserves_multi_source_scope(self) -> None:
        retry = _validate_generated_code_contract('''
execution_intent = "probe"
observations = []
for sql in ["SELECT 1", "SELECT 2"]:
    try:
        frame = load_data_with_sql(sql=sql)
        observations.append({"status": "success", "rows": len(frame)})
    except Exception as exc:
        observations.append({"status": "query_error", "error": str(exc)})
result = request_retry(
    retry_type="probe_feedback",
    message="done",
    diagnostics={"observations": observations},
)
''')

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "probe_sql_outcomes_require_structured_helper",
        )
        self.assertTrue(any(
            "不要缩成单个数据源" in item
            for item in retry.repair_instructions
        ))
        self.assertTrue(any(
            "probe_data_with_sql" in item
            for item in retry.repair_instructions
        ))

    def test_sql_projection_restores_schema_field_casing(self) -> None:
        raw = pd.DataFrame([{
            "wm_extra_ts_expiration_time_year": 2026,
            "contracted_customer": "A",
        }])

        restored = _restore_sql_projection_column_names(
            "SELECT WM_EXTRA_TS_expiration_time_YEAR, contracted_customer FROM contracts",
            raw,
        )

        self.assertEqual(
            list(restored.columns),
            ["WM_EXTRA_TS_expiration_time_YEAR", "contracted_customer"],
        )

    def test_sql_projection_preserves_explicit_alias_casing(self) -> None:
        raw = pd.DataFrame([{"expiration_year": 2026}])

        restored = _restore_sql_projection_column_names(
            "SELECT WM_EXTRA_TS_expiration_time_YEAR AS expiration_year FROM contracts",
            raw,
        )

        self.assertEqual(list(restored.columns), ["expiration_year"])

    def test_save_analysis_result_normalizes_period_values_at_runtime_boundary(self) -> None:
        result = save_analysis_result(
            analysis_report="月度趋势。",
            charts=[{
                "title": "月度趋势",
                "chart_document": {
                    "chart_kind": "line",
                    "dataset": {"rows": [{"month": pd.Period("2026-01", freq="M"), "count": 3}]},
                    "encoding": {"category_field": "month", "value_field": "count"},
                },
            }],
            tables=[{
                "title": "月度明细",
                "columns": [{"key": "month", "label": "月份"}],
                "rows": [{"month": pd.Period("2026-01", freq="M")}],
            }],
        )

        serialized = json.dumps(result.model_dump(), ensure_ascii=False)
        self.assertIn("2026-01", serialized)
        self.assertEqual(result.tables[0]["rows"][0]["month"], "2026-01")

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
    def test_relationship_key_fallback_is_bounded_and_deterministic(self, load_sql) -> None:
        load_sql.return_value = pd.DataFrame([
            {"code": "A", "__relationship_frequency": 3},
        ])

        from agent.tools import _relationship_key_counts

        counts = _relationship_key_counts(
            {
                "datasource_type": "table",
                "datasource_identifier": "dw.contracts",
            },
            ["code"],
        )

        sql = load_sql.call_args.args[0]
        self.assertIn("GROUP BY code", sql)
        self.assertIn("ORDER BY code LIMIT 10000", sql)
        self.assertEqual(counts, {("A",): 3})

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

    @patch("agent.tools._relationship_key_counts")
    @patch("agent.tools._inspect_sql_preparation_relationship")
    def test_sql_relationship_probe_reconciles_decimal_and_text_identifiers(
        self,
        inspect_sql,
        key_counts,
    ) -> None:
        inspect_sql.return_value = {
            "observation_id": "probe:status_dictionary",
            "kind": "relationship_coverage",
            "status": "verified",
            "complete": True,
            "facts": {"join_supported": False, "cardinality": "unknown"},
        }
        key_counts.side_effect = [
            {("166832699355251625.00",): 12},
            {("166832699355251625",): 1},
        ]

        observation = _inspect_preparation_relationship(
            {
                "relationship_id": "status_dictionary",
                "left_datasource_id": 11,
                "right_datasource_id": 12,
                "left_fields": ["status_id"],
                "right_fields": ["id"],
            },
            {
                11: {"datasource_type": "table"},
                12: {"datasource_type": "table"},
            },
        )

        self.assertTrue(observation["facts"]["join_supported"])
        self.assertEqual(
            observation["facts"]["key_normalization"],
            "integral_numeric_text",
        )

    def test_structured_table_rejects_opaque_first_dimension_column(self) -> None:
        result = StructuredResult(
            analysis_report="## Department performance",
            charts=[],
            tables=[{
                "title": "Department performance",
                "columns": [
                    {"key": "department", "label": "Department"},
                    {"key": "amount", "label": "Amount"},
                ],
                "rows": [
                    {"department": "5313416643652174324", "amount": "10.00"},
                    {"department": "6325791984017753736", "amount": "20.00"},
                ],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["opaque_dimension_artifacts"][0]["reason"],
            "all_structured_dimension_values_are_opaque_identifiers",
        )

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

    def test_chinese_topn_text_discloses_dataframe_reduction(self) -> None:
        result = StructuredResult(
            analysis_report="## 客户集中度\n\nTop5客户合计占比 76.2%。",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[{"customer": "A", "count": 10}],
                title="客户合同数量",
                category_field="customer",
                value_field="count",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code=(
                "top5 = customers.head(5)\n"
                "top5_ratio = top5['count'].sum() / customers['count'].sum()\n"
                "charts = [build_chart_result(data=customers)]"
            ),
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

    def test_duplicate_chart_error_identifies_chart_and_related_helper_call(self) -> None:
        chart = build_chart_result(
            chart_kind="bar",
            data=[
                {"quality_item": "purity", "product": "A", "value": 1.0},
                {"quality_item": "purity", "product": "A", "value": 2.0},
            ],
            title="Quality deviation",
            category_field="quality_item",
            value_field="value",
            series_field="product",
        )

        with self.assertRaisesRegex(ValueError, r"chart\[1\] title='Quality deviation'") as caught:
            _normalize_chart_items([chart])

        code = '''
chart_data = detail.copy()
chart_data["unique_batch"] = chart_data["batch_id"]
chart = build_chart_result(
    chart_kind="bar",
    data=chart_data,
    title="Quality deviation",
    category_field="quality_item",
    value_field="value",
    series_field="product",
)
result = save_analysis_result(analysis_report=report, charts=[chart], tables=[])
'''
        context = _build_generated_code_error_context(code, caught.exception, "traceback")

        related_code = "\n".join(item["code"] for item in context["related_context_lines"])
        self.assertIn('chart_data["unique_batch"]', related_code)
        self.assertIn('series_field="product"', related_code)

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

    def test_report_rejects_repeated_numeric_display_units(self) -> None:
        result = StructuredResult(
            analysis_report="## Subscription cycle\n\nThe leading cycle is **365天天**.",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[{"cycle": "365天", "count": 10}],
                title="Subscription cycles",
                category_field="cycle",
                value_field="count",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "repeated_numeric_display_unit",
        )

    def test_report_rejects_unrendered_generated_code_placeholders(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "## Summary\n"
                "- Range: {start_date} to {end_date[:10]}\n"
                "- Missing share: {100 - covered_pct:.1f}%"
            ),
            charts=[{"title": "Trend", "chart_document": {"series": []}}],
            tables=[],
        )
        generated_code = """
start_date = '2026-08-03'
end_date = '2026-08-10'
covered_pct = 25.0
"""

        retry = _validate_structured_result_contract(
            result,
            generated_code=generated_code,
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "result_contract_error")
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unrendered_report_placeholder",
        )
        self.assertEqual(len(retry.diagnostics["placeholders"]), 3)

    def test_report_allows_literal_braces_not_bound_to_generated_variables(self) -> None:
        result = StructuredResult(
            analysis_report="## Summary\nThe set is {high, medium, low} and the formula uses {x}.",
            charts=[{"title": "Trend", "chart_document": {"series": []}}],
            tables=[],
        )

        self.assertIsNone(
            _validate_structured_result_contract(
                result,
                generated_code="metric_value = 3",
            )
        )

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

    def test_wrapped_scientific_identifier_category_is_rejected(self) -> None:
        result = StructuredResult(
            analysis_report="## Delivery status",
            charts=[build_chart_result(
                chart_kind="pie",
                data=[
                    {"status": "unknown(1.6683269935525162e+17)", "count": 12},
                    {"status": "unknown(-1.261257595985902e+18)", "count": 8},
                ],
                title="Delivery status",
                category_field="status",
                value_field="count",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)

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

    def test_generated_code_allows_zero_for_absent_fixed_count_category(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    values = data.set_index("status")["contract_count"].to_dict()
    active_count = values.get("active", 0)
    result = save_analysis_result(analysis_report=f"active={active_count}", charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_zero_for_absent_fixed_value_counts_category(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status FROM t")
    values = data["status"].value_counts().to_dict()
    active_count = values.get("active", 0)
    result = save_analysis_result(analysis_report=f"active={active_count}", charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_rejects_positional_row_as_superlative_evidence(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    analysis_report = f"合同状态以 {data.iloc[0]['status']} 为主，共 {data.iloc[0]['contract_count']} 个。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "report_superlative_requires_explicit_extrema",
        )

    def test_generated_code_allows_explicit_extrema_row_in_report(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    peak_row = data.loc[data["contract_count"].idxmax()]
    analysis_report = f"合同状态以 {peak_row['status']} 为主，共 {peak_row['contract_count']} 个。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_rejects_one_sided_filter_for_bounded_future_period(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT expiration_time FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT expiration_time FROM t")
    now = pd.Timestamp.now()
    upcoming = data[data["expiration_time"] >= now]
    upcoming_count = len(upcoming)
    analysis_report = f"未来6个月内有 {upcoming_count} 个合同即将到期。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "bounded_report_period_requires_two_date_boundaries",
        )

    def test_generated_code_allows_two_sided_filter_for_bounded_future_period(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT expiration_time FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT expiration_time FROM t")
    now = pd.Timestamp.now()
    end = now + pd.DateOffset(months=6)
    upcoming = data[(data["expiration_time"] >= now) & (data["expiration_time"] < end)]
    upcoming_count = len(upcoming)
    analysis_report = f"未来6个月内有 {upcoming_count} 个合同即将到期。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_rejects_paired_percentage_and_count_from_different_populations(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, expiration_time FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, expiration_time FROM t")
    status_share = data["status"].value_counts(normalize=True)
    active_pct = status_share.get("active", 0)
    data["days_to_expire"] = (pd.to_datetime(data["expiration_time"]) - pd.Timestamp.now()).dt.days
    active_count = len(data[data["days_to_expire"] >= 0])
    analysis_report = f"有效合同占比 {active_pct:.1%}（{active_count} 个）。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "paired_report_metrics_require_shared_population",
        )
        self.assertEqual(
            retry.diagnostics["findings"][0]["percentage_population_fields"],
            ["status"],
        )
        self.assertEqual(
            retry.diagnostics["findings"][0]["count_population_fields"],
            ["expiration_time"],
        )

    def test_generated_code_allows_paired_percentage_and_count_from_one_population(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status FROM t")
    active_mask = data["status"] == "active"
    active_count = int(active_mask.sum())
    active_pct = active_count / len(data) if len(data) else 0
    analysis_report = f"有效合同占比 {active_pct:.1%}（{active_count} 个）。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_does_not_treat_an_independent_sample_size_as_percentage_numerator(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "pass_rate"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT score FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT score FROM t")
    pass_rate = (data["score"] >= 60).mean()
    sample_count = len(data)
    analysis_report = f"样本合格率为 {pass_rate:.1%}（样本量 {sample_count} 个）。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

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

    def test_analysis_rejects_pandas_entity_counts_on_fanout_join_rows(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["id"],
                    "right_fields": ["contract_id"],
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
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall", "metric": "contract_instance"},
}
sql = """
SELECT c.id, c.status, d.detail_id
FROM dw.contracts c
LEFT JOIN dw.contract_details d ON c.id = d.contract_id
"""
data = load_data_with_sql(sql=sql)
total_contracts = len(data)
status_dist = data["status"].value_counts().reset_index()
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "relationship_fanout_population_grain",
        )

    def test_analysis_allows_entity_counts_after_explicit_population_dedup(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
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
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall", "metric": "contract_instance"},
}
sql = """
SELECT c.id, c.status, d.detail_id
FROM dw.contracts c
LEFT JOIN dw.contract_details d ON c.id = d.contract_id
"""
data = load_data_with_sql(sql=sql)
population_df = data.drop_duplicates(subset=["id"])
total_contracts = len(population_df)
status_dist = population_df["status"].value_counts().reset_index()
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_counts_of_rows_after_explicit_grouped_grain(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.batches"},
                {"datasource_id": 12, "datasource_identifier": "dw.samples"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "batch_samples",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "event_of_entity",
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
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall", "metric": "alarm_pattern"},
}
sql = """
SELECT b.batch_id, s.parameter_name, s.alarm_flag
FROM dw.batches b
JOIN dw.samples s ON b.batch_id = s.batch_id
"""
data = load_data_with_sql(sql=sql)
parameter_stats = data[data["alarm_flag"] == 1].groupby(
    "parameter_name", as_index=False
).agg(
    alarm_count=("alarm_flag", "count"),
    batch_count=("batch_id", "nunique"),
)
has_parameter_results = len(parameter_stats) > 0
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_requires_runtime_normalization_for_numeric_text_relation(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.status_labels"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_status",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["status_id"],
                    "right_fields": ["id"],
                    "relationship_type": "dimension_enrichment",
                    "joinability": "joinable",
                    "key_normalization": "integral_numeric_text",
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT status_id FROM dw.contracts")
labels = load_data_with_sql("SELECT id, label FROM dw.status_labels")
contracts["key"] = contracts["status_id"].astype(str)
result_data = contracts.merge(labels, left_on="key", right_on="id")
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "relationship_key_normalization_required",
        )

    def test_analysis_accepts_runtime_normalization_for_numeric_text_relation(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.status_labels"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_status",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["status_id"],
                    "right_fields": ["id"],
                    "relationship_type": "dimension_enrichment",
                    "joinability": "joinable",
                    "key_normalization": "integral_numeric_text",
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql(
    "SELECT status_id FROM dw.contracts", text_columns=["status_id"]
)
labels = load_data_with_sql(
    "SELECT id, label FROM dw.status_labels", text_columns=["id"]
)
contracts["key"] = normalize_relationship_key(contracts["status_id"])
labels["key"] = normalize_relationship_key(labels["id"])
result_data = contracts.merge(labels, on="key")
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_pandas_use_of_sql_aggregate_join_result(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "left_measure_after_join": {"status": "unsafe_repeated_by_join"},
                        "right_measure_after_join": {"status": "safe"},
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall", "metric": "contract_instance"},
}
sql = """
SELECT c.status, COUNT(DISTINCT c.id) AS instance_count
FROM dw.contracts c
LEFT JOIN dw.contract_details d ON c.id = d.contract_id
GROUP BY c.status
"""
data = load_data_with_sql(sql=sql)
status_count = len(data)
total_contracts = int(data["instance_count"].sum())
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_count_of_explicit_key_domain_intersection(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "left_measure_after_join": {"status": "unsafe_repeated_by_join"},
                        "right_measure_after_join": {"status": "safe"},
                        "key_domain_coverage": {
                            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
                        },
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall", "metric": "contract_instance"},
}
contracts = load_data_with_sql("SELECT id FROM dw.contracts")
details = load_data_with_sql("SELECT contract_id FROM dw.contract_details")
contract_ids = set(contracts["id"].dropna().unique())
detail_ids = set(details["contract_id"].dropna().unique())
matched_ids = contract_ids & detail_ids
covered_contracts = len(matched_ids)
coverage_ratio = covered_contracts / len(contract_ids)
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_rejects_coverage_from_independent_key_domains(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "key_domain_coverage": {
                            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
                            "denominator": "distinct_local_population_keys",
                        },
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id FROM dw.contracts")
details = load_data_with_sql("SELECT COUNT(DISTINCT contract_id) AS covered FROM dw.contract_details")
total_contracts = len(contracts)
covered_contracts = int(details["covered"].iloc[0])
coverage_ratio = covered_contracts / total_contracts
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "cross_source_key_domain_coverage",
        )

    def test_analysis_rejects_ratio_of_independent_sql_relationship_key_counts(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["id"],
                    "right_fields": ["instance_id"],
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {"key_domain_coverage": {}},
                }]
            },
        })
        code = '''
execution_intent = "analysis"
sql_main = "SELECT COUNT(DISTINCT id) AS distinct_instances FROM dw.contracts"
main = load_data_with_sql(sql=sql_main)
sql_detail = "SELECT COUNT(DISTINCT instance_id) AS contracted_instances FROM dw.contract_details"
detail = load_data_with_sql(sql=sql_detail)
distinct_instances = int(main["distinct_instances"].iloc[0])
contracted_instances = int(detail["contracted_instances"].iloc[0])
contract_rate = contracted_instances / distinct_instances
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "cross_source_key_domain_coverage",
        )

    def test_analysis_allows_independent_relationship_key_count_as_standalone_fact(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["id"],
                    "right_fields": ["instance_id"],
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {"key_domain_coverage": {}},
                }]
            },
        })
        code = '''
execution_intent = "analysis"
details = load_data_with_sql(
    "SELECT COUNT(DISTINCT instance_id) AS detail_instance_count FROM dw.contract_details"
)
detail_instance_count = int(details["detail_instance_count"].iloc[0])
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_primary_source_field_coverage_with_auxiliary_relationships(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["id"],
                    "right_fields": ["contract_id"],
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {"key_domain_coverage": {}},
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id, license_id FROM dw.contracts")
details = load_data_with_sql("SELECT contract_id FROM dw.contract_details")
has_license = contracts["license_id"].notna()
license_coverage = has_license.sum()
license_ratio = license_coverage / len(contracts)
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_does_not_infer_cross_source_coverage_from_with_variable_name(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.batches"},
                {"datasource_id": 12, "datasource_identifier": "dw.process_params"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "batch_process_params",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["batch_id"],
                    "right_fields": ["batch_id"],
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {"key_domain_coverage": {}},
                }]
            },
        })
        code = '''
execution_intent = "analysis"
batches = load_data_with_sql("SELECT batch_id FROM dw.batches")
process_params = load_data_with_sql(
    "SELECT batch_id, value, upper_control_limit, lower_control_limit FROM dw.process_params"
)
params_with_ucl = process_params[
    process_params["upper_control_limit"].notna()
    & process_params["lower_control_limit"].notna()
]
above_ucl = params_with_ucl[params_with_ucl["value"] > params_with_ucl["upper_control_limit"]]
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_report_to_present_independent_source_facts_together(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "left_fields": ["id"],
                    "right_fields": ["contract_id"],
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {"key_domain_coverage": {}},
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id, license_id FROM dw.contracts")
details = load_data_with_sql("SELECT COUNT(DISTINCT contract_id) AS detail_count FROM dw.contract_details")
license_coverage = contracts["license_id"].notna().sum()
detail_count = int(details["detail_count"].iloc[0])
report_sections = [
    f"主表授权字段完整 {license_coverage} 条。",
    f"明细表独立涉及 {detail_count} 个合同。",
]
analysis_report = "\\n".join(report_sections)
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_allows_coverage_from_aligned_key_domain(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "key_domain_coverage": {
                            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
                            "denominator": "distinct_local_population_keys",
                        },
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
aligned = load_data_with_sql("""
SELECT COUNT(DISTINCT c.id) AS total_contracts,
       COUNT(DISTINCT CASE WHEN d.contract_id IS NOT NULL THEN c.id END) AS covered_contracts
FROM dw.contracts c
LEFT JOIN dw.contract_details d ON c.id = d.contract_id
""")
total_contracts = int(aligned["total_contracts"].iloc[0])
covered_contracts = int(aligned["covered_contracts"].iloc[0])
coverage_ratio = covered_contracts / total_contracts
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_analysis_rejects_bound_count_from_only_the_detail_key_domain(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "key_domain_coverage": {
                            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
                            "denominator": "distinct_local_population_keys",
                        },
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id FROM dw.contracts")
details = load_data_with_sql("SELECT COUNT(DISTINCT contract_id) AS bound_count FROM dw.contract_details")
total_contracts = len(contracts)
instances_with_contract = int(details["bound_count"].iloc[0])
'''

        retry = _validate_analysis_relationship_contract(context, code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "cross_source_key_domain_coverage",
        )

    def test_analysis_allows_bound_count_from_explicit_key_intersection(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
            ],
            "analysis_semantic_context": {
                "relationships": [{
                    "relationship_id": "contract_details",
                    "left_datasource_id": 11,
                    "right_datasource_id": 12,
                    "relationship_type": "parent_child",
                    "joinability": "joinable",
                    "aggregation_safety": {
                        "key_domain_coverage": {
                            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
                            "denominator": "distinct_local_population_keys",
                        },
                    },
                }]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id FROM dw.contracts")
details = load_data_with_sql("SELECT contract_id FROM dw.contract_details")
main_instance_ids = set(contracts["id"].dropna())
detail_instance_ids = set(details["contract_id"].dropna())
instances_with_contract = len(main_instance_ids & detail_instance_ids)
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))

    def test_key_intersection_is_checked_only_against_its_own_relationship(self) -> None:
        context = SimpleNamespace(active_datasource_snapshot={
            "selected_datasource_snapshot": [
                {"datasource_id": 11, "datasource_identifier": "dw.contracts"},
                {"datasource_id": 12, "datasource_identifier": "dw.contract_details"},
                {"datasource_id": 13, "datasource_identifier": "dw.authorizations"},
            ],
            "analysis_semantic_context": {
                "relationships": [
                    {
                        "relationship_id": "contract_details",
                        "left_datasource_id": 11,
                        "right_datasource_id": 12,
                        "relationship_type": "parent_child",
                        "joinability": "joinable",
                        "aggregation_safety": {"key_domain_coverage": {}},
                    },
                    {
                        "relationship_id": "contract_authorizations",
                        "left_datasource_id": 11,
                        "right_datasource_id": 13,
                        "relationship_type": "parent_child",
                        "joinability": "joinable",
                        "aggregation_safety": {"key_domain_coverage": {}},
                    },
                ]
            },
        })
        code = '''
execution_intent = "analysis"
contracts = load_data_with_sql("SELECT id FROM dw.contracts")
details = load_data_with_sql("SELECT contract_id FROM dw.contract_details")
authorizations = load_data_with_sql("SELECT contract_id FROM dw.authorizations")
main_ids = set(contracts["id"].dropna())
detail_ids = set(details["contract_id"].dropna())
authorization_ids = set(authorizations["contract_id"].dropna())
instances_with_contract = len(main_ids & detail_ids)
instances_with_auth = len(main_ids & authorization_ids)
'''

        self.assertIsNone(_validate_analysis_relationship_contract(context, code))



if __name__ == "__main__":
    unittest.main()
