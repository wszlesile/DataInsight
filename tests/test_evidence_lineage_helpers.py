import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.invoker import _build_expected_time_range_from_user_query  # noqa: E402
from agent.tools import (  # noqa: E402
    CURRENT_LAST_QUERY_KEY,
    CURRENT_QUERY_SNAPSHOTS,
    StructuredResult,
    _begin_query_snapshot_execution,
    _build_execution_namespace,
    _check_time_range_constraint,
    _resolve_requested_enum_values,
    build_markdown_table,
    build_table_result,
    load_local_file,
    load_local_file_low_memory,
    request_retry,
    save_analysis_result,
    validate_query_constraints,
)


def test_markdown_table_accepts_business_header_labels():
    table = build_markdown_table(
        [{"department": "Sales", "amount_fmt": "1,200.00"}],
        columns=[
            {"key": "department", "label": "Department"},
            {"key": "amount_fmt", "label": "Contract amount"},
        ],
    )

    assert table.splitlines()[0] == "| Department | Contract amount |"
    assert "amount_fmt" not in table


def test_formal_runtime_does_not_expose_evidence_generation_contract():
    result_fields = set(StructuredResult.model_fields)
    assert not result_fields.intersection({
        "evidence_definitions",
        "chart_trace_refs",
        "table_trace_refs",
        "report_trace_refs",
        "table_trace_metadata",
    })
    assert not set(inspect.signature(save_analysis_result).parameters).intersection({
        "evidence_definitions",
        "chart_trace_refs",
        "table_trace_refs",
        "report_trace_refs",
        "table_trace_metadata",
        "metric_evidence_bindings",
    })
    namespace = _build_execution_namespace()
    assert "define_evidence" not in namespace
    assert "build_report_result" not in namespace


def test_legacy_evidence_retry_type_is_not_a_runtime_protocol():
    retry = request_retry(
        retry_type="invalid_report_trace_ref",
        message="legacy",
    )
    assert retry.retry_type == "contract_error"


def test_build_table_result_only_returns_display_artifact():
    result = build_table_result(
        title="区域完工率",
        columns=[
            {"key": "region", "label": "区域", "role": "dimension"},
            {"key": "completion_rate", "label": "完工率", "role": "metric", "unit": "%"},
        ],
        rows=[{"region": "华东", "completion_rate": 76.3}],
    )

    assert result == {"table": {
        "title": "区域完工率",
        "description": "",
        "columns": [
            {"key": "region", "label": "区域"},
            {"key": "completion_rate", "label": "完工率"},
        ],
        "rows": [{"region": "华东", "completion_rate": 76.3}],
    }}

    normalized = save_analysis_result(
        analysis_report="ok",
        charts=[],
        tables=[result],
    )
    assert normalized.tables == [{
        "title": "区域完工率",
        "description": "",
        "columns": [
            {"key": "region", "label": "区域"},
            {"key": "completion_rate", "label": "完工率"},
        ],
        "rows": [{"region": "华东", "completion_rate": 76.3}],
    }]


def test_build_table_result_accepts_records_data():
    result = build_table_result(
        title="区域完工率",
        data=[{"region": "华东", "completion_rate": 76.3}],
        columns=[
            {"key": "region", "label": "区域"},
            {"key": "completion_rate", "label": "完工率"},
        ],
    )

    assert result["table"]["rows"] == [{"region": "华东", "completion_rate": 76.3}]


def test_build_table_result_rejects_metric_rows_mixed_into_period_columns():
    with pytest.raises(ValueError, match="table_semantic_error"):
        build_table_result(
            title="月度指标",
            columns=[
                {"key": "指标", "label": "指标"},
                {"key": "department", "label": "部门"},
                {"key": "2026-01", "label": "2026-01"},
            ],
            rows=[
                {"指标": "合同额", "department": "销售", "2026-01": 1200.0},
                {"指标": "平台套数", "department": "销售", "2026-01": 8},
            ],
        )


def test_requested_enum_values_resolve_only_unique_high_confidence_matches():
    resolutions = _resolve_requested_enum_values(
        ["宁波团队", "运用管理部", "未知部门"],
        ["宁波团队", "运营管理部", "业务发展总部", "生态发展总部"],
    )

    assert resolutions[0]["status"] == "exact"
    assert resolutions[0]["observed_value"] == "宁波团队"
    assert resolutions[1]["status"] == "similar"
    assert resolutions[1]["observed_value"] == "运营管理部"
    assert resolutions[2]["status"] == "unresolved"


def test_save_analysis_result_keeps_backend_query_snapshots_only():
    CURRENT_QUERY_SNAPSHOTS.set({})
    retry = validate_query_constraints(
        query_key="q_completion",
        query_constraints={"time_range": {}, "target_filters": [], "aggregation": {"grain": "month"}},
        sql="select month, sum(v) from t group by month",
        aggregation_hints=["按月份聚合"],
    )

    assert retry is None
    result = save_analysis_result(
        analysis_report="## 分析报告\n\n- 完工率 76.3%。",
        charts=[],
        tables=[],
    )
    assert set(result.query_snapshots) == {"q_completion"}
    assert "evidence" not in result.model_dump_json().lower()


def test_parameterized_query_constraints_validate_bound_time_range():
    CURRENT_QUERY_SNAPSHOTS.set({})
    retry = validate_query_constraints(
        query_key="q_2026_revenue",
        query_constraints={
            "time_range": {
                "start": "2026-01-01",
                "end": "2027-01-01",
                "end_exclusive": True,
            },
            "target_filters": [],
            "aggregation": {"grain": "year", "metric": "revenue"},
        },
        sql="SELECT SUM(amount) FROM revenue WHERE revenue_time >= %s AND revenue_time < %s",
        query_params=["2026-01-01", "2027-01-01"],
        aggregation_hints=["year", "revenue"],
    )

    assert retry is None
    snapshot = CURRENT_QUERY_SNAPSHOTS.get()["q_2026_revenue"]
    assert snapshot["implementation"]["query_params"] == [
        "2026-01-01",
        "2027-01-01",
    ]


def test_query_constraints_accept_exact_multi_value_filter():
    retry = validate_query_constraints(
        query_key="q_departments",
        query_constraints={
            "time_range": {},
            "target_filters": [{
                "semantic_role": "department_filter",
                "field_hint": "department_name",
                "value": ["运营管理部", "生态发展总部"],
                "match_mode": "exact",
            }],
            "aggregation": {"grain": "month"},
        },
        sql=(
            "SELECT department_name, month, SUM(amount) FROM revenue "
            "WHERE department_name IN (%s, %s) GROUP BY department_name, month"
        ),
        query_params=["运营管理部", "生态发展总部"],
        aggregation_hints=["month", "department_name"],
    )

    assert retry is None


def test_named_sql_time_parameters_are_recognized_without_literal_dates():
    retry = validate_query_constraints(
        query_key="q_named_time_range",
        query_constraints={
            "time_range": {
                "start": "2026-01-01",
                "end": "2027-01-01",
                "end_exclusive": True,
            },
            "target_filters": [],
            "aggregation": {"grain": "year", "metric": "revenue"},
        },
        sql=(
            "SELECT SUM(amount) AS total_amount FROM revenue "
            "WHERE revenue_time >= :start_date AND revenue_time < :end_date"
        ),
        aggregation_hints=["year", "revenue"],
    )

    assert retry is None


def test_each_physical_sql_gets_an_independent_execution_snapshot():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    first_sql = "select sum(amount) as total_amount from sales"
    second_sql = "select department, sum(amount) as amount from sales group by department"
    assert validate_query_constraints(
        query_key="q_sales",
        query_constraints={"time_range": {}, "target_filters": [], "aggregation": {"grain": "overall"}},
        sql=first_sql,
    ) is None

    _begin_query_snapshot_execution(sql=first_sql, params={"month": 6})
    _begin_query_snapshot_execution(sql=second_sql, params={"month": 6})

    snapshots = CURRENT_QUERY_SNAPSHOTS.get()
    assert len(snapshots) == 2
    assert snapshots["q_sales"]["implementation"]["sql"] == first_sql
    derived = next(value for key, value in snapshots.items() if key != "q_sales")
    assert derived["implementation"]["sql"] == second_sql
    assert derived["implementation"]["scope_source_query_key"] == "q_sales"
    assert derived["query_constraints"] == snapshots["q_sales"]["query_constraints"]


def test_local_file_loader_records_a_physical_access_snapshot(tmp_path):
    file_path = tmp_path / "sales.csv"
    file_path.write_text("department,amount\nSales,12.5\n", encoding="utf-8")
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")

    data = load_local_file(str(file_path))

    snapshots = CURRENT_QUERY_SNAPSHOTS.get()
    assert len(snapshots) == 1
    query_key, snapshot = next(iter(snapshots.items()))
    assert snapshot["implementation"]["loader"] == "load_local_file"
    assert snapshot["implementation"]["datasource_identifier"] == str(file_path)
    assert snapshot["execution"]["status"] == "success"
    assert data.attrs["data_insight_query_key"] == query_key


def test_low_memory_local_loader_finishes_snapshot_after_iteration(tmp_path):
    file_path = tmp_path / "sales.csv"
    file_path.write_text("department,amount\nSales,12.5\nService,8.0\n", encoding="utf-8")
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")

    chunks = list(load_local_file_low_memory(str(file_path), chunk_size=1))

    query_key, snapshot = next(iter(CURRENT_QUERY_SNAPSHOTS.get().items()))
    assert snapshot["implementation"]["loader"] == "load_local_file_low_memory"
    assert snapshot["execution"]["status"] == "success"
    assert snapshot["execution"]["row_count"] == 2
    assert snapshot["execution"]["columns"] == ["department", "amount"]
    assert all(chunk.attrs["data_insight_query_key"] == query_key for chunk in chunks)


def test_expected_time_range_prefers_anchor_month_over_year():
    result = _build_expected_time_range_from_user_query("今年3月各产品成本和利润分析")

    assert result["start"].endswith("-03-01")
    assert result["end"].endswith("-04-01")
    assert result["source"] == "今年3月"


def test_month_grain_year_filter_satisfies_daily_time_range():
    query_constraints = {
        "time_range": {"start": "2026-01-01", "end": "2027-01-01", "end_exclusive": True}
    }

    assert _check_time_range_constraint(
        query_constraints,
        "cost_data['年月'] >= '2026-01'\ncost_data['年月'] < '2027-01'",
    ) is None
    assert _check_time_range_constraint(
        query_constraints,
        "cost_data['年月'].str.startswith('2026')",
    ) is None


def test_separate_year_month_columns_satisfy_exact_month_range():
    query_constraints = {
        "time_range": {"start": "2026-01-01", "end": "2026-08-01", "end_exclusive": True}
    }

    assert _check_time_range_constraint(
        query_constraints,
        """
        SELECT SUM(amount)
        FROM sales
        WHERE WM_EXTRA_TS_paid_at_YEAR = 2026
          AND WM_EXTRA_TS_paid_at_MONTH BETWEEN 1 AND 7
        """,
    ) is None


def test_separate_year_month_columns_do_not_satisfy_incomplete_month_range():
    query_constraints = {
        "time_range": {"start": "2026-01-01", "end": "2026-08-01", "end_exclusive": True}
    }

    issue = _check_time_range_constraint(
        query_constraints,
        """
        SELECT SUM(amount)
        FROM sales
        WHERE WM_EXTRA_TS_paid_at_YEAR = 2026
          AND WM_EXTRA_TS_paid_at_MONTH BETWEEN 2 AND 7
        """,
    )

    assert issue is not None


def test_relational_year_column_satisfies_full_year_time_range():
    query_constraints = {
        "time_range": {"start": "2026-01-01", "end": "2027-01-01", "end_exclusive": True}
    }

    assert _check_time_range_constraint(
        query_constraints,
        "SELECT SUM(amount) FROM sales WHERE WM_EXTRA_TS_paid_at_YEAR = 2026",
    ) is None
    assert _check_time_range_constraint(
        query_constraints,
        "filtered = data[data['paid_at_year'] == 2026]",
    ) is None


def test_relational_year_column_does_not_satisfy_a_different_year_range():
    query_constraints = {
        "time_range": {"start": "2026-01-01", "end": "2027-01-01", "end_exclusive": True}
    }

    issue = _check_time_range_constraint(
        query_constraints,
        "SELECT SUM(amount) FROM sales WHERE WM_EXTRA_TS_paid_at_YEAR = 2025",
    )

    assert issue is not None
    assert issue["diagnostics"]["missing_parts"] == ["start", "end"]
