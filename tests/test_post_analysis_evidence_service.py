from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from service.post_analysis_evidence_service import (  # noqa: E402
    AnalysisExecutionSnapshot,
    CalculationSourceRef,
    EvidencePlan,
    LineageIndexer,
    PlannedMetric,
    PostAnalysisEvidenceCompiler,
    ReportSourceAnchorRef,
    UnresolvedEvidenceOutput,
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


def test_lineage_field_match_does_not_cross_unrelated_containers():
    assert LineageIndexer._target_produces_field(
        "alarm_sum",
        "alarm_sum['process_alarm_count']",
    )
    assert not LineageIndexer._target_produces_field(
        "rows['process_alarm_count']",
        "alarm_sum['process_alarm_count']",
    )


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
    grouped: dict[tuple, dict] = {}
    unresolved: list[UnresolvedEvidenceOutput] = []
    non_metric: list[ReportSourceAnchorRef] = []
    for anchor in payload.get("source_anchor_catalog") or []:
        anchor_id = str(anchor.get("anchor_id") or "")
        reference = ReportSourceAnchorRef(anchor_id=anchor_id)
        hints = anchor.get("verification_hints") or []
        if hints:
            for hint in hints:
                signature = (
                    tuple(hint.get("operation_node_ids") or []),
                    tuple(hint.get("lineage_fields") or []),
                    repr(sorted((hint.get("parameter_bindings") or {}).items())),
                )
                grouped.setdefault(signature, {
                    "anchors": [],
                    "hint": hint,
                })["anchors"].append(reference)
            continue
        expression = str(anchor.get("expression") or "").lower()
        if (
            anchor.get("verified_report_output")
            and not anchor.get("query_reachable")
            and not anchor.get("dimension_origin")
            and not any(token in expression for token in ("rank", "index"))
        ):
            unresolved.append(UnresolvedEvidenceOutput(
                source_anchor=reference,
                reason="The test planner cannot prove this source expression from a query.",
            ))
        else:
            non_metric.append(reference)
    return EvidencePlan(
        metrics=[
            PlannedMetric(
                calculation_id="report_semantic_scope",
                source_anchors=list({
                    anchor.anchor_id: anchor for anchor in item["anchors"]
                }.values()),
                operation_node_ids=item["hint"].get("operation_node_ids") or [],
                metric_source_fields=[],
                parameter_bindings=item["hint"].get("parameter_bindings") or {},
                metric_key=f"metric_{index}",
                business_name=f"Business metric {index}",
                business_explanation="Calculated from the confirmed query and calculation boundary.",
                value_type="number",
                display_unit="",
            )
            for index, item in enumerate(grouped.values(), start=1)
        ],
        unresolved_outputs=unresolved,
        non_metric_source_anchors=non_metric,
    )


def _source_anchor_refs(payload: dict, output_ids: list[str]) -> list[ReportSourceAnchorRef]:
    del output_ids
    return [
        ReportSourceAnchorRef(anchor_id=str(item.get("anchor_id") or ""))
        for item in payload.get("source_anchor_catalog") or []
        if item.get("verification_hints")
    ]


def test_truncated_query_sql_uses_execution_columns_without_parsing_partial_text():
    snapshot = _query_snapshot(
        "SELECT batch_id, AVG(value) AS avg_value FROM samples WHERE batch_id IN ('A', 'B",
        columns=["batch_id", "avg_value"],
    )
    snapshot["implementation"]["sql_truncated"] = True

    operations = LineageIndexer._query_operation_nodes({"q_samples": snapshot})

    source_fields = {
        target
        for operation in operations
        if operation.get("kind") == "source_field"
        for target in operation.get("targets") or []
    }
    assert source_fields == {"batch_id", "avg_value"}


def test_truncated_query_prefers_persisted_projection_semantics():
    snapshot = _query_snapshot(
        "SELECT batch_id, AVG(value) AS avg_value FROM samples WHERE batch_id IN ('A', 'B",
        columns=["batch_id", "avg_value"],
    )
    snapshot["implementation"].update({
        "sql_truncated": True,
        "projection_semantics": [
            {
                "alias": "batch_id",
                "semantic_kind": "projection",
                "projection_role": "dimension",
                "expression": "batch_id",
            },
            {
                "alias": "avg_value",
                "semantic_kind": "aggregation",
                "projection_role": "measure",
                "expression": "AVG(value)",
            },
        ],
    })

    operations = LineageIndexer._query_operation_nodes({"q_samples": snapshot})

    projection = next(
        operation
        for operation in operations
        if operation.get("kind") == "query_projection"
        and operation.get("targets") == ["avg_value"]
    )
    assert projection["query_node_id"] == "q_samples"
    assert projection["semantic_kind"] == "aggregation"


def test_truncated_dynamic_sql_binds_by_unique_static_prefix():
    code = r'''
batch_placeholders = ",".join(["%s"] * len(batch_ids))
sql = f"""
SELECT product, AVG(value) AS avg_value
FROM samples
WHERE batch_id IN ({batch_placeholders})
GROUP BY product
"""
data = load_data_with_sql(sql=sql)
chart = build_chart_result(
    data=data,
    chart_kind="bar",
    category_field="product",
    value_field="avg_value",
)
result = save_analysis_result(analysis_report="", charts=[chart], tables=[])
'''
    executed_sql = """
SELECT product, AVG(value) AS avg_value
FROM samples
WHERE batch_id IN ('B-1', 'B-2', 'B-3'
"""
    query_snapshot = _query_snapshot(
        executed_sql,
        columns=["product", "avg_value"],
    )
    query_snapshot["implementation"]["sql_truncated"] = True
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Average value",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "product",
                    "value_field": "avg_value",
                }
            },
        }],
        query_snapshots={"q_samples": query_snapshot},
    )

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        lineage = LineageIndexer().build(snapshot)
    output = next(
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "chart"
    )
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["candidate_query_node_ids"] == ["q_samples"]
    assert bundle["status"] == "complete", str(bundle["diagnostics"])
    assert len(bundle["chart_trace_refs"]) == 1


def test_table_lineage_keeps_groupby_multi_agg_positions_after_column_rename():
    code = r'''
df = load_data_with_sql(sql="SELECT device_type, power_kw FROM devices")
power_by_type = df.groupby("device_type")["power_kw"].agg(["sum", "mean", "count"]).reset_index()
power_by_type.columns = ["设备类型", "总功率_kW", "平均功率_kW", "设备数量"]
table = build_table_result(
    title="设备类型汇总",
    data=power_by_type,
    value_precision=1,
)
result = save_analysis_result(analysis_report="", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "设备类型汇总",
            "columns": [
                {"key": "设备类型", "label": "设备类型"},
                {"key": "总功率_kW", "label": "总功率_kW"},
                {"key": "平均功率_kW", "label": "平均功率_kW"},
                {"key": "设备数量", "label": "设备数量"},
            ],
            "rows": [
                {"设备类型": "A", "总功率_kW": 12, "平均功率_kW": 6.0, "设备数量": 2},
            ],
        }],
        query_snapshots={
            "q_devices": _query_snapshot(
                "SELECT device_type, power_kw FROM devices",
                columns=["device_type", "power_kw"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    table_columns = {
        ref["column_key"]
        for ref in bundle["table_trace_refs"]
        if ref.get("table_index") == 0
    }
    assert {"总功率_kW", "平均功率_kW", "设备数量"}.issubset(table_columns)


def test_unparseable_query_sql_does_not_abort_other_lineage_sources():
    snapshot = _query_snapshot(
        "SELECT batch_id FROM samples WHERE batch_id IN ('A",
        columns=["batch_id"],
    )

    operations = LineageIndexer._query_operation_nodes({"q_samples": snapshot})

    assert any(
        operation.get("kind") == "source_field"
        and operation.get("targets") == ["batch_id"]
        for operation in operations
    )


def test_planner_payload_preserves_resolver_splits_for_one_calculation():
    payload = {
        "metrics": [
                {
                    "calculation_id": "calc_shared",
                    "output_node_ids": ["report_running"],
                    "metric_key": "contract_count",
                "business_name": "合同数量",
                "business_explanation": "按合同实例去重统计。",
                "value_type": "integer",
                "display_unit": "",
            },
                {
                    "calculation_id": "calc_shared",
                    "output_node_ids": ["report_stopped"],
                    "metric_key": "contract_count_in_chart",
                "business_name": "合同数量（图表）",
                "business_explanation": "",
                "value_type": "integer",
                "display_unit": "个",
            },
        ]
    }

    canonical = PostAnalysisEvidenceCompiler._canonicalize_planner_payload(payload)

    assert len(canonical["metrics"]) == 2
    assert canonical["metrics"][0]["output_node_ids"] == ["report_running"]
    assert canonical["metrics"][1]["output_node_ids"] == ["report_stopped"]
    assert canonical["unresolved_outputs"] == []


def test_planner_payload_discards_llm_rewritten_metric_field_names():
    canonical = PostAnalysisEvidenceCompiler._canonicalize_planner_payload({
        "metrics": [{
            "calculation_id": "calc_alarm_rate",
            "output_node_ids": ["out_alarm_rate"],
            "operation_node_ids": ["op_alarm_rate"],
            "query_node_ids": ["q_alarm"],
            "metric_source_fields": ["alarm_rate"],
            "metric_key": "alarm_rate",
            "business_name": "报警率",
            "business_explanation": "统计报警记录占全部采样记录的比例。",
        }],
        "unresolved_outputs": [],
    })

    assert "metric_source_fields" not in canonical["metrics"][0]


def test_planner_messages_keep_candidate_scope_before_code_and_repeat_ids_last():
    messages = PostAnalysisEvidenceCompiler._planner_messages({
        "analysis_code_with_lines": "0001: value = frame['amount'].sum()",
        "report_candidate_catalog": [{"candidate_id": "rc1", "text_examples": ["12"]}],
        "source_anchor_catalog": [{"anchor_id": "a1", "expression": "value"}],
        "calculation_selection_mode": "source_locator",
    })

    assert "rc1" in messages[1].content
    assert "analysis_code_with_lines" not in messages[1].content
    assert "0001: value" in messages[2].content
    assert "report_candidate_ids=['rc1']" in messages[3].content
    assert "source_anchor_ids=['a1']" in messages[3].content


def test_resolver_requires_each_output_to_be_bound_or_explicitly_unresolved():
    candidates = {
        "calc_shared": {
            "output_node_ids": ["report_running", "report_stopped"],
            "operation_node_ids": ["op_count"],
            "query_node_ids": ["query_devices"],
        }
    }
    plan = EvidencePlan.model_validate({
        "metrics": [{
            "calculation_id": "calc_shared",
            "output_node_ids": ["report_running"],
            "operation_node_ids": ["op_count"],
            "query_node_ids": ["query_devices"],
            "metric_key": "running_count",
            "business_name": "运行中设备数",
            "business_explanation": "统计当前运行中的设备。",
        }],
        "unresolved_outputs": [],
    })

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(plan, candidates)

    assert errors == ["missing output bindings: ['report_stopped']"]


def test_resolver_cannot_drop_data_derived_detail_value_as_unresolved():
    candidates = {
        "calc_detail": {
            "evidence_required": True,
            "output_node_ids": ["report_test_value"],
            "operation_node_ids": ["op_test_value"],
            "query_node_ids": ["query_quality"],
            "available_metric_fields": ["test_value"],
        }
    }
    plan = EvidencePlan.model_validate({
        "metrics": [],
        "unresolved_outputs": [{
            "output_node_id": "report_test_value",
            "reason": "It is a row-level value rather than an aggregate.",
        }],
    })

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(plan, candidates)

    assert errors == [
        "unresolved_outputs[0] is evidence_required: report_test_value"
    ]


def test_resolver_rejects_output_bound_to_an_unrelated_calculation():
    candidates = {
        "calc_running": {
            "output_node_ids": ["report_running"],
            "operation_node_ids": ["op_running"],
            "query_node_ids": ["query_devices"],
        },
        "calc_stopped": {
            "output_node_ids": ["report_stopped"],
            "operation_node_ids": ["op_stopped"],
            "query_node_ids": ["query_devices"],
        },
    }
    plan = EvidencePlan.model_validate({
        "metrics": [{
            "calculation_id": "calc_running",
            "output_node_ids": ["report_stopped"],
            "operation_node_ids": ["op_running"],
            "query_node_ids": ["query_devices"],
            "metric_key": "stopped_count",
            "business_name": "停机设备数",
            "business_explanation": "统计当前停机设备。",
        }],
        "unresolved_outputs": [{
            "output_node_id": "report_running",
            "reason": "当前事实无法证明指标语义。",
        }],
    })

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(plan, candidates)

    assert any("outside calculation candidate" in error for error in errors)


def test_resolver_can_split_one_static_candidate_into_distinct_evidence_boundaries():
    snapshot = _snapshot(code="", tables=[{"title": "Device status"}])
    lineage_index = {
        "operation_nodes": [
            {
                "operation_node_id": "op_running",
                "kind": "aggregation",
                "semantic_kind": "aggregation",
                "source": "data['running_count'].sum()",
                "line": 1,
            },
            {
                "operation_node_id": "op_stopped",
                "kind": "aggregation",
                "semantic_kind": "aggregation",
                "source": "data['stopped_count'].sum()",
                "line": 2,
            },
        ],
        "query_nodes": [{
            "query_node_id": "query_devices",
            "query_key": "query_devices",
            "query_scope": {},
        }],
        "output_nodes": [
            {
                "output_node_id": "table_running",
                "artifact_type": "table",
                "artifact_index": 0,
                "artifact_id": 0,
                "field": "running_count",
                "lineage_fields": ["running_count"],
            },
            {
                "output_node_id": "table_stopped",
                "artifact_type": "table",
                "artifact_index": 0,
                "artifact_id": 0,
                "field": "stopped_count",
                "lineage_fields": ["stopped_count"],
            },
        ],
    }
    candidates = {
        "calc_shared": {
            "calculation_id": "calc_shared",
            "boundary_operation_node_id": "op_running",
            "output_node_ids": ["table_running", "table_stopped"],
            "operation_node_ids": ["op_running", "op_stopped"],
            "query_node_ids": ["query_devices"],
            "metric_source_fields": ["running_count", "stopped_count"],
        }
    }
    plan = EvidencePlan.model_validate({
        "metrics": [
            {
                "calculation_id": "calc_shared",
                "output_node_ids": ["table_running"],
                "operation_node_ids": ["op_running"],
                "query_node_ids": ["query_devices"],
                "metric_source_fields": ["running_count"],
                "metric_key": "running_device_count",
                "business_name": "运行中设备数",
                "business_explanation": "汇总运行中设备数量。",
                "value_type": "integer",
                "display_unit": "台",
            },
            {
                "calculation_id": "calc_shared",
                "output_node_ids": ["table_stopped"],
                "operation_node_ids": ["op_stopped"],
                "query_node_ids": ["query_devices"],
                "metric_source_fields": ["stopped_count"],
                "metric_key": "stopped_device_count",
                "business_name": "停机设备数",
                "business_explanation": "汇总停机设备数量。",
                "value_type": "integer",
                "display_unit": "台",
            },
        ]
    })

    compiler = PostAnalysisEvidenceCompiler(planner=lambda _: plan)
    errors = compiler._plan_reference_errors(plan, candidates)
    bundle = compiler._compile_plan(snapshot, lineage_index, plan, candidates)

    assert errors == []
    assert len(bundle["evidence_definitions"]) == 2
    refs = {item["column_key"]: item["evidence_key"] for item in bundle["table_trace_refs"]}
    assert refs["running_count"] != refs["stopped_count"]
    definitions = {item["evidence_key"]: item for item in bundle["evidence_definitions"]}
    assert definitions[refs["running_count"]]["calculation"]["operation_node_ids"] == ["op_running"]
    assert definitions[refs["stopped_count"]]["calculation"]["operation_node_ids"] == ["op_stopped"]


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
        output_by_id = {
            item["output_node_id"]: item for item in payload.get("output_catalog") or []
        }
        metrics = []
        for item in payload["calculation_candidates"]:
            output = output_by_id[item["output_node_ids"][0]]
            metrics.append(PlannedMetric(
                calculation_id=item["calculation_id"],
                output_node_ids=item.get("output_node_ids") or [],
                operation_node_ids=item.get("operation_node_ids") or [],
                query_node_ids=item.get("query_node_ids") or [],
                metric_source_fields=item.get("metric_source_fields") or [],
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
    assert seen_payload == {}


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
    amount_anchor = next(
        anchor for anchor in captured["source_anchor_catalog"]
        if anchor["expression"] == "amount_change / 10000"
    )
    assert "0008: analysis_report = (" in captured["analysis_code_with_lines"]
    assert "ten-thousand CNY" in amount_anchor["display_suffix"]
    assert "rendered_context" not in amount_anchor
    operation_by_id = {
        operation["operation_node_id"]: operation
        for operation in captured["operation_catalog"]
    }
    assert any(
        "amount_change" in operation_by_id[operation_id]["expression"]
        for hint in amount_anchor["verification_hints"]
        for operation_id in hint["operation_node_ids"]
    )


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
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == [
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


def test_report_len_of_dynamically_written_mapping_reaches_source_query():
    sql = "SELECT batch_id, alarm_count FROM process_signals"
    code = r'''
sql = "SELECT batch_id, alarm_count FROM process_signals"
data = load_data_with_sql(sql=sql)
affected_batches = {}
for _, row in data.iterrows():
    if row["alarm_count"] > 0:
        affected_batches[row["batch_id"]] = int(row["alarm_count"])
analysis_report = f"Affected batches: {len(affected_batches)}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Affected batches: 6.",
        query_snapshots={
            "q_signals": _query_snapshot(
                sql,
                columns=["batch_id", "alarm_count"],
            ),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    output = next(
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
        and item.get("text") == "6"
    )

    assert output["field"] == "len(affected_batches)"
    assert output["candidate_query_node_ids"] == ["q_signals"]


def test_report_len_of_nested_mapping_mutation_reaches_source_query():
    sql = "SELECT batch_id, event_count FROM equipment_events"
    code = r'''
sql = "SELECT batch_id, event_count FROM equipment_events"
data = load_data_with_sql(sql=sql)
affected_batches = {}
for _, row in data.iterrows():
    batch_id = row["batch_id"]
    affected_batches.setdefault(batch_id, {"events": 0})
    affected_batches[batch_id]["events"] += int(row["event_count"])
analysis_report = f"Affected batches: {len(affected_batches)}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Affected batches: 1.",
        query_snapshots={
            "q_events": _query_snapshot(
                sql,
                columns=["batch_id", "event_count"],
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
        and item.get("text") == "1"
    )

    assert output["field"] == "len(affected_batches)"
    assert output["candidate_query_node_ids"] == ["q_events"]


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


def test_chained_iterrows_report_metrics_keep_source_query_lineage():
    sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
    code = r'''
sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
data = load_data_with_sql(sql=sql)
report_sections = []
for _, row in data.head(2).iterrows():
    report_sections.append(f"- {row['category']}: {int(row['count'])} items")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="- machine: 3 items\n- vehicle: 1 items",
        query_snapshots={"q_assets": _query_snapshot(sql, columns=["category", "count"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["3", "1"]
    assert len(bundle["evidence_definitions"]) == 1


def test_chained_itertuples_attribute_metrics_keep_source_query_lineage():
    sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
    code = r'''
sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
data = load_data_with_sql(sql=sql)
report_sections = []
for row in data.head(2).itertuples():
    report_sections.append(f"- {row.category}: {int(row.count)} items")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="- machine: 3 items\n- vehicle: 1 items",
        query_snapshots={"q_assets": _query_snapshot(sql, columns=["category", "count"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["3", "1"]


def test_inline_dataframe_nunique_report_metric_is_not_omitted():
    sql = "SELECT product_id FROM quality_results"
    code = r'''
data = load_data_with_sql(sql="SELECT product_id FROM quality_results")
analysis_report = f"Results cover {data['product_id'].nunique()} products."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Results cover 9 products.",
        query_snapshots={"q_quality": _query_snapshot(sql, columns=["product_id"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["9"]
    assert len(bundle["evidence_definitions"]) == 1


def test_conditional_nunique_or_len_report_metric_is_not_omitted():
    sql = "SELECT test_result_id FROM quality_results"
    code = r'''
data = load_data_with_sql(sql="SELECT test_result_id FROM quality_results")
analysis_report = f"There are {data['test_result_id'].nunique() if 'test_result_id' in data.columns else len(data)} test records."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="There are 7 test records.",
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["test_result_id"])
        },
    )
    snapshot = replace(
        snapshot,
        selected_datasource_snapshot=[{
            "metadata_schema": {
                "properties": {
                    "test_result_id": {
                        "type": "string",
                        "description": "Test result identifier",
                    }
                }
            }
        }],
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["7"]
    assert len(bundle["evidence_definitions"]) == 1


def test_conditional_frame_assignment_preserves_grouped_report_metric_lineage():
    sql = "SELECT product, test_id, deviation FROM quality_results"
    code = r'''
sql = "SELECT product, test_id, deviation FROM quality_results"
data = load_data_with_sql(sql=sql)
detail = data.copy() if data is not None else pd.DataFrame()
summary = detail.groupby("product").agg(
    test_count=("test_id", "count"),
    avg_deviation=("deviation", "mean"),
).reset_index().sort_values("test_count", ascending=False)
report_sections = []
for _, row in summary.iterrows():
    report_sections.append(
        f"- {row['product']}: {int(row['test_count'])} tests, average deviation {row['avg_deviation']:.2f}"
    )
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="- A: 3 tests, average deviation 0.25\n- B: 2 tests, average deviation 0.10",
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["product", "test_id", "deviation"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert sorted(item["text"] for item in bundle["report_trace_refs"]) == [
        "0.10", "0.25", "2", "3",
    ]
    assert len(bundle["evidence_definitions"]) == 2


def test_multi_column_projection_preserves_derived_aggregate_report_lineage():
    sql = "SELECT product, upper_deviation, lower_deviation FROM quality_results"
    code = r'''
sql = "SELECT product, upper_deviation, lower_deviation FROM quality_results"
data = load_data_with_sql(sql=sql)
data["max_deviation"] = data[["upper_deviation", "lower_deviation"]].max(axis=1)
summary = data.groupby("product").agg(
    avg_deviation=("max_deviation", "mean"),
).reset_index()
report_sections = []
for _, row in summary.iterrows():
    report_sections.append(f"- {row['product']}: average deviation {row['avg_deviation']:.2f}")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="- A: average deviation 0.25\n- B: average deviation 0.10",
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["product", "upper_deviation", "lower_deviation"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert sorted(item["text"] for item in bundle["report_trace_refs"]) == [
        "0.10", "0.25",
    ]
    assert len(bundle["evidence_definitions"]) == 1


def test_prebuilt_finding_list_keeps_dynamic_metrics_in_final_report():
    sql = "SELECT status FROM assets"
    code = r'''
sql = "SELECT status FROM assets"
data = load_data_with_sql(sql=sql)
active_count = len(data[data["status"] == "active"])
findings = []
if active_count > 0:
    findings.append(f"Active assets: {active_count}")
report_sections = ["## Findings"]
for finding in findings:
    report_sections.append(f"- {finding}")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Findings\n- Active assets: 3",
        query_snapshots={"q_assets": _query_snapshot(sql, columns=["status"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["3"]


def test_prebuilt_rank_lines_do_not_treat_enumeration_ordinals_as_metrics():
    sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
    code = r'''
sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
data = load_data_with_sql(sql=sql)
rank_lines = []
for index, (_, row) in enumerate(data.iterrows()):
    rank_lines.append(f"{index + 1}. {row['category']}: {int(row['count'])}")
report_sections = ["## Ranking"]
for line in rank_lines:
    report_sections.append(line)
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Ranking\n1. machine: 3\n2. vehicle: 1",
        query_snapshots={"q_assets": _query_snapshot(sql, columns=["category", "count"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["3", "1"]


def test_nested_report_row_builder_keeps_all_metric_occurrences_in_its_section():
    sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
    code = r'''
sql = "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
data = load_data_with_sql(sql=sql)
data["ratio"] = data["count"] / data["count"].sum()
report_sections = ["## Overview", "## Category detail"]
detail_lines = []
for _, row in data.iterrows():
    detail_lines.append(f"- {row['category']}: {int(row['count'])} items ({row['ratio']:.1%})")
report_sections.append("\n".join(detail_lines))
report_sections.append("## Advice")
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Overview\n## Category detail\n"
        "- machine: 3 items (75.0%)\n"
        "- vehicle: 1 items (25.0%)\n"
        "## Advice"
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_assets": _query_snapshot(sql, columns=["category", "count"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == [
        "3", "75.0", "1", "25.0",
    ]
    assert len(bundle["evidence_definitions"]) == 2


def test_nested_value_count_builders_keep_renamed_columns_and_reused_row_scopes():
    sql = "SELECT category, manufacturer FROM assets"
    code = r'''
sql = "SELECT category, manufacturer FROM assets"
data = load_data_with_sql(sql=sql)
total = len(data)
category_dist = data["category"].value_counts().reset_index()
category_dist.columns = ["category", "count"]
category_dist["ratio"] = category_dist["count"] / total
manufacturer_dist = data["manufacturer"].value_counts().reset_index()
manufacturer_dist.columns = ["manufacturer", "count"]
manufacturer_dist["ratio"] = manufacturer_dist["count"] / total
report_sections = ["## Category"]
category_lines = []
for _, row in category_dist.iterrows():
    category_lines.append(f"- {row['category']}: {int(row['count'])} ({row['ratio']:.1%})")
report_sections.append("\n".join(category_lines))
report_sections.append("## Manufacturer")
manufacturer_lines = []
for _, row in manufacturer_dist.iterrows():
    manufacturer_lines.append(f"- {row['manufacturer']}: {int(row['count'])} ({row['ratio']:.1%})")
report_sections.append("\n".join(manufacturer_lines))
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Category\n- machine: 3 (75.0%)\n- vehicle: 1 (25.0%)\n"
        "## Manufacturer\n- Alpha: 2 (50.0%)\n- Beta: 2 (50.0%)"
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_assets": _query_snapshot(sql, columns=["category", "manufacturer"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs_by_text = {}
    for item in sorted(bundle["report_trace_refs"], key=lambda ref: ref["start"]):
        refs_by_text.setdefault(item["text"], []).append(item["evidence_key"])
    assert len(bundle["report_trace_refs"]) == 8
    category_count_key = refs_by_text["3"][0]
    manufacturer_count_key = refs_by_text["2"][0]
    category_ratio_key = refs_by_text["75.0"][0]
    manufacturer_ratio_key = refs_by_text["50.0"][0]
    assert category_count_key != manufacturer_count_key
    assert category_ratio_key != manufacturer_ratio_key


def test_named_conditional_prose_fragments_keep_every_numeric_slot_and_occurrence():
    sql = "SELECT status, power FROM assets"
    code = r'''
sql = "SELECT status, power FROM assets"
data = load_data_with_sql(sql=sql)
total = len(data)
status_dist = data["status"].value_counts().reset_index()
status_dist.columns = ["status", "count"]
running = int(status_dist[status_dist["status"] == "running"]["count"].values[0]) if "running" in status_dist["status"].values else 0
running_ratio = running / total
if running_ratio >= 0.8:
    status_finding = f"Running share {running_ratio:.1%} ({running} items), healthy"
else:
    status_finding = f"Running share {running_ratio:.1%} ({running} items), needs attention"
minimum = data["power"].min()
maximum = data["power"].max()
average = data["power"].mean()
suggestions = []
suggestions.append(f"Power range {minimum:.0f}-{maximum:.0f}, average {average:.1f}.")
report_parts = [
    "## Summary",
    f"- {status_finding}.",
    "## Detail",
    f"- {status_finding}.",
    "## Advice",
]
report_parts.extend(suggestions)
analysis_report = "\n".join(report_parts)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Summary\n- Running share 70.0% (7 items), needs attention.\n"
        "## Detail\n- Running share 70.0% (7 items), needs attention.\n"
        "## Advice\nPower range 3-350, average 88.5."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_assets": _query_snapshot(sql, columns=["status", "power"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [
        item["text"]
        for item in sorted(bundle["report_trace_refs"], key=lambda ref: ref["start"])
    ] == ["70.0", "7", "70.0", "7", "3", "350", "88.5"]


def test_conditional_join_fragments_keep_disjoint_report_source_regions():
    code = r'''
oos_products = {"PP": 1}
oos_grades = {"PP-H1100": 1}
near_products = {"HDPE": 6, "LLDPE": 4}
near_grades = {"HD5502": 6, "LL7042": 4}
oos_product_text = "、".join([f"{k}({v} batches)" for k, v in oos_products.items()]) if oos_products else "none"
oos_grade_text = "、".join([f"{k}({v} batches)" for k, v in oos_grades.items()]) if oos_grades else "none"
near_product_text = "、".join([f"{k}({v} batches)" for k, v in near_products.items()]) if near_products else "none"
near_grade_text = "、".join([f"{k}({v} batches)" for k, v in near_grades.items()]) if near_grades else "none"
report_parts = [
    "## Distribution",
    f"- OOS products: {oos_product_text}; grades: {oos_grade_text}.",
    f"- Near-limit products: {near_product_text}; grades: {near_grade_text}.",
]
analysis_report = "\n".join(report_parts)
'''
    report = (
        "## Distribution\n"
        "- OOS products: PP(1 batches); grades: PP-H1100(1 batches).\n"
        "- Near-limit products: HDPE(6 batches)、LLDPE(4 batches); "
        "grades: HD5502(6 batches)、LL7042(4 batches)."
    )

    _, expressions, _, _, _, _ = LineageIndexer()._index_code(code, {})
    outputs = LineageIndexer._locate_report_outputs(report, expressions)
    numeric_outputs = [
        item for item in outputs
        if item.get("field") == "v"
    ]

    assert [item["text"] for item in numeric_outputs] == ["1", "1", "6", "4", "6", "4"]
    assert [item["source_line"] for item in numeric_outputs] == [
        6, 7, 8, 8, 9, 9,
    ]


def test_conditional_join_fragments_compile_independent_calculation_boundaries():
    sql = "SELECT risk_type, product, grade FROM quality_results"
    code = r'''
sql = "SELECT risk_type, product, grade FROM quality_results"
data = load_data_with_sql(sql=sql)
oos = data[data["risk_type"] == "oos"]
near = data[data["risk_type"] == "near"]
oos_products = oos.groupby("product").size().to_dict()
oos_grades = oos.groupby("grade").size().to_dict()
near_products = near.groupby("product").size().to_dict()
near_grades = near.groupby("grade").size().to_dict()
oos_product_text = "、".join([f"{k}({v} batches)" for k, v in oos_products.items()]) if oos_products else "none"
oos_grade_text = "、".join([f"{k}({v} batches)" for k, v in oos_grades.items()]) if oos_grades else "none"
near_product_text = "、".join([f"{k}({v} batches)" for k, v in near_products.items()]) if near_products else "none"
near_grade_text = "、".join([f"{k}({v} batches)" for k, v in near_grades.items()]) if near_grades else "none"
report_parts = [
    "## Distribution",
    f"- OOS products: {oos_product_text}; grades: {oos_grade_text}.",
    f"- Near-limit products: {near_product_text}; grades: {near_grade_text}.",
]
analysis_report = "\n".join(report_parts)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Distribution\n"
        "- OOS products: PP(1 batches); grades: PP-H1100(1 batches).\n"
        "- Near-limit products: HDPE(6 batches)、LLDPE(4 batches); "
        "grades: HD5502(6 batches)、LL7042(4 batches)."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["risk_type", "product", "grade"],
            ),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["1", "1", "6", "4", "6", "4"]
    keys = [item["evidence_key"] for item in refs]
    assert len(set(keys)) == 4
    assert keys[2] == keys[3]
    assert keys[4] == keys[5]
    assert keys[0] != keys[1]
    assert keys[2] != keys[4]


def test_named_aggregate_columns_used_only_by_table_keep_evidence():
    sql = "SELECT product, price, revenue FROM sales"
    code = r'''
sql = "SELECT product, price, revenue FROM sales"
data = load_data_with_sql(sql=sql)
summary = data.groupby("product", as_index=False).agg(
    average_price=("price", "mean"),
    total_revenue=("revenue", "sum"),
)
table = build_table_result(
    title="Product summary",
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "average_price", "label": "Average price"},
        {"key": "total_revenue", "label": "Total revenue"},
    ],
    data=summary,
)
result = save_analysis_result(
    analysis_report="## Summary\nSee the product table.",
    charts=[],
    tables=[table["table"]],
)
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\nSee the product table.",
        query_snapshots={
            "q_sales": _query_snapshot(sql, columns=["product", "price", "revenue"])
        },
        tables=[{
            "title": "Product summary",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "average_price", "label": "Average price"},
                {"key": "total_revenue", "label": "Total revenue"},
            ],
            "rows": [
                {"product": "A", "average_price": 12.5, "total_revenue": 100.0},
                {"product": "B", "average_price": 15.0, "total_revenue": 80.0},
            ],
        }],
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == {
        "average_price", "total_revenue",
    }


def test_column_aliases_do_not_collapse_formula_to_first_source_field():
    tree = __import__("ast").parse(
        """
priority_df["score"] = (
    priority_df["cnt"] * 0.5
    + priority_df["evidence_strength"] * 0.5
)
priority_df["clean_cnt"] = priority_df["cnt"].fillna(0)
"""
    )

    aliases = LineageIndexer._column_aliases(tree)

    assert "score" not in aliases.get("priority_df", {})
    assert aliases["priority_df"]["clean_cnt"] == "cnt"


def test_named_lambda_aggregates_on_same_source_keep_independent_table_metrics():
    sql = "SELECT batch_id, quality_status FROM quality_tests"
    code = r'''
sql = "SELECT batch_id, quality_status FROM quality_tests"
data = load_data_with_sql(sql=sql)
summary = data.groupby("batch_id", as_index=False).agg(
    oos_count=("quality_status", lambda values: (values == "oos").sum()),
    near_limit_count=("quality_status", lambda values: (values == "near_limit").sum()),
)
table = build_table_result(
    title="Batch quality",
    data=[
        {
            "Batch": row["batch_id"],
            "OOS count": int(row["oos_count"]),
            "Near-limit count": int(row["near_limit_count"]),
        }
        for row in summary.head(20).to_dict("records")
    ],
    columns=[
        {"key": "Batch", "label": "Batch"},
        {"key": "OOS count", "label": "OOS count"},
        {"key": "Near-limit count", "label": "Near-limit count"},
    ],
)
result = save_analysis_result(analysis_report="Batch quality.", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        report="Batch quality.",
        tables=[{
            "title": "Batch quality",
            "columns": [
                {"key": "Batch", "label": "Batch"},
                {"key": "OOS count", "label": "OOS count"},
                {"key": "Near-limit count", "label": "Near-limit count"},
            ],
            "rows": [{"Batch": "B1", "OOS count": 1, "Near-limit count": 0}],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["batch_id", "quality_status"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == {
        "OOS count", "Near-limit count",
    }


def test_table_priority_sequence_is_presentation_not_evidence_metric():
    sql = "SELECT cause, alarm_count, fault_count FROM risks"
    code = r'''
sql = "SELECT cause, alarm_count, fault_count FROM risks"
data = load_data_with_sql(sql=sql)
summary = data.groupby("cause").agg(
    alarm_total=("alarm_count", "sum"),
    fault_total=("fault_count", "sum"),
).reset_index()
summary["score"] = summary["alarm_total"] + summary["fault_total"] * 2
summary = summary.sort_values("score", ascending=False)
priority_rows = []
rank = 1
for _, row in summary.iterrows():
    priority_rows.append({
        "Priority": rank,
        "Cause": row["cause"],
        "Score": row["score"],
    })
    rank += 1
priority_table = pd.DataFrame(priority_rows)
table = build_table_result(title="Cause priority", data=priority_table)
result = save_analysis_result(analysis_report="## Summary\nSee ranking.", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\nSee ranking.",
        query_snapshots={
            "q_risks": _query_snapshot(
                sql,
                columns=["cause", "alarm_count", "fault_count"],
            )
        },
        tables=[{
            "title": "Cause priority",
            "columns": [
                {"key": "Priority", "label": "Priority"},
                {"key": "Cause", "label": "Cause"},
                {"key": "Score", "label": "Score"},
            ],
            "rows": [
                {"Priority": 1, "Cause": "A", "Score": 8.0},
                {"Priority": 2, "Cause": "B", "Score": 3.0},
            ],
        }],
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == {"Score"}


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


def test_formatted_top_n_table_keeps_one_sequential_calculation_boundary_per_column():
    sql = "SELECT department, SUM(shipped_sets) AS shipped_sets, SUM(total_sets) AS total_sets FROM contracts GROUP BY department ORDER BY SUM(shipped_sets) DESC"
    code = r'''
sql = "SELECT department, SUM(shipped_sets) AS shipped_sets, SUM(total_sets) AS total_sets FROM contracts GROUP BY department ORDER BY SUM(shipped_sets) DESC"
data = load_data_with_sql(sql=sql)
top_departments = data.head(10).copy()
top_departments["shipment_rate"] = top_departments.apply(
    lambda row: row["shipped_sets"] / row["total_sets"] * 100 if row["total_sets"] else 0,
    axis=1,
)
display = top_departments.head(5).copy()
display["Shipped sets"] = display["shipped_sets"].map(lambda value: f"{value:,.0f}")
display["Total sets"] = display["total_sets"].map(lambda value: f"{value:,.0f}")
display["Shipment rate"] = display["shipment_rate"].map(lambda value: f"{value:.1f}%")
tables = [build_table_result(
    title="Top departments",
    columns=[
        {"key": "department", "label": "Department"},
        {"key": "Shipped sets", "label": "Shipped sets"},
        {"key": "Total sets", "label": "Total sets"},
        {"key": "Shipment rate", "label": "Shipment rate"},
    ],
    rows=display.to_dict("records"),
)]
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Top departments",
            "columns": [
                {"key": "department", "label": "Department"},
                {"key": "Shipped sets", "label": "Shipped sets"},
                {"key": "Total sets", "label": "Total sets"},
                {"key": "Shipment rate", "label": "Shipment rate"},
            ],
            "rows": [{
                "department": "Operations",
                "Shipped sets": "8,226",
                "Total sets": "9,049",
                "Shipment rate": "90.9%",
            }],
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(
                sql,
                columns=["department", "shipped_sets", "total_sets"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    ]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert {item["field"] for item in outputs} == {
        "Shipped sets", "Total sets", "Shipment rate",
    }
    assert all(item["ambiguous_calculation_boundary"] is False for item in outputs)
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 3


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


def test_parameterized_score_column_with_shared_calculation_ancestry_is_bound():
    sql = "SELECT cause, alarm_count, fault_count FROM risks"
    code = r'''
sql = "SELECT cause, alarm_count, fault_count FROM risks"
data = load_data_with_sql(sql=sql)
grouped = data.groupby("cause").agg(
    alarm_count=("alarm_count", "sum"),
    fault_count=("fault_count", "sum"),
).reset_index()
grouped["alarm_score"] = grouped["alarm_count"] * 2
grouped["fault_score"] = grouped["fault_count"] * 5
score_data = pd.DataFrame({
    "cause_type": ["Alarm", "Fault"],
    "risk_score": [grouped["alarm_score"].sum(), grouped["fault_score"].sum()],
})
charts = [build_chart_result(
    data=score_data,
    chart_kind="pie",
    category_field="cause_type",
    value_field="risk_score",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Root-cause risk scores",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "cause_type",
                    "value_field": "risk_score",
                }
            },
        }],
        query_snapshots={
            "q_risks": _query_snapshot(
                sql,
                columns=["cause", "alarm_count", "fault_count"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1


def test_partition_counts_with_remainder_share_one_chart_evidence():
    sql = "SELECT contract_id, status FROM contracts"
    code = r'''
sql = "SELECT contract_id, status FROM contracts"
data = load_data_with_sql(sql=sql)
total_count = len(data)
active_count = len(data[data["status"] == "active"])
expired_count = len(data[data["status"] == "expired"])
other_count = total_count - active_count - expired_count
chart_data = pd.DataFrame({
    "status": ["Active", "Expired", "Other"],
    "count": [active_count, expired_count, other_count],
})
charts = [build_chart_result(
    data=chart_data,
    chart_kind="pie",
    category_field="status",
    value_field="count",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Contract status",
            "chart_data_contract": {
                "encoding": {"category_field": "status", "value_field": "count"}
            },
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["contract_id", "status"])
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1


def test_table_wrapper_subscript_keeps_helper_data_lineage():
    sql = "SELECT product, test_value, target_value FROM quality_results"
    code = r'''
sql = "SELECT product, test_value, target_value FROM quality_results"
data = load_data_with_sql(sql=sql)
display = data.copy()
display["Measured value"] = display["test_value"]
display["Target value"] = display["target_value"]
tables = []
table_result = build_table_result(
    title="Quality detail",
    data=display,
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "Measured value", "label": "Measured value"},
        {"key": "Target value", "label": "Target value"},
    ],
)
tables.append(table_result["table"])
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Quality detail",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "Measured value", "label": "Measured value"},
                {"key": "Target value", "label": "Target value"},
            ],
            "rows": [{
                "product": "A",
                "Measured value": 9.8,
                "Target value": 10.0,
            }],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["product", "test_value", "target_value"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = {
        item["field"]: item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    }
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert outputs["Measured value"]["source_frame"] == "display"
    assert outputs["Target value"]["source_frame"] == "display"
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 2


def test_record_list_table_keeps_aggregate_lineage_with_static_fstring_keys():
    sql = "SELECT product, quality_status, deviation FROM quality_results"
    code = r'''
sql = "SELECT product, quality_status, deviation FROM quality_results"
data = load_data_with_sql(sql=sql)
summary = data.groupby("product").agg(
    oos_count=("quality_status", lambda values: (values == "oos").sum()),
    total_count=("quality_status", "count"),
    avg_deviation=("deviation", "mean"),
).reset_index()
table_rows = []
for _, row in summary.iterrows():
    table_rows.append({
        "Product": row["product"],
        f"OOS count": int(row["oos_count"]),
        f"Total count": int(row["total_count"]),
        f"Average deviation": f"{row['avg_deviation']:.2f}",
    })
table = build_table_result(title="Quality summary", data=table_rows)
result = save_analysis_result(analysis_report="", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Quality summary",
            "columns": [
                {"key": "Product", "label": "Product"},
                {"key": "OOS count", "label": "OOS count"},
                {"key": "Total count", "label": "Total count"},
                {"key": "Average deviation", "label": "Average deviation"},
            ],
            "rows": [{
                "Product": "A",
                "OOS count": 1,
                "Total count": 3,
                "Average deviation": "0.25",
            }],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["product", "quality_status", "deviation"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = {
        item["field"]: item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    }
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert set(outputs) == {"OOS count", "Total count", "Average deviation"}
    assert all(outputs[field]["candidate_query_node_ids"] == ["q_quality"] for field in outputs)
    assert all(outputs[field]["boundary_operation_node_ids"] for field in outputs)
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == set(outputs)


def test_direct_record_table_keeps_each_scalar_calculation_boundary():
    batch_sql = "SELECT batch_id, quality_status FROM quality_results"
    alarm_sql = "SELECT batch_id, alarm_id FROM process_alarms"
    code = r'''
batch_data = load_data_with_sql(sql="SELECT batch_id, quality_status FROM quality_results")
alarm_data = load_data_with_sql(sql="SELECT batch_id, alarm_id FROM process_alarms")
total_batches = batch_data["batch_id"].nunique()
oos_batches = batch_data.loc[batch_data["quality_status"] == "oos", "batch_id"].nunique()
oos_rate = oos_batches / total_batches * 100 if total_batches else 0
alarm_batches = alarm_data["batch_id"].nunique()
tables = [build_table_result(
    title="Quality overview",
    columns=[
        {"key": "Total batches", "label": "Total batches"},
        {"key": "OOS batches", "label": "OOS batches"},
        {"key": "OOS rate", "label": "OOS rate"},
        {"key": "Alarm batches", "label": "Alarm batches"},
    ],
    rows=[{
        "Total batches": total_batches,
        "OOS batches": oos_batches,
        "OOS rate": oos_rate,
        "Alarm batches": alarm_batches,
    }],
)]
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Quality overview",
            "columns": [
                {"key": "Total batches", "label": "Total batches"},
                {"key": "OOS batches", "label": "OOS batches"},
                {"key": "OOS rate", "label": "OOS rate"},
                {"key": "Alarm batches", "label": "Alarm batches"},
            ],
            "rows": [{
                "Total batches": 20,
                "OOS batches": 3,
                "OOS rate": 15.0,
                "Alarm batches": 4,
            }],
        }],
        query_snapshots={
            "q_batches": _query_snapshot(
                batch_sql,
                columns=["batch_id", "quality_status"],
            ),
            "q_alarms": _query_snapshot(
                alarm_sql,
                columns=["batch_id", "alarm_id"],
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = {
        item["field"]: item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    }
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert outputs["Total batches"]["candidate_query_node_ids"] == ["q_batches"]
    assert outputs["OOS batches"]["candidate_query_node_ids"] == ["q_batches"]
    assert outputs["OOS rate"]["candidate_query_node_ids"] == ["q_batches"]
    assert outputs["Alarm batches"]["candidate_query_node_ids"] == ["q_alarms"]
    assert len({
        tuple(outputs[field]["boundary_operation_node_ids"])
        for field in outputs
    }) == 4
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == set(outputs)


def test_record_list_branches_share_parameterized_formula_boundary():
    sql = "SELECT batch_id, process_alarm_count, fault_count FROM quality_risk"
    code = r'''
data = load_data_with_sql(sql="SELECT batch_id, process_alarm_count, fault_count FROM quality_risk")
total_batches = data["batch_id"].nunique()
process_batches = data[data["process_alarm_count"] > 0]["batch_id"].nunique()
fault_batches = data[data["fault_count"] > 0]["batch_id"].nunique()
root_causes = []
root_causes.append({
    "candidate": "Process",
    "evidence_count": process_batches,
    "affected_batches_pct": process_batches / total_batches * 100 if total_batches else 0,
})
root_causes.append({
    "candidate": "Equipment",
    "evidence_count": fault_batches,
    "affected_batches_pct": fault_batches / total_batches * 100 if total_batches else 0,
})
root_df = pd.DataFrame(root_causes)
chart = build_chart_result(
    data=root_df,
    chart_kind="bar",
    category_field="candidate",
    value_field="affected_batches_pct",
)
table = build_table_result(
    title="Root causes",
    data=root_df,
    columns=[
        {"key": "candidate", "label": "Candidate"},
        {"key": "evidence_count", "label": "Count"},
        {"key": "affected_batches_pct", "label": "Affected (%)"},
    ],
)
result = save_analysis_result(analysis_report="", charts=[chart], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Root causes",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "candidate",
                    "value_field": "affected_batches_pct",
                },
            },
        }],
        tables=[{
            "title": "Root causes",
            "columns": [
                {"key": "candidate", "label": "Candidate"},
                {"key": "evidence_count", "label": "Count"},
                {"key": "affected_batches_pct", "label": "Affected (%)"},
            ],
            "rows": [{
                "candidate": "Process",
                "evidence_count": 2,
                "affected_batches_pct": 20.0,
            }],
        }],
        query_snapshots={
            "q_risk": _query_snapshot(
                sql,
                columns=["batch_id", "process_alarm_count", "fault_count"],
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    affected_outputs = [
        item
        for item in lineage["output_nodes"]
        if item.get("field") == "affected_batches_pct"
    ]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert len(affected_outputs) == 2
    assert all(not item["ambiguous_calculation_boundary"] for item in affected_outputs)
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert any(
        item["value_field"] == "affected_batches_pct"
        for item in bundle["chart_trace_refs"]
    )
    assert any(
        item["column_key"] == "affected_batches_pct"
        for item in bundle["table_trace_refs"]
    )


def test_record_list_table_uses_business_row_selectors_for_distinct_boundaries():
    process_sql = "SELECT batch_id FROM process_events WHERE alarm_flag = 1"
    device_sql = "SELECT equipment_id FROM equipment_faults WHERE fault_flag = 1"
    code = r'''
process = load_data_with_sql(sql="SELECT batch_id FROM process_events WHERE alarm_flag = 1")
device = load_data_with_sql(sql="SELECT equipment_id FROM equipment_faults WHERE fault_flag = 1")
process_count = process["batch_id"].nunique()
device_count = device["equipment_id"].nunique()
root_causes = []
root_causes.append({"candidate": "Process", "evidence_count": process_count})
root_causes.append({"candidate": "Device", "evidence_count": device_count})
root_df = pd.DataFrame(root_causes)
table = build_table_result(
    title="Root causes",
    data=root_df,
    columns=[
        {"key": "candidate", "label": "Candidate"},
        {"key": "evidence_count", "label": "Evidence count"},
    ],
)
result = save_analysis_result(analysis_report="", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Root causes",
            "columns": [
                {"key": "candidate", "label": "Candidate"},
                {"key": "evidence_count", "label": "Evidence count"},
            ],
            "rows": [
                {"candidate": "Process", "evidence_count": 2},
                {"candidate": "Device", "evidence_count": 1},
            ],
        }],
        query_snapshots={
            "q_process": _query_snapshot(process_sql, columns=["batch_id"]),
            "q_device": _query_snapshot(device_sql, columns=["equipment_id"]),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
        and item.get("field") == "evidence_count"
    ]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)
    refs = [
        item for item in bundle["table_trace_refs"]
        if item["column_key"] == "evidence_count"
    ]

    assert len(outputs) == 2
    assert {tuple(item["row_selector"].items()) for item in outputs} == {
        (("candidate", "Process"),),
        (("candidate", "Device"),),
    }
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert {tuple(item["row_selector"].items()) for item in refs} == {
        (("candidate", "Process"),),
        (("candidate", "Device"),),
    }
    assert len({item["evidence_key"] for item in refs}) == 2


def test_structured_llm_resolver_recovers_locally_ambiguous_output():
    sql = "SELECT amount FROM sales"
    code = '''data = load_data_with_sql(sql="SELECT amount FROM sales")
total_amount = data["amount"].sum()
table = build_table_result(
    title="Sales total",
    data=[{"total_amount": total_amount}],
    columns=[{"key": "total_amount", "label": "Total amount"}],
)
result = save_analysis_result(analysis_report="", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Sales total",
            "columns": [{"key": "total_amount", "label": "Total amount"}],
            "rows": [{"total_amount": 30.0}],
        }],
        query_snapshots={
            "q_sales": _query_snapshot(sql, columns=["amount"]),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    output = next(
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
        and item.get("field") == "total_amount"
    )
    output["ambiguous_calculation_boundary"] = True

    def resolve_structured(payload: dict) -> EvidencePlan:
        catalog = payload["structured_output_catalog"]
        assert [item["output_node_id"] for item in catalog] == [
            output["output_node_id"]
        ]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="structured_recovery",
            output_node_ids=[output["output_node_id"]],
            calculation_source_refs=[CalculationSourceRef(
                source_line=2,
                expression='total_amount = data["amount"].sum()',
            )],
            claimed_query_node_ids=["q_sales"],
            claimed_metric_fields=["amount"],
            attestation_confidence="high",
            metric_key="total_amount",
            business_name="Total amount",
            business_explanation="Sum of sales amount.",
            value_type="currency",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(
            planner=_semantic_plan,
            structured_planner=resolve_structured,
        ).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 1
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_structured_recovery_keeps_locally_verified_lineage_over_planner_claims():
    sales_sql = "SELECT amount FROM sales"
    quality_sql = "SELECT deviation FROM quality"
    code = '''sales = load_data_with_sql(sql="SELECT amount FROM sales")
quality = load_data_with_sql(sql="SELECT deviation FROM quality")
total_amount = sales["amount"].sum()
table = build_table_result(
    title="Sales total",
    data=[{"total_amount": total_amount}],
    columns=[{"key": "total_amount", "label": "Total amount"}],
)
result = save_analysis_result(analysis_report="", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Sales total",
            "columns": [{"key": "total_amount", "label": "Total amount"}],
            "rows": [{"total_amount": 30.0}],
        }],
        query_snapshots={
            "q_sales": _query_snapshot(sales_sql, columns=["amount"]),
            "q_quality": _query_snapshot(quality_sql, columns=["deviation"]),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    output = next(
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
        and item.get("field") == "total_amount"
    )
    output["ambiguous_calculation_boundary"] = True

    def resolve_structured(_payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="structured_recovery",
            output_node_ids=[output["output_node_id"]],
            calculation_source_refs=[CalculationSourceRef(
                source_line=3,
                expression='total_amount = sales["amount"].sum()',
            )],
            # These claims are intentionally wrong.  They must not veto or
            # replace the locally proven structured-artifact lineage.
            claimed_query_node_ids=["q_quality"],
            claimed_metric_fields=["deviation"],
            attestation_confidence="high",
            metric_key="total_amount",
            business_name="Total amount",
            business_explanation="Sum of sales amount.",
            value_type="currency",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(
            planner=_semantic_plan,
            structured_planner=resolve_structured,
        ).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]
    assert bundle["evidence_definitions"][0]["calculation"]["metric_fields"] == [
        "total_amount"
    ]


def test_sorted_metric_mapping_dataframe_keeps_chart_lineage():
    sql = "SELECT process_score, equipment_score FROM risks"
    code = r'''
sql = "SELECT process_score, equipment_score FROM risks"
data = load_data_with_sql(sql=sql)
dimension_scores = {
    "Process": data["process_score"].sum(),
    "Equipment": data["equipment_score"].sum(),
}
dimension_ranking = sorted(dimension_scores.items(), key=lambda item: item[1], reverse=True)
score_data = pd.DataFrame(dimension_ranking, columns=["dimension", "risk_score"])
charts = [build_chart_result(
    data=score_data,
    chart_kind="bar",
    category_field="dimension",
    value_field="risk_score",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Root-cause risk scores",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "dimension",
                    "value_field": "risk_score",
                }
            },
        }],
        query_snapshots={
            "q_risks": _query_snapshot(
                sql,
                columns=["process_score", "equipment_score"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["direct_operation_node_ids"]
    assert output["candidate_query_node_ids"] == ["q_risks"]
    assert len(bundle["chart_trace_refs"]) == 1
    assert bundle["status"] == "complete", bundle["diagnostics"]


def test_conditional_table_declaration_matches_runtime_table_by_title():
    sql = "SELECT product, oos_count, near_limit_count FROM quality_summary"
    code = r'''
sql = "SELECT product, oos_count, near_limit_count FROM quality_summary"
data = load_data_with_sql(sql=sql)
tables = []
empty_detail = data[data["oos_count"] < 0]
if not empty_detail.empty:
    tables.append(build_table_result(
        title="Optional detail",
        data=empty_detail,
        columns=[{"key": "oos_count", "label": "OOS count"}],
    ))
summary = data.copy()
tables.append(build_table_result(
    title="Product quality summary",
    data=summary,
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "oos_count", "label": "OOS count"},
        {"key": "near_limit_count", "label": "Near-limit count"},
    ],
))
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Product quality summary",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "oos_count", "label": "OOS count"},
                {"key": "near_limit_count", "label": "Near-limit count"},
            ],
            "rows": [{"product": "A", "oos_count": 1, "near_limit_count": 2}],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["product", "oos_count", "near_limit_count"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    ]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert {item["source_frame"] for item in outputs} == {"summary"}
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 2


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


def test_boolean_mask_sum_counts_share_one_column_level_evidence_boundary():
    sql = "SELECT batch_id, quality_status FROM quality_results"
    code = r'''
sql = "SELECT batch_id, quality_status FROM quality_results"
data = load_data_with_sql(sql=sql)
oos_count = int(data["quality_status"].astype(str).str.lower().isin(["oos"]).sum())
near_limit_count = int(data["quality_status"].astype(str).str.lower().isin(["near_limit"]).sum())
chart_data = pd.DataFrame({
    "status": ["OOS", "Near limit"],
    "test_count": [oos_count, near_limit_count],
})
charts = [build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="status",
    value_field="test_count",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Quality status",
            "chart_data_contract": {
                "encoding": {"category_field": "status", "value_field": "test_count"}
            },
        }],
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["batch_id", "quality_status"])
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert len(output["boundary_operation_node_ids"]) == 2
    assert len(bundle["chart_trace_refs"]) == 1
    assert len(bundle["evidence_definitions"]) == 1


def test_numeric_sums_remain_distinct_structured_metric_boundaries():
    sql = "SELECT amount, profit FROM contracts"
    code = r'''
sql = "SELECT amount, profit FROM contracts"
data = load_data_with_sql(sql=sql)
amount = data["amount"].sum()
profit = data["profit"].sum()
chart_data = pd.DataFrame({
    "metric": ["Amount", "Profit"],
    "value": [amount, profit],
})
charts = [build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="metric",
    value_field="value",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Financial metrics",
            "chart_data_contract": {
                "encoding": {"category_field": "metric", "value_field": "value"}
            },
        }],
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["amount", "profit"])
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")

    assert output["ambiguous_calculation_boundary"] is True


def test_melted_same_boundary_metric_columns_keep_chart_evidence():
    sql = "SELECT product, material_cost, labor_cost FROM product_costs"
    code = r'''
sql = "SELECT product, material_cost, labor_cost FROM product_costs"
data = load_data_with_sql(sql=sql)
summary = data.groupby("product", as_index=False).agg({
    "material_cost": "mean",
    "labor_cost": "mean",
})
chart_data = summary.melt(
    id_vars=["product"],
    value_vars=["material_cost", "labor_cost"],
    var_name="cost_component",
    value_name="unit_cost",
)
charts = [build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="product",
    value_field="unit_cost",
    series_field="cost_component",
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Product cost structure",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "product",
                    "value_field": "unit_cost",
                    "series_field": "cost_component",
                },
            },
        }],
        query_snapshots={
            "q_costs": _query_snapshot(
                sql,
                columns=["product", "material_cost", "labor_cost"],
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(item for item in lineage["output_nodes"] if item["artifact_type"] == "chart")
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert output["ambiguous_calculation_boundary"] is False
    assert len(output["boundary_operation_node_ids"]) == 1
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1


def test_loop_built_wide_to_long_records_keep_column_level_query_lineage():
    sql = (
        "SELECT product, SUM(temp_alarm) AS total_temp_alarm, "
        "SUM(pressure_alarm) AS total_pressure_alarm, "
        "COUNT(DISTINCT batch_id) AS batch_count "
        "FROM quality_events GROUP BY product"
    )
    code = r'''
sql = "SELECT product, SUM(temp_alarm) AS total_temp_alarm, SUM(pressure_alarm) AS total_pressure_alarm, COUNT(DISTINCT batch_id) AS batch_count FROM quality_events GROUP BY product"
wide = load_data_with_sql(sql=sql)
alarm_cols = ["total_temp_alarm", "total_pressure_alarm"]
parameter_names = ["temperature", "pressure"]
long_rows = []
for _, row in wide.iterrows():
    for source_column, parameter_name in zip(alarm_cols, parameter_names):
        value = row[source_column]
        long_rows.append({
            "product": row["product"],
            "parameter_name": parameter_name,
            "alarm_count": int(value),
            "batch_count": int(row["batch_count"]),
        })
long_data = pd.DataFrame(long_rows) if long_rows else pd.DataFrame()
display = long_data[["product", "parameter_name", "alarm_count", "batch_count"]].copy()
tables = [build_table_result(
    title="Alarm details",
    data=display,
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "parameter_name", "label": "Parameter"},
        {"key": "alarm_count", "label": "Alarm count"},
        {"key": "batch_count", "label": "Batch count"},
    ],
)]
result = save_analysis_result(analysis_report="", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        tables=[{
            "title": "Alarm details",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "parameter_name", "label": "Parameter"},
                {"key": "alarm_count", "label": "Alarm count"},
                {"key": "batch_count", "label": "Batch count"},
            ],
            "rows": [{
                "product": "A",
                "parameter_name": "temperature",
                "alarm_count": 12,
                "batch_count": 5,
            }],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=[
                    "product",
                    "total_temp_alarm",
                    "total_pressure_alarm",
                    "batch_count",
                ],
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = {
        item["field"]: item
        for item in lineage["output_nodes"]
        if item["artifact_type"] == "table"
    }

    assert outputs["alarm_count"]["candidate_query_node_ids"] == ["q_quality"]
    assert outputs["batch_count"]["candidate_query_node_ids"] == ["q_quality"]
    assert outputs["alarm_count"]["ambiguous_calculation_boundary"] is False


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


def test_paginated_joined_sql_family_keeps_chart_lineage():
    runtime_sql = (
        "SELECT batch_id, pattern FROM process_events "
        "ORDER BY batch_id LIMIT 4000 OFFSET 4000"
    )
    code = r'''
page_size = 4000
offset = 0
sql_page = f"SELECT batch_id, pattern FROM process_events ORDER BY batch_id LIMIT {page_size} OFFSET {offset}"
page = load_data_with_sql(sql=sql_page)
frames = []
frames.append(page)
data = pd.concat(frames, ignore_index=True)
status_counts = data["pattern"].value_counts().reset_index(name="count")
charts = [build_chart_result(
    data=status_counts,
    chart_kind="pie",
    category_field="pattern",
    value_field="count",
)]
result = save_analysis_result(analysis_report="Pattern distribution.", charts=charts, tables=[])
'''
    family_snapshot = _query_snapshot(
        runtime_sql,
        columns=["batch_id", "pattern"],
    )
    family_snapshot["query_scope"] = {
        "scope_status": "implicit",
        "query_key": "query_auto_page_family",
        "logical_query_family": "query_auto_page_family",
    }
    family_snapshot["implementation"]["logical_query_family"] = (
        "query_auto_page_family"
    )
    snapshot = _snapshot(
        code=code,
        report="Pattern distribution.",
        charts=[{
            "title": "Pattern distribution",
            "chart_document": {
                "chart_kind": "pie",
                "dataset": {"rows": [
                    {"pattern": "single_burst", "count": 8},
                    {"pattern": "continuous_drift", "count": 2},
                ]},
                "encoding": {
                    "category_field": "pattern",
                    "value_field": "count",
                },
            },
        }],
        query_snapshots={"query_auto_page_family": family_snapshot},
    )

    lineage = LineageIndexer().build(snapshot)
    page_load = next(
        item for item in lineage["operation_nodes"]
        if "page" in (item.get("targets") or [])
    )
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert page_load["query_node_id"] == "query_auto_page_family"
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert bundle["evidence_definitions"][0]["query_refs"] == [
        "query_auto_page_family"
    ]


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


def test_report_fragment_augmented_with_fstrings_keeps_numeric_lineage():
    sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
data = load_data_with_sql(sql=sql)
active_count = int(data.loc[data["status"] == "active", "contract_count"].sum())
expired_count = int(data.loc[data["status"] == "expired", "contract_count"].sum())
status_finding = f"Active contracts: {active_count}.\n"
status_finding += f"Expired contracts: {expired_count}.\n"
analysis_report = f"""## Status analysis
{status_finding}
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="""## Status analysis
Active contracts: 8.
Expired contracts: 2.
""",
        query_snapshots={
            "q_contracts": _query_snapshot(
                sql,
                columns=["status", "contract_count"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 2


def test_report_builder_string_concatenation_keeps_numeric_lineage():
    sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS contract_count FROM contracts GROUP BY status"
data = load_data_with_sql(sql=sql)
total_count = int(data["contract_count"].sum())
active_count = int(data.loc[data["status"] == "active", "contract_count"].sum())
active_share = active_count / total_count * 100 if total_count else 0
report_parts = []
report_parts.append("Total contracts: " + str(total_count) + ".")
report_parts.append("Active contracts: " + str(active_count) + f" ({active_share:.1f}%).")
analysis_report = "\n".join(report_parts)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total contracts: 10.\nActive contracts: 8 (80.0%).",
        query_snapshots={
            "q_contracts": _query_snapshot(
                sql,
                columns=["status", "contract_count"],
            )
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 3


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


def test_planner_failure_keeps_ast_verified_report_evidence():
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

    assert bundle["status"] == "complete"
    assert bundle["evidence_definitions"]
    assert len(bundle["report_trace_refs"]) == 1
    assert bundle["diagnostics"]["reason"] == "planner_unavailable"


def test_report_discovery_runs_without_ast_report_output_candidates():
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
    lineage = LineageIndexer().build(snapshot)
    lineage["output_nodes"] = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") != "report"
    ]
    called = []

    def discover(payload: dict) -> EvidencePlan:
        called.append(payload)
        assert "output_catalog" not in payload
        assert "calculation_candidates" not in payload
        operation = next(
            item for item in payload["operation_catalog"]
            if "sales_amount = data" in item["expression"]
        )
        anchor = next(
            item for item in payload["source_anchor_catalog"]
            if item["expression"] == "sales_amount"
        )
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=[ReportSourceAnchorRef(anchor_id=anchor["anchor_id"])],
            operation_node_ids=[operation["operation_node_id"]],
            metric_key="sales_amount",
            business_name="Sales amount",
            business_explanation="Sum of sales amount in the confirmed scope.",
            value_type="currency",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert len(called) == 1
    assert bundle["status"] == "partial"
    assert bundle["report_trace_refs"] == []


def test_rendered_report_candidates_recover_metrics_when_ast_output_location_is_missing():
    sql = "SELECT SUM(amount) AS sales_amount FROM sales"
    code = r'''
sql = "SELECT SUM(amount) AS sales_amount FROM sales"
data = load_data_with_sql(sql=sql)
sales_amount = data.iloc[0]["sales_amount"]
analysis_report = f"2026 sales amount: {sales_amount:,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="2026 sales amount: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["sales_amount"])} ,
    )
    def discover(payload: dict) -> EvidencePlan:
        candidates = payload["report_candidate_catalog"]
        year = next(item["candidate_id"] for item in candidates if item["text_examples"] == ["2026"])
        amount = next(
            item["candidate_id"]
            for item in candidates
            if item["text_examples"] == ["1,280.50"]
        )
        operation = next(
            item for item in payload["operation_catalog"]
            if "sales_amount = data" in item["expression"]
        )
        return EvidencePlan(
            metrics=[PlannedMetric(
                calculation_id="report_semantic_scope",
                report_candidate_ids=[amount],
                operation_node_ids=[operation["operation_node_id"]],
                metric_key="sales_amount",
                business_name="Sales amount",
                business_explanation="Sum of sales amount in the confirmed scope.",
                value_type="currency",
            )],
            non_metric_report_candidate_ids=[year],
        )

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["1,280.50"]


def test_repeated_static_report_value_reuses_the_verified_calculation_boundary():
    sql = "SELECT status FROM quality_results"
    code = r'''
data = load_data_with_sql(sql="SELECT status FROM quality_results")
near_count = int((data["status"] == "near_limit").sum())
if near_count == 1:
    observation = "There are 1 records of near-limit tests requiring attention."
else:
    observation = f"There are {near_count} near-limit tests requiring attention."
analysis_report = f"Near-limit tests: {near_count} records.\n{observation}"
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "Near-limit tests: 1 records.\n"
        "There are 1 records of near-limit tests requiring attention."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_quality": _query_snapshot(sql, columns=["status"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in refs] == ["1", "1"]
    assert refs[0]["evidence_key"] == refs[1]["evidence_key"]


def test_static_zero_result_branch_gets_query_backed_report_evidence():
    sql = "SELECT batch_id FROM batch_context"
    code = r'''
batch_data = load_data_with_sql(sql="SELECT batch_id FROM batch_context")
batch_available = not batch_data.empty
if batch_available:
    batch_line = f"Matched {len(batch_data)} batches."
else:
    batch_line = "No batch matched the confirmed scope (0 batches)."
analysis_report = "## Overview\n" + batch_line
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    query_snapshot = _query_snapshot(sql, columns=["batch_id"])
    query_snapshot["execution"]["row_count"] = 0
    snapshot = _snapshot(
        code=code,
        report="## Overview\nNo batch matched the confirmed scope (0 batches).",
        query_snapshots={"q_batch": query_snapshot},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["0"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_batch"]
    assert (
        bundle["evidence_definitions"][0]["calculation"]["lineage_verification"]
        == "verified"
    )


def test_repeated_value_completion_does_not_guess_between_equal_boundaries():
    sql = "SELECT category FROM quality_results"
    code = r'''
data = load_data_with_sql(sql="SELECT category FROM quality_results")
first_count = int((data["category"] == "first").sum())
second_count = int((data["category"] == "second").sum())
if first_count == second_count:
    detail = "The selected category has 1 records."
else:
    detail = "The selected categories have different counts."
analysis_report = f"First: {first_count} records; second: {second_count} records.\n{detail}"
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "First: 1 records; second: 1 records.\n"
        "The selected category has 1 records."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_quality": _query_snapshot(sql, columns=["category"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert [item["start"] for item in bundle["report_trace_refs"]] == [7, 26]
    assert bundle["status"] == "partial"


def test_static_zero_without_a_query_empty_check_remains_unresolved():
    sql = "SELECT amount FROM sales"
    code = r'''
data = load_data_with_sql(sql="SELECT amount FROM sales")
total = data["amount"].sum()
if total > 100:
    note = "Score 0 is the lowest display category."
else:
    note = "Score 0 is the lowest display category."
analysis_report = f"Total: {total:.2f}.\n{note}"
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total: 50.00.\nScore 0 is the lowest display category.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert [item["text"] for item in bundle["report_trace_refs"]] == ["50.00"]
    assert bundle["status"] == "partial"


def test_llm_source_locator_recovers_inline_calculation_missing_from_operation_catalog():
    sql = "SELECT amount FROM sales"
    code = '''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
analysis_report = f"Sales amount: {sum(data['amount']):,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def discover(payload: dict) -> EvidencePlan:
        candidate = payload["report_candidate_catalog"][0]["candidate_id"]
        assert not any(
            item["expression"] == "sum(data['amount'])"
            for item in payload["operation_catalog"]
        )
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[candidate],
            calculation_source_refs=[CalculationSourceRef(
                source_line=4,
                expression="sum(data['amount'])",
            )],
            metric_key="sales_amount",
            business_name="Sales amount",
            business_explanation="Sum of sales amount in the confirmed scope.",
            value_type="currency",
        )])

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["1,280.50"]
    operation_ids = bundle["evidence_definitions"][0]["calculation"]["operation_node_ids"]
    assert any(item.startswith(("op_recovered_4_", "op_expr_4_")) for item in operation_ids)
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_llm_source_locator_does_not_guess_unmatched_calculation_syntax():
    sql = "SELECT amount FROM sales"
    code = '''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
analysis_report = f"Sales amount: {sum(data['amount']):,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def discover(payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(
                source_line=4,
                expression="sum(other_data['amount'])",
            )],
            metric_key="sales_amount",
            business_name="Sales amount",
            business_explanation="Sum of sales amount in the confirmed scope.",
        )])

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert bundle["status"] == "partial"
    assert bundle["report_trace_refs"] == []
    assert bundle["diagnostics"]["resolver_unresolved_outputs"]


def test_llm_source_locator_requires_successful_query_reachability():
    sql = "SELECT amount FROM sales"
    code = '''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
external_values = read_external_values()
analysis_report = f"External amount: {sum(external_values):,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="External amount: 99.00.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def discover(payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(
                source_line=5,
                expression="sum(external_values)",
            )],
            metric_key="external_amount",
            business_name="External amount",
            business_explanation="Value from an unverified external source.",
        )])

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert bundle["status"] == "partial"
    assert bundle["report_trace_refs"] == []
    assert bundle["diagnostics"]["resolver_unresolved_outputs"]


def test_llm_source_locator_verifies_joined_loop_value_through_its_producer():
    sql = "SELECT category, amount FROM sales"
    code = '''
sql = "SELECT category, amount FROM sales"
data = load_data_with_sql(sql=sql)
counts = data.groupby("category")["amount"].count().to_dict()
detail = "、".join([f"{k} {v} times" for k, v in counts.items()])
analysis_report = f"Category counts: {detail}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Category counts: A 2 times.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["category", "amount"])},
    )

    def discover(payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(source_line=5, expression="v")],
            metric_key="category_count",
            business_name="Category count",
            business_explanation="Record count for each category.",
            value_type="integer",
        )])

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=discover).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["2"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_rendered_markdown_table_candidates_are_grouped_by_column():
    report = (
        "## Detail\n"
        "| Product | Amount | Count |\n"
        "| --- | ---: | ---: |\n"
        "| A | 12.5 | 3 |\n"
        "| B | 8.0 | 1 |"
    )
    candidates = LineageIndexer._locate_report_numeric_candidates(report, occupied=set())
    catalog = PostAnalysisEvidenceCompiler._report_candidate_catalog({
        "output_nodes": candidates,
    })

    assert len(candidates) == 4
    assert len(catalog) == 2
    assert sorted(item["occurrence_count"] for item in catalog) == [2, 2]
    assert all(item["kind"] == "markdown_table_column" for item in catalog)


def test_report_candidate_source_hints_find_nearby_static_report_builder_lines():
    code = '''
alarm_detail = "、".join([f"{k} {v}次" for k, v in alarm_counts.items()])
report_sections.append(
    f"共检测到 **{alarm_total} 次**过程参数报警，"
    f"主要报警参数：{alarm_detail}。"
)
'''
    catalog = [{
        "candidate_id": "rc1",
        "heading": "过程参数报警",
        "column_label": "过程参数报警",
        "line": "共检测到 **5 次**过程参数报警。主要报警参数：temperature 2次。",
    }]

    enriched = PostAnalysisEvidenceCompiler._attach_candidate_source_line_hints(catalog, code)

    assert [item["line"] for item in enriched[0]["source_line_hints"]] == [4]


def test_report_candidate_source_hints_match_exact_dynamic_string_slot():
    code = '''
for rank, (name, score) in enumerate(ranking, 1):
    parts.append(f"{rank}. {name} (score {score})")
'''
    catalog = [{
        "candidate_id": "rc_score",
        "text_examples": ["39"],
        "heading": "Root cause ranking",
        "line": "1. Material (score 39); 2. Process (score 22)",
        "token_prefix": "1. Material (score ",
        "token_suffix": "); 2. Process (score 22)",
    }]
    anchors = [{
        "anchor_id": "a_score",
        "source_line": 3,
        "expression": "score",
        "display_prefix": " (score ",
        "display_suffix": ")",
        "numeric_suffix": "",
    }, {
        "anchor_id": "a_rank",
        "source_line": 3,
        "expression": "rank",
        "display_prefix": "",
        "display_suffix": ". ",
        "numeric_suffix": "",
    }]

    enriched = PostAnalysisEvidenceCompiler._attach_candidate_source_line_hints(
        catalog,
        code,
        anchors,
    )
    slot_hints = [
        item
        for item in enriched[0]["source_line_hints"]
        if item.get("match_kind") == "rendered_slot"
    ]

    assert [(item["anchor_id"], item["expression"]) for item in slot_hints] == [
        ("a_score", "score"),
    ]


def test_report_candidate_source_hints_ignore_unbounded_dynamic_slots():
    code = '''
rates["abnormal"] = rates["abnormal"].map(lambda x: f"{x:.4f}")
report_sections.append(
    f"Only 1 batch is classified as a single-batch fluctuation."
)
'''
    catalog = [{
        "candidate_id": "rc_threshold",
        "text_examples": ["1"],
        "heading": "Pattern classification",
        "line": "Only 1 batch is classified as a single-batch fluctuation.",
        "token_prefix": "Only ",
        "token_suffix": " batch is classified as a single-batch fluctuation.",
    }]
    anchors = [{
        "anchor_id": "a_unrelated_rate",
        "source_line": 2,
        "expression": "x",
        "display_prefix": "",
        "display_suffix": "",
        "numeric_suffix": "",
    }]

    enriched = PostAnalysisEvidenceCompiler._attach_candidate_source_line_hints(
        catalog,
        code,
        anchors,
    )

    assert not any(
        item.get("match_kind") == "rendered_slot"
        for item in enriched[0]["source_line_hints"]
    )


def test_high_confidence_llm_attestation_materializes_when_local_dataflow_is_unavailable():
    sql = "SELECT category, amount FROM sales"
    code = '''
sql = "SELECT category, amount FROM sales"
data = load_data_with_sql(sql=sql)
counts = data.groupby("category")["amount"].count().to_dict()
detail = "、".join([f"{k} {v} times" for k, v in counts.items()])
analysis_report = f"Category counts: {detail}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Category counts: A 2 times.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["category", "amount"])},
    )
    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        lineage = LineageIndexer().build(snapshot)
    lineage["operation_nodes"] = [
        operation
        for operation in lineage["operation_nodes"]
        if "v" not in (operation.get("targets") or [])
    ]

    def attest(payload: dict) -> EvidencePlan:
        query_id = payload["query_catalog"][0]["query_node_id"]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(source_line=5, expression="v")],
            claimed_query_node_ids=[query_id],
            claimed_metric_fields=["amount"],
            attestation_confidence="high",
            metric_key="category_count",
            business_name="Category count",
            business_explanation="Record count for each category.",
            value_type="integer",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(planner=attest).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    calculation = bundle["evidence_definitions"][0]["calculation"]
    assert calculation["lineage_verification"] == "attested"
    assert calculation["metric_fields"] == ["amount"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_source_anchor_attestation_materializes_across_unresolved_helper_dataflow():
    sql = "SELECT category, alarm_count, sample_count FROM signals"
    code = r'''
data = load_data_with_sql(sql="SELECT category, alarm_count, sample_count FROM signals")
def build_rates(frame):
    result = {}
    for _, row in frame.iterrows():
        total = row["sample_count"]
        result[row["category"]] = {
            "rate": row["alarm_count"] / total if total else 0.0,
        }
    return result
rates = build_rates(data)
selected = rates.get("target", {"rate": 0.0})
rate = selected["rate"]
analysis_report = f"Target alarm rate: {rate:.2%}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Target alarm rate: 12.50%.",
        query_snapshots={
            "q_signals": _query_snapshot(
                sql,
                columns=["category", "alarm_count", "sample_count"],
            ),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    report_output = next(
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
        and item.get("field") == "rate"
    )
    report_output["candidate_query_node_ids"] = []
    report_output["candidate_operation_node_ids"] = []
    report_output["boundary_operation_node_ids"] = []
    lineage["operation_nodes"] = [
        operation
        for operation in lineage["operation_nodes"]
        if not set(operation.get("targets") or []) & {"rates", "selected", "rate"}
    ]

    def attest(payload: dict) -> EvidencePlan:
        anchor = next(
            item for item in payload["source_anchor_catalog"]
            if item["expression"] == "rate"
        )
        query = payload["query_catalog"][0]
        assert query["result_bindings"][0]["variable"] == "data"
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=[ReportSourceAnchorRef.model_validate(anchor)],
            calculation_source_refs=[CalculationSourceRef(
                source_line=13,
                expression='rate = selected["rate"]',
            )],
            claimed_query_node_ids=[query["query_node_id"]],
            claimed_metric_fields=["alarm_count", "sample_count"],
            attestation_confidence="high",
            metric_key="target_alarm_rate",
            business_name="Target alarm rate",
            business_explanation="Alarm count divided by sample count for the target category.",
            value_type="percentage",
            display_unit="%",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(planner=attest).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 1
    calculation = bundle["evidence_definitions"][0]["calculation"]
    assert calculation["lineage_verification"] == "attested"
    assert calculation["metric_fields"] == ["alarm_count", "sample_count"]


def test_query_reachable_fields_include_derived_dataframe_columns():
    operation_by_id = {
        "op_query": {
            "operation_node_id": "op_query",
            "query_node_id": "q_quality",
            "targets": ["data"],
            "loads": [],
        },
        "op_copy": {
            "operation_node_id": "op_copy",
            "query_node_id": "",
            "targets": ["summary"],
            "loads": ["data"],
        },
        "op_rate": {
            "operation_node_id": "op_rate",
            "query_node_id": "",
            "targets": ['summary["alarm_rate"]'],
            "loads": ["summary"],
        },
        "op_unrelated": {
            "operation_node_id": "op_unrelated",
            "query_node_id": "q_other",
            "targets": ['other["missing_amount"]'],
            "loads": [],
        },
    }
    producers = {
        target: [operation_id]
        for operation_id, operation in operation_by_id.items()
        for target in operation["targets"]
    }

    fields = PostAnalysisEvidenceCompiler._query_reachable_fields(
        "q_quality",
        _query_snapshot(
            "SELECT alarm_count, sample_count FROM quality",
            columns=["alarm_count", "sample_count"],
        ),
        operation_by_id,
        producers,
    )

    assert {"alarm_count", "sample_count", "alarm_rate"}.issubset(fields)
    assert "missing_amount" not in fields


def test_table_wrapper_and_frame_transform_keep_derived_column_lineage():
    code = '''
sql_domain = "SELECT batch_count, sample_count, oos_count, alarm_count FROM quality"
domain = load_data_with_sql(sql=sql_domain)

def risk_metrics(df, denominator="batch_count"):
    result = df.copy()
    result["oos_rate"] = result["oos_count"] / result[denominator] * 100
    alarm_rate = result["alarm_count"] / result[denominator]
    result["alarm_rate"] = alarm_rate
    return result

def business_table(data, columns, labels, title):
    return build_table_result(
        title=title,
        columns=[{"key": key, "label": label} for key, label in zip(columns, labels)],
        data=data,
    )

domain = risk_metrics(domain, denominator="sample_count")
tables = [business_table(
    domain.head(5),
    ["batch_count", "oos_rate", "alarm_rate"],
    ["Batches", "OOS rate", "Alarms per batch"],
    "Quality risk",
)]
'''
    snapshot = _snapshot(
        code=code,
        query_snapshots={
            "q_quality": _query_snapshot(
                "SELECT batch_count, sample_count, oos_count, alarm_count FROM quality",
                columns=["batch_count", "sample_count", "oos_count", "alarm_count"],
            ),
        },
        tables=[{
            "title": "Quality risk",
            "columns": [
                {"key": "batch_count", "label": "Batches"},
                {"key": "oos_rate", "label": "OOS rate"},
                {"key": "alarm_rate", "label": "Alarms per batch"},
            ],
            "rows": [{"batch_count": 10, "oos_rate": 20.0, "alarm_rate": 0.3}],
        }],
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = {
        item["field"]: item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "table"
    }

    assert outputs["oos_rate"]["source_frame"] == "domain"
    assert outputs["oos_rate"]["candidate_query_node_ids"] == ["q_quality"]
    assert outputs["oos_rate"]["structured_origin"] is True
    assert outputs["oos_rate"]["boundary_operation_node_ids"]
    assert outputs["alarm_rate"]["candidate_query_node_ids"] == ["q_quality"]
    assert outputs["alarm_rate"]["structured_origin"] is True

    bundle = PostAnalysisEvidenceCompiler(
        planner=lambda _: EvidencePlan(),
    ).compile(snapshot)
    refs = {
        item["column_key"]: item["evidence_key"]
        for item in bundle["table_trace_refs"]
    }
    definitions = {
        item["evidence_key"]: item
        for item in bundle["evidence_definitions"]
    }

    assert bundle["status"] == "complete"
    assert set(refs) == {"batch_count", "oos_rate", "alarm_rate"}
    assert set(
        definitions[refs["oos_rate"]]["calculation"]["metric_fields"]
    ) == {"sample_count", "oos_count"}
    assert set(
        definitions[refs["alarm_rate"]]["calculation"]["metric_fields"]
    ) == {"sample_count", "alarm_count"}
    assert refs["oos_rate"] != refs["alarm_rate"]


def test_same_derived_table_column_keeps_distinct_query_boundaries():
    code = '''
sql_product = "SELECT total_count, issue_count FROM product_quality"
sql_team = "SELECT total_count, issue_count FROM team_quality"
product = load_data_with_sql(sql=sql_product)
team = load_data_with_sql(sql=sql_team)

def with_issue_rate(df):
    result = df.copy()
    result["issue_rate"] = result["issue_count"] / result["total_count"] * 100
    return result

def business_table(data, title):
    return build_table_result(
        title=title,
        columns=[{"key": "issue_rate", "label": "Issue rate"}],
        data=data,
    )

product = with_issue_rate(product)
team = with_issue_rate(team)
tables = [
    business_table(product, "Product quality"),
    business_table(team, "Team quality"),
]
'''
    snapshot = _snapshot(
        code=code,
        query_snapshots={
            "q_product": _query_snapshot(
                "SELECT total_count, issue_count FROM product_quality",
                columns=["total_count", "issue_count"],
            ),
            "q_team": _query_snapshot(
                "SELECT total_count, issue_count FROM team_quality",
                columns=["total_count", "issue_count"],
            ),
        },
        tables=[
            {
                "title": "Product quality",
                "columns": [{"key": "issue_rate", "label": "Issue rate"}],
                "rows": [{"issue_rate": 10.0}],
            },
            {
                "title": "Team quality",
                "columns": [{"key": "issue_rate", "label": "Issue rate"}],
                "rows": [{"issue_rate": 20.0}],
            },
        ],
    )

    bundle = PostAnalysisEvidenceCompiler(
        planner=lambda _: EvidencePlan(),
    ).compile(snapshot)
    definitions = {
        item["evidence_key"]: item
        for item in bundle["evidence_definitions"]
    }
    refs = sorted(
        bundle["table_trace_refs"],
        key=lambda item: item["table_index"],
    )

    assert bundle["status"] == "complete"
    assert len(refs) == 2
    assert refs[0]["evidence_key"] != refs[1]["evidence_key"]
    assert definitions[refs[0]["evidence_key"]]["query_refs"] == ["q_product"]
    assert definitions[refs[1]["evidence_key"]]["query_refs"] == ["q_team"]


def test_attested_derived_field_is_not_rejected_by_static_candidate_field_list():
    candidate = {
        "calculation_id": "report_semantic_scope",
        "output_node_ids": ["out_report"],
        "operation_node_ids": ["op_attested"],
        "query_node_ids": ["q_quality"],
        "available_metric_fields": ["alarm_count", "sample_count"],
        "metric_source_fields": ["alarm_count", "sample_count"],
        "semantic_scope": True,
        "output_operation_ids": {"out_report": ["op_attested"]},
        "operation_query_ids": {"op_attested": ["q_quality"]},
        "operation_family_signatures": {},
        "operation_write_signatures": {},
        "operation_semantic_kinds": {"op_attested": "formula"},
        "evidence_required": False,
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="report_semantic_scope",
        output_node_ids=["out_report"],
        operation_node_ids=["op_attested"],
        query_node_ids=["q_quality"],
        metric_source_fields=["alarm_rate"],
        lineage_verification="attested",
        metric_key="alarm_rate",
        business_name="Alarm rate",
        business_explanation="Alarm count divided by sample count.",
    )])

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(
        plan,
        {"report_semantic_scope": candidate},
        require_complete=False,
    )

    assert errors == []


def test_recovery_plan_cannot_claim_outputs_outside_supplied_catalog():
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="report_semantic_scope",
        output_node_ids=["out_allowed", "out_hidden"],
        operation_node_ids=["op1"],
        query_node_ids=["q1"],
        metric_key="metric",
        business_name="Metric",
        business_explanation="Metric explanation.",
    )])

    filtered = PostAnalysisEvidenceCompiler._filter_plan_claims(
        plan,
        excluded_output_ids=set(),
        allowed_output_ids={"out_allowed"},
    )

    assert filtered.metrics[0].output_node_ids == ["out_allowed"]


@pytest.mark.parametrize(
    ("confidence", "claimed_fields", "expected_reason"),
    [
        ("medium", ["amount"], "requires high confidence"),
        ("high", ["missing_amount"], "unknown query fields"),
    ],
)
def test_llm_attestation_does_not_override_unverified_or_contradictory_facts(
    confidence,
    claimed_fields,
    expected_reason,
):
    sql = "SELECT category, amount FROM sales"
    code = '''
sql = "SELECT category, amount FROM sales"
data = load_data_with_sql(sql=sql)
counts = data.groupby("category")["amount"].count().to_dict()
detail = " | ".join([f"{k}: {v}" for k, v in counts.items()])
analysis_report = f"Category counts: {detail}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Category counts: A: 2.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["category", "amount"])},
    )
    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        lineage = LineageIndexer().build(snapshot)
    lineage["operation_nodes"] = [
        operation
        for operation in lineage["operation_nodes"]
        if "v" not in (operation.get("targets") or [])
    ]

    def attest(payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(source_line=5, expression="v")],
            claimed_query_node_ids=[payload["query_catalog"][0]["query_node_id"]],
            claimed_metric_fields=claimed_fields,
            attestation_confidence=confidence,
            metric_key="category_count",
            business_name="Category count",
            business_explanation="Record count for each category.",
            value_type="integer",
        )])

    with patch.object(LineageIndexer, "build", return_value=lineage):
        bundle = PostAnalysisEvidenceCompiler(planner=attest).compile(snapshot)

    assert bundle["status"] == "partial"
    assert bundle["evidence_definitions"] == []
    assert expected_reason in str(
        bundle["diagnostics"]["resolver_unresolved_outputs"]
    )


def test_posthoc_candidate_attestation_is_not_vetoed_by_an_inaccurate_source_hint():
    sales_sql = "SELECT amount FROM sales"
    quality_sql = "SELECT deviation FROM quality"
    code = '''
sales = load_data_with_sql(sql="SELECT amount FROM sales")
quality = load_data_with_sql(sql="SELECT deviation FROM quality")
wrong_total = sales["amount"].sum()
analysis_report = f"Sales total: {wrong_total}. Observed quality deviation: 21.75."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales total: 100. Observed quality deviation: 21.75.",
        query_snapshots={
            "q_sales": _query_snapshot(sales_sql, columns=["amount"]),
            "q_quality": _query_snapshot(quality_sql, columns=["deviation"]),
        },
    )

    def attest(payload: dict) -> EvidencePlan:
        incorrectly_claimed_sales_query = next(
            item["query_node_id"]
            for item in payload["query_catalog"]
            if "amount" in (item.get("columns") or [])
        )
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            calculation_source_refs=[CalculationSourceRef(
                source_line=4,
                expression='sales["amount"].sum()',
            )],
            claimed_query_node_ids=[incorrectly_claimed_sales_query],
            claimed_metric_fields=["deviation"],
            attestation_confidence="high",
            metric_key="quality_deviation",
            business_name="Quality deviation",
            business_explanation="Observed quality deviation from the quality query.",
            value_type="number",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=attest).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    attested_definition = next(
        item
        for item in bundle["evidence_definitions"]
        if (item.get("calculation") or {}).get("lineage_verification") == "attested"
    )
    assert attested_definition["query_refs"] == ["q_quality"]


def test_posthoc_candidate_keeps_locally_verified_boundary_when_planner_query_hint_is_incomplete():
    code = '''
sales = load_data_with_sql(sql="SELECT amount FROM sales")
quality = load_data_with_sql(sql="SELECT deviation FROM quality")
quality_peak = quality["deviation"].max()
analysis_report = f"Peak quality deviation: {quality_peak:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Peak quality deviation: 21.75.",
        query_snapshots={
            "q_sales": _query_snapshot("SELECT amount FROM sales", columns=["amount"]),
            "q_quality": _query_snapshot("SELECT deviation FROM quality", columns=["deviation"]),
        },
    )

    def classify(payload: dict) -> EvidencePlan:
        candidate = payload["report_candidate_catalog"][0]["candidate_id"]
        sales_query_id = next(
            item["query_node_id"]
            for item in payload["query_catalog"]
            if "amount" in (item.get("columns") or [])
        )
        operation = next(
            item for item in payload["operation_catalog"]
            if "quality_peak" in item["expression"]
        )
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[candidate],
            operation_node_ids=[operation["operation_node_id"]],
            claimed_query_node_ids=[sales_query_id],
            metric_key="quality_peak",
            business_name="Peak quality deviation",
            business_explanation="Maximum observed deviation.",
            value_type="number",
        )])

    with patch.object(LineageIndexer, "_locate_report_outputs", return_value=[]):
        bundle = PostAnalysisEvidenceCompiler(planner=classify).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_quality"]


def test_source_locator_resolves_multiline_assignment_from_an_interior_line():
    sql = "SELECT category, deviation FROM quality"
    code = '''
data = load_data_with_sql(sql="SELECT category, deviation FROM quality")
summary = (
    data.groupby("category", as_index=False)
    .agg(avg_deviation=("deviation", "mean"))
)
analysis_report = f"Average deviation: {summary['avg_deviation'].max()}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Average deviation: 21.75.",
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["category", "deviation"]),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    compiler = PostAnalysisEvidenceCompiler(planner=_semantic_plan)

    operation_ids, errors = compiler._resolve_calculation_source_refs(
        code,
        lineage,
        [CalculationSourceRef(
            source_line=4,
            expression=(
                'summary = data.groupby("category", as_index=False)'
                '.agg(avg_deviation=("deviation", "mean"))'
            ),
        )],
    )

    assert errors == []
    assert len(operation_ids) == 1
    operation_by_id = {
        item["operation_node_id"]: item
        for item in lineage["operation_nodes"]
    }
    producers = {
        target: [
            operation_id
            for operation_id, operation in operation_by_id.items()
            if target in (operation.get("targets") or [])
        ]
        for target in {"data", "summary"}
    }
    assert LineageIndexer._query_dependencies(
        operation_ids,
        operation_by_id,
        producers,
    ) == ["q_quality"]


def test_source_locator_prefers_exact_expression_over_nearby_same_target():
    code = '''data = load_data_with_sql(sql="SELECT amount FROM sales")
value = data["amount"].sum()
value = data["amount"].max()
analysis_report = f"Maximum: {value}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Maximum: 20.",
        query_snapshots={
            "q_sales": _query_snapshot(
                "SELECT amount FROM sales", columns=["amount"]
            ),
        },
    )
    lineage = LineageIndexer().build(snapshot)

    operation_ids, errors = (
        PostAnalysisEvidenceCompiler._resolve_calculation_source_refs(
            code,
            lineage,
            [CalculationSourceRef(
                source_line=3,
                expression='value = data["amount"].max()',
            )],
        )
    )

    assert errors == []
    assert len(operation_ids) == 1
    operation = next(
        item for item in lineage["operation_nodes"]
        if item["operation_node_id"] == operation_ids[0]
    )
    assert ".max()" in operation["source"]


def test_source_locator_compares_assignment_syntax_not_quote_spelling():
    code = '''data = load_data_with_sql(sql="SELECT retry_flag FROM quality")
signals = {}
signals['retest'] = int(data['retry_flag'].fillna(0).sum())
analysis_report = f"Retest: {signals['retest']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Retest: 6.",
        query_snapshots={
            "q_quality": _query_snapshot(
                "SELECT retry_flag FROM quality", columns=["retry_flag"]
            ),
        },
    )
    lineage = LineageIndexer().build(snapshot)

    operation_ids, errors = (
        PostAnalysisEvidenceCompiler._resolve_calculation_source_refs(
            code,
            lineage,
            [CalculationSourceRef(
                source_line=3,
                expression=(
                    'signals["retest"] = '
                    'int(data["retry_flag"].fillna(0).sum())'
                ),
            )],
        )
    )

    assert errors == []
    assert len(operation_ids) == 1


def test_default_planner_repairs_a_locally_rejected_semantic_claim_once():
    sql = "SELECT deviation FROM quality"
    code = '''
quality = load_data_with_sql(sql="SELECT deviation FROM quality")
maximum = quality["deviation"].max()
analysis_report = f"Maximum: {maximum}. Confirmed deviation: 21.75."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Maximum: 21.75. Confirmed deviation: 21.75.",
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["deviation"]),
        },
    )
    calls: list[dict] = []

    def planner(payload: dict) -> EvidencePlan:
        calls.append(payload)
        field = "missing_deviation" if len(calls) == 1 else "deviation"
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[payload["report_candidate_catalog"][0]["candidate_id"]],
            claimed_query_node_ids=[payload["query_catalog"][0]["query_node_id"]],
            claimed_metric_fields=[field],
            attestation_confidence="high",
            metric_key="confirmed_deviation",
            business_name="Confirmed deviation",
            business_explanation="Confirmed deviation from the successful quality query.",
            value_type="number",
        )])

    compiler = PostAnalysisEvidenceCompiler(planner=planner)
    compiler._uses_default_planner = True
    bundle = compiler.compile(snapshot)

    assert len(calls) == 2
    assert calls[1]["repair_feedback"]["unresolved"]
    assert bundle["status"] == "complete", bundle["diagnostics"]


def test_default_planner_does_not_recheck_locally_verified_report_output():
    sql = "SELECT amount FROM sales"
    code = '''
data = load_data_with_sql(sql="SELECT amount FROM sales")
total = data["amount"].sum()
analysis_report = f"Sales total: {total:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales total: 1280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def planner(_payload: dict) -> EvidencePlan:
        raise AssertionError("locally verified report output must not reach planner")

    compiler = PostAnalysisEvidenceCompiler(planner=planner)
    compiler._uses_default_planner = True
    bundle = compiler.compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["1280.50"]


def test_report_mapping_member_keeps_its_own_calculation_container():
    sql = "SELECT process_alarm_count FROM quality"
    code = '''
data = load_data_with_sql(sql="SELECT process_alarm_count FROM quality")
alarm_fields = ["process_alarm_count"]
alarm_sum = {field: int(data[field].fillna(0).sum()) for field in alarm_fields}
rows = {"process_alarm_count": 0}
analysis_report = f"Process alarms: {alarm_sum['process_alarm_count']} times."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Process alarms: 10 times.",
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["process_alarm_count"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["10"]
    operations = bundle["evidence_definitions"][0]["calculation"]["operation_node_ids"]
    assert any("op_4_" in operation_id for operation_id in operations)
    assert not any("op_5_" in operation_id for operation_id in operations)


def test_nested_mapping_member_exposes_outer_business_selector():
    code = '''
evidence = {}
evidence["Process"] = {
    "candidate_share": int(candidate_counts.get("Process", 0)),
    "evidence_share": process_count / population_count,
}
'''
    tree = __import__("ast").parse(code)
    assignment = tree.body[1]

    operations = LineageIndexer._mapping_literal_member_operations(
        assignment,
        code,
    )

    assert [item["parameter_bindings"] for item in operations] == [
        {
            "mapping_key": "candidate_share",
            "mapping_path": ["Process", "candidate_share"],
            "mapping_selector": "Process",
        },
        {
            "mapping_key": "evidence_share",
            "mapping_path": ["Process", "evidence_share"],
            "mapping_selector": "Process",
        },
    ]


def test_rendered_report_year_month_is_not_a_metric_candidate():
    candidates = LineageIndexer._locate_report_numeric_candidates(
        "2026年3月销售收入为 1,280.50 元。",
        occupied=set(),
    )
    metric_candidates = [
        item for item in candidates
        if not item.get("presentation_temporal_literal")
    ]

    assert [item["text"] for item in metric_candidates] == ["1,280.50"]


def test_rendered_report_presentation_literals_do_not_reach_metric_catalog():
    report = (
        "2026\u5e743\u6708\u5206\u6790\n"
        "**1. \u5173\u952e\u53d1\u73b0**\n"
        "- \u6bdb\u5229\u7387\u9ad8\u4e8e 40% \u7684\u4ea7\u54c1\u6709 7 \u4e2a\uff0c"
        "\u4f4e\u4e8e 30% \u7684\u4ea7\u54c1\u6709 1 \u4e2a\u3002"
    )
    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]
    catalog = PostAnalysisEvidenceCompiler._report_candidate_catalog({
        "output_nodes": candidates,
        "operation_nodes": [],
    })

    assert metric_texts == ["7", "1"]
    assert [item["text_examples"] for item in catalog] == [["7"], ["1"]]


def test_rendered_report_compact_date_range_is_not_a_metric_candidate():
    report = (
        "分析周期：2026-08-03 至 08-09（本周）\n"
        "本周期涉及 33 个批次。"
    )

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert metric_texts == ["33"]


def test_rendered_report_unitless_comparison_cutoff_is_not_a_metric_candidate():
    report = "关联多物料批次（≥3）涉及 33 个批次。"

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert metric_texts == ["33"]


def test_rendered_report_labelled_interval_endpoints_are_not_metric_candidates():
    report = "偏离度 20%~50%（中等偏离）风险批次 0 个。"

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert metric_texts == ["0"]


def test_rendered_report_bold_list_ordinals_are_not_metric_candidates():
    report = (
        "### \u56db\u3001\u6839\u56e0\u5019\u9009\u4f18\u5148\u7ea7\u6392\u5e8f\n"
        "- **1. \u7269\u6599\u6279\u6b21**\uff0881\u5206\uff09\uff1a"
        "\u5173\u8054\u7269\u6599\u6279\u6b21\u517181\u6279\n"
        "- **2. \u8fc7\u7a0b\u6ce2\u52a8**\uff0810\u5206\uff09\uff1a"
        "\u8fc7\u7a0b\u62a5\u8b66\u603b\u8ba110\u6b21\n"
    )

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    ordinal_texts = [
        item["text"]
        for item in candidates
        if item.get("presentation_ordinal")
    ]
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert ordinal_texts == ["1", "2"]
    assert metric_texts == ["81", "81", "10", "10"]


def test_rendered_report_inline_rank_ordinals_are_not_metric_candidates():
    report = "1. Process (39); 2. Equipment (34); 3. Material (22); 4. Retest (11)"

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    ordinal_texts = [
        item["text"] for item in candidates if item.get("presentation_ordinal")
    ]
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert ordinal_texts == ["1", "2", "3", "4"]
    assert metric_texts == ["39", "34", "22", "11"]


def test_rendered_report_heading_number_is_not_a_metric_candidate():
    report = "### Key finding 1: batch and quality distribution\n- 35 near-limit records"

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )
    ordinal_texts = [
        item["text"] for item in candidates if item.get("presentation_ordinal")
    ]
    metric_texts = [
        item["text"]
        for item in candidates
        if PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
    ]

    assert ordinal_texts == ["1"]
    assert metric_texts == ["35"]


def test_rendered_report_question_reference_number_is_not_a_metric_candidate():
    report = "- Question 3 cannot be completed because the source relationship is unresolved."

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )

    assert len(candidates) == 1
    assert candidates[0]["text"] == "3"
    assert candidates[0]["presentation_ordinal"]
    assert not PostAnalysisEvidenceCompiler._is_report_metric_output(
        candidates[0],
        {"operation_nodes": []},
    )


def test_rendered_report_numbers_adjacent_to_chinese_text_are_candidates():
    report = "\u517181\u6279\uff0c\u603b\u8ba110\u6b21\uff08\u6e29\u5ea62\u3001\u538b\u529b1\uff09"

    candidates = LineageIndexer._locate_report_numeric_candidates(
        report,
        occupied=set(),
    )

    assert [item["text"] for item in candidates] == ["81", "10", "2", "1"]
    assert all(
        PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
        for item in candidates
    )


def test_ast_report_rank_slot_at_list_prefix_is_presentation_only():
    sql = "SELECT category, score, description FROM root_causes"
    code = r'''
sql = "SELECT category, score, description FROM root_causes"
root_cause_df = load_data_with_sql(sql=sql).sort_values("score", ascending=False)
root_cause_df["rank"] = range(1, len(root_cause_df) + 1)
report_sections = ["### Ranking"]
for _, row in root_cause_df.iterrows():
    report_sections.append(
        f"- **{row['rank']}. {row['category']}** ({int(row['score'])}): {row['description']}"
    )
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report=(
            "### Ranking\n"
            "- **1. material** (81): batch evidence\n"
            "- **2. process** (10): alarm evidence"
        ),
        query_snapshots={
            "q_root": _query_snapshot(
                sql,
                columns=["category", "score", "description"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    report_outputs = [
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
    ]
    ordinals = [
        item["text"]
        for item in report_outputs
        if item.get("presentation_ordinal")
    ]
    metric_texts = [
        item["text"]
        for item in report_outputs
        if PostAnalysisEvidenceCompiler._is_report_metric_output(item, lineage)
    ]

    assert ordinals == ["1", "2"]
    assert metric_texts == ["81", "10"]


def test_report_display_limit_from_analysis_parameters_is_not_a_metric():
    code = r'''
analysis_parameters_schema = {"defaults": {"top_n": 10}}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])
top_n = int(analysis_parameters["top_n"])
analysis_report = "- Detail table shows top " + str(top_n) + " products."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    lineage = LineageIndexer().build(_snapshot(
        code=code,
        report="- Detail table shows top 10 products.",
    ))
    outputs = [
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report" and item.get("text") == "10"
    ]

    assert len(outputs) == 1
    assert not PostAnalysisEvidenceCompiler._is_report_metric_output(
        outputs[0],
        lineage,
    )


def test_rendered_report_top_n_display_limit_is_not_a_metric_candidate():
    candidates = LineageIndexer._locate_report_numeric_candidates(
        "下表为报警样本数最高的前10个参数组合。",
        occupied=set(),
    )

    assert len(candidates) == 1
    assert candidates[0]["text"] == "10"
    assert candidates[0]["presentation_display_limit"] is True
    assert not PostAnalysisEvidenceCompiler._is_report_metric_output(
        candidates[0],
        {"operation_nodes": []},
    )


def test_rendered_report_range_separator_is_not_parsed_as_a_negative_sign():
    candidates = LineageIndexer._locate_report_numeric_candidates(
        "Normal range: 0.77%-0.80%.",
        occupied=set(),
    )

    assert [item["text"] for item in candidates] == ["0.77", "0.80"]


def test_rendered_report_formula_coefficient_is_not_a_metric_candidate():
    candidates = LineageIndexer._locate_report_numeric_candidates(
        "Composite score = OOS rate ×3 + near-limit rate.",
        occupied=set(),
    )

    assert len(candidates) == 1
    assert candidates[0]["presentation_formula_coefficient"] is True
    assert not PostAnalysisEvidenceCompiler._is_report_metric_output(
        candidates[0],
        {"operation_nodes": []},
    )


def test_rendered_report_numeric_fragments_inside_business_ids_are_not_metrics():
    candidates = LineageIndexer._locate_report_numeric_candidates(
        "Products TPU-85A, HDPE-5502 and BA-01 require attention.",
        occupied=set(),
    )

    assert [item["text"] for item in candidates] == ["85", "5502", "01"]
    assert all(item["presentation_business_identifier"] for item in candidates)
    assert all(
        not PostAnalysisEvidenceCompiler._is_report_metric_output(
            item,
            {"operation_nodes": []},
        )
        for item in candidates
    )


def test_planner_typed_alias_mismatch_is_recovered_without_losing_claim():
    compiler = PostAnalysisEvidenceCompiler(planner=_semantic_plan)
    compiler._resolver_id_maps = {
        "anchor": {"a1": "anchor_total"},
        "report_candidate": {"rc1": "candidate_total"},
        "calculation": {},
        "output": {},
        "operation": {},
        "query": {},
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="report_semantic_scope",
        source_anchors=[ReportSourceAnchorRef(anchor_id="rc1")],
        report_candidate_ids=["a1"],
        metric_key="total",
        business_name="Total",
        business_explanation="Total in the confirmed scope.",
    )])

    restored = compiler._restore_resolver_plan_ids(plan)
    metric = restored.metrics[0]

    assert metric.report_candidate_ids == ["candidate_total"]
    assert [item.anchor_id for item in metric.source_anchors] == ["anchor_total"]


def test_planner_unresolved_candidate_alias_is_recovered_from_anchor_field():
    compiler = PostAnalysisEvidenceCompiler(planner=_semantic_plan)
    compiler._resolver_id_maps = {
        "anchor": {"a1": "anchor_total"},
        "report_candidate": {"rc1": "candidate_total"},
        "calculation": {},
        "output": {},
        "operation": {},
        "query": {},
    }
    plan = EvidencePlan(unresolved_outputs=[
        UnresolvedEvidenceOutput(
            source_anchor=ReportSourceAnchorRef(anchor_id="rc1"),
            reason="Cannot prove this rendered value.",
        ),
        UnresolvedEvidenceOutput(
            source_anchor=ReportSourceAnchorRef(),
            reason="Shape-only placeholder.",
        ),
    ])

    normalized = compiler._normalize_typed_resolver_aliases(plan)

    assert len(normalized.unresolved_outputs) == 1
    assert normalized.unresolved_outputs[0].output_node_id == "rc1"
    assert normalized.unresolved_outputs[0].source_anchor is None


def test_markdown_table_candidates_inherit_build_markdown_table_column_origins():
    report = (
        "| Product | Amount | Count |\n"
        "| --- | ---: | ---: |\n"
        "| A | 12.5 | 3 |\n"
        "| B | 8.0 | 1 |"
    )
    code = '''
summary_table_md = build_markdown_table(
    display_df,
    columns=[
        {"key": "amount", "label": "Amount"},
        {"key": "count", "label": "Count"},
    ],
)
'''
    candidates = LineageIndexer._locate_report_numeric_candidates(report, occupied=set())
    resolved = LineageIndexer._attach_report_markdown_table_origins(
        candidates,
        code=code,
    )

    assert {item["field"] for item in resolved} == {"amount", "count"}
    assert all(item["source_frame"] == "display_df" for item in resolved)


def test_discovery_contract_requires_every_verified_report_anchor_to_be_classified():
    payload = {
        "source_anchor_catalog": [
            {"anchor_id": "a1", "verified_report_output": True},
            {"anchor_id": "a2", "verified_report_output": False},
        ],
        "operation_catalog": [],
    }

    errors = PostAnalysisEvidenceCompiler._discovery_plan_reference_errors(
        EvidencePlan(non_metric_source_anchors=[ReportSourceAnchorRef(anchor_id="a2")]),
        payload,
    )

    assert errors == ["verified report source anchors are unclassified: ['a1']"]


def test_discovery_contract_rejects_merging_distinct_mapping_selectors():
    payload = {
        "source_anchor_catalog": [],
        "query_catalog": [{"query_node_id": "q1"}],
        "operation_catalog": [
            {
                "operation_node_id": "op1",
                "parameter_bindings": {"mapping_selector": "Process"},
            },
            {
                "operation_node_id": "op2",
                "parameter_bindings": {"mapping_selector": "Equipment"},
            },
        ],
        "report_candidate_catalog": [
            {"candidate_id": "rc1", "slot_bindings": {"k": "Process"}},
            {"candidate_id": "rc2", "slot_bindings": {"k": "Equipment"}},
        ],
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="report_semantic_scope",
        report_candidate_ids=["rc1", "rc2"],
        operation_node_ids=["op1", "op2"],
        claimed_query_node_ids=["q1"],
        metric_key="candidate_share",
        business_name="Candidate count",
        business_explanation="Count by candidate.",
    )])

    errors = PostAnalysisEvidenceCompiler._discovery_plan_reference_errors(
        plan,
        payload,
    )

    assert any("split one metric per selector" in error for error in errors)


def test_planner_mapping_metric_is_split_by_proven_rendered_selector():
    payload = {
        "operation_catalog": [
            {
                "operation_node_id": "op1",
                "parameter_bindings": {"mapping_selector": "Process"},
            },
            {
                "operation_node_id": "op2",
                "parameter_bindings": {"mapping_selector": "Equipment"},
            },
        ],
        "report_candidate_catalog": [
            {"candidate_id": "rc1", "slot_bindings": {"k": "Process"}},
            {"candidate_id": "rc2", "slot_bindings": {"k": "Equipment"}},
        ],
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="report_semantic_scope",
        report_candidate_ids=["rc1", "rc2"],
        operation_node_ids=["op1", "op2"],
        calculation_source_refs=[
            CalculationSourceRef(source_line=10, expression="process_count"),
            CalculationSourceRef(source_line=11, expression="equipment_count"),
        ],
        claimed_query_node_ids=["q1", "q2"],
        claimed_metric_fields=["process_count", "equipment_count"],
        metric_key="candidate_count",
        business_name="Candidate count",
        business_explanation="Count by candidate.",
    )])

    normalized = PostAnalysisEvidenceCompiler._split_distinct_mapping_selector_metrics(
        plan,
        payload,
    )

    assert len(normalized.metrics) == 2
    assert [metric.report_candidate_ids for metric in normalized.metrics] == [
        ["rc1"],
        ["rc2"],
    ]
    assert [metric.operation_node_ids for metric in normalized.metrics] == [
        ["op1"],
        ["op2"],
    ]
    assert all(not metric.calculation_source_refs for metric in normalized.metrics)
    assert all(not metric.claimed_query_node_ids for metric in normalized.metrics)
    assert all(not metric.claimed_metric_fields for metric in normalized.metrics)


def test_mapping_selector_split_compiles_reused_loop_alias_positions():
    sql = "SELECT process_count, equipment_count, population_count FROM risks"
    code = '''
data = load_data_with_sql(sql="SELECT process_count, equipment_count, population_count FROM risks")
population_count = int(data["population_count"].sum())
evidence = {}
evidence["Process"] = {
    "share": data["process_count"].sum() / population_count,
}
evidence["Equipment"] = {
    "share": data["equipment_count"].sum() / population_count,
}
ranked = list(evidence.items())
report_lines = []
for name, values in ranked:
    share = values["share"]
    report_lines.append(f"{name}: {share:.1%}")
analysis_report = "\\n".join(report_lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Process: 25.0%\nEquipment: 50.0%",
        query_snapshots={
            "q_risks": _query_snapshot(
                sql,
                columns=["process_count", "equipment_count", "population_count"],
            ),
        },
    )

    def planner(payload: dict) -> EvidencePlan:
        selectors = {"Process", "Equipment"}
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            report_candidate_ids=[
                item["candidate_id"]
                for item in payload["report_candidate_catalog"]
            ],
            operation_node_ids=[
                item["operation_node_id"]
                for item in payload["operation_catalog"]
                if (item.get("parameter_bindings") or {}).get("mapping_selector")
                in selectors
            ],
            metric_key="evidence_share",
            business_name="Evidence share",
            business_explanation="Share by candidate.",
            value_type="percentage",
            display_unit="%",
        )])

    compiler = PostAnalysisEvidenceCompiler(planner=planner)
    compiler._uses_default_planner = True
    bundle = compiler.compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == [
        "25.0",
        "50.0",
    ]


def test_unresolved_report_outputs_are_deduplicated_and_never_override_claims():
    metric = PlannedMetric(
        calculation_id="report_semantic_scope",
        output_node_ids=["out1"],
        operation_node_ids=["op1"],
        query_node_ids=["q1"],
        metric_key="metric",
        business_name="Metric",
        business_explanation="Confirmed metric.",
    )
    plan = EvidencePlan(
        metrics=[metric],
        unresolved_outputs=[
            UnresolvedEvidenceOutput(output_node_id="out1", reason="stale conflict"),
            UnresolvedEvidenceOutput(output_node_id="out2", reason="unproven"),
            UnresolvedEvidenceOutput(output_node_id="out2", reason="duplicate"),
        ],
    )

    normalized = PostAnalysisEvidenceCompiler._normalize_unresolved_report_outputs(plan)

    assert [item.output_node_id for item in normalized.unresolved_outputs] == ["out2"]


def test_unknown_planner_unresolved_output_does_not_drop_ast_verified_report_refs():
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

    def noisy_planner(_payload: dict) -> EvidencePlan:
        return EvidencePlan(unresolved_outputs=[
            UnresolvedEvidenceOutput(
                output_node_id="out_report_no_longer_exists",
                reason="stale planner unresolved item",
            )
        ])

    bundle = PostAnalysisEvidenceCompiler(planner=noisy_planner).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 1
    assert bundle["diagnostics"].get("reason") is None


def test_report_planner_failure_preserves_structured_artifact_evidence():
    sql = "SELECT product, SUM(amount) AS sales_amount FROM sales GROUP BY product"
    code = r'''
sql = "SELECT product, SUM(amount) AS sales_amount FROM sales GROUP BY product"
data = load_data_with_sql(sql=sql)
total_amount = data["sales_amount"].sum()
analysis_report = f"Sales amount: {total_amount:,.2f}."
table = build_table_result(
    title="Sales by product",
    data=data,
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "sales_amount", "label": "Sales amount"},
    ],
)
result = save_analysis_result(
    analysis_report=analysis_report,
    charts=[],
    tables=[table],
)
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50.",
        tables=[{
            "title": "Sales by product",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "sales_amount", "label": "Sales amount"},
            ],
            "rows": [{"product": "A", "sales_amount": 1280.5}],
        }],
        query_snapshots={
            "q_sales": _query_snapshot(sql, columns=["product", "sales_amount"]),
        },
    )

    def unavailable(_payload: dict) -> EvidencePlan:
        raise TimeoutError("planner timeout")

    bundle = PostAnalysisEvidenceCompiler(planner=unavailable).compile(snapshot)

    assert bundle["status"] == "complete"
    assert len(bundle["table_trace_refs"]) == 1
    assert len(bundle["report_trace_refs"]) == 1
    assert bundle["evidence_definitions"]
    assert bundle["diagnostics"]["reason"] == "planner_unavailable"


def test_table_record_comprehension_keeps_column_lineage_without_llm_recovery():
    sql = "SELECT product, amount FROM sales"
    code = r'''
sql = "SELECT product, amount FROM sales"
data = load_data_with_sql(sql=sql)
summary = data.groupby("product", as_index=False).agg(sales_amount=("amount", "sum"))
table = build_table_result(
    title="Sales by product",
    data=[
        {"Product": row["product"], "Sales amount": float(row["sales_amount"])}
        for row in summary.head(10).to_dict(orient="records")
    ],
    columns=[
        {"key": "Product", "label": "Product"},
        {"key": "Sales amount", "label": "Sales amount"},
    ],
)
result = save_analysis_result(analysis_report="Sales by product.", charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales by product.",
        tables=[{
            "title": "Sales by product",
            "columns": [
                {"key": "Product", "label": "Product"},
                {"key": "Sales amount", "label": "Sales amount"},
            ],
            "rows": [{"Product": "A", "Sales amount": 1280.5}],
        }],
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["product", "amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["table_trace_refs"]) == 1
    assert bundle["table_trace_refs"][0]["column_key"] == "Sales amount"


def test_groupby_selected_rows_rebuilt_as_dataframe_keep_structured_lineage():
    sql = "SELECT batch_id, test_value, spec_upper, spec_lower FROM quality_tests"
    code = r'''
sql = "SELECT batch_id, test_value, spec_upper, spec_lower FROM quality_tests"
data = load_data_with_sql(sql=sql)
data["deviation"] = (
    (data["test_value"] - data["spec_lower"])
    / (data["spec_upper"] - data["spec_lower"])
)
selected_rows = []
for batch_id, group in data.groupby("batch_id"):
    row = group.loc[group["deviation"].abs().idxmax()].copy()
    selected_rows.append(row)
summary = pd.DataFrame(selected_rows) if selected_rows else pd.DataFrame()
chart_data = summary[["batch_id", "deviation"]].copy()
chart_data["absolute_deviation"] = chart_data["deviation"].abs()
chart = build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="batch_id",
    value_field="absolute_deviation",
)
table = build_table_result(
    data=summary,
    columns=[
        {"key": "batch_id", "label": "Batch"},
        {"key": "test_value", "label": "Value"},
        {"key": "deviation", "label": "Deviation"},
    ],
)
result = save_analysis_result(analysis_report="", charts=[chart], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Deviation",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "batch_id",
                    "value_field": "absolute_deviation",
                }
            },
        }],
        tables=[{
            "title": "Deviation details",
            "columns": [
                {"key": "batch_id", "label": "Batch"},
                {"key": "test_value", "label": "Value"},
                {"key": "deviation", "label": "Deviation"},
            ],
            "rows": [{"batch_id": "B1", "test_value": 12.0, "deviation": 1.2}],
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=["batch_id", "test_value", "spec_upper", "spec_lower"],
            ),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(
        planner=lambda _: EvidencePlan(),
        structured_planner=lambda _: EvidencePlan(),
    ).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["chart_trace_refs"]) == 1
    assert {item["column_key"] for item in bundle["table_trace_refs"]} == {
        "test_value",
        "deviation",
    }
    assert {
        query_ref
        for definition in bundle["evidence_definitions"]
        for query_ref in definition["query_refs"]
    } == {"q_quality"}


def test_chart_scalar_selected_from_dynamic_record_mapping_keeps_query_lineage():
    sql = """
SELECT has_alarm, COUNT(*) AS batch_count, AVG(offset_value) AS mean_offset
FROM batch_offsets
GROUP BY has_alarm
"""
    code = r'''
sql = """
SELECT has_alarm, COUNT(*) AS batch_count, AVG(offset_value) AS mean_offset
FROM batch_offsets
GROUP BY has_alarm
"""
data = load_data_with_sql(sql=sql)
offsets = {}
for _, row in data.iterrows():
    offsets[int(row["has_alarm"])] = {
        "batch_count": int(row["batch_count"]),
        "mean_offset": float(row["mean_offset"]),
    }
with_alarm = offsets.get(1, {"batch_count": 0, "mean_offset": 0.0})
without_alarm = offsets.get(0, {"batch_count": 0, "mean_offset": 0.0})
mean_with = with_alarm["mean_offset"]
mean_without = without_alarm["mean_offset"]
chart_data = pd.DataFrame({
    "scope": ["target batches"],
    "with alarm": [mean_with],
    "without alarm": [mean_without],
})
charts = [build_multi_metric_chart_result(
    data=chart_data,
    category_field="scope",
    value_fields=["with alarm", "without alarm"],
)]
result = save_analysis_result(analysis_report="", charts=charts, tables=[])
'''
    snapshot = _snapshot(
        code=code,
        charts=[{
            "title": "Offset comparison",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "scope",
                    "value_fields": ["with alarm", "without alarm"],
                }
            },
        }],
        query_snapshots={
            "q_offsets": _query_snapshot(
                sql,
                columns=["has_alarm", "batch_count", "mean_offset"],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    outputs = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "chart"
    ]

    assert len(outputs) == 2
    assert all(output["candidate_query_node_ids"] == ["q_offsets"] for output in outputs)
    operation_by_id = {
        item["operation_node_id"]: item
        for item in lineage["operation_nodes"]
    }
    for output in outputs:
        boundary_fields = {
            field
            for operation_id in output["boundary_operation_node_ids"]
            for field in operation_by_id[operation_id].get("targets") or []
        }
        assert "mean_offset" in boundary_fields
        assert "batch_count" not in boundary_fields


def test_ranked_row_numeric_projection_remains_a_report_metric():
    sql = "SELECT cause, batch_id FROM risk_events"
    code = r'''
sql = "SELECT cause, batch_id FROM risk_events"
data = load_data_with_sql(sql=sql)
ranking = data.groupby("cause")["batch_id"].nunique().reset_index(name="batch_count")
ranking = ranking.sort_values("batch_count", ascending=False)
top_cause = ranking.iloc[0]["cause"]
top_count = ranking.iloc[0]["batch_count"]
analysis_report = f"Top cause is {top_cause}, covering {top_count} batches."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Top cause is process variation, covering 38 batches.",
        query_snapshots={
            "q_risks": _query_snapshot(sql, columns=["cause", "batch_id"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["38"]


def test_invalid_report_metric_does_not_erase_verified_structured_evidence(monkeypatch):
    sql = "SELECT product, SUM(amount) AS sales_amount FROM sales GROUP BY product"
    code = r'''
sql = "SELECT product, SUM(amount) AS sales_amount FROM sales GROUP BY product"
data = load_data_with_sql(sql=sql)
total_amount = data["sales_amount"].sum()
analysis_report = f"Sales amount: {total_amount:,.2f}."
table = build_table_result(
    title="Sales by product",
    data=data,
    columns=[
        {"key": "product", "label": "Product"},
        {"key": "sales_amount", "label": "Sales amount"},
    ],
)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[table])
'''
    snapshot = _snapshot(
        code=code,
        report="Sales amount: 1,280.50.",
        tables=[{
            "title": "Sales by product",
            "columns": [
                {"key": "product", "label": "Product"},
                {"key": "sales_amount", "label": "Sales amount"},
            ],
            "rows": [{"product": "A", "sales_amount": 1280.5}],
        }],
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["product", "sales_amount"])},
    )
    original_errors = PostAnalysisEvidenceCompiler._plan_reference_errors
    calls = {"count": 0, "bad_metric_key": ""}

    def reject_one_metric(plan, candidates, require_complete=False):
        errors = original_errors(plan, candidates, require_complete=require_complete)
        calls["count"] += 1
        if errors:
            return errors
        if calls["bad_metric_key"] and any(
            metric.metric_key == calls["bad_metric_key"] for metric in plan.metrics
        ):
            return ["metrics[0] selects queries not reachable from its operations: ['q_wrong']"]
        if len(plan.metrics) > 1:
            bad_metric = next(
                (
                    metric for metric in plan.metrics
                    if any("report" in str(output_id) for output_id in metric.output_node_ids)
                ),
                plan.metrics[-1],
            )
            calls["bad_metric_key"] = bad_metric.metric_key
            return ["metrics[1] selects queries not reachable from its operations: ['q_wrong']"]
        return []

    monkeypatch.setattr(
        PostAnalysisEvidenceCompiler,
        "_plan_reference_errors",
        staticmethod(reject_one_metric),
    )

    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)

    assert bundle["evidence_definitions"]
    assert bundle["table_trace_refs"]
    assert bundle["status"] in {"complete", "partial"}
    assert calls["bad_metric_key"]
    assert calls["count"] > 1


def test_unproven_operation_reachability_does_not_reject_llm_source_locator():
    candidates = {
        "calc_unproven": {
            "calculation_id": "calc_unproven",
            "output_node_ids": ["out_report"],
            "operation_node_ids": ["op_recovered"],
            "boundary_operation_node_ids": ["op_recovered"],
            "query_node_ids": ["q_sales"],
            "operation_query_ids": {"op_recovered": ["q_sales"]},
            "output_operation_ids": {"out_report": []},
            "semantic_scope": True,
            "ambiguous_input": False,
        },
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="calc_unproven",
        output_node_ids=["out_report"],
        operation_node_ids=["op_recovered"],
        query_node_ids=["q_sales"],
        metric_key="sales",
        business_name="Sales",
        business_explanation="Recovered from an exact source locator.",
    )])

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(
        plan,
        candidates,
        require_complete=False,
    )

    assert not any("do not reach its outputs" in error for error in errors)


def test_proven_conflicting_operation_reachability_rejects_llm_binding():
    candidates = {
        "calc_conflict": {
            "calculation_id": "calc_conflict",
            "output_node_ids": ["out_report"],
            "operation_node_ids": ["op_actual", "op_wrong"],
            "boundary_operation_node_ids": ["op_actual", "op_wrong"],
            "query_node_ids": ["q_sales"],
            "operation_query_ids": {
                "op_actual": ["q_sales"],
                "op_wrong": ["q_sales"],
            },
            "output_operation_ids": {"out_report": ["op_actual"]},
            "semantic_scope": True,
            "ambiguous_input": False,
        },
    }
    plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id="calc_conflict",
        output_node_ids=["out_report"],
        operation_node_ids=["op_wrong"],
        query_node_ids=["q_sales"],
        metric_key="sales",
        business_name="Sales",
        business_explanation="Wrong operation for the output.",
    )])

    errors = PostAnalysisEvidenceCompiler._plan_reference_errors(
        plan,
        candidates,
        require_complete=False,
    )

    assert any("do not reach its outputs" in error for error in errors)


def test_ambiguous_report_candidate_is_resolved_by_llm_selection_then_locally_verified():
    lineage = {
        "operation_nodes": [
            {
                "operation_node_id": "op_amount",
                "line": 10,
                "kind": "calculation",
                "semantic_kind": "aggregation",
                "query_node_id": "q_amount",
                "targets": ["amount"],
                "loads": [],
                "source": "amount = amount_data['amount'].sum()",
            },
            {
                "operation_node_id": "op_count",
                "line": 11,
                "kind": "calculation",
                "semantic_kind": "aggregation",
                "query_node_id": "q_count",
                "targets": ["count"],
                "loads": [],
                "source": "count = len(count_data)",
            },
            {
                "operation_node_id": "op_rank",
                "line": 12,
                "kind": "calculation",
                "semantic_kind": "ranking",
                "query_node_id": "q_amount",
                "targets": ["max_amount_idx"],
                "loads": [],
                "source": "max_amount_idx = amount_data['amount'].idxmax()",
            },
        ],
        "output_nodes": [{
            "output_node_id": "out_report_value",
            "artifact_type": "report",
            "field": "value",
            "lineage_fields": ["value"],
            "numeric_hint": True,
            "ambiguous_calculation_boundary": True,
            "boundary_operation_node_ids": ["op_amount", "op_count", "op_rank"],
            "candidate_operation_node_ids": ["op_amount", "op_count", "op_rank"],
            "candidate_query_node_ids": ["q_amount", "q_count"],
        }],
        "literal_iteration_bindings": [],
    }

    candidates = PostAnalysisEvidenceCompiler._build_calculation_candidates(lineage)
    candidate = next(iter(candidates.values()))
    resolved_plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id=candidate["calculation_id"],
        output_node_ids=["out_report_value"],
        operation_node_ids=["op_amount"],
        query_node_ids=["q_amount"],
        metric_key="total_amount",
        business_name="Total amount",
        business_explanation="Sum of amount in the selected scope.",
    )])
    mismatched_plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id=candidate["calculation_id"],
        output_node_ids=["out_report_value"],
        operation_node_ids=["op_amount"],
        query_node_ids=["q_count"],
        metric_key="total_amount",
        business_name="Total amount",
        business_explanation="Sum of amount in the selected scope.",
    )])
    ranked_plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id=candidate["calculation_id"],
        output_node_ids=["out_report_value"],
        operation_node_ids=["op_amount", "op_rank"],
        query_node_ids=["q_amount"],
        metric_key="largest_amount",
        business_name="Largest amount",
        business_explanation="Amount selected from the row with the largest value.",
    )])
    mixed_plan = EvidencePlan(metrics=[PlannedMetric(
        calculation_id=candidate["calculation_id"],
        output_node_ids=["out_report_value"],
        operation_node_ids=["op_amount", "op_count"],
        query_node_ids=["q_amount", "q_count"],
        metric_key="mixed_metric",
        business_name="Mixed metric",
        business_explanation="Incorrectly mixes unrelated calculations.",
    )])

    assert candidate["ambiguous_input"] is True
    assert PostAnalysisEvidenceCompiler._plan_reference_errors(
        resolved_plan,
        candidates,
    ) == []
    assert PostAnalysisEvidenceCompiler._plan_reference_errors(
        ranked_plan,
        candidates,
    ) == []
    assert any(
        "not reachable" in error
        for error in PostAnalysisEvidenceCompiler._plan_reference_errors(
            mismatched_plan,
            candidates,
        )
    )
    assert any(
        "ambiguous calculation boundary" in error
        for error in PostAnalysisEvidenceCompiler._plan_reference_errors(
            mixed_plan,
            candidates,
        )
    )


def test_verified_report_completion_leaves_ambiguous_loop_output_for_planner():
    lineage = {
        "query_nodes": [{"query_node_id": "q_root"}],
        "operation_nodes": [],
        "output_nodes": [{
            "output_node_id": "out_report_value",
            "artifact_type": "report",
            "field": "value",
            "numeric_hint": True,
            "boundary_operation_node_ids": ["op_a", "op_b"],
            "candidate_operation_node_ids": ["op_a", "op_b"],
            "candidate_query_node_ids": ["q_root"],
        }],
    }
    candidates = {
        "calc_ambiguous": {
            "calculation_id": "calc_ambiguous",
            "output_node_ids": ["out_report_value"],
            "operation_node_ids": ["op_a", "op_b"],
            "query_node_ids": ["q_root"],
            "ambiguous_input": True,
        }
    }

    plan = PostAnalysisEvidenceCompiler._build_verified_report_completion_plan(
        EvidencePlan(), candidates, lineage,
    )

    assert plan.metrics == []


def test_ambiguous_template_occurrences_are_independent_semantic_candidates():
    sql = "SELECT material_count, retry_count FROM root_causes"
    code = r'''
sql = "SELECT material_count, retry_count FROM root_causes"
data = load_data_with_sql(sql=sql)
material_count = data["material_count"].sum()
retry_count = data["retry_count"].sum()
ranking = [("Material", material_count), ("Retest", retry_count)]
lines = []
for index, (name, value) in enumerate(ranking, 1):
    lines.append(f"{index}. {name}: {value}")
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="1. Material: 120\n2. Retest: 34",
        query_snapshots={
            "q_root": _query_snapshot(
                sql, columns=["material_count", "retry_count"]
            )
        },
    )
    compiler = PostAnalysisEvidenceCompiler()
    lineage = LineageIndexer().build(snapshot)
    calculations = compiler._build_calculation_candidates(lineage)
    semantic = compiler._build_report_semantic_candidates(
        lineage, calculations,
    )
    verified = compiler._build_verified_report_completion_plan(
        EvidencePlan(), calculations, lineage,
    )
    payload = compiler._planner_fact_payload(
        snapshot,
        lineage,
        semantic,
        recovery_only=True,
        excluded_output_ids={
            output_id
            for metric in verified.metrics
            for output_id in metric.output_node_ids
        },
    )

    candidates = payload["report_candidate_catalog"]
    assert len(candidates) == 2
    assert {item["line"] for item in candidates} == {
        "1. Material: 120", "2. Retest: 34",
    }
    assert payload["source_anchor_catalog"] == []


def test_report_recovery_query_catalog_exposes_python_result_binding():
    sql = "SELECT status, COUNT(*) AS total FROM contracts GROUP BY status"
    code = r'''
sql = "SELECT status, COUNT(*) AS total FROM contracts GROUP BY status"
status_rows = load_data_with_sql(sql=sql)
summary = status_rows.iloc[0]
total = summary["total"]
analysis_report = f"Contracts: {total}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Contracts: 12.",
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "total"]),
        },
    )
    compiler = PostAnalysisEvidenceCompiler()
    lineage = LineageIndexer().build(snapshot)
    calculations = compiler._build_calculation_candidates(lineage)
    semantic = compiler._build_report_semantic_candidates(
        lineage, calculations,
    )

    payload = compiler._planner_fact_payload(
        snapshot,
        lineage,
        semantic,
        recovery_only=True,
        excluded_output_ids=set(),
    )

    query = next(
        item for item in payload["query_catalog"]
        if item["query_node_id"] == "q1"
    )
    assert query["result_bindings"] == [{
        "variable": "status_rows",
        "source_line": 3,
    }]


def test_sorted_dynamic_template_without_local_closure_exposes_each_occurrence():
    sql = "SELECT material_count, retry_count FROM root_causes"
    code = r'''
sql = "SELECT material_count, retry_count FROM root_causes"
data = load_data_with_sql(sql=sql)
material_count = int(data["material_count"].sum())
retry_count = int(data["retry_count"].sum())
ranking = [("Material", material_count), ("Retest", retry_count)]
ranking = sorted(ranking, key=lambda item: item[1], reverse=True)
lines = []
rank = 1
for name, value in ranking:
    lines.append(f"{rank}. **{name}**: {value} batches")
    rank += 1
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="1. **Material**: 120 batches\n2. **Retest**: 34 batches",
        query_snapshots={
            "q_root": _query_snapshot(
                sql, columns=["material_count", "retry_count"]
            )
        },
    )
    compiler = PostAnalysisEvidenceCompiler()
    lineage = LineageIndexer().build(snapshot)
    calculations = compiler._build_calculation_candidates(lineage)
    semantic = compiler._build_report_semantic_candidates(
        lineage, calculations,
    )
    payload = compiler._planner_fact_payload(
        snapshot,
        lineage,
        semantic,
        recovery_only=True,
        excluded_output_ids=set(),
    )

    candidates = payload["report_candidate_catalog"]
    assert len(candidates) == 2
    assert {item["text_examples"][0] for item in candidates} == {"120", "34"}
    assert {item["line"] for item in candidates} == {
        "1. **Material**: 120 batches",
        "2. **Retest**: 34 batches",
    }
    assert payload["source_anchor_catalog"] == []


def test_ranked_mapping_exposes_each_labelled_calculation_to_semantic_planner():
    equipment_sql = "SELECT fault_event_count FROM equipment_events"
    packaging_sql = "SELECT package_id FROM packaging_events"
    code = r'''
equipment = load_data_with_sql(sql="SELECT fault_event_count FROM equipment_events")
packaging = load_data_with_sql(sql="SELECT package_id FROM packaging_events")
scores = {
    "Equipment": equipment["fault_event_count"].sum(),
    "Packaging": packaging["package_id"].count(),
}
ranking = sorted(scores.items(), key=lambda item: item[1], reverse=True)
lines = []
for rank, (category, score) in enumerate(ranking, 1):
    lines.append(f"{rank}. **{category}**: evidence strength {score}")
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report=(
            "1. **Packaging**: evidence strength 66\n"
            "2. **Equipment**: evidence strength 1"
        ),
        query_snapshots={
            "q_equipment": _query_snapshot(
                equipment_sql, columns=["fault_event_count"]
            ),
            "q_packaging": _query_snapshot(
                packaging_sql, columns=["package_id"]
            ),
        },
    )
    compiler = PostAnalysisEvidenceCompiler()
    lineage = LineageIndexer().build(snapshot)
    members = {
        target: operation
        for operation in lineage["operation_nodes"]
        for target in operation.get("targets") or []
        if target.startswith("scores[")
    }

    assert set(members) == {"scores['Equipment']", "scores['Packaging']"}
    assert members["scores['Equipment']"]["loads"] == ["equipment"]
    assert members["scores['Packaging']"]["loads"] == ["packaging"]

    calculations = compiler._build_calculation_candidates(lineage)
    semantic = compiler._build_report_semantic_candidates(lineage, calculations)
    payload = compiler._planner_fact_payload(
        snapshot,
        lineage,
        semantic,
        recovery_only=True,
        excluded_output_ids=set(),
    )
    operation_sources = {
        item["expression"] for item in payload["operation_catalog"]
    }
    assert any(
        "scores['Packaging']" in source and "package_id" in source
        for source in operation_sources
    )


def test_sorted_dynamic_template_semantic_sources_survive_local_verification():
    sql = "SELECT material_count, retry_count FROM root_causes"
    code = r'''
sql = "SELECT material_count, retry_count FROM root_causes"
data = load_data_with_sql(sql=sql)
material_count = int(data["material_count"].sum())
retry_count = int(data["retry_count"].sum())
ranking = [("Material", material_count), ("Retest", retry_count)]
ranking = sorted(ranking, key=lambda item: item[1], reverse=True)
lines = []
rank = 1
for name, value in ranking:
    lines.append(f"{rank}. **{name}**: {value} batches")
    rank += 1
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="1. **Material**: 120 batches\n2. **Retest**: 34 batches",
        query_snapshots={
            "q_root": _query_snapshot(
                sql, columns=["material_count", "retry_count"]
            )
        },
    )

    def resolve(payload: dict) -> EvidencePlan:
        metrics = []
        for candidate in payload["report_candidate_catalog"]:
            is_material = "Material" in candidate["line"]
            metrics.append(PlannedMetric(
                calculation_id="report_semantic_scope",
                report_candidate_ids=[candidate["candidate_id"]],
                calculation_source_refs=[CalculationSourceRef(
                    source_line=4 if is_material else 5,
                    expression=(
                        'int(data["material_count"].sum())'
                        if is_material
                        else 'int(data["retry_count"].sum())'
                    ),
                )],
                claimed_query_node_ids=["q_root"],
                claimed_metric_fields=[
                    "material_count" if is_material else "retry_count"
                ],
                metric_key="material" if is_material else "retry",
                business_name="Material" if is_material else "Retest",
                business_explanation="Count of affected batches.",
                display_unit="batches",
            ))
        return EvidencePlan(metrics=metrics)

    bundle = PostAnalysisEvidenceCompiler(planner=resolve).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["120", "34"]
    assert refs[0]["evidence_key"] != refs[1]["evidence_key"]


def test_invalid_posthoc_metric_does_not_erase_independent_evidence():
    candidates = {
        "calc_valid": {
            "calculation_id": "calc_valid",
            "output_node_ids": ["out_chart"],
            "operation_node_ids": ["op_valid"],
            "query_node_ids": ["q_valid"],
            "operation_query_ids": {"op_valid": ["q_valid"]},
        },
        "calc_invalid": {
            "calculation_id": "calc_invalid",
            "output_node_ids": ["out_report"],
            "operation_node_ids": ["op_a", "op_b"],
            "query_node_ids": ["q_valid"],
            "operation_query_ids": {
                "op_a": ["q_valid"],
                "op_b": ["q_valid"],
            },
            "ambiguous_input": True,
            "operation_family_signatures": {"op_a": "a", "op_b": "b"},
            "operation_write_signatures": {"op_a": "a", "op_b": "b"},
            "operation_semantic_kinds": {
                "op_a": "aggregation",
                "op_b": "aggregation",
            },
        },
    }
    valid = PlannedMetric(
        calculation_id="calc_valid",
        output_node_ids=["out_chart"],
        operation_node_ids=["op_valid"],
        query_node_ids=["q_valid"],
        metric_key="valid",
        business_name="Valid",
        business_explanation="Valid boundary",
    )
    invalid = PlannedMetric(
        calculation_id="calc_invalid",
        output_node_ids=["out_report"],
        operation_node_ids=["op_a", "op_b"],
        query_node_ids=["q_valid"],
        metric_key="invalid",
        business_name="Invalid",
        business_explanation="Ambiguous boundary",
    )

    isolated = PostAnalysisEvidenceCompiler._isolate_invalid_metrics(
        EvidencePlan(metrics=[valid, invalid]), candidates,
    )

    assert [metric.metric_key for metric in isolated.metrics] == ["valid"]
    assert [item.output_node_id for item in isolated.unresolved_outputs] == [
        "out_report"
    ]


def test_structured_chart_keeps_query_lineage_through_dataframe_helper_function():
    sql = "SELECT product_name, batch_id FROM quality_batches"
    code = r'''
data = load_data_with_sql(sql="SELECT product_name, batch_id FROM quality_batches")
near_batches = data[data["batch_id"].notna()]
def product_concentration(frame):
    if frame.empty:
        return pd.DataFrame(columns=["product_name", "batch_count"])
    result = frame.groupby("product_name").agg(
        batch_count=("batch_id", "nunique")
    ).reset_index()
    return result.sort_values("batch_count", ascending=False)
near_products = product_concentration(near_batches)
chart = build_chart_result(
    data=near_products.head(8),
    chart_kind="bar",
    category_field="product_name",
    value_field="batch_count",
)
result = save_analysis_result(analysis_report="Near-limit distribution.", charts=[chart], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Near-limit distribution.",
        charts=[{
            "title": "Near-limit distribution",
            "chart_data_contract": {
                "encoding": {
                    "category_field": "product_name",
                    "value_field": "batch_count",
                },
            },
        }],
        query_snapshots={
            "q_quality": _query_snapshot(
                sql, columns=["product_name", "batch_id"]
            ),
        },
    )

    lineage = LineageIndexer().build(snapshot)
    output = next(
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "chart"
        and item.get("field") == "batch_count"
    )

    assert output["candidate_query_node_ids"] == ["q_quality"]
    assert len(output["boundary_operation_node_ids"]) == 1


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


def test_compiled_horizontal_bar_selection_reads_category_from_y_axis():
    service = InsightNsConversationService(None)
    artifact = {
        "id": 22,
        "artifact_type": "chart",
        "content_json": {
            "chart_spec": {
                "xAxis": {"type": "value"},
                "yAxis": {"type": "category", "data": ["HDPE", "PP"]},
                "series": [{"name": "高风险批次", "data": [2, 1]}],
            },
            "chart_data_contract": {
                "encoding": {
                    "category_field": "product",
                    "value_field": "batch_count",
                    "value_fields": ["batch_count"],
                },
            },
        },
    }

    context = service._build_selected_context(
        artifact,
        {"data_index": 0, "series_index": 0, "value_field": "batch_count"},
    )

    assert context["raw_value"] == 2
    assert context["dimensions"] == {"product": "HDPE", "series_name": "高风险批次"}


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


def test_report_list_comprehension_rows_keep_numeric_slot_lineage():
    sql = "SELECT status, amount FROM contracts"
    code = r'''
sql = "SELECT status, amount FROM contracts"
data = load_data_with_sql(sql=sql)
summary = data.groupby("status", as_index=False).agg(
    contract_count=("status", "size"),
    total_amount=("amount", "sum"),
)
report_sections = ["## Detail"]
detail_lines = [
    f"- {row['status']}: {row['contract_count']} contracts, {row['total_amount']:,.2f} yuan."
    for _, row in summary.iterrows()
]
report_sections.extend(detail_lines)
analysis_report = "\n".join(report_sections)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report=(
            "## Detail\n"
            "- active: 2 contracts, 500.00 yuan.\n"
            "- pending: 1 contracts, 100.00 yuan."
        ),
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "amount"])
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in sorted(
        bundle["report_trace_refs"],
        key=lambda item: item["start"],
    )] == [
        "2", "500.00", "1", "100.00",
    ]


def test_joined_comprehension_fragment_in_report_keeps_numeric_slots():
    sql = "SELECT status FROM contracts"
    code = r'''
sql = "SELECT status FROM contracts"
data = load_data_with_sql(sql=sql)
summary = data.groupby("status").size().to_dict()
detail = "、".join(f"{name} {count} times" for name, count in summary.items())
analysis_report = f"## Summary\nBreakdown: {detail}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\nBreakdown: active 2 times、pending 1 times.",
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["status"])} ,
    )

    lineage = LineageIndexer().build(snapshot)
    report_outputs = [
        item for item in lineage["output_nodes"]
        if item["artifact_type"] == "report"
    ]

    assert [item["text"] for item in sorted(
        report_outputs,
        key=lambda item: item["start"],
    )] == ["2", "1"]
    assert any(
        not item.get("posthoc_report_candidate")
        for item in report_outputs
    )


def test_report_concatenation_with_joined_row_fragments_keeps_all_numeric_slots():
    sql = "SELECT status, amount FROM contracts"
    code = r'''
sql = "SELECT status, amount FROM contracts"
data = load_data_with_sql(sql=sql)
total_count = len(data)
total_amount = data["amount"].sum()
status_detail = data.groupby("status", as_index=False).agg(
    contract_count=("status", "size"),
    total_amount=("amount", "sum"),
)
status_lines = []
for _, row in status_detail.iterrows():
    status_lines.append(
        f"- {row['status']}: {int(row['contract_count'])} contracts, "
        f"amount {row['total_amount']:,.2f}."
    )
analysis_report = (
    f"Total: {total_count} contracts, amount {total_amount:,.2f}.\n"
    "## Detail\n"
    + "\n".join(status_lines)
)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "Total: 3 contracts, amount 600.00.\n"
        "## Detail\n"
        "- active: 2 contracts, amount 500.00.\n"
        "- pending: 1 contracts, amount 100.00."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_contracts": _query_snapshot(sql, columns=["status", "amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == [
        "3", "600.00", "2", "500.00", "1", "100.00",
    ]
    metric_fields = {
        field
        for definition in bundle["evidence_definitions"]
        for field in definition["calculation"]["metric_fields"]
    }
    assert metric_fields == {"contract_count", "total_amount", "total_count"}


def test_report_augmented_assignment_keeps_repeated_numeric_slots():
    sql = "SELECT product_name, status FROM quality_events"
    code = r'''
sql = "SELECT product_name, status FROM quality_events"
data = load_data_with_sql(sql=sql)
total_count = len(data)
summary = data.groupby("product_name", as_index=False).agg(
    event_count=("status", "size"),
)
analysis_report = f"Total events: {total_count}."
for _, row in summary.iterrows():
    share = row["event_count"] / total_count * 100
    analysis_report += f"\n- {row['product_name']}: {row['event_count']} events ({share:.1f}%)."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "Total events: 5.\n"
        "- Alpha: 3 events (60.0%).\n"
        "- Beta: 2 events (40.0%)."
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_quality": _query_snapshot(sql, columns=["product_name", "status"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [
        item["text"]
        for item in sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    ] == ["5", "3", "60.0", "2", "40.0"]


def test_report_embedded_joined_builder_keeps_repeated_numeric_slots():
    sql = "SELECT signal_name, occurrence_count FROM process_signals"
    code = r'''
sql = "SELECT signal_name, occurrence_count FROM process_signals"
data = load_data_with_sql(sql=sql)
signal_lines = []
for _, row in data.iterrows():
    signal_lines.append(
        f"- {row['signal_name']}: {int(row['occurrence_count'])} occurrences"
    )
signal_text = "\n".join(signal_lines) if signal_lines else "No signals"
analysis_report = f"""## Signals
{signal_text}
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = "## Signals\n- Temperature: 3 occurrences\n- Pressure: 2 occurrences\n"
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_signals": _query_snapshot(
                sql,
                columns=["signal_name", "occurrence_count"],
            ),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [
        item["text"]
        for item in sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    ] == ["3", "2"]


def test_multiline_report_fragment_does_not_cross_bind_similar_numeric_slots():
    sql = "SELECT status FROM quality_events"
    code = r'''
sql = "SELECT status FROM quality_events"
data = load_data_with_sql(sql=sql)
total = len(data)
oos_count = (data["status"] == "oos").sum()
near_count = (data["status"] == "near").sum()
oos_pct = oos_count / total * 100
near_pct = near_count / total * 100
detail_lines = ["- Temperature alarm", "- Equipment stop"]
detail_text = "\n".join(detail_lines)
analysis_report = f"""## Summary
- OOS: {oos_count} ({oos_pct:.1f}%)
- Near: {near_count} ({near_pct:.1f}%)
## Detail
{detail_text}
## Total
{total}
"""
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = (
        "## Summary\n"
        "- OOS: 0 (0.0%)\n"
        "- Near: 10 (41.7%)\n"
        "## Detail\n"
        "- Temperature alarm\n"
        "- Equipment stop\n"
        "## Total\n24\n"
    )
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={"q_quality": _query_snapshot(sql, columns=["status"])},
    )

    lineage = LineageIndexer().build(snapshot)
    outputs_by_text = {
        (item["text"], item["start"]): item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
    }
    near_start = report.index("41.7")

    assert outputs_by_text[("41.7", near_start)]["field"] == "near_pct"


def test_mapping_item_fields_keep_distinct_calculation_boundaries():
    sql = "SELECT batch_id, alarm_count FROM process_signals"
    code = r'''
sql = "SELECT batch_id, alarm_count FROM process_signals"
data = load_data_with_sql(sql=sql)
total = data["alarm_count"].sum()
affected = (data.groupby("batch_id")["alarm_count"].sum() > 0).sum()
summary = {}
summary["Alarm"] = {
    "total_occurrences": int(total),
    "batches_with_evidence": int(affected),
}
lines = []
for label, info in summary.items():
    lines.append(
        f"- {label}: {info['total_occurrences']} occurrences, "
        f"{info['batches_with_evidence']} batches"
    )
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = "- Alarm: 7 occurrences, 3 batches"
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_signals": _query_snapshot(sql, columns=["batch_id", "alarm_count"]),
        },
    )

    def resolver(payload: dict) -> EvidencePlan:
        operation_by_id = {
            item["operation_node_id"]: item for item in payload["operation_catalog"]
        }
        metrics = []
        for index, anchor in enumerate(payload["source_anchor_catalog"], start=1):
            hints = anchor.get("verification_hints") or []
            if not hints:
                continue
            wants_grouped = "batches_with_evidence" in str(anchor.get("expression") or "")
            available = list(dict.fromkeys(
                operation_id
                for hint in hints
                for operation_id in hint.get("operation_node_ids") or []
            ))
            selected = [
                operation_id for operation_id in available
                if ("groupby" in operation_by_id[operation_id]["expression"]) == wants_grouped
            ] or available
            metrics.append(PlannedMetric(
                calculation_id="report_semantic_scope",
                source_anchors=[ReportSourceAnchorRef(anchor_id=anchor["anchor_id"])],
                operation_node_ids=selected,
                metric_key=f"process_metric_{index}",
                business_name=f"Process metric {index}",
                business_explanation="Calculated from the confirmed process records.",
            ))
        return EvidencePlan(metrics=metrics)

    bundle = PostAnalysisEvidenceCompiler(planner=resolver).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["7", "3"]
    assert refs[0]["evidence_key"] != refs[1]["evidence_key"]
    definitions = {
        item["evidence_key"]: item for item in bundle["evidence_definitions"]
    }
    assert definitions[refs[0]["evidence_key"]]["calculation"]["operation_node_ids"] != (
        definitions[refs[1]["evidence_key"]]["calculation"]["operation_node_ids"]
    )


def test_loop_rendered_report_field_recovers_local_query_closure_from_direct_expression():
    sql = "SELECT batch_id, alarm_count FROM process_signals"
    code = r'''
sql = "SELECT batch_id, alarm_count FROM process_signals"
data = load_data_with_sql(sql=sql)
summary = {"affected_batches": int((data.groupby("batch_id")["alarm_count"].sum() > 0).sum())}
for _, row in pd.DataFrame([summary]).iterrows():
    analysis_report = f"Affected batches: {row['affected_batches']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Affected batches: 3.",
        query_snapshots={
            "q_signals": _query_snapshot(sql, columns=["batch_id", "alarm_count"]),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    target = next(
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report" and item.get("text") == "3"
    )
    # Simulate the known AST walk gap: the rendered loop expression is known,
    # but its broad candidate walk did not populate query IDs.
    target["candidate_query_node_ids"] = []
    target["boundary_operation_node_ids"] = [next(
        item["operation_node_id"]
        for item in lineage["operation_nodes"]
        if item.get("targets") == ["summary"]
    )]

    candidates = PostAnalysisEvidenceCompiler._build_calculation_candidates(lineage)

    candidate = next(
        item
        for item in candidates.values()
        if target["output_node_id"] in item["output_node_ids"]
    )
    assert candidate["query_node_ids"] == ["q_signals"]
    assert target["output_node_id"] in candidate["output_node_ids"]


def test_list_of_mapping_report_field_uses_record_write_query_lineage():
    sql = "SELECT parameter_name, alarm_flag FROM process_signals"
    code = r'''
sql = "SELECT parameter_name, alarm_flag FROM process_signals"
data = load_data_with_sql(sql=sql)
alarm_summary = []
for label in ["temperature", "pressure"]:
    cnt = int(((data["parameter_name"] == label) & (data["alarm_flag"] > 0)).sum())
    alarm_summary.append({"parameter_name": label, "alarm_count": cnt})
analysis_report = f"Temperature alarms: {alarm_summary[0]['alarm_count']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Temperature alarms: 2.",
        query_snapshots={
            "q_signals": _query_snapshot(
                sql,
                columns=["parameter_name", "alarm_flag"],
            ),
        },
    )
    lineage = LineageIndexer().build(snapshot)
    target = next(
        item
        for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report" and item.get("text") == "2"
    )
    assert target["boundary_operation_node_ids"] == []

    candidates = PostAnalysisEvidenceCompiler._build_calculation_candidates(lineage)

    candidate = next(
        item
        for item in candidates.values()
        if target["output_node_id"] in item["output_node_ids"]
    )
    assert candidate["query_node_ids"] == ["q_signals"]
    assert candidate["boundary_operation_node_ids"]


def test_semantic_planner_merged_mapping_fields_are_split_by_proven_boundaries():
    sql = "SELECT batch_id, alarm_count FROM process_signals"
    code = r'''
sql = "SELECT batch_id, alarm_count FROM process_signals"
data = load_data_with_sql(sql=sql)
indicators = [("alarm_count", "Alarm")]
summary = {}
for column, label in indicators:
    total = data[column].sum()
    affected = (data.groupby("batch_id")[column].sum() > 0).sum()
    summary[label] = {
        "total_occurrences": int(total),
        "batches_with_evidence": int(affected),
    }
lines = []
for label, info in summary.items():
    lines.append(
        f"- {label}: {info['total_occurrences']} occurrences, "
        f"{info['batches_with_evidence']} batches"
    )
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="- Alarm: 7 occurrences, 3 batches",
        query_snapshots={
            "q_signals": _query_snapshot(sql, columns=["batch_id", "alarm_count"]),
        },
    )

    def merged_plan(payload: dict) -> EvidencePlan:
        operation_by_id = {
            item["operation_node_id"]: item
            for item in payload["operation_catalog"]
        }
        selected_operations = [
            operation_id for operation_id in operation_by_id
            if (
                "data[column].sum()" in operation_by_id[operation_id]["expression"]
                or "groupby" in operation_by_id[operation_id]["expression"]
            )
        ]
        anchors = [
            ReportSourceAnchorRef(anchor_id=item["anchor_id"])
            for item in payload["source_anchor_catalog"]
            if item.get("verification_hints")
        ]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=anchors,
            operation_node_ids=selected_operations,
            metric_key="alarm_evidence",
            business_name="Alarm evidence",
            business_explanation="Alarm occurrence and affected-batch evidence.",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=merged_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["7", "3"]
    assert refs[0]["evidence_key"] != refs[1]["evidence_key"]
    definitions = {
        item["evidence_key"]: item for item in bundle["evidence_definitions"]
    }
    assert definitions[refs[0]["evidence_key"]]["calculation"]["operation_node_ids"] != (
        definitions[refs[1]["evidence_key"]]["calculation"]["operation_node_ids"]
    )


def test_parameterized_metric_field_loop_creates_distinct_evidence_boundaries():
    sql = "SELECT batch_id, alarm_count, fault_count FROM process_signals"
    code = r'''
sql = "SELECT batch_id, alarm_count, fault_count FROM process_signals"
data = load_data_with_sql(sql=sql)
indicators = [
    ("alarm_count", "Alarm"),
    ("fault_count", "Fault"),
]
summary = {}
for column, label in indicators:
    total = data[column].sum()
    affected = (data.groupby("batch_id")[column].sum() > 0).sum()
    summary[label] = {
        "total_occurrences": int(total),
        "batches_with_evidence": int(affected),
    }
lines = []
for label, info in summary.items():
    lines.append(
        f"- {label}: {info['total_occurrences']} occurrences, "
        f"{info['batches_with_evidence']} batches"
    )
analysis_report = "\n".join(lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = "- Alarm: 7 occurrences, 3 batches\n- Fault: 2 occurrences, 1 batches"
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_signals": _query_snapshot(
                sql,
                columns=["batch_id", "alarm_count", "fault_count"],
            ),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["7", "3", "2", "1"]
    assert refs[0]["evidence_key"] != refs[2]["evidence_key"]
    definitions = {
        item["evidence_key"]: item for item in bundle["evidence_definitions"]
    }
    assert definitions[refs[0]["evidence_key"]]["calculation"]["metric_fields"] == [
        "alarm_count"
    ]
    assert definitions[refs[2]["evidence_key"]]["calculation"]["metric_fields"] == [
        "fault_count"
    ]


def test_unresolved_ast_report_numeric_slot_makes_compilation_partial():
    sql = "SELECT amount FROM sales"
    code = r'''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
total = data["amount"].sum()
external_value = read_external_value()
analysis_report = f"Total: {total:.2f}; external: {external_value:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total: 20.00; external: 9.00.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "partial"
    assert bundle["diagnostics"]["metric_output_count"] == 2
    assert len(bundle["diagnostics"]["resolver_unresolved_outputs"]) == 1


def test_report_semantic_planner_recovers_custom_helper_calculation():
    sql = "SELECT amount FROM sales"
    code = r'''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)

def calculate_total(frame):
    return frame["amount"].sum()

total = calculate_total(data)
analysis_report = f"Total sales: {total:,.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total sales: 1,280.50.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def semantic_recovery(payload: dict) -> EvidencePlan:
        operation = next(
            item
            for item in payload["operation_catalog"]
            if "total = calculate_total(data)" in item["expression"]
        )
        operation_id = operation["operation_node_id"]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=_source_anchor_refs(payload, []),
            operation_node_ids=[operation_id],
            metric_key="total_sales",
            business_name="Total sales",
            business_explanation="Sum of sales amount in the confirmed scope.",
            value_type="currency",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=semantic_recovery).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["1,280.50"]
    assert len(bundle["evidence_definitions"]) == 1
    definition = bundle["evidence_definitions"][0]
    assert definition["calculation"]["metric_fields"] == ["total"]
    assert any(
        item.startswith("op_stmt_")
        for item in definition["calculation"]["operation_node_ids"]
    )


def test_semantic_plan_isolates_constant_output_from_query_backed_metric():
    sql = "SELECT amount FROM sales"
    code = r'''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
total = data["amount"].sum()
known_target = 99
analysis_report = f"Total: {total:.2f}; target: {known_target}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total: 20.00; target: 99.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    def over_broad_plan(payload: dict) -> EvidencePlan:
        operation = next(
            item
            for item in payload["operation_catalog"]
            if "total = data" in item["expression"]
        )
        operation_id = operation["operation_node_id"]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=[
                ReportSourceAnchorRef(anchor_id=item["anchor_id"])
                for item in payload["source_anchor_catalog"]
                if item.get("verified_report_output")
            ],
            operation_node_ids=[operation_id],
            metric_key="total_sales",
            business_name="Total sales",
            business_explanation="Sum of sales amount in the confirmed scope.",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=over_broad_plan).compile(snapshot)

    assert bundle["status"] == "partial"
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["20.00"]
    assert len(bundle["diagnostics"]["resolver_unresolved_outputs"]) == 1


def test_semantic_plan_derives_query_refs_from_selected_operations():
    sales_sql = "SELECT amount FROM sales"
    target_sql = "SELECT target FROM targets"
    code = r'''
sales_sql = "SELECT amount FROM sales"
target_sql = "SELECT target FROM targets"
sales = load_data_with_sql(sql=sales_sql)
targets = load_data_with_sql(sql=target_sql)
total = sales["amount"].sum()
analysis_report = f"Total: {total:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total: 20.00.",
        query_snapshots={
            "q_sales": _query_snapshot(sales_sql, columns=["amount"]),
            "q_targets": _query_snapshot(target_sql, columns=["target"]),
        },
    )

    def wrong_query_plan(payload: dict) -> EvidencePlan:
        operation = next(
            item
            for item in payload["operation_catalog"]
            if "total = sales" in item["expression"]
        )
        wrong_query = payload["query_catalog"][-1]["query_node_id"]
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=_source_anchor_refs(payload, []),
            operation_node_ids=[operation["operation_node_id"]],
            query_node_ids=[wrong_query],
            metric_key="total_sales",
            business_name="Total sales",
            business_explanation="Sum of sales amount.",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=wrong_query_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["evidence_definitions"]) == 1
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_report_source_anchor_uses_local_boundary_when_llm_locator_is_ambiguous():
    sql = "SELECT amount FROM sales"
    code = r'''
sql = "SELECT amount FROM sales"
data = load_data_with_sql(sql=sql)
total = data["amount"].sum()
summary = {"total": int(total)}
analysis_report = f"Total sales: {summary['total']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Total sales: 20.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])} ,
    )

    def ambiguous_locator_plan(payload: dict) -> EvidencePlan:
        return EvidencePlan(metrics=[PlannedMetric(
            calculation_id="report_semantic_scope",
            source_anchors=_source_anchor_refs(payload, []),
            # This line deliberately does not identify a unique expression.
            # The report anchor itself has an exact local operation closure.
            calculation_source_refs=[CalculationSourceRef(
                source_line=4,
                expression="summary",
            )],
            metric_key="total_sales",
            business_name="Total sales",
            business_explanation="Sum of sales amount in the confirmed scope.",
        )])

    bundle = PostAnalysisEvidenceCompiler(planner=ambiguous_locator_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert [item["text"] for item in bundle["report_trace_refs"]] == ["20"]
    assert bundle["evidence_definitions"][0]["query_refs"] == ["q_sales"]


def test_filtered_generator_report_builder_keeps_dynamic_precision_slots():
    sql = "SELECT amount, contract_count FROM sales"
    code = r'''
sql = "SELECT amount, contract_count FROM sales"
data = load_data_with_sql(sql=sql)
precision = 1
total_amount = data["amount"].sum()
total_count = data["contract_count"].sum()
report_sections = [
    "## Summary",
    f"Amount: {total_amount:,.{precision}f} yuan.",
    "" if total_count <= 0 else f"Contracts: {total_count:,.{precision}f}.",
]
analysis_report = "\n".join(section for section in report_sections if section)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="## Summary\nAmount: 1,280.5 yuan.\nContracts: 12.0.",
        query_snapshots={
            "q_sales": _query_snapshot(sql, columns=["amount", "contract_count"]),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert bundle["status"] == "complete", bundle["diagnostics"]
    refs = sorted(bundle["report_trace_refs"], key=lambda item: item["start"])
    assert [item["text"] for item in refs] == ["1,280.5", "12.0"]


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


def test_reused_concatenated_conditional_fragment_keeps_inner_numeric_slots():
    code = r'''
top_name = "Material"
top_score = 247.2
second_name = "Laboratory"
second_score = 69.4
gap = top_score - second_score
priority_text = (
    f"Top cause: {top_name} ({top_score:.1f}); "
    + (f"ahead of {second_name} ({second_score:.1f}) by {gap:.1f}." if second_name else "")
)
report_parts = [
    "## Summary",
    f"- {priority_text}",
    "## Ranking",
    f"{priority_text}",
]
analysis_report = "\n".join(report_parts)
'''
    report = (
        "## Summary\n- Top cause: Material (247.2); ahead of Laboratory "
        "(69.4) by 177.8.\n## Ranking\nTop cause: Material (247.2); "
        "ahead of Laboratory (69.4) by 177.8."
    )

    _, expressions, _, _, _, _ = LineageIndexer()._index_code(code, {})
    outputs = LineageIndexer._locate_report_outputs(report, expressions)
    relevant = [
        item for item in outputs
        if item.get("field") in {"top_score", "second_score", "gap"}
    ]

    assert [item["text"] for item in relevant] == [
        "247.2", "247.2", "69.4", "177.8", "69.4", "177.8",
    ]
    assert [item["field"] for item in relevant] == [
        "top_score", "top_score", "second_score", "gap", "second_score", "gap",
    ]


def test_top_level_conditional_report_expression_keeps_rendered_numeric_slots():
    sql = "SELECT status, amount FROM contracts"
    code = r'''
sql = "SELECT status, amount FROM contracts"
data = load_data_with_sql(sql=sql)
total = len(data)
amount = data["amount"].sum()
has_data = total > 0
rate = total / 10
finding = f"Coverage: {rate:.1%}."
analysis_report = (
    f"## Summary\nContracts: {total}.\n"
    f"Amount: {amount:,.2f}.\n"
    f"{finding}\n"
    if has_data
    else
    "## Summary\nNo contracts."
)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    report = "## Summary\nContracts: 2.\nAmount: 30.00.\nCoverage: 20.0%.\n"
    snapshot = _snapshot(
        code=code,
        report=report,
        query_snapshots={
            "q_contracts": _query_snapshot(sql, columns=["status", "amount"]),
        },
    )

    index = LineageIndexer().build(snapshot)
    report_outputs = [
        output for output in index["output_nodes"]
        if output["artifact_type"] == "report"
    ]

    assert [
        output["text"] for output in sorted(report_outputs, key=lambda item: item["start"])
    ] == ["2", "30.00", "20.0"]
    assert index["report_construction_issues"] == []


def test_report_list_concatenation_keeps_all_fragment_lineage():
    sql = "SELECT amount FROM sales"
    code = r'''
sql = "SELECT amount FROM sales"
frame = load_data_with_sql(sql=sql)
total = frame["amount"].sum()
count = len(frame)
summary_lines = [f"合计 {total:.2f} 元"]
detail_lines = []
detail_lines.append(f"共 {count} 条记录")
analysis_report = "\n".join(["# 概览"] + summary_lines + [""] + detail_lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="# 概览\n合计 30.00 元\n\n共 2 条记录",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["amount"])},
    )

    index = LineageIndexer().build(snapshot)

    report_outputs = [
        output for output in index["output_nodes"]
        if output["artifact_type"] == "report"
    ]
    assert {output["text"] for output in report_outputs} == {"30.00", "2"}
    assert index["report_construction_issues"] == []


def test_report_metric_keeps_mutually_exclusive_dataframe_formula_branches():
    sql = "SELECT batch_id, test_value, target_value, lower_limit, upper_limit FROM quality_results"
    code = r'''
sql = "SELECT batch_id, test_value, target_value, lower_limit, upper_limit FROM quality_results"
data = load_data_with_sql(sql=sql)
data["deviation_pct"] = 0.0
for idx, row in data.iterrows():
    value = row.get("test_value")
    target = row.get("target_value")
    lower = row.get("lower_limit")
    upper = row.get("upper_limit")
    if target:
        data.at[idx, "deviation_pct"] = abs(value - target) / abs(target) * 100
    elif upper and value > upper:
        data.at[idx, "deviation_pct"] = (value - upper) / upper * 100
    elif lower and value < lower:
        data.at[idx, "deviation_pct"] = (lower - value) / lower * 100
top_rows = data.sort_values("deviation_pct", ascending=False).head(2)
report_lines = ["## Deviation detail"]
for _, row in top_rows.iterrows():
    deviation = row.get("deviation_pct")
    report_lines.append(f"- Batch {row.get('batch_id')}: deviation {deviation:.1f}%")
analysis_report = "\n".join(report_lines)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report=(
            "## Deviation detail\n"
            "- Batch B-1: deviation 12.5%\n"
            "- Batch B-2: deviation 8.0%"
        ),
        query_snapshots={
            "q_quality": _query_snapshot(
                sql,
                columns=[
                    "batch_id", "test_value", "target_value",
                    "lower_limit", "upper_limit",
                ],
            )
        },
    )

    lineage = LineageIndexer().build(snapshot)
    deviation_outputs = [
        item for item in lineage["output_nodes"]
        if item.get("artifact_type") == "report"
        and item.get("field") == "deviation"
    ]
    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)

    assert len(deviation_outputs) == 2
    assert all(
        item["ambiguous_calculation_boundary"] is False
        for item in deviation_outputs
    )
    assert all(
        len(item.get("boundary_operation_node_ids") or []) == 3
        for item in deviation_outputs
    )
    assert bundle["status"] == "complete", bundle["diagnostics"]
    assert len(bundle["report_trace_refs"]) == 2
    assert set(bundle["evidence_definitions"][0]["calculation"]["metric_fields"]) == {
        "test_value", "target_value", "lower_limit", "upper_limit",
    }


def test_row_selector_alias_preserves_column_level_query_lineage():
    cost_sql = "SELECT product, cost FROM product_cost"
    profit_sql = "SELECT product, profit, margin FROM product_profit"
    code = r'''
cost_sql = "SELECT product, cost FROM product_cost"
profit_sql = "SELECT product, profit, margin FROM product_profit"
cost = load_data_with_sql(sql=cost_sql)
profit = load_data_with_sql(sql=profit_sql)
summary = cost.merge(profit, on="product")
top = summary.sort_values("profit", ascending=False).iloc[0]
top_profit = top["profit"]
top_cost = top["cost"]
analysis_report = f"Top profit {top_profit:.2f}; cost {top_cost:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Top profit 30.00; cost 12.00.",
        query_snapshots={
            "q_cost": _query_snapshot(cost_sql, columns=["product", "cost"]),
            "q_profit": _query_snapshot(profit_sql, columns=["product", "profit", "margin"]),
        },
    )

    outputs = [
        item for item in LineageIndexer().build(snapshot)["output_nodes"]
        if item.get("artifact_type") == "report"
    ]
    by_field = {
        item["field"]: set(item.get("candidate_query_node_ids") or [])
        for item in outputs
    }

    assert by_field["top_profit"] == {"q_profit"}
    assert by_field["top_cost"] == {"q_cost"}


def test_grouped_single_measure_keeps_only_dimension_and_measure_fields():
    sql = "SELECT equipment_type, power_kw, manufacturer, location FROM equipment"
    code = r'''
sql = "SELECT equipment_type, power_kw, manufacturer, location FROM equipment"
data = load_data_with_sql(sql=sql)
power_by_type = data.groupby("equipment_type")["power_kw"].sum().reset_index()
power_by_type.columns = ["type", "total_power"]
top_power = power_by_type.sort_values("total_power", ascending=False).iloc[0]
share = top_power["total_power"] / power_by_type["total_power"].sum()
analysis_report = f"Top power share {share:.1%}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Top power share 39.5%.",
        query_snapshots={
            "q_equipment": _query_snapshot(
                sql,
                columns=["equipment_type", "power_kw", "manufacturer", "location"],
            ),
        },
    )

    bundle = PostAnalysisEvidenceCompiler(planner=_semantic_plan).compile(snapshot)
    definition = bundle["evidence_definitions"][0]
    assert set(definition["calculation"]["metric_fields"]) == {"power_kw"}


def test_explicit_table_label_unit_is_preserved_without_business_inference():
    sql = "SELECT product, revenue FROM sales"
    code = r'''
sales = load_data_with_sql(sql="SELECT product, revenue FROM sales")
tables = [build_table_result(
    title="Sales",
    columns=[{"key": "revenue", "label": "Revenue (CNY)"}],
    rows=sales.to_dict("records"),
)]
result = save_analysis_result(analysis_report="Sales detail", charts=[], tables=tables)
'''
    snapshot = _snapshot(
        code=code,
        report="Sales detail",
        tables=[{
            "title": "Sales",
            "columns": [{"key": "revenue", "label": "Revenue (CNY)"}],
            "rows": [{"product": "A", "revenue": 30}],
        }],
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["product", "revenue"])},
    )

    table_output = next(
        item for item in LineageIndexer().build(snapshot)["output_nodes"]
        if item.get("artifact_type") == "table"
    )
    assert table_output["display_unit_hint"] == "CNY"


def test_report_unit_is_taken_from_immediate_literal_suffix():
    sql = "SELECT revenue, margin FROM sales"
    code = r'''
sql = "SELECT revenue, margin FROM sales"
sales = load_data_with_sql(sql=sql)
revenue = sales["revenue"].sum()
margin = sales["margin"].mean()
analysis_report = f"Revenue {revenue:.2f} 元; margin {margin:.1f}% vs baseline."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
    snapshot = _snapshot(
        code=code,
        report="Revenue 30.00 元; margin 12.5% vs baseline.",
        query_snapshots={"q_sales": _query_snapshot(sql, columns=["revenue", "margin"])},
    )

    outputs = {
        item["field"]: item for item in LineageIndexer().build(snapshot)["output_nodes"]
        if item.get("artifact_type") == "report" and item.get("source_anchor_id")
    }
    assert outputs["revenue"]["display_unit_hint"] == "元"
    assert outputs["margin"]["display_unit_hint"] == "%"


def test_evidence_detail_does_not_invent_formula_from_metric_name():
    service = InsightNsConversationService(None)
    text = service._describe_data_calculation(
        {"display_value": "2.5%"},
        {"metric_name": "毛利率差异"},
        {},
        {"formula": "current_margin - baseline_margin"},
        [],
    )

    assert "毛利 / 销售收入" not in text
    assert "current_margin - baseline_margin" in text
