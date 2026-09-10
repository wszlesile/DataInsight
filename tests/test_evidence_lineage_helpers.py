import inspect
import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent.invoker as agent_invoker  # noqa: E402
from agent.invoker import _build_expected_time_range_from_user_query  # noqa: E402
from agent.tools import (  # noqa: E402
    CURRENT_ANALYSIS_PARAMETERS_OVERRIDE,
    CURRENT_EXECUTION_INTENT,
    CURRENT_EXPECTED_TIME_RANGE,
    CURRENT_LAST_QUERY_KEY,
    CURRENT_QUERY_SNAPSHOTS,
    CURRENT_TIME_RANGE_GUARD_ACTIVE,
    StructuredResult,
    _begin_query_snapshot_execution,
    _mark_latest_query_snapshot_execution,
    _build_execution_namespace,
    _check_time_range_constraint,
    _extract_sql_projection_semantics,
    _validate_generated_code_contract,
    _resolve_requested_enum_values,
    build_markdown_table,
    build_table_result,
    get_analysis_parameters,
    load_local_file,
    load_local_file_low_memory,
    load_data_with_sql,
    normalize_relationship_key,
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


def test_markdown_table_accepts_explicit_value_precision():
    table = build_markdown_table(
        [{"metric": "毛利率", "value": 71.4849}],
        columns=[
            {"key": "metric", "label": "指标"},
            {"key": "value", "label": "数值"},
        ],
        value_precision=3,
    )

    assert "| 毛利率 | 71.485 |" in table


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


def test_request_retry_normalizes_numpy_diagnostics_to_native_json_scalars():
    import numpy as np

    retry = request_retry(
        retry_type="probe_feedback",
        message="枚举探测完成",
        diagnostics={
            "total_rows": np.int64(143),
            "ratio": np.float64(0.25),
            "values": [np.int32(4)],
        },
    )

    assert retry.diagnostics["total_rows"] == 143
    assert isinstance(retry.diagnostics["total_rows"], int)
    assert retry.diagnostics["ratio"] == 0.25
    assert retry.diagnostics["values"] == [4]


def test_probe_feedback_marks_time_coverage_as_diagnostic_only():
    token = CURRENT_EXPECTED_TIME_RANGE.set({})
    try:
        retry = request_retry(
            retry_type="probe_feedback",
            message="已确认真实时间覆盖范围",
            diagnostics={"actual_time_coverage": {"start": "2026-02-01", "end": "2033-06-21"}},
            repair_instructions=[
                "下一版代码应基于实际时间覆盖范围设置 time_range",
                "修正状态枚举后重新查询",
            ],
        )
    finally:
        CURRENT_EXPECTED_TIME_RANGE.reset(token)

    assert retry.repair_instructions == [
        "修正状态枚举后重新查询",
        *retry.repair_instructions[1:],
    ]
    assert all("设置 time_range" not in item for item in retry.repair_instructions)
    assert retry.diagnostics["_probe_contract"]["time_coverage_role"] == "diagnostic_only"
    assert retry.diagnostics["_probe_contract"]["path_scope_role"] == "observed_paths_only"
    assert "unprobed selected datasources" in retry.diagnostics["_probe_contract"][
        "formal_scope_policy"
    ]
    assert retry.diagnostics["_probe_contract"]["explicit_user_time_range"] is None


def test_open_overall_relational_analysis_allows_disclosed_analysis_window():
    intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set({})
    override_token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({})
    try:
        retry = validate_query_constraints(
            query_key="q_contracts",
            query_constraints={
                "time_range": {"start": "2026-01-01", "end": "2027-01-01", "end_exclusive": True},
                "target_filters": [],
                "aggregation": {"grain": "overall", "metrics": ["contract_count"]},
            },
            sql=(
                "SELECT COUNT(*) AS contract_count FROM contracts "
                "WHERE expiration_time >= '2026-01-01' AND expiration_time < '2027-01-01'"
            ),
        )
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(override_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert retry is None


def test_open_detail_baseline_allows_disclosed_analysis_window():
    intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set({})
    override_token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({})
    try:
        retry = validate_query_constraints(
            query_key="q_contract_details",
            query_constraints={
                "time_range": {"start": "2025-01-01", "end": "2026-08-03", "end_exclusive": True},
                "target_filters": [],
                "aggregation": {"grain": "detail", "metric": "annual_fee_contracts"},
            },
            sql=(
                "SELECT id, status, expiration_time FROM contracts "
                "WHERE expiration_time >= '2025-01-01' AND expiration_time < '2026-08-03'"
            ),
        )
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(override_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert retry is None


def test_open_grouped_trend_can_use_an_observation_window():
    intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set({})
    override_token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({})
    try:
        retry = validate_query_constraints(
            query_key="q_contract_trend",
            query_constraints={
                "time_range": {"start": "2026-01-01", "end": "2027-01-01", "end_exclusive": True},
                "target_filters": [],
                "aggregation": {"grain": "month", "metrics": ["contract_count"]},
            },
            sql=(
                "SELECT DATE_TRUNC('month', expiration_time) AS expiration_month, "
                "COUNT(*) AS contract_count FROM contracts "
                "WHERE expiration_time >= '2026-01-01' AND expiration_time < '2027-01-01' "
                "GROUP BY expiration_month"
            ),
            aggregation_hints=["按月份聚合"],
        )
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(override_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert retry is None


def test_inferred_numeric_projection_only_converts_returned_columns(monkeypatch):
    monkeypatch.setattr(
        "agent.tools._load_data_with_fed_query",
        lambda **kwargs: pd.DataFrame([{"id": "1"}]),
    )
    intent_token = CURRENT_EXECUTION_INTENT.set("probe")
    try:
        data = load_data_with_sql(
            "SELECT id, WM_EXTRA_TS_expiration_time_YEAR FROM contracts"
        )
    finally:
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert list(data.columns) == ["id"]


def test_numeric_case_aggregate_alias_is_restored_without_explicit_hint(monkeypatch):
    monkeypatch.setattr(
        "agent.tools._load_data_with_fed_query",
        lambda **kwargs: pd.DataFrame([
            {"batch_id": "B-1", "has_oos": "0"},
            {"batch_id": "B-2", "has_oos": "1"},
        ]),
    )
    intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
    try:
        data = load_data_with_sql(
            "SELECT batch_id, MAX(CASE WHEN quality_status = 'oos' "
            "THEN 1 ELSE 0 END) AS has_oos FROM quality GROUP BY batch_id"
        )
    finally:
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert data["has_oos"].tolist() == [0, 1]
    assert data["has_oos"].sum() == 1


def test_timestamp_difference_arithmetic_is_numeric_without_explicit_hint(monkeypatch):
    monkeypatch.setattr(
        "agent.tools._load_data_with_fed_query",
        lambda **kwargs: pd.DataFrame([
            {"batch_id": "B-1", "run_hours": "5.016667"},
            {"batch_id": "B-2", "run_hours": "4.433333"},
        ]),
    )
    intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
    try:
        data = load_data_with_sql(
            "SELECT batch_id, "
            "TIMESTAMPDIFF(SECOND, batch_start_time, batch_end_time) / 3600.0 "
            "AS run_hours FROM batches"
        )
    finally:
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert data["run_hours"].sum() == pytest.approx(9.45)


def test_build_table_result_accepts_column_oriented_dict():
    result = build_table_result(
        title="质量概览",
        data={
            "一次合格率": [0.9032],
            "OOS批次率": [0.0417],
        },
    )

    assert result["table"]["columns"] == [
        {"key": "一次合格率", "label": "一次合格率"},
        {"key": "OOS批次率", "label": "OOS批次率"},
    ]
    assert result["table"]["rows"] == [{
        "一次合格率": 0.9032,
        "OOS批次率": 0.0417,
    }]


def test_explicit_numeric_projection_still_rejects_missing_result_column(monkeypatch):
    monkeypatch.setattr(
        "agent.tools._load_data_with_fed_query",
        lambda **kwargs: pd.DataFrame([{"id": "1"}]),
    )
    intent_token = CURRENT_EXECUTION_INTENT.set("probe")
    try:
        with pytest.raises(ValueError, match="numeric_columns references unavailable"):
            load_data_with_sql(
                "SELECT id FROM contracts",
                numeric_columns=["missing_amount"],
            )
    finally:
        CURRENT_EXECUTION_INTENT.reset(intent_token)


def test_text_projection_preserves_large_identifier_before_normalization(monkeypatch):
    from decimal import Decimal

    monkeypatch.setattr(
        "agent.tools._load_data_with_fed_query",
        lambda **kwargs: pd.DataFrame([{
            "status_id": Decimal("166832699355251625.00"),
        }]),
    )
    intent_token = CURRENT_EXECUTION_INTENT.set("probe")
    try:
        data = load_data_with_sql(
            "SELECT status_id FROM contracts",
            text_columns=["status_id"],
        )
    finally:
        CURRENT_EXECUTION_INTENT.reset(intent_token)

    assert data.iloc[0]["status_id"] == "166832699355251625.00"
    assert normalize_relationship_key(data["status_id"]).tolist() == [
        "166832699355251625"
    ]


def test_probe_may_reuse_query_validation_without_becoming_formal_analysis():
    code = '''
execution_intent = "probe"
sql = "SELECT status, COUNT(*) AS count FROM contracts GROUP BY status"
query_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "overall"},
}
validation = validate_query_constraints(query_constraints=query_constraints, sql=sql)
data = load_data_with_sql(sql=sql)
result = request_retry(
    retry_type="probe_feedback",
    message="status values observed",
    diagnostics={"values": data.to_dict("records")},
)
'''

    assert _validate_generated_code_contract(code) is None


def test_sql_projection_semantics_preserve_aggregate_boundaries_before_snapshot_clipping():
    projections = _extract_sql_projection_semantics(
        "SELECT product_id, AVG(alarm_count) AS avg_alarm_count "
        "FROM events GROUP BY product_id"
    )

    assert projections == [
        {
            "alias": "product_id",
            "semantic_kind": "projection",
            "projection_role": "dimension",
            "expression": "product_id",
        },
        {
            "alias": "avg_alarm_count",
            "semantic_kind": "aggregation",
            "projection_role": "measure",
            "expression": "AVG(alarm_count)",
        },
    ]


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


def test_save_analysis_result_removes_only_empty_chart_shells_and_empty_list_items():
    result = save_analysis_result(
        analysis_report="## Findings\n-\n- A retained finding",
        charts=[
            {
                "title": "Empty branch",
                "chart_type": "echarts",
                "chart_document": {
                    "dataset": {"columns": ["category", "count"], "rows": []},
                    "chart_kind": "bar",
                    "encoding": {"category_field": "category", "value_field": "count"},
                },
            },
            {
                "title": "Zero is data",
                "chart_type": "echarts",
                "chart_spec": {"series": [{"type": "bar", "data": [0]}]},
            },
        ],
        tables=[],
    )

    assert result.analysis_report == "## Findings\n- A retained finding"
    assert [chart["title"] for chart in result.charts] == ["Zero is data"]


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


def test_build_table_result_applies_display_precision_to_finite_decimal_values():
    result = build_table_result(
        title="Metrics",
        rows=[{
            "name": "A",
            "amount": 12.3456,
            "count": 7,
            "active": True,
            "missing": float("nan"),
        }],
        value_precision=2,
    )

    row = result["table"]["rows"][0]
    assert row["amount"] == 12.35
    assert row["count"] == 7
    assert row["active"] is True
    assert math.isnan(row["missing"])


@pytest.mark.parametrize("value_precision", [-1, 11, 1.5, True])
def test_build_table_result_rejects_invalid_display_precision(value_precision):
    with pytest.raises(ValueError, match="value_precision"):
        build_table_result(title="Metrics", rows=[], value_precision=value_precision)


def test_save_analysis_result_merges_nested_table_presentation_metadata():
    normalized = save_analysis_result(
        analysis_report="ok",
        charts=[],
        tables=[{
            "title": "年费合同明细",
            "description": "展示需要关注的合同。",
            "table": {
                "columns": [{"key": "subscription_cycle", "label": "订阅周期"}],
                "rows": [{"subscription_cycle": "455天"}],
            },
        }],
    )

    assert normalized.tables == [{
        "title": "年费合同明细",
        "description": "展示需要关注的合同。",
        "columns": [{"key": "subscription_cycle", "label": "订阅周期"}],
        "rows": [{"subscription_cycle": "455天"}],
    }]


def test_build_table_result_keeps_metric_rows_mixed_into_period_columns():
    result = build_table_result(
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

    assert result["table"]["rows"] == [
        {"指标": "合同额", "department": "销售", "2026-01": 1200.0},
        {"指标": "平台套数", "department": "销售", "2026-01": 8},
    ]


def test_build_table_result_keeps_formatted_metric_rows_in_generic_columns():
    result = build_table_result(
        title="质量指标汇总",
        columns=[
            {"key": "指标", "label": "指标"},
            {"key": "数值", "label": "比率/数值"},
            {"key": "数量", "label": "分子/分母"},
        ],
        rows=[
            {"指标": "一次合格率", "数值": "0.9735", "数量": "2021/2076"},
            {"指标": "OOS批次率", "数值": "0.0414", "数量": "31/748"},
        ],
    )

    assert result["table"]["rows"][1]["指标"] == "OOS批次率"


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


def test_derived_query_inherits_parent_analysis_scope_through_result_binding():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    constraints = {
        "time_range": {
            "start": "2026-08-24T00:00:00+08:00",
            "end": "2026-08-31T00:00:00+08:00",
            "end_exclusive": True,
        },
        "target_filters": [],
        "aggregation": {"grain": "batch", "metric": "count"},
    }
    parent_sql = (
        "SELECT batch_id FROM quality_result "
        "WHERE test_time >= %s AND test_time < %s"
    )
    assert validate_query_constraints(
        query_key="q_risk_batches",
        query_constraints=constraints,
        sql=parent_sql,
        query_params=[
            "2026-08-24T00:00:00+08:00",
            "2026-08-31T00:00:00+08:00",
        ],
        aggregation_hints=["batch"],
    ) is None
    _mark_latest_query_snapshot_execution(
        query_key="q_risk_batches",
        status="success",
        columns=["batch_id"],
        row_count=2,
    )

    child_sql = "SELECT batch_id, alarm_flag FROM process_alarm WHERE batch_id IN (%s, %s)"
    assert validate_query_constraints(
        query_key="q_risk_batch_alarms",
        query_constraints=constraints,
        sql=child_sql,
        query_params=["B001", "B002"],
        aggregation_hints=["batch"],
        scope_source_query_key="q_risk_batches",
        scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
    ) is None

    child = CURRENT_QUERY_SNAPSHOTS.get()["q_risk_batch_alarms"]
    assert child["analysis_scope"] == CURRENT_QUERY_SNAPSHOTS.get()["q_risk_batches"]["analysis_scope"]
    assert child["query_scope"]["scope_source_query_key"] == "q_risk_batches"
    assert child["query_scope"]["scope_binding"] == {
        "source_field": "batch_id",
        "target_field": "batch_id",
    }


def test_derived_query_rejects_unexecuted_or_unbound_parent_scope():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    constraints = {
        "time_range": {"start": "2026-08-24", "end": "2026-08-31", "end_exclusive": True},
        "target_filters": [],
        "aggregation": {"grain": "batch", "metric": "count"},
    }
    retry = validate_query_constraints(
        query_key="q_child",
        query_constraints=constraints,
        sql="SELECT batch_id FROM alarms WHERE batch_id IN (%s)",
        query_params=["B001"],
        aggregation_hints=["batch"],
        scope_source_query_key="q_missing_parent",
        scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
    )

    assert retry is not None
    assert retry.retry_type == "invalid_query_constraints"
    assert retry.diagnostics["violated_constraint"] == "derived_query_scope"
    assert retry.diagnostics["reason"] == "source_query_not_found"


def test_derived_query_inherits_explicit_time_range_from_parent_scope():
    expected = {
        "start": "2026-08-24",
        "end": "2026-08-31",
        "end_exclusive": True,
    }
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set(expected)
    guard_token = CURRENT_TIME_RANGE_GUARD_ACTIVE.set(True)
    try:
        parent_constraints = {
            "time_range": expected,
            "target_filters": [],
            "aggregation": {"grain": "batch", "metric": "count"},
        }
        assert validate_query_constraints(
            query_key="q_risk_batches",
            query_constraints=parent_constraints,
            sql=(
                "SELECT batch_id FROM quality_result "
                "WHERE test_time >= %s AND test_time < %s"
            ),
            query_params=[expected["start"], expected["end"]],
            aggregation_hints=["batch"],
        ) is None
        _mark_latest_query_snapshot_execution(
            query_key="q_risk_batches",
            status="success",
            columns=["batch_id"],
            row_count=2,
        )

        retry = validate_query_constraints(
            query_key="q_risk_batch_alarms",
            query_constraints={
                "time_range": None,
                "target_filters": [],
                "aggregation": {"grain": "batch", "metric": "alarm_count"},
            },
            sql="SELECT batch_id FROM alarms WHERE batch_id IN (%s, %s)",
            query_params=["B001", "B002"],
            aggregation_hints=["batch", "alarm_count"],
            scope_source_query_key="q_risk_batches",
            scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
        )

        assert retry is None
        child = CURRENT_QUERY_SNAPSHOTS.get()["q_risk_batch_alarms"]
        assert child["analysis_scope"] == CURRENT_QUERY_SNAPSHOTS.get()["q_risk_batches"]["analysis_scope"]
    finally:
        CURRENT_TIME_RANGE_GUARD_ACTIVE.reset(guard_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)


def test_explicit_time_guard_allows_unscoped_reference_lookup():
    expected = {
        "start": "2026-08-24",
        "end": "2026-08-31",
        "end_exclusive": True,
    }
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set(expected)
    guard_token = CURRENT_TIME_RANGE_GUARD_ACTIVE.set(True)
    try:
        retry = validate_query_constraints(
            query_key="q_product_master",
            query_constraints={
                "time_range": None,
                "target_filters": [],
                "aggregation": None,
            },
            sql="SELECT product_id, product_name FROM product_master",
        )
    finally:
        CURRENT_TIME_RANGE_GUARD_ACTIVE.reset(guard_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)

    assert retry is None


def test_explicit_time_guard_still_rejects_unscoped_fact_query():
    expected = {
        "start": "2026-08-24",
        "end": "2026-08-31",
        "end_exclusive": True,
    }
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set(expected)
    guard_token = CURRENT_TIME_RANGE_GUARD_ACTIVE.set(True)
    try:
        retry = validate_query_constraints(
            query_key="q_events",
            query_constraints={
                "time_range": None,
                "target_filters": [],
                "aggregation": {"grain": "event", "metric": "count"},
            },
            sql="SELECT event_id, event_time FROM events",
        )
    finally:
        CURRENT_TIME_RANGE_GUARD_ACTIVE.reset(guard_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)

    assert retry is not None
    assert retry.retry_type == "query_constraint_violation"


def test_time_range_retry_explains_independent_and_parent_derived_scope_paths():
    retry = validate_query_constraints(
        query_key="q_batch_events",
        query_constraints={
            "time_range": {
                "start": "2026-08-24",
                "end": "2026-08-31",
                "end_exclusive": True,
            },
            "target_filters": [],
            "aggregation": {"grain": "batch", "metric": "count"},
        },
        sql="SELECT batch_id, event_type FROM child_events WHERE batch_id IN (%s)",
        query_params=["B001"],
        aggregation_hints=["batch"],
    )

    assert retry is not None
    assert retry.retry_type == "query_constraint_violation"
    assert retry.diagnostics["violated_constraint"] == "time_range"
    instructions = "\n".join(retry.repair_instructions)
    assert "scope_source_query_key" in instructions
    assert "scope_binding" in instructions
    assert "%s" in instructions


def test_query_constraints_reject_fedquery_scalar_cte_references():
    retry = validate_query_constraints(
        query_key="q_quality",
        query_constraints={
            "time_range": {},
            "target_filters": [],
            "aggregation": {"grain": "overall"},
        },
        sql=(
            "WITH batches AS (SELECT batch_id FROM quality), "
            "failed AS (SELECT batch_id FROM batches WHERE batch_id IS NULL) "
            "SELECT (SELECT COUNT(*) FROM batches) AS total_batches, "
            "(SELECT COUNT(*) FROM failed) AS failed_batches"
        ),
    )

    assert retry is not None
    assert retry.diagnostics["violated_constraint"] == "fedquery_scalar_cte_reference"
    assert retry.diagnostics["cte_aliases"] == ["batches", "failed"]


def test_query_constraints_allow_main_from_and_chained_ctes():
    retry = validate_query_constraints(
        query_key="q_quality",
        query_constraints={
            "time_range": {},
            "target_filters": [],
            "aggregation": {"grain": "overall"},
        },
        sql=(
            "WITH batches AS (SELECT batch_id FROM quality), "
            "batch_counts AS (SELECT COUNT(*) AS total_batches FROM batches), "
            "failed_counts AS (SELECT COUNT(*) AS failed_batches FROM batches WHERE batch_id IS NULL) "
            "SELECT total_batches, failed_batches FROM batch_counts CROSS JOIN failed_counts"
        ),
    )

    assert retry is None


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
    assert derived["implementation"]["logical_query_family"] == derived["query_key"]
    assert derived["query_constraints"] == {}
    assert derived["analysis_scope"] == snapshots["q_sales"]["analysis_scope"]
    assert derived["query_scope"] == {
        "scope_status": "implicit",
        "query_key": derived["query_key"],
        "logical_query_family": derived["query_key"],
    }


def test_time_series_execution_snapshot_records_inner_vxbase_datasource():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    identifier = "_7_d_3f745e57719dd764736b9429f6d664db"
    inner_sql = (
        f"SELECT name, ts, status, avg(value) FROM {identifier} "
        'WHERE ts >= "2026-01-01T00:00:00+08:00" interval("1d", "10s")'
    )
    sql = f"SELECT * FROM ts_query('vxbase', '{inner_sql}')"

    _begin_query_snapshot_execution(sql=sql)

    snapshot = next(iter(CURRENT_QUERY_SNAPSHOTS.get().values()))
    assert snapshot["implementation"]["datasource_identifier"] == identifier
    assert snapshot["implementation"]["datasource_identifiers"] == [identifier]


def test_time_series_execution_snapshot_records_quoted_timescale_datasource():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    identifier = "Factory.Metric$Value"
    inner_sql = f'SELECT "timeStamp", value FROM "{identifier}"'
    sql = f"SELECT * FROM ts_query('timescale', '{inner_sql}')"

    _begin_query_snapshot_execution(sql=sql)

    snapshot = next(iter(CURRENT_QUERY_SNAPSHOTS.get().values()))
    assert snapshot["implementation"]["datasource_identifier"] == identifier
    assert snapshot["implementation"]["datasource_identifiers"] == [identifier]


def test_paginated_sql_executions_share_one_implicit_logical_query_family():
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set("")
    first_page = "SELECT batch_id, alarm_count FROM alarms ORDER BY batch_id LIMIT 4000 OFFSET 0"
    second_page = "SELECT batch_id, alarm_count FROM alarms ORDER BY batch_id LIMIT 4000 OFFSET 4000"

    _begin_query_snapshot_execution(sql=first_page)
    first_key = CURRENT_LAST_QUERY_KEY.get()
    _mark_latest_query_snapshot_execution(
        status="success",
        row_count=4000,
        columns=["batch_id", "alarm_count"],
    )
    _begin_query_snapshot_execution(sql=second_page)
    second_key = CURRENT_LAST_QUERY_KEY.get()
    _mark_latest_query_snapshot_execution(
        status="success",
        row_count=125,
        columns=["batch_id", "alarm_count"],
    )

    snapshots = CURRENT_QUERY_SNAPSHOTS.get()
    assert first_key == second_key
    assert len(snapshots) == 1
    family = snapshots[first_key]
    assert family["execution"]["physical_execution_count"] == 2
    assert family["execution"]["family_row_count"] == 4125
    assert len(family["implementation"]["physical_executions"]) == 1
    assert family["query_scope"]["scope_status"] == "implicit"


def test_query_snapshot_separates_analysis_scope_from_query_aggregation():
    CURRENT_QUERY_SNAPSHOTS.set({})
    retry = validate_query_constraints(
        query_key="q_root_causes",
        query_constraints={
            "time_range": {"start": "2026-01-01", "end": "2026-02-01", "end_exclusive": True},
            "target_filters": [{"semantic_role": "batch", "value": "B-1", "match_mode": "exact"}],
            "aggregation": {"grain": "root_cause", "metric": "fault_event_count"},
        },
        sql=(
            "SELECT fault_event_count FROM root_causes "
            "WHERE observed_at >= %s AND observed_at < %s"
        ),
        query_params=["2026-01-01", "2026-02-01"],
    )

    assert retry is None
    snapshot = CURRENT_QUERY_SNAPSHOTS.get()["q_root_causes"]
    assert snapshot["analysis_scope"] == {
        "time_range": {"start": "2026-01-01", "end": "2026-02-01", "end_exclusive": True},
        "target_filters": [{"semantic_role": "batch", "value": "B-1", "match_mode": "exact"}],
    }
    assert snapshot["query_scope"] == {
        "scope_status": "explicit",
        "query_key": "q_root_causes",
        "aggregation": {"grain": "root_cause", "metric": "fault_event_count"},
    }


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


def test_analysis_parameters_preserve_calendar_dates_despite_expected_timezone():
    expected_token = CURRENT_EXPECTED_TIME_RANGE.set({
        "start": "2026-07-01",
        "end": "2026-08-01",
        "timezone_name": "Asia/Shanghai",
    })
    override_token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({})
    try:
        parameters = get_analysis_parameters({
            "time_range": {
                "start": "2026-07-01",
                "end": "2026-08-01",
                "end_exclusive": True,
            },
            "top_n": 10,
        })
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(override_token)
        CURRENT_EXPECTED_TIME_RANGE.reset(expected_token)

    assert parameters["time_range"] == {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "end_exclusive": True,
    }
    assert parameters["top_n"] == 10


@pytest.mark.parametrize("bounds", [
    {"start": "2026-07-01", "end": "2026-08-01", "end_exclusive": True},
    {"start": "2026-07-01T00:00:00+08:00", "end": "2026-07-01T01:00:00+08:00"},
    {"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T01:00:00Z"},
    {"start": datetime(2026, 7, 1), "end": datetime(2026, 8, 1)},
])
def test_analysis_parameter_overrides_keep_query_value_types_and_boundaries(bounds):
    defaults = {"time_range": {"start": "2025-01-01", "end": "2025-02-01"}, "top_n": 5}
    override = {"time_range": bounds}
    token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set(override)
    try:
        parameters = get_analysis_parameters(defaults)
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(token)

    assert parameters == {"time_range": bounds, "top_n": 5}
    assert defaults["time_range"]["start"] == "2025-01-01"
    assert override == {"time_range": bounds}
    assert type(parameters["time_range"]["start"]) is type(bounds["start"])


def test_analysis_parameters_do_not_interpret_arbitrary_nested_start_end_values():
    defaults = {"labels": {"start": "2026-07-01", "end": "2026-08-01", "timezone": "not-a-timezone"}}
    token = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set({})
    try:
        assert get_analysis_parameters(defaults) == defaults
    finally:
        CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.reset(token)


def test_recent_calendar_days_include_today_without_adding_an_extra_day(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 22, 12, 0, 0)
            return value.replace(tzinfo=tz) if tz is not None else value

    monkeypatch.setattr(agent_invoker, "datetime", FrozenDateTime)

    one_day = _build_expected_time_range_from_user_query("分析近1天小时均值")
    seven_days = _build_expected_time_range_from_user_query("展示近7天历史趋势")

    assert one_day["start"] == "2026-08-22"
    assert one_day["end"] == "2026-08-23"
    assert seven_days["start"] == "2026-08-16"
    assert seven_days["end"] == "2026-08-23"
    assert seven_days["alignment"] == "calendar_day"


def test_turn_expected_time_range_is_frozen_across_stages(monkeypatch):
    generated = [
        {"start": "2026-08-22T00:00:00+08:00", "end": "2026-08-22T00:10:00+08:00"},
        {"start": "2026-08-22T00:00:15+08:00", "end": "2026-08-22T00:10:15+08:00"},
    ]
    calls = []

    def build(_query):
        calls.append(_query)
        return generated[len(calls) - 1]

    monkeypatch.setattr(agent_invoker, "_build_expected_time_range_from_user_query", build)
    runtime = SimpleNamespace(
        turn=SimpleNamespace(id=91, user_query="展示近10分钟趋势"),
        short_term_memory={},
    )

    preparation_range = agent_invoker._resolve_turn_expected_time_range(runtime, "展示近10分钟趋势")
    runtime_range = agent_invoker._resolve_turn_expected_time_range(runtime, "展示近10分钟趋势")

    assert runtime_range == preparation_range == generated[0]
    assert calls == ["展示近10分钟趋势"]


def test_stale_time_range_contract_is_rebuilt(monkeypatch):
    expected = {"start": "2026-08-22", "end": "2026-08-23"}
    monkeypatch.setattr(
        agent_invoker,
        "_build_expected_time_range_from_user_query",
        lambda _query: expected,
    )
    runtime = SimpleNamespace(
        turn=SimpleNamespace(id=91, user_query="展示近1天趋势"),
        short_term_memory={
            "expected_time_range_contract": {
                "version": 1,
                "turn_id": 91,
                "user_query": "展示近1天趋势",
                "value": {"start": "2026-08-21", "end": "2026-08-23"},
            }
        },
    )

    assert agent_invoker._resolve_turn_expected_time_range(
        runtime, "展示近1天趋势"
    ) == expected
    assert runtime.short_term_memory["expected_time_range_contract"]["version"] == agent_invoker.EXPECTED_TIME_RANGE_CONTRACT_VERSION


@pytest.mark.parametrize('query', ['分析本周期数据', '对比上周期数据', '分析过去3周期的波动'])
def test_unspecified_business_cycle_is_not_an_explicit_calendar_week(query):
    assert agent_invoker._build_expected_time_range_from_user_query(query) == {}
    assert not agent_invoker._user_query_has_explicit_time_range(query)


@pytest.mark.parametrize('query', ['分析本周数据', '对比上周数据', '分析过去3周的波动'])
def test_explicit_calendar_weeks_keep_their_time_range(query):
    assert agent_invoker._build_expected_time_range_from_user_query(query)
    assert agent_invoker._user_query_has_explicit_time_range(query)


def test_misclassified_cycle_contract_is_invalidated_on_resume():
    query = '分析本周期数据'
    runtime = SimpleNamespace(
        turn=SimpleNamespace(id=91, user_query=query),
        short_term_memory={'expected_time_range_contract': {
            'version': 2, 'turn_id': 91, 'user_query': query,
            'value': {'start': '2026-08-31', 'end': '2026-09-07', 'source': '本周'},
        }},
    )
    assert agent_invoker._resolve_turn_expected_time_range(runtime, query) == {}


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


def test_time_range_mismatch_reports_expected_and_observed_boundaries():
    query_constraints = {
        "time_range": {
            "start": "2026-01-01",
            "end": "2026-07-01",
            "end_exclusive": True,
        }
    }

    issue = _check_time_range_constraint(
        query_constraints,
        "SELECT SUM(amount) FROM sales WHERE signed_at >= '2026-01-01' AND signed_at <= '2026-06-30'",
    )

    assert issue is not None
    assert issue["diagnostics"]["missing_parts"] == ["end"]
    assert issue["diagnostics"]["expected_boundaries"] == {
        "start": "2026-01-01",
        "end": "2026-07-01",
        "end_exclusive": True,
    }
    assert issue["diagnostics"]["observed_date_literals"] == [
        "2026-01-01",
        "2026-06-30",
    ]
    assert issue["diagnostics"]["repair_strategy"]["preserve_declared_constraint"] is True


def test_zoned_midnight_boundaries_accept_equivalent_date_column_filters():
    query_constraints = {
        "time_range": {
            "start": "2026-08-24T00:00:00+08:00",
            "end": "2026-08-31T00:00:00+08:00",
            "end_exclusive": True,
        }
    }

    assert _check_time_range_constraint(
        query_constraints,
        "SELECT * FROM batches WHERE batch_date >= '2026-08-24' AND batch_date < '2026-08-31'",
    ) is None


def test_non_midnight_boundaries_do_not_collapse_to_date_only_filters():
    query_constraints = {
        "time_range": {
            "start": "2026-08-24T08:30:00+08:00",
            "end": "2026-08-31T08:30:00+08:00",
            "end_exclusive": True,
        }
    }

    issue = _check_time_range_constraint(
        query_constraints,
        "SELECT * FROM events WHERE event_time >= '2026-08-24' AND event_time < '2026-08-31'",
    )

    assert issue is not None
    assert issue["diagnostics"]["missing_parts"] == ["start", "end"]


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
