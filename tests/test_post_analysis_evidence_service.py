from __future__ import annotations

import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from service.post_analysis_evidence_service import (  # noqa: E402
    AnalysisExecutionSnapshot,
    EvidencePlan,
    LineageIndexer,
    PlannedMetric,
    PostAnalysisEvidenceCompiler,
)
from service.insight_ns_conversation_service import InsightNsConversationService  # noqa: E402


def _query_snapshot(sql: str, *, columns: list[str] | None = None) -> dict:
    execution = {"status": "success"}
    if columns:
        execution["columns"] = columns
    return {
        "implementation": {
            "sql": sql,
            "sql_hash": hashlib.sha256(sql.encode()).hexdigest(),
        },
        "execution": execution,
    }


def _loader_snapshot(identifier: str, *, columns: list[str]) -> dict:
    return {
        "implementation": {
            "loader": "load_local_file",
            "datasource_identifier": identifier,
            "loader_options": {},
        },
        "execution": {
            "status": "success",
            "columns": columns,
        },
    }


def _snapshot(
    *,
    code: str,
    report: str = "",
    charts: list[dict] | None = None,
    tables: list[dict] | None = None,
    query_snapshots: dict | None = None,
) -> AnalysisExecutionSnapshot:
    return AnalysisExecutionSnapshot(
        execution_id=10,
        code_hash="code-hash",
        result_hash="result-hash",
        generated_code=code,
        query_snapshots=query_snapshots or {},
        selected_datasource_snapshot=[],
        user_query="Analyze business performance",
        analysis_report=report,
        charts=charts or [],
        tables=tables or [],
    )


def _semantic_plan(payload: dict) -> EvidencePlan:
    return EvidencePlan(metrics=[
        PlannedMetric(
            calculation_id=item["calculation_id"],
            metric_key=f"metric_{index}",
            business_name=f"Business metric {index}",
            business_explanation="Calculated from the confirmed query and calculation boundary.",
            value_type="number",
            display_unit="",
        )
        for index, item in enumerate(payload["calculation_candidates"], start=1)
    ])


def test_planner_payload_collapses_duplicate_annotations_for_one_calculation():
    payload = {
        "metrics": [
            {
                "calculation_id": "calc_shared",
                "metric_key": "contract_count",
                "business_name": "合同数量",
                "business_explanation": "按合同实例去重统计。",
                "value_type": "integer",
                "display_unit": "",
            },
            {
                "calculation_id": "calc_shared",
                "metric_key": "contract_count_in_chart",
                "business_name": "合同数量（图表）",
                "business_explanation": "",
                "value_type": "integer",
                "display_unit": "个",
            },
        ]
    }

    canonical = PostAnalysisEvidenceCompiler._canonicalize_planner_payload(payload)

    assert len(canonical["metrics"]) == 1
    assert canonical["metrics"][0]["calculation_id"] == "calc_shared"
    assert canonical["metrics"][0]["business_name"] == "合同数量"
    assert canonical["metrics"][0]["display_unit"] == "个"


def test_report_string_concatenation_preserves_numeric_expression_lineage():
    sql = "SELECT COUNT(*) AS contract_count FROM contracts"
    code = r'''
sql = "SELECT COUNT(*) AS contract_count FROM contracts"
data = load_data_with_sql(sql=sql)
contract_count = int(data["contract_count"].iloc[0])
ratio = contract_count / 200
analysis_report = "合同总数 **" + str(contract_count) + "** 个，占比 " + f"{ratio:.1%}" + "。"
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="合同总数 **143** 个，占比 71.5%。",
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["contract_count"])},
    )

    lineage = LineageIndexer().build(snapshot)
    report_outputs = [
        item for item in lineage["output_nodes"] if item["artifact_type"] == "report"
    ]

    assert [item["text"] for item in report_outputs] == ["143", "71.5"]
    assert all(item["direct_operation_node_ids"] for item in report_outputs)


def test_grouped_chart_and_table_reuse_boundary_while_total_is_distinct():
    sql = "SELECT month, region, SUM(amount) AS sales_amount FROM sales GROUP BY month, region"
    code = r'''
sql = "SELECT month, region, SUM(amount) AS sales_amount FROM sales GROUP BY month, region"
data = load_data_with_sql(sql=sql)
sales_by_month = data.groupby(["month", "region"])["sales_amount"].sum().reset_index()
total_sales = sales_by_month["sales_amount"].sum()
analysis_report = f"Total sales: {total_sales:,.2f} CNY."
charts = [build_chart_result(data=sales_by_month, chart_kind="line", category_field="month", value_field="sales_amount")]
tables = [build_table_result(title="Sales", columns=[{"key": "sales_amount", "label": "Sales"}], rows=sales_by_month.to_dict("records"))]
result = save_analysis_result(analysis_report=analysis_report, charts=charts, tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Total sales: 1,280.50 CNY.",
        charts=[{
            "title": "Sales trend",
            "chart_document": {
                "dataset": {"rows": [{"month": "2026-01", "sales_amount": 1280.5}]},
                "encoding": {"category_field": "month", "value_field": "sales_amount"},
            },
        }],
        tables=[{
            "title": "Sales",
            "columns": [{"key": "sales_amount", "label": "Sales"}],
            "rows": [{"sales_amount": 1280.5}],
        }],
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["month", "region", "sales_amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1
    assert len(bundle["table_trace_refs"]) == 1
    assert len(bundle["report_trace_refs"]) == 1
    assert bundle["diagnostics"]["structured_output_slot_count"] == 2
    assert bundle["diagnostics"]["structured_output_bound_count"] == 2
    assert bundle["diagnostics"]["unresolved_structured_output_node_ids"] == []
    assert bundle["chart_trace_refs"][0]["evidence_key"] == bundle["table_trace_refs"][0]["evidence_key"]
    assert bundle["report_trace_refs"][0]["evidence_key"] != bundle["chart_trace_refs"][0]["evidence_key"]


def test_unit_bearing_table_values_compile_one_column_level_evidence_ref():
    sql = "SELECT contract_name, subscription_cycle, expiration_date FROM contracts"
    code = r'''
sql = "SELECT contract_name, subscription_cycle, expiration_date FROM contracts"
data = load_data_with_sql(sql=sql)
cycle_counts = data["subscription_cycle"].value_counts().reset_index()
cycle_counts.columns = ["subscription_cycle", "contract_count"]
most_common_cycle = cycle_counts.iloc[0]["subscription_cycle"]
detail_df = data[["contract_name", "subscription_cycle", "expiration_date"]].copy()
detail_df = detail_df.rename(columns={
    "contract_name": "合同名称",
    "subscription_cycle": "订阅周期",
    "expiration_date": "到期日期",
})
tables = []
tables.append({
    "title": "Contract details",
    "table": {
        "columns": [
            {"key": "合同名称", "label": "Contract"},
            {"key": "订阅周期", "label": "Subscription duration"},
            {"key": "到期日期", "label": "Expiration date"},
        ],
        "rows": detail_df.head(20).to_dict("records"),
    },
})
result = save_analysis_result(analysis_report="Contract details.", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Contract details.",
        tables=[{
            "title": "Contract details",
            "columns": [
                {"key": "合同名称", "label": "Contract"},
                {"key": "订阅周期", "label": "Subscription duration"},
                {"key": "到期日期", "label": "Expiration date"},
            ],
            "rows": [
                {"合同名称": "A", "订阅周期": "455天", "到期日期": "2027-10-01"},
                {"合同名称": "B", "订阅周期": "365天", "到期日期": "2027-07-03"},
            ],
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(
                sql,
                columns=["contract_name", "subscription_cycle", "expiration_date"],
            ),
        },
    )
    seen_payload: dict = {}

    lineage = LineageIndexer().build(snapshot)
    table_output = next(
        item for item in lineage["output_nodes"]
        if item["artifact_type"] == "table"
    )
    operation_by_id = {
        item["operation_node_id"]: item for item in lineage["operation_nodes"]
    }
    assert table_output["source_frame"] == "detail_df"
    assert {
        operation_by_id[item]["kind"]
        for item in table_output["boundary_operation_node_ids"]
    } == {"query_projection"}

    def plan(payload: dict) -> EvidencePlan:
        seen_payload.update(payload)
        metrics = []
        for item in payload["calculation_candidates"]:
            output = item["outputs"][0]
            metrics.append(PlannedMetric(
                calculation_id=item["calculation_id"],
                metric_key="subscription_duration",
                business_name="订阅周期",
                business_explanation="展示合同记录中的订阅周期。",
                value_type="duration",
                display_unit=output.get("display_unit_hint") or "",
            ))
        return EvidencePlan(metrics=metrics)

    bundle = PostAnalysisEvidenceCompiler(planner=plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert bundle["table_trace_refs"] == [{
        "table_index": 0,
        "artifact_id": 0,
        "column_key": "订阅周期",
        "row_scope": "all",
        "evidence_key": bundle["table_trace_refs"][0]["evidence_key"],
        "display_unit": "天",
    }]
    assert bundle["diagnostics"]["structured_output_slot_count"] == 1
    assert seen_payload["calculation_candidates"][0]["outputs"][0]["display_unit_hint"] == "天"


def test_unit_bearing_grouping_dimension_does_not_become_table_metric():
    sql = "SELECT subscription_cycle FROM contracts"
    code = r'''
sql = "SELECT subscription_cycle FROM contracts"
data = load_data_with_sql(sql=sql)
cycle_dist = data["subscription_cycle"].value_counts().reset_index(name="contract_count")
tables = [build_table_result(
    title="Cycle distribution",
    columns=[
        {"key": "subscription_cycle", "label": "Cycle"},
        {"key": "contract_count", "label": "Contracts"},
    ],
    rows=cycle_dist.to_dict("records"),
)]
result = save_analysis_result(analysis_report="Cycle distribution.", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Cycle distribution.",
        tables=[{
            "title": "Cycle distribution",
            "columns": [
                {"key": "subscription_cycle", "label": "Cycle"},
                {"key": "contract_count", "label": "Contracts"},
            ],
            "rows": [
                {"subscription_cycle": "1年", "contract_count": 60},
                {"subscription_cycle": "3年", "contract_count": 20},
            ],
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["subscription_cycle"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["column_key"] for item in bundle["table_trace_refs"]] == ["contract_count"]


def test_same_named_chart_values_keep_distinct_grouping_boundaries():
    sql = "SELECT status, cycle FROM contracts"
    code = r'''
sql = "SELECT status, cycle FROM contracts"
data = load_data_with_sql(sql=sql)
status_dist = data.groupby("status").size().reset_index(name="instance_count")
cycle_dist = data.groupby("cycle").size().reset_index(name="instance_count")
chart_status = build_chart_result(data=status_dist, chart_kind="pie", category_field="status", value_field="instance_count")
chart_cycle = build_chart_result(data=cycle_dist, chart_kind="bar", category_field="cycle", value_field="instance_count")
result = save_analysis_result(analysis_report="合同结构。", charts=[chart_status, chart_cycle], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="合同结构。",
        charts=[
            {
                "title": "状态分布",
                "chart_document": {
                    "dataset": {"rows": [{"status": "有效", "instance_count": 12}]},
                    "encoding": {"category_field": "status", "value_field": "instance_count"},
                },
            },
            {
                "title": "周期分布",
                "chart_document": {
                    "dataset": {"rows": [{"cycle": "1年", "instance_count": 8}]},
                    "encoding": {"category_field": "cycle", "value_field": "instance_count"},
                },
            },
        ],
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["status", "cycle"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 2
    assert len(bundle["evidence_definitions"]) == 2
    assert bundle["chart_trace_refs"][0]["evidence_key"] != bundle["chart_trace_refs"][1]["evidence_key"]


def test_multi_metric_chart_fields_keep_distinct_evidence_boundaries():
    sql = "SELECT month, SUM(amount) AS amount, SUM(contract_count) AS contract_count FROM sales GROUP BY month"
    code = r'''
sql = "SELECT month, SUM(amount) AS amount, SUM(contract_count) AS contract_count FROM sales GROUP BY month"
data = load_data_with_sql(sql=sql)
charts = [build_multi_metric_chart_result(
    data=data,
    chart_kind="line",
    category_field="month",
    value_fields=["amount", "contract_count"],
    title="Monthly performance",
)]
result = save_analysis_result(analysis_report="Monthly performance", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Monthly performance",
        charts=[{
            "title": "Monthly performance",
            "chart_document": {
                "dataset": {"rows": [{"month": "2026-01", "amount": 1280.5, "contract_count": 12}]},
                "encoding": {
                    "category_field": "month",
                    "value_field": "amount",
                    "value_fields": ["amount", "contract_count"],
                },
            },
        }],
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["month", "amount", "contract_count"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    refs = {item["value_field"]: item["evidence_key"] for item in bundle["chart_trace_refs"]}
    assert refs.keys() == {"amount", "contract_count"}
    assert refs["amount"] != refs["contract_count"]


def test_report_template_slots_bind_percentage_and_amount_to_distinct_boundaries():
    sql = (
        "SELECT SUM(CASE WHEN year = 2026 THEN amount ELSE 0 END) AS current_amount, "
        "SUM(CASE WHEN year = 2025 THEN amount ELSE 0 END) AS prior_amount FROM revenue"
    )
    code = r'''
sql = "SELECT SUM(CASE WHEN year = 2026 THEN amount ELSE 0 END) AS current_amount, SUM(CASE WHEN year = 2025 THEN amount ELSE 0 END) AS prior_amount FROM revenue"
data = load_data_with_sql(sql=sql)
current_amount = data.iloc[0]["current_amount"]
prior_amount = data.iloc[0]["prior_amount"]
yoy_rate = (current_amount - prior_amount) / prior_amount * 100
amount_change = current_amount - prior_amount
analysis_report = (
    f"Current revenue is {current_amount / 10000:,.2f} ten-thousand CNY; "
    f"prior revenue is {prior_amount / 10000:,.2f} ten-thousand CNY; "
    f"decline is {abs(yoy_rate):.2f}%, or {amount_change / 10000:,.2f} ten-thousand CNY."
)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "Current revenue is 200.00 ten-thousand CNY; prior revenue is 500.00 ten-thousand CNY; "
        "decline is 60.00%, or -300.00 ten-thousand CNY."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_revenue": _query_snapshot(sql, columns=["current_amount", "prior_amount"])},
    )

    captured = {}

    def planner(payload: dict) -> EvidencePlan:
        captured.update(payload)
        return _semantic_plan(payload)

    bundle = PostAnalysisEvidenceCompiler(planner=planner).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 4
    refs = {item["text"]: item["evidence_key"] for item in bundle["report_trace_refs"]}
    assert refs["60.00"] != refs["-300.00"]
    assert refs["200.00"] != refs["500.00"]
    outputs = [
        output
        for candidate in captured["calculation_candidates"]
        for output in candidate["outputs"]
    ]
    amount_output = next(output for output in outputs if output["field"] == "amount_change / 10000")
    assert "ten-thousand CNY" in amount_output["display_context"]["suffix"]
    assert "decline is 60.00%" in amount_output["display_context"]["rendered_context"]
    amount_candidate = next(
        candidate
        for candidate in captured["calculation_candidates"]
        if amount_output in candidate["outputs"]
    )
    assert any("amount_change" in expression for expression in amount_candidate["calculation_expressions"])


def test_multiline_report_template_allows_saved_outer_whitespace_normalization():
    sql = "SELECT SUM(amount) AS amount FROM sales"
    code = r'''
sql = "SELECT SUM(amount) AS amount FROM sales"
data = load_data_with_sql(sql=sql)
amount = data.iloc[0]["amount"]
analysis_report = f"""## Summary

Total amount: {amount / 10000:,.2f} ten-thousand CNY.
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\n\nTotal amount: 128.05 ten-thousand CNY.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["128.05"]


def test_dynamic_precision_format_does_not_merge_nested_fstring_templates():
    sql = "SELECT SUM(amount) AS current_amount, SUM(prior_amount) AS prior_amount FROM revenue"
    code = r'''
sql = "SELECT SUM(amount) AS current_amount, SUM(prior_amount) AS prior_amount FROM revenue"
data = load_data_with_sql(sql=sql)
current_amount = data.iloc[0]["current_amount"]
prior_amount = data.iloc[0]["prior_amount"]
precision = 2
analysis_report = f"""## Revenue

Current: {current_amount / 10000:,.{precision}f}; prior: {prior_amount / 10000:,.{precision}f}.
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Revenue\n\nCurrent: 200.00; prior: 500.00.",
        query_snapshots={
            "q_revenue": _query_snapshot(
                sql,
                columns=["current_amount", "prior_amount"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == [
        "200.00",
        "500.00",
    ]


def test_preformatted_percentage_is_traced_but_numeric_dimension_code_is_not():
    sql = "SELECT current_amount, prior_amount, category_code FROM revenue_summary"
    code = r'''
sql = "SELECT current_amount, prior_amount, category_code FROM revenue_summary"
data = load_data_with_sql(sql=sql)
current_amount = data.iloc[0]["current_amount"]
prior_amount = data.iloc[0]["prior_amount"]
category_code = data.iloc[0]["category_code"]
rate = (current_amount - prior_amount) / prior_amount
rate_text = f"{rate:+.1%}"
analysis_report = f"Year-on-year: {rate_text}; category code: {category_code}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Year-on-year: -58.1%; category code: -4634325740024215503.",
        query_snapshots={
            "q_revenue": _query_snapshot(
                sql,
                columns=["current_amount", "prior_amount", "category_code"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["-58.1"]


def test_dynamic_table_columns_keep_current_and_prior_boundaries_distinct():
    sql = "SELECT current_amount, prior_amount FROM revenue_summary"
    code = r'''
sql = "SELECT current_amount, prior_amount FROM revenue_summary"
data = load_data_with_sql(sql=sql)
year = 2026
table_data = data[["current_amount", "prior_amount"]].copy()
table_data["current_amount"] = table_data["current_amount"].apply(lambda value: f"{value / 10000:,.2f}")
table_data["prior_amount"] = table_data["prior_amount"].apply(lambda value: f"{value / 10000:,.2f}")
table_data.columns = [f"{year} amount", f"{year - 1} amount"]
tables = [build_table_result(
    title="Revenue comparison",
    columns=[
        {"key": f"{year} amount", "label": f"{year} amount"},
        {"key": f"{year - 1} amount", "label": f"{year - 1} amount"},
    ],
    rows=table_data.to_dict("records"),
)]
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Revenue comparison",
            "columns": [
                {"key": "2026 amount", "label": "2026 amount"},
                {"key": "2025 amount", "label": "2025 amount"},
            ],
            "rows": [{"2026 amount": "200.00", "2025 amount": "500.00"}],
        }],
        query_snapshots={"q_revenue": _query_snapshot(sql, columns=["current_amount", "prior_amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 2
    assert len({item["evidence_key"] for item in bundle["table_trace_refs"]}) == 2


def test_static_sql_expression_binds_runtime_snapshot_to_dataframe():
    code = r'''
year = get_analysis_parameters().get("year", 2026)
sql = f"SELECT month, SUM(amount) AS revenue FROM revenue WHERE year = {year} GROUP BY month"
data = load_data_with_sql(sql=sql)
monthly_total = data["revenue"].sum()
analysis_report = f"Revenue is {monthly_total:,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    sql = "SELECT month, SUM(amount) AS revenue FROM revenue WHERE year = 2026 GROUP BY month"
    snapshot = _snapshot(
        code=code,
        report="Revenue is 1,200.00.",
        query_snapshots={"q_monthly": _query_snapshot(sql, columns=["month", "revenue"])},
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "report")

    assert output["candidate_query_node_ids"] == ["q_monthly"]


def test_local_file_uses_execution_schema_as_source_field_lineage():
    path = "C:/data/sales.csv"
    code = r'''
data = load_local_file("C:/data/sales.csv")
total_amount = data["amount"].sum()
analysis_report = f"Total amount: {total_amount:,.2f}."
charts = [build_chart_result(data=data, chart_kind="bar", category_field="department", value_field="amount")]
result = save_analysis_result(analysis_report=analysis_report, charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total amount: 1,280.50.",
        charts=[{
            "title": "Department amount",
            "chart_data_contract": {
                "encoding": {"category_field": "department", "value_field": "amount"},
            },
        }],
        query_snapshots={"q_file": _loader_snapshot(path, columns=["department", "amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1
    assert len(bundle["report_trace_refs"]) == 1
    assert all(item["query_refs"] == ["q_file"] for item in bundle["evidence_definitions"])


def test_timeseries_select_star_uses_actual_output_columns_for_lineage():
    sql = "SELECT * FROM ts_query('vxbase', 'SELECT * FROM sensor WHERE ts >= now()-1d')"
    code = r'''
sql = "SELECT * FROM ts_query('vxbase', 'SELECT * FROM sensor WHERE ts >= now()-1d')"
data = load_data_with_sql(sql=sql)
average_temperature = data["temperature"].mean()
analysis_report = f"Average temperature: {average_temperature:.2f} C."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Average temperature: 26.50 C.",
        query_snapshots={"q_ts": _query_snapshot(sql, columns=["ts", "quality", "temperature"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 1
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_ts"]


def test_sql_aggregates_in_one_query_are_distinct_evidence_boundaries():
    sql = "SELECT SUM(amount) AS sales_amount, COUNT(*) AS contract_count FROM sales"
    code = r'''
sql = "SELECT SUM(amount) AS sales_amount, COUNT(*) AS contract_count FROM sales"
data = load_data_with_sql(sql=sql)
sales_amount = data.iloc[0]["sales_amount"]
contract_count = data.iloc[0]["contract_count"]
analysis_report = f"Sales amount: {sales_amount:,.2f}. Contract count: {contract_count}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50. Contract count: 12.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["sales_amount", "contract_count"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["evidence_definitions"]) == 2
    assert len(bundle["report_trace_refs"]) == 2
    assert bundle["report_trace_refs"][0]["evidence_key"] != bundle["report_trace_refs"][1]["evidence_key"]


def test_group_by_dimension_in_report_is_not_compiled_as_metric_evidence():
    sql = "SELECT month, SUM(amount) AS revenue FROM sales GROUP BY month"
    code = r'''
sql = "SELECT month, SUM(amount) AS revenue FROM sales GROUP BY month"
data = load_data_with_sql(sql=sql)
report_sections = []
for _, row in data.iterrows():
    report_sections.append(f"Month {int(row['month'])}: revenue {row['revenue']:,.2f}.")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Month 1: revenue 100.00.\nMonth 2: revenue 200.00.",
        query_snapshots={"q_monthly": _query_snapshot(sql, columns=["month", "revenue"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["100.00", "200.00"]
    assert len(bundle["evidence_definitions"]) == 1


def test_string_join_is_not_described_as_a_relational_data_join():
    sql = "SELECT department, SUM(amount) AS amount FROM sales GROUP BY department"
    code = r'''
sql = "SELECT department, SUM(amount) AS amount FROM sales GROUP BY department"
data = load_data_with_sql(sql=sql)
departments = data["department"].tolist()
total_amount = data["amount"].sum()
department_text = ", ".join(departments)
analysis_report = f"Departments: {department_text}. Total amount: {total_amount:,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Departments: Sales, Service. Total amount: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["department", "amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    transformations = bundle["evidence_definitions"][0]["transformations"]
    assert "按已确认的业务关联关系合并相关数据。" not in transformations


def test_table_display_label_uses_declared_lineage_field_for_renamed_metric():
    sql = "SELECT department, month, unit FROM contracts"
    code = r'''
sql = "SELECT department, month, unit FROM contracts"
data = load_data_with_sql(sql=sql)
summary = data.groupby(["department", "month"]).agg({"unit": "sum"}).reset_index()
summary.columns = ["部门", "月份", "平台套数"]
detail = summary[["月份", "部门", "平台套数"]].copy()
detail.columns = ["月份", "部门", "平台套数(套)"]
tables = [build_table_result(
    title="Department details",
    columns=[
        {"key": "月份", "label": "月份"},
        {"key": "部门", "label": "部门"},
        {"key": "平台套数(套)", "label": "平台套数(套)"},
    ],
    rows=detail.to_dict("records"),
)]
result = save_analysis_result(analysis_report="Department details.", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Department details.",
        tables=[{
            "title": "Department details",
            "columns": [
                {"key": "月份", "label": "月份"},
                {"key": "部门", "label": "部门"},
                {"key": "平台套数(套)", "label": "平台套数(套)"},
            ],
            "rows": [{"月份": "2026-01", "部门": "Sales", "平台套数(套)": 8}],
        }],
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["department", "month", "unit"])},
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item.get("field") == "平台套数(套)")

    assert output["candidate_query_node_ids"] == ["q_contracts"]
    assert output["boundary_operation_node_ids"]


def test_numeric_looking_string_dimension_is_not_a_report_metric():
    code = r'''
best_row = data.loc[data["amount"].idxmax()]
analysis_report = f"Best department: {best_row['department']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Best department: 5313416643652174324.",
        query_snapshots={"q_departments": _query_snapshot("SELECT department, amount FROM departments")},
    )
    snapshot = AnalysisExecutionSnapshot(**{
        **snapshot.__dict__,
        "selected_datasource_snapshot": [{
            "metadata_schema": {"properties": {"department": {"type": "string"}}},
        }],
    })

    lineage = LineageIndexer().build(snapshot)

    assert not [item for item in lineage["output_nodes"] if item["artifact_type"] == "report"]


def test_heterogeneous_chart_value_column_is_not_bound_to_one_evidence():
    sql = "SELECT SUM(amount) AS amount, SUM(profit) AS profit, COUNT(*) AS contract_count FROM contracts"
    code = r'''
sql = "SELECT SUM(amount) AS amount, SUM(profit) AS profit, COUNT(*) AS contract_count FROM contracts"
data = load_data_with_sql(sql=sql)
chart_data = pd.DataFrame({
    "metric": ["amount", "profit", "contract count"],
    "value": [data.iloc[0]["amount"], data.iloc[0]["profit"], data.iloc[0]["contract_count"]],
})
charts = [build_chart_result(data=chart_data, chart_kind="bar", category_field="metric", value_field="value")]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Key metrics",
            "chart_data_contract": {"encoding": {"category_field": "metric", "value_field": "value"}},
        }],
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["amount", "profit", "contract_count"])},
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is True
    assert bundle["chart_trace_refs"] == []
    assert bundle["status"] == "partial"


def test_categorical_counts_share_one_column_level_evidence_boundary():
    sql = "SELECT contract_no, status FROM contracts"
    code = r'''
sql = "SELECT contract_no, status FROM contracts"
data = load_data_with_sql(sql=sql)
active_contracts = len(data[data["status"] == "active"])
expired_contracts = len(data[data["status"] == "expired"])
chart_data = pd.DataFrame({
    "status_label": ["Active", "Expired"],
    "count": [active_contracts, expired_contracts],
})
charts = [build_chart_result(data=chart_data, chart_kind="pie", category_field="status_label", value_field="count")]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Contract status",
            "chart_data_contract": {"encoding": {"category_field": "status_label", "value_field": "count"}},
        }],
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["contract_no", "status"])},
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert len(output["boundary_operation_node_ids"]) == 2
    assert len(bundle["chart_trace_refs"]) == 1
    assert len(bundle["evidence_definitions"]) == 1


def test_value_counts_reset_index_chart_keeps_column_level_evidence_boundary():
    sql = "SELECT status, customer FROM contracts"
    code = r'''
sql = "SELECT status, customer FROM contracts"
data = load_data_with_sql(sql=sql)
status_counts = data["status"].value_counts()
status_data = status_counts.reset_index()
status_data.columns = ["status_label", "count"]
charts = [build_chart_result(
    data=status_data,
    chart_kind="pie",
    category_field="status_label",
    value_field="count",
)]
result = save_analysis_result(analysis_report="Status distribution.", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Status distribution.",
        charts=[{
            "title": "Contract status",
            "chart_document": {
                "chart_kind": "pie",
                "dataset": {"rows": [
                    {"status_label": "Active", "count": 8},
                    {"status_label": "Expired", "count": 2},
                ]},
                "encoding": {
                    "category_field": "status_label",
                    "value_field": "count",
                },
            },
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "customer"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1
    assert len(bundle["evidence_definitions"]) == 1
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_contracts"]


def test_skipped_conditional_chart_does_not_drop_other_chart_lineage():
    sql = "SELECT status, customer FROM contracts"
    code = r'''
sql = "SELECT status, customer FROM contracts"
data = load_data_with_sql(sql=sql)
status_counts = data["status"].value_counts().reset_index(name="count")
customer_counts = data["customer"].value_counts().reset_index(name="count")
empty_months = data[data["status"] == "never"]["status"].value_counts().reset_index(name="count")
charts = []
if not status_counts.empty:
    charts.append(build_chart_result(data=status_counts, title="Status", chart_kind="pie", category_field="status", value_field="count"))
if not empty_months.empty:
    charts.append(build_chart_result(data=empty_months, title="Months", chart_kind="line", category_field="status", value_field="count"))
if not customer_counts.empty:
    charts.append(build_chart_result(data=customer_counts, title="Customers", chart_kind="bar", category_field="customer", value_field="count"))
result = save_analysis_result(analysis_report="Summary.", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Summary.",
        charts=[
            {
                "title": "Status",
                "chart_document": {
                    "chart_kind": "pie",
                    "dataset": {"rows": [{"status": "Active", "count": 8}]},
                    "encoding": {"category_field": "status", "value_field": "count"},
                },
            },
            {
                "title": "Customers",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [{"customer": "A", "count": 6}]},
                    "encoding": {"category_field": "customer", "value_field": "count"},
                },
            },
        ],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "customer"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 2


def test_inline_report_ratio_is_compiled_as_one_formula_boundary():
    sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
data = load_data_with_sql(sql=sql)
total_count = data["contract_count"].sum()
active_row = data[data["status"] == "active"]
active_count = int(active_row["contract_count"].iloc[0]) if not active_row.empty else 0
analysis_report = f"Active share: {active_count/total_count*100:.1f}%."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Active share: 80.0%.",
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["status", "contract_count"])},
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "report")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert len(output["boundary_operation_node_ids"]) == 1
    boundary = next(
        item for item in lineage["operation_nodes"]
        if item["operation_node_id"] == output["boundary_operation_node_ids"][0]
    )
    assert boundary["semantic_kind"] == "formula"
    assert len(bundle["report_trace_refs"]) == 1


def test_multiline_report_keeps_ast_numeric_slots_when_dynamic_prose_changes_lines():
    sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
data = load_data_with_sql(sql=sql)
total_count = int(data["contract_count"].sum())
active_count = int(data.loc[data["status"] == "active", "contract_count"].sum())
active_share = active_count / total_count * 100 if total_count else 0
dynamic_observation = "Active contracts dominate.\nThe remaining contracts need review."
analysis_report = f"""## Overview
There are {total_count} contracts in total.

## Status
There are {active_count} active contracts, representing {active_share:.1f}%.
{dynamic_observation}
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = """## Overview
There are 10 contracts in total.

## Status
There are 8 active contracts, representing 80.0%.
Active contracts dominate.
The remaining contracts need review.
"""
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "contract_count"]),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    report_values = {
        item["text"] for item in lineage["output_nodes"]
        if item["artifact_type"] == "report"
    }
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert {"10", "8", "80.0"}.issubset(report_values)
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 3


def test_multiline_report_local_slot_keeps_percent_format_suffix():
    sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
data = load_data_with_sql(sql=sql)
total_count = int(data["contract_count"].sum())
active_count = int(data.loc[data["status"] == "active", "contract_count"].sum())
active_share = active_count / total_count if total_count else 0
dynamic_observation = "Active contracts dominate.\nReview the remainder."
analysis_report = f"""## Overview
There are {total_count} contracts.
{dynamic_observation}
Active share is {active_share:.1%}.
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Overview\nThere are 10 contracts.\nActive contracts dominate.\nReview the remainder.\nActive share is 80.0%.\n",
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "contract_count"]),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    report_values = {
        item["text"] for item in lineage["output_nodes"]
        if item["artifact_type"] == "report"
    }

    assert {"10", "80.0"}.issubset(report_values)


def test_dataframe_record_with_dynamic_column_names_keeps_positional_lineage():
    sql = "SELECT expiration_year, COUNT(*) AS contract_count FROM contracts GROUP BY expiration_year"
    code = r'''
sql = "SELECT expiration_year, COUNT(*) AS contract_count FROM contracts GROUP BY expiration_year"
data = load_data_with_sql(sql=sql)
current_year = 2026
this_year_count = int(data.loc[data["expiration_year"] == current_year, "contract_count"].sum())
last_year_count = int(data.loc[data["expiration_year"] == current_year - 1, "contract_count"].sum())
summary = pd.DataFrame([{
    f"{current_year} contracts": this_year_count,
    f"{current_year - 1} contracts": last_year_count,
}])
tables = [build_table_result(
    title="Year summary",
    columns=[
        {"key": f"{current_year} contracts", "label": f"{current_year} contracts"},
        {"key": f"{current_year - 1} contracts", "label": f"{current_year - 1} contracts"},
    ],
    rows=summary.to_dict("records"),
)]
result = save_analysis_result(analysis_report="Year summary.", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Year summary.",
        tables=[{
            "title": "Year summary",
            "columns": [
                {"key": "2026 contracts", "label": "2026 contracts"},
                {"key": "2025 contracts", "label": "2025 contracts"},
            ],
            "rows": [{"2026 contracts": 49, "2025 contracts": 0}],
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["expiration_year", "contract_count"]),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    table_outputs = [item for item in lineage["output_nodes"] if item["artifact_type"] == "table"]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert len(table_outputs) == 2
    assert all(item["boundary_operation_node_ids"] for item in table_outputs)
    assert len(bundle["table_trace_refs"]) == 2


def test_filtered_frame_count_keeps_lineage_in_dynamic_table_column():
    sql = "SELECT expiration_year FROM contracts"
    code = r'''
sql = "SELECT expiration_year FROM contracts"
data = load_data_with_sql(sql=sql)
current_year = 2026
current_contracts = data[data["expiration_year"] == current_year]
current_count = len(current_contracts)
summary = pd.DataFrame([{f"{current_year} contracts": current_count}])
tables = [build_table_result(
    title="Current year",
    columns=[{"key": f"{current_year} contracts", "label": "Current year contracts"}],
    rows=summary.to_dict("records"),
)]
result = save_analysis_result(analysis_report="Current-year contracts.", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Current-year contracts.",
        tables=[{
            "title": "Current year",
            "columns": [{"key": "2026 contracts", "label": "Current year contracts"}],
            "rows": [{"2026 contracts": 49}],
        }],
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["expiration_year"])},
    )

    lineage = LineageIndexer().build(snapshot)
    table_output = next(
        item for item in lineage["output_nodes"]
        if item["artifact_type"] == "table"
    )
    operation_by_id = {
        item["operation_node_id"]: item for item in lineage["operation_nodes"]
    }
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert {
        operation_by_id[item]["source"]
        for item in table_output["boundary_operation_node_ids"]
    } == {"current_count = len(current_contracts)"}
    assert len(bundle["table_trace_refs"]) == 1


def test_planner_failure_never_guesses_evidence():
    sql = "SELECT SUM(amount) AS sales_amount FROM sales"
    code = r'''
sql = "SELECT SUM(amount) AS sales_amount FROM sales"
data = load_data_with_sql(sql=sql)
sales_amount = data.iloc[0]["sales_amount"]
analysis_report = f"Sales amount: {sales_amount:,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["sales_amount"])},
    )

    def unavailable(_payload: dict) -> EvidencePlan:
        raise TimeoutError("planner timeout")

    bundle = PostAnalysisEvidenceCompiler(planner=unavailable).compile(snapshot)

    assert bundle["status"] == "partial"
    assert bundle["evidence_definitions"] == []
    assert bundle["report_trace_refs"] == []
    assert bundle["diagnostics"]["reason"] == "planner_unavailable"


def test_compiled_multi_series_chart_selection_resolves_current_value():
    service = InsightNsConversationService(None)
    artifact = {
        "id": 20,
        "artifact_type": "chart",
        "content_json": {
            "chart_spec": {
                "xAxis": {"type": "category", "data": ["Jan", "Feb"]},
                "series": [
                    {"name": "Current", "data": [100, 200]},
                    {"name": "Prior", "data": [80, 160]},
                ],
            },
            "chart_data_contract": {
                "encoding": {
                    "category_field": "month",
                    "value_field": "current_amount",
                    "value_fields": ["current_amount", "prior_amount"],
                },
            },
        },
    }

    context = service._build_selected_context(
        artifact,
        {"data_index": 1, "series_index": 1, "value_field": "prior_amount"},
    )

    assert context["raw_value"] == 160
    assert context["display_value"] == "160"
    assert context["dimensions"] == {"month": "Feb", "series_name": "Prior"}


def test_compiled_pie_chart_selection_resolves_named_point():
    service = InsightNsConversationService(None)
    artifact = {
        "id": 21,
        "artifact_type": "chart",
        "content_json": {
            "chart_spec": {
                "series": [{
                    "name": "Revenue source",
                    "data": [{"name": "Direct", "value": 1200}, {"name": "Channel", "value": 800}],
                }],
            },
            "chart_data_contract": {
                "encoding": {
                    "category_field": "source",
                    "value_field": "revenue",
                    "value_fields": ["revenue"],
                },
            },
        },
    }

    context = service._build_selected_context(
        artifact,
        {"data_index": 0, "value_field": "revenue"},
    )

    assert context["raw_value"] == 1200
    assert context["dimensions"] == {"source": "Direct", "series_name": "Revenue source"}


def test_list_built_report_keeps_repeated_tables_in_their_own_lineage_regions():
    sql = "SELECT status, customer, expiry_year FROM contracts"
    code = r'''
sql = "SELECT status, customer, expiry_year FROM contracts"
data = load_data_with_sql(sql=sql)
total = len(data)
status_dist = data["status"].value_counts().reset_index()
status_dist.columns = ["status", "count"]
status_dist["ratio"] = status_dist["count"] / total * 100
status_map = {}
for _, row in status_dist.iterrows():
    status_map[row["status"]] = row["count"]
active_count = status_map.get("active", 0)
expired_count = status_map.get("expired", 0)
active_ratio = active_count / total * 100
customer_dist = data["customer"].value_counts().reset_index()
customer_dist.columns = ["customer", "count"]
customer_dist["ratio"] = customer_dist["count"] / total * 100
top_customers = customer_dist.head(2)
expiry_dist = data.groupby("expiry_year").size().reset_index()
expiry_dist.columns = ["year", "count"]
report_parts = []
report_parts.append("## Summary\n")
report_parts.append(f"Active: {active_count}; expired: {expired_count}.\n")
report_parts.append("## Status\n| status | count | ratio |\n")
for _, row in status_dist.iterrows():
    report_parts.append(f"| {row['status']} | {row['count']} | {row['ratio']:.1f}% |\n")
report_parts.append("## Customers\n| rank | customer | count | ratio |\n")
for rank, (_, row) in enumerate(top_customers.iterrows(), 1):
    report_parts.append(f"| {rank} | {row['customer']} | {row['count']} | {row['ratio']:.1f}% |\n")
report_parts.append("## Expiry\n| year | count |\n")
for _, row in expiry_dist.iterrows():
    report_parts.append(f"| {int(row['year'])} | {row['count']} |\n")
report_parts.append("Health: {:.1f}%.\n".format(active_ratio))
analysis_report = "".join(report_parts)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Summary\nActive: 8; expired: 2.\n"
        "## Status\n| status | count | ratio |\n"
        "| active | 8 | 80.0% |\n| expired | 2 | 20.0% |\n"
        "## Customers\n| rank | customer | count | ratio |\n"
        "| 1 | Alpha | 7 | 70.0% |\n| 2 | Beta | 3 | 30.0% |\n"
        "## Expiry\n| year | count |\n"
        "| 2026 | 6 |\n| 2027 | 4 |\n"
        "Health: 80.0%."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_contracts": _query_snapshot(
                sql,
                columns=["status", "customer", "expiry_year"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs_by_start = {item["start"]: item for item in bundle["report_trace_refs"]}

    def ref_for(fragment: str, numeric_text: str, occurrence: int = 0):
        fragment_start = report.index(fragment)
        numeric_start = report.index(numeric_text, fragment_start)
        if occurrence:
            for _ in range(occurrence):
                numeric_start = report.index(numeric_text, numeric_start + 1)
        return refs_by_start[numeric_start]

    active_summary = ref_for("Active:", "8")
    active_row = ref_for("| active", "8")
    customer_row = ref_for("| 1 | Alpha", "7")
    expiry_row = ref_for("| 2026", "6", occurrence=1)
    assert active_summary["evidence_key"]
    assert active_row["evidence_key"]
    assert customer_row["evidence_key"] != active_row["evidence_key"]
    assert expiry_row["evidence_key"] != active_row["evidence_key"]
    assert ref_for("Health:", "80.0")["evidence_key"]
    assert report.index("2026") not in refs_by_start
    assert report.index("1 | Alpha") not in refs_by_start


def test_report_list_initializers_and_extend_calls_are_declared_report_slots():
    sql = "SELECT SUM(amount) AS amount, COUNT(*) AS record_count FROM sales"
    code = r'''
sql = "SELECT SUM(amount) AS amount, COUNT(*) AS record_count FROM sales"
data = load_data_with_sql(sql=sql)
amount = data.iloc[0]["amount"]
record_count = data.iloc[0]["record_count"]
report_sections = [
    "## Summary",
    f"Amount: {amount:,.2f}.",
]
report_sections.extend([
    "## Coverage",
    f"Records: {record_count}.",
])
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\nAmount: 1,280.50.\n## Coverage\nRecords: 12.",
        query_snapshots={
            "q_sales": _query_snapshot(sql, columns=["amount", "record_count"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["1,280.50", "12"]


def test_report_embedded_conditional_fragments_keep_their_numeric_slots():
    sql = "SELECT status FROM contracts"
    code = r'''
sql = "SELECT status FROM contracts"
data = load_data_with_sql(sql=sql)
total = len(data)
active = (data["status"] == "active").sum()
expired = (data["status"] == "expired").sum()
active_rate = active / total * 100 if total else 0
if active_rate >= 90:
    status_finding = f"Healthy: {active} active ({active_rate:.1f}%), {expired} expired."
else:
    status_finding = f"Attention: {active} active ({active_rate:.1f}%), {expired} expired."
analysis_report = f"""## Summary
Total: {total}.
## Finding
{status_finding}
## Restatement
Total contracts: {total}.
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Summary\nTotal: 100.\n"
        "## Finding\nHealthy: 95 active (95.0%), 5 expired.\n"
        "## Restatement\nTotal contracts: 100.\n"
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["status"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs_by_start = {item["start"]: item for item in bundle["report_trace_refs"]}
    expected_positions = [
        report.index("100"),
        report.index("95 active"),
        report.index("95.0"),
        report.index("5 expired"),
        report.rindex("100"),
    ]
    assert all(position in refs_by_start for position in expected_positions)
    assert len(bundle["report_trace_refs"]) == len(expected_positions)
