import ast
import sys
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from langgraph.graph import END


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import (  # noqa: E402
    AnalysisClaimAssessment,
    CURRENT_EXECUTION_INTENT,
    CURRENT_DATASOURCE_NUMERIC_FIELDS,
    CURRENT_QUERY_SNAPSHOTS,
    QueryConstraintRetrySignal,
    StructuredResult,
    _build_generated_code_error_context,
    _build_query_capacity_diagnostics,
    _build_dataframe_projection_mismatch_diagnostics,
    _build_schema_field_mismatch_diagnostics,
    _augment_missing_mapping_key_feedback,
    _build_execution_namespace,
    _canonicalize_used_datasource_ids,
    _extract_dataframe_reduction_clauses,
    _extract_error_signature,
    _find_empty_markdown_leaf_sections,
    _find_repeated_numeric_display_units,
    _classify_execution_error,
    _inspect_preparation_relationship,
    _inspect_preparation_schema_fact,
    _inspect_sql_preparation_relationship,
    _normalize_chart_items,
    _restore_sql_projection_column_names,
    _static_sql_candidates,
    _terminal_tool_success,
    _tool_success_message,
    _validate_analysis_relationship_contract,
    _validate_analysis_scope_against_execution_history,
    _validate_explicit_time_range_guard,
    _validate_probe_observation_provenance,
    _validate_report_extrema_selection_contract,
    _validate_report_paired_metric_population_contract,
    _validate_runtime_helper_signature_contract,
    _validate_dataframe_subset_column_contract,
    _validate_query_dataframe_projection_contract,
    _validate_analysis_query_result_consumption_contract,
    _validate_sql_projection_contract,
    _validate_generated_code_contract,
    _validate_scope_preserving_technical_repair,
    _validate_static_composed_query_constraints_contract,
    _validate_static_sql_field_ownership_contract,
    _validate_structured_result_contract,
    load_data_with_sql,
    probe_data_with_sql,
    probe_distinct_values,
    query_data_with_sql,
    query_local_file,
    query_local_file_low_memory,
    request_retry,
    save_analysis_result,
    validate_query_constraints,
)
from utils.chart_document_utils import build_chart_result, normalize_chart_result_item  # noqa: E402
from service.post_analysis_evidence_service import LineageIndexer  # noqa: E402


class ChartContractValidationTestCase(unittest.TestCase):

    def test_static_runtime_helper_signature_reports_unsupported_keywords(self) -> None:
        code = '''
summary = build_markdown_table(frame, value_precision=2, unknown_option=True)
'''

        retry = _validate_runtime_helper_signature_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "runtime_helper_signature_mismatch",
        )
        findings = retry.diagnostics["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["helper"], "build_markdown_table")
        self.assertEqual(findings[0]["unsupported_keywords"], ["unknown_option"])

    def test_static_runtime_helper_signature_accepts_markdown_precision(self) -> None:
        code = 'summary = build_markdown_table(frame, value_precision=2)'

        self.assertIsNone(
            _validate_runtime_helper_signature_contract(ast.parse(code), code)
        )

    def test_static_composed_query_constraints_reports_all_physical_scope_mismatches(self) -> None:
        code = '''
analysis_scope = {
    "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
    "target_filters": [],
}
week_start = "2026-08-31 00:00:00"
week_end = "2026-09-07 00:00:00"
base = query_data_with_sql(
    query_key="q_base",
    query_constraints={**analysis_scope, "aggregation": None},
    sql="SELECT batch_id FROM warehouse.batch WHERE batch_time >= %s AND batch_time < %s",
    params=[week_start, week_end],
)
products = query_data_with_sql(
    query_key="q_products",
    query_constraints={**analysis_scope, "aggregation": None},
    sql="SELECT product_id, product_name FROM warehouse.products",
)
samples = query_data_with_sql(
    query_key="q_samples",
    query_constraints={**analysis_scope, "aggregation": None},
    sql="SELECT sample_id, sample_type FROM warehouse.samples",
)
'''

        retry = _validate_static_composed_query_constraints_contract(
            ast.parse(code),
            code,
        )

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "composed_query_scope_mismatch",
        )
        self.assertEqual(
            {item["query_key"] for item in retry.diagnostics["findings"]},
            {"q_products", "q_samples"},
        )

    def test_static_composed_query_constraints_accepts_parent_scope_inheritance(self) -> None:
        code = '''
analysis_scope = {
    "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
    "target_filters": [],
}
child = query_data_with_sql(
    query_key="q_child",
    query_constraints={**analysis_scope, "aggregation": None},
    sql="SELECT batch_id, event_type FROM warehouse.events WHERE batch_id IN (%s)",
    params=["B-1"],
    scope_source_query_key="q_parent",
    scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
)
'''

        self.assertIsNone(
            _validate_static_composed_query_constraints_contract(
                ast.parse(code),
                code,
            )
        )

    def test_static_composed_query_constraints_requires_scope_for_derived_ids(self) -> None:
        code = '''
period = query_data_with_sql(
    query_key="q_period",
    query_constraints={
        "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
        "target_filters": [],
        "aggregation": {"grain": "batch"},
    },
    sql="SELECT batch_id, sample_id FROM warehouse.quality WHERE test_date >= %s AND test_date < %s",
    params=["2026-08-31", "2026-09-07"],
)
batch_ids = period["batch_id"].dropna().unique().tolist()
sample_ids = period["sample_id"].dropna().unique().tolist()
events = query_data_with_sql(
    query_key="q_events",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT batch_id, event_type FROM warehouse.events WHERE batch_id IN (%s)",
    params=batch_ids,
)
samples = query_data_with_sql(
    query_key="q_samples",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT sample_id, sample_type FROM warehouse.samples WHERE sample_id IN (%s)",
    params=sample_ids,
)
'''

        retry = _validate_static_composed_query_constraints_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        assert retry is not None
        findings = retry.diagnostics["findings"]
        self.assertEqual(
            {item["query_key"] for item in findings},
            {"q_events", "q_samples"},
        )
        for finding in findings:
            issue = next(
                item for item in finding["issues"]
                if item["constraint"] == "derived_query_scope"
            )
            self.assertEqual(
                issue["diagnostics"]["expected_parent_query_keys"],
                ["q_period"],
            )

    def test_static_composed_query_constraints_accepts_derived_scope_binding(self) -> None:
        code = '''
period = query_data_with_sql(
    query_key="q_period",
    query_constraints={
        "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
        "target_filters": [],
        "aggregation": None,
    },
    sql="SELECT batch_id FROM warehouse.quality WHERE test_date >= %s AND test_date < %s",
    params=["2026-08-31", "2026-09-07"],
)
batch_ids = period["batch_id"].dropna().unique().tolist()
events = query_data_with_sql(
    query_key="q_events",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT batch_id, event_type FROM warehouse.events WHERE batch_id IN (%s)",
    params=batch_ids,
    scope_source_query_key="q_period",
    scope_binding={"source_field": "batch_id", "target_field": "batch_id"},
)
'''

        self.assertIsNone(
            _validate_static_composed_query_constraints_contract(ast.parse(code), code)
        )

    def test_static_composed_query_constraints_requires_parent_even_if_scope_was_copied(self) -> None:
        code = '''
period = query_data_with_sql(
    query_key="q_period",
    query_constraints={
        "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
        "target_filters": [],
        "aggregation": None,
    },
    sql="SELECT batch_id FROM warehouse.quality WHERE test_date >= %s AND test_date < %s",
    params=["2026-08-31", "2026-09-07"],
)
batch_ids = period["batch_id"].dropna().unique().tolist()
events = query_data_with_sql(
    query_key="q_events",
    query_constraints={
        "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
        "target_filters": [],
        "aggregation": None,
    },
    sql="SELECT batch_id, event_type FROM warehouse.events WHERE batch_id IN (%s)",
    params=batch_ids,
)
'''

        retry = _validate_static_composed_query_constraints_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        assert retry is not None
        issue = retry.diagnostics["findings"][0]["issues"][0]
        self.assertEqual(issue["constraint"], "derived_query_scope")
        self.assertEqual(
            issue["diagnostics"]["reason"],
            "missing_scope_source_query_key",
        )

    def test_static_composed_query_constraints_defers_unknown_bound_params_to_runtime(self) -> None:
        code = '''
analysis_parameters = get_analysis_parameters({"start": "2026-08-31", "end": "2026-09-07"})
start_date = analysis_parameters["start"]
end_date = analysis_parameters["end"]
period = query_data_with_sql(
    query_key="q_period",
    query_constraints={
        "time_range": {"start": "2026-08-31", "end": "2026-09-07", "end_exclusive": True},
        "target_filters": [],
        "aggregation": None,
    },
    sql="SELECT batch_id FROM warehouse.quality WHERE test_date >= %s AND test_date < %s",
    params=[start_date, end_date],
)
'''

        self.assertIsNone(
            _validate_static_composed_query_constraints_contract(ast.parse(code), code)
        )

    def test_static_composed_query_constraints_does_not_infer_static_ids_as_parent_scope(self) -> None:
        code = '''
batch_ids = ["B-1", "B-2"]
master = query_data_with_sql(
    query_key="q_master",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT batch_id, product_name FROM warehouse.batch_master WHERE batch_id IN (%s, %s)",
    params=batch_ids,
)
'''

        self.assertIsNone(
            _validate_static_composed_query_constraints_contract(ast.parse(code), code)
        )

    def test_local_dataframe_projection_reports_all_missing_downstream_columns(self) -> None:
        code = '''
detail = source[["batch_id", "value"]].copy()
detail["ratio"] = detail["value"] / detail["spec_range"]
label = detail["quality_item"].astype(str)
'''

        retry = _validate_dataframe_subset_column_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        self.assertEqual(retry.diagnostics["violated_contract"], "dataframe_subset_missing_columns")
        self.assertEqual(
            {item["missing_column"] for item in retry.diagnostics["findings"]},
            {"spec_range", "quality_item"},
        )

    def test_local_dataframe_projection_accepts_derived_column_before_read(self) -> None:
        code = '''
detail = source[["batch_id", "value"]].copy()
detail["ratio"] = detail["value"] / 10
report_value = detail["ratio"].mean()
'''

        self.assertIsNone(_validate_dataframe_subset_column_contract(ast.parse(code), code))

    def test_local_dataframe_projection_accepts_columns_created_by_rename(self) -> None:
        code = '''
detail = source[["batch_id", "quality_item", "value"]].copy()
detail = detail.rename(columns={
    "batch_id": "批次号",
    "quality_item": "检测项",
    "value": "检测值",
})
labels = detail["批次号"].astype(str) + detail["检测项"].astype(str)
metric = detail["检测值"].mean()
'''

        self.assertIsNone(_validate_dataframe_subset_column_contract(ast.parse(code), code))

    def test_local_dataframe_projection_accepts_inplace_column_rename(self) -> None:
        code = '''
detail = source[["batch_id", "value"]].copy()
detail.rename(columns={"batch_id": "批次号", "value": "检测值"}, inplace=True)
metric = detail["检测值"].mean()
'''

        self.assertIsNone(_validate_dataframe_subset_column_contract(ast.parse(code), code))

    def test_query_dataframe_projection_reports_all_missing_derived_frame_columns(self) -> None:
        code = '''
q_equipment = """SELECT linked_batch_id, state, reason_code FROM equipment_events"""
equip_events = query_data_with_sql(
    query_key="q_equipment",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql=q_equipment,
)
fault_events = equip_events[equip_events["state"] == "fault"]
equip_summary = fault_events.groupby("reason_code").agg(
    event_count=("state_event_id", "count")
).reset_index()

q_steps = """SELECT batch_id, operation_code, status FROM operation_events"""
op_events = query_data_with_sql(
    query_key="q_steps",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql=q_steps,
)
interrupted = op_events[op_events["status"] == "interrupted"]
step_summary = interrupted.groupby("operation_code").agg(
    interrupt_count=("step_event_id", "count")
).reset_index()
'''

        retry = _validate_query_dataframe_projection_contract(code)

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "query_dataframe_projection_mismatch",
        )
        findings = retry.diagnostics["findings"]
        self.assertEqual(
            {
                (item["dataframe"], tuple(item["missing_projection_fields"]))
                for item in findings
            },
            {
                ("fault_events", ("state_event_id",)),
                ("interrupted", ("step_event_id",)),
            },
        )

    def test_sql_projection_contract_reports_duplicate_inline_fields(self) -> None:
        code = '''
result_frame = query_data_with_sql(
    query_key="q_quality",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="""
        SELECT batch_id, quality_oos_nc_count, process_sample_count,
               quality_oos_nc_count
        FROM quality_candidates
    """,
)
'''

        retry = _validate_sql_projection_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "duplicate_sql_projection_fields",
        )
        self.assertEqual(
            retry.diagnostics["findings"],
            [{
                "query_key": "q_quality",
                "duplicate_output_fields": ["quality_oos_nc_count"],
                "call_line": 2,
            }],
        )

    def test_duplicate_column_binder_error_is_not_classified_as_datasource_outage(self) -> None:
        self.assertEqual(
            _classify_execution_error(
                None,
                'Binder Error: table "mysql_query" has duplicate column name "amount"',
            ),
            "schema_or_column_mismatch",
        )

    def test_formal_query_result_must_be_consumed(self) -> None:
        code = '''
primary = query_data_with_sql(
    query_key="q_primary",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT status, COUNT(*) AS cnt FROM primary_table GROUP BY status",
)
primary_total = int(primary["cnt"].sum())
cross_check = query_data_with_sql(
    query_key="q_cross_check",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT status, COUNT(*) AS cnt FROM secondary_table GROUP BY status",
)
'''

        retry = _validate_analysis_query_result_consumption_contract(ast.parse(code), code)

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unconsumed_analysis_query_results",
        )
        self.assertEqual(
            retry.diagnostics["findings"],
            [{
                "query_key": "q_cross_check",
                "result_variable": "cross_check",
                "assignment_line": 8,
            }],
        )

    def test_formal_query_result_used_by_no_data_branch_is_consumed(self) -> None:
        code = '''
frame = query_data_with_sql(
    query_key="q_detail",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="SELECT * FROM detail_table",
)
if frame.empty:
    raise_no_data_error("empty", ["no rows"])
'''

        self.assertIsNone(
            _validate_analysis_query_result_consumption_contract(ast.parse(code), code)
        )

    def test_formal_report_cannot_copy_probe_observation_as_metric_literal(self) -> None:
        previous = SimpleNamespace(
            result_payload_json=json.dumps({
                "retry_type": "probe_feedback",
                "diagnostics": {
                    "query_observations": [{
                        "columns": ["total_batches", "min_date"],
                        "value_samples": ["24", "2026-08-10"],
                    }]
                },
            })
        )
        code = '''
execution_intent = "analysis"
total_batches = 24
analysis_report = f"Total {total_batches} batches."
'''

        retry = _validate_probe_observation_provenance(
            previous_executions=[previous],
            current_code=code,
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "probe_observation_used_as_formal_metric",
        )
        self.assertEqual(retry.diagnostics["metrics"][0]["variable"], "total_batches")

    def test_formal_report_accepts_metric_recomputed_from_query_result(self) -> None:
        previous = SimpleNamespace(
            result_payload_json=json.dumps({
                "retry_type": "probe_feedback",
                "diagnostics": {
                    "query_observations": [{
                        "columns": ["total_batches"],
                        "value_samples": ["24"],
                    }]
                },
            })
        )
        code = '''
execution_intent = "analysis"
summary = load_data_with_sql(sql="SELECT COUNT(*) AS total_batches FROM batches")
total_batches = int(summary.iloc[0]["total_batches"])
analysis_report = f"Total {total_batches} batches."
'''

        retry = _validate_probe_observation_provenance(
            previous_executions=[previous],
            current_code=code,
        )

        self.assertIsNone(retry)

    def test_schema_mismatch_feedback_lists_all_fields_owned_by_another_source(self) -> None:
        code = '''
sql_batch = f"""
SELECT batch_id, fault_event_count, material_lot_count
FROM warehouse.dim_batch_quality_context
WHERE batch_start_time >= %s
"""
batch_df = load_data_with_sql(sql=sql_batch)
'''
        selected_snapshot = [
            {
                "datasource_id": 61,
                "datasource_name": "Batch quality context",
                "datasource_identifier": "warehouse.dim_batch_quality_context",
                "metadata_schema": {
                    "properties": {
                        "batch_id": {"type": "string"},
                        "batch_start_time": {"type": "string"},
                    },
                },
            },
            {
                "datasource_id": 62,
                "datasource_name": "Root cause candidates",
                "datasource_identifier": "warehouse.dim_root_cause_candidates",
                "metadata_schema": {
                    "properties": {
                        "fault_event_count": {"type": "integer"},
                        "material_lot_count": {"type": "integer"},
                    },
                },
            },
        ]

        diagnostics = _build_schema_field_mismatch_diagnostics(
            code=code,
            error_message="Column 'fault_event_count' cannot be resolved.",
            selected_datasource_snapshot=selected_snapshot,
        )

        self.assertEqual(
            diagnostics["violated_contract"],
            "datasource_field_ownership_mismatch",
        )
        self.assertEqual(diagnostics["query_datasource_id"], 61)
        self.assertEqual(
            [item["field_name"] for item in diagnostics["incompatible_fields"]],
            ["fault_event_count", "material_lot_count"],
        )
        self.assertEqual(
            diagnostics["sql_assignment_range"]["role"],
            "failed_query_definition",
        )

    def test_schema_mismatch_uses_reaching_sql_assignment_at_failed_load(self) -> None:
        code = '''
sql_batch = """
SELECT batch_id, obsolete_field
FROM warehouse.dim_batch_quality_context
"""
sql_batch = """
SELECT batch_id, fault_event_count, material_lot_count
FROM warehouse.dim_batch_quality_context
"""
batch_df = load_data_with_sql(sql=sql_batch)
'''
        selected_snapshot = [
            {
                "datasource_id": 61,
                "datasource_name": "Batch quality context",
                "datasource_identifier": "warehouse.dim_batch_quality_context",
                "metadata_schema": {"properties": {"batch_id": {"type": "string"}}},
            },
            {
                "datasource_id": 62,
                "datasource_name": "Root cause candidates",
                "datasource_identifier": "warehouse.dim_root_cause_candidates",
                "metadata_schema": {
                    "properties": {
                        "fault_event_count": {"type": "integer"},
                        "material_lot_count": {"type": "integer"},
                    },
                },
            },
        ]

        diagnostics = _build_schema_field_mismatch_diagnostics(
            code=code,
            error_message="Column 'fault_event_count' cannot be resolved.",
            selected_datasource_snapshot=selected_snapshot,
            code_error_context={"line_no": 10},
        )

        self.assertEqual(
            [item["field_name"] for item in diagnostics["incompatible_fields"]],
            ["fault_event_count", "material_lot_count"],
        )
        self.assertEqual(diagnostics["sql_assignment_range"]["start_line"], 6)

    def test_static_sql_candidates_include_inline_query_sql(self) -> None:
        code = '''
detail = query_data_with_sql(
    query_key="q_detail",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="""
    SELECT b.batch_id, b.product_name
    FROM warehouse.raw_batch b
    """,
)
'''

        candidates = _static_sql_candidates(code)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["origin"], "inline_query_call")
        self.assertIn("b.product_name", candidates[0]["sql"])

    def test_static_sql_alias_ownership_reports_all_proven_cross_source_fields(self) -> None:
        code = '''
detail = query_data_with_sql(
    query_key="q_detail",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="""
    SELECT b.batch_id, b.product_name, b.grade, e.state
    FROM warehouse.raw_batch b
    LEFT JOIN warehouse.equipment_event e ON b.batch_id = e.linked_batch_id
    """,
)
'''
        selected_snapshot = [
            {
                "datasource_id": 61,
                "datasource_name": "Batch master",
                "datasource_identifier": "warehouse.raw_batch",
                "metadata_schema": {
                    "properties": {
                        "batch_id": {"type": "string"},
                    },
                },
            },
            {
                "datasource_id": 62,
                "datasource_name": "Batch quality context",
                "datasource_identifier": "warehouse.dim_batch_quality_context",
                "metadata_schema": {
                    "properties": {
                        "product_name": {"type": "string"},
                        "grade": {"type": "string"},
                    },
                },
            },
            {
                "datasource_id": 68,
                "datasource_name": "Equipment events",
                "datasource_identifier": "warehouse.equipment_event",
                "metadata_schema": {
                    "properties": {
                        "linked_batch_id": {"type": "string"},
                        "state": {"type": "string"},
                    },
                },
            },
        ]

        retry = _validate_static_sql_field_ownership_contract(
            code=code,
            selected_datasource_snapshot=selected_snapshot,
        )

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "static_sql_alias_field_ownership_mismatch",
        )
        self.assertEqual(
            {item["field_name"] for item in retry.diagnostics["findings"]},
            {"product_name", "grade"},
        )
        self.assertTrue(retry.code_error_context["suggested_patch_ranges"])

    def test_static_sql_alias_ownership_ignores_unqualified_and_unknown_fields(self) -> None:
        code = '''
detail = query_data_with_sql(
    query_key="q_detail",
    query_constraints={"time_range": None, "target_filters": [], "aggregation": None},
    sql="""
    SELECT batch_id, b.vendor_specific_field
    FROM warehouse.raw_batch b
    """,
)
'''
        selected_snapshot = [
            {
                "datasource_id": 61,
                "datasource_name": "Batch master",
                "datasource_identifier": "warehouse.raw_batch",
                "metadata_schema": {"properties": {"batch_id": {"type": "string"}}},
            },
        ]

        retry = _validate_static_sql_field_ownership_contract(
            code=code,
            selected_datasource_snapshot=selected_snapshot,
        )

        self.assertIsNone(retry)

    def test_unresolved_database_column_is_schema_mismatch(self) -> None:
        self.assertEqual(
            _classify_execution_error(
                None,
                "Column 'fault_event_count' cannot be resolved.",
            ),
            "schema_or_column_mismatch",
        )
        self.assertEqual(
            _extract_error_signature(
                "schema_or_column_mismatch",
                "query date '2026-08-10': Column 'fault_event_count' cannot be resolved.",
            )["field_name"],
            "fault_event_count",
        )

    def test_dataframe_projection_feedback_lists_all_downstream_missing_fields(self) -> None:
        code = '''
sql_ops = """
SELECT batch_id, operation_code, status
FROM warehouse.operation_events
"""
sql_material = """
SELECT material_name, supplier_or_source, quantity_kg
FROM warehouse.material_events
"""
df_ops = load_data_with_sql(sql=sql_ops)
df_material = load_data_with_sql(sql=sql_material)
op_summary = df_ops.groupby(["batch_id", "operation_code", "status"]).agg(
    issue_count=("step_event_id", "count"),
).reset_index()
material_summary = df_material.groupby(["material_name", "supplier_or_source"]).agg(
    movement_count=("movement_id", "count"),
    quantity=("quantity_kg", "sum"),
).reset_index()
'''

        diagnostics = _build_dataframe_projection_mismatch_diagnostics(code)

        mismatches = diagnostics["dataframe_projection_mismatches"]
        self.assertEqual(
            {
                item["dataframe"]: item["missing_projection_fields"]
                for item in mismatches
            },
            {
                "df_ops": ["step_event_id"],
                "df_material": ["movement_id"],
            },
        )

    def test_dataframe_projection_feedback_accepts_computed_frame_fields(self) -> None:
        code = '''
sql_events = "SELECT batch_id, alarm_count FROM warehouse.events"
df = load_data_with_sql(sql=sql_events)
df["alarm_rate"] = df["alarm_count"] / 10
summary = df.groupby("batch_id").agg(rate=("alarm_rate", "mean")).reset_index()
'''

        self.assertEqual(_build_dataframe_projection_mismatch_diagnostics(code), {})

    def test_chart_semantic_error_is_chart_contract_error(self) -> None:
        error_message = (
            "chart_semantic_error: 图表把多个业务指标放在类别行中，"
            "并共用一个通用数值列"
        )

        self.assertEqual(
            _classify_execution_error(RuntimeError(error_message), error_message),
            "chart_contract_error",
        )

    def test_missing_mapping_key_feedback_lists_all_same_mapping_usages(self) -> None:
        code = '''
time_range = analysis_parameters["time_range"]
chart_description = f"{time_range['display_end']}"
report_line = f"{time_range['start']} - {time_range['display_end']}"
other = f"{another_mapping['display_end']}"
'''

        diagnostics, instructions, context = _augment_missing_mapping_key_feedback(
            code=code,
            error_type="schema_or_column_mismatch",
            error_message="'display_end'\n错误定位：生成代码第 3 行",
            diagnostics={},
            repair_instructions=["keep scope"],
            code_error_context={"line_no": 3, "suggested_patch_ranges": []},
        )

        self.assertEqual(diagnostics["violated_contract"], "missing_mapping_key")
        self.assertEqual(diagnostics["mapping_expression"], "time_range")
        self.assertEqual(diagnostics["usage_lines"], [3, 4])
        self.assertEqual(
            [item["start_line"] for item in context["suggested_patch_ranges"]],
            [3, 4, 2],
        )
        self.assertIn("lines [3, 4]", instructions[0])

    def test_query_capacity_feedback_includes_all_related_high_cardinality_queries(self) -> None:
        code = '''
quality_sql = """
SELECT batch_id, test_value FROM warehouse.quality_results
"""
quality = load_data_with_sql(sql=quality_sql)
process_sql = """
SELECT batch_id, AVG(value) AS avg_value
FROM warehouse.process_samples
GROUP BY batch_id
"""
process = load_data_with_sql(sql=process_sql)
'''
        selected_snapshot = [
            {
                "datasource_id": 79,
                "datasource_name": "Quality results",
                "datasource_identifier": "warehouse.quality_results",
                "metadata_schema": {
                    "relationships": [{
                        "relationship_id": "quality_batch",
                        "local_fields": ["batch_id"],
                        "observed_key_cardinality": {"local_distinct_key_count": 18653},
                    }],
                },
            },
            {
                "datasource_id": 76,
                "datasource_name": "Process samples",
                "datasource_identifier": "warehouse.process_samples",
                "metadata_schema": {
                    "relationships": [{
                        "relationship_id": "process_batch",
                        "local_fields": ["batch_id"],
                        "observed_key_cardinality": {"local_distinct_key_count": 18653},
                    }],
                },
            },
        ]

        diagnostics = _build_query_capacity_diagnostics(
            code=code,
            failed_sql="SELECT batch_id, test_value FROM warehouse.quality_results",
            selected_datasource_snapshot=selected_snapshot,
        )

        self.assertEqual(
            diagnostics["violated_contract"],
            "observed_query_capacity_boundary",
        )
        self.assertEqual(
            diagnostics["failed_query_capacity_risks"][0]["observed_distinct_count"],
            18653,
        )
        self.assertEqual(len(diagnostics["related_high_cardinality_queries"]), 2)
        self.assertEqual(
            diagnostics["known_high_cardinality_result_dimensions"],
            ["batch_id"],
        )
        self.assertTrue(
            diagnostics["required_result_design"]["final_result_grain_required"]
        )

    def test_structured_result_accepts_point_scoped_metrics_in_one_value_field(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT alarm_count, fault_count FROM risks")
total_alarms = int(data["alarm_count"].sum())
total_faults = int(data["fault_count"].sum())
chart_data = pd.DataFrame({
    "signal": ["Alarms", "Faults"],
    "count": [total_alarms, total_faults],
})
chart = build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="signal",
    value_field="count",
)
result = save_analysis_result(analysis_report="Risk signals.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Risk signals.",
            charts=[{
                "title": "Risk signals",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["Alarms", "Faults"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": [8, 3]}],
                },
                "chart_data_contract": {
                    "encoding": {
                        "category_field": "signal",
                        "value_field": "count",
                    },
                },
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT alarm_count, fault_count FROM risks",
                    },
                    "execution": {
                        "status": "success",
                        "columns": ["alarm_count", "fault_count"],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code=code,
        )

        self.assertIsNone(retry)

    def test_structured_result_accepts_mapping_members_with_point_scoped_evidence(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT process_alarm_count, fault_event_count FROM risks")
evidence = {}
evidence["Process"] = int(data["process_alarm_count"].sum())
evidence["Equipment"] = int(data["fault_event_count"].sum())
chart_data = pd.DataFrame([
    {"cause": label, "strength": value}
    for label, value in evidence.items()
])
chart = build_chart_result(
    data=chart_data,
    chart_kind="bar",
    category_field="cause",
    value_field="strength",
)
result = save_analysis_result(analysis_report="Risk signals.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Risk signals.",
            charts=[{
                "title": "Risk signals",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["Process", "Equipment"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": [8, 3]}],
                },
                "chart_data_contract": {
                    "encoding": {
                        "category_field": "cause",
                        "value_field": "strength",
                    },
                },
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT process_alarm_count, fault_event_count FROM risks",
                    },
                    "execution": {
                        "status": "success",
                        "columns": ["process_alarm_count", "fault_event_count"],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_structured_result_accepts_distinct_multi_metric_series(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT alarm_count, vision_fail_count, issue_count FROM quality_summary")
alarm_count = int(data["alarm_count"].sum())
vision_fail_count = int(data["vision_fail_count"].sum())
issue_count = int(data["issue_count"].sum())
chart = build_multi_metric_chart_result(
    data=pd.DataFrame([{
        "period": "2026-07",
        "alarm count": alarm_count,
        "vision failure count": vision_fail_count,
        "issue count": issue_count,
    }]),
    chart_kind="bar",
    category_field="period",
    value_fields=["alarm count", "vision failure count", "issue count"],
)
result = save_analysis_result(analysis_report="Quality events.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Quality events.",
            charts=[{
                "title": "Quality events",
                "chart_type": "echarts",
                "chart_spec": {
                    "xAxis": {"type": "category", "data": ["2026-07"]},
                    "yAxis": {"type": "value"},
                    "series": [
                        {"type": "bar", "name": "alarm count", "data": [8]},
                        {"type": "bar", "name": "vision failure count", "data": [3]},
                        {"type": "bar", "name": "issue count", "data": [2]},
                    ],
                },
                "chart_data_contract": {
                    "encoding": {
                        "category_field": "period",
                        "value_field": "alarm count",
                        "value_fields": [
                            "alarm count",
                            "vision failure count",
                            "issue count",
                        ],
                    },
                },
            }],
            query_snapshots={
                "q_quality": {
                    "implementation": {
                        "sql": "SELECT alarm_count, vision_fail_count, issue_count FROM quality_summary",
                    },
                    "execution": {
                        "status": "success",
                        "columns": ["alarm_count", "vision_fail_count", "issue_count"],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_mapping_partition_counts_from_one_fact_field_remain_coherent(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT status FROM contracts")
counts = {}
counts["Active"] = int((data["status"] == "active").sum())
counts["Expired"] = int((data["status"] == "expired").sum())
chart_data = pd.DataFrame([
    {"status": label, "count": value}
    for label, value in counts.items()
])
chart = build_chart_result(
    data=chart_data,
    chart_kind="pie",
    category_field="status",
    value_field="count",
)
result = save_analysis_result(analysis_report="Status distribution.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Status distribution.",
            charts=[{
                "title": "Status distribution",
                "chart_type": "echarts",
                "chart_spec": {
                    "series": [{
                        "type": "pie",
                        "data": [
                            {"name": "Active", "value": 8},
                            {"name": "Expired", "value": 2},
                        ],
                    }],
                },
                "chart_data_contract": {
                    "encoding": {
                        "category_field": "status",
                        "value_field": "count",
                    },
                },
            }],
            query_snapshots={
                "q_contracts": {
                    "implementation": {"sql": "SELECT status FROM contracts"},
                    "execution": {"status": "success", "columns": ["status"]},
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_runtime_does_not_use_ast_ranking_semantics_as_a_hard_gate(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks")
process_count = int(data["process_alarm_count"].sum())
fault_count = int(data["fault_event_count"].sum())
material_count = int(data["material_lot_count"].sum())
scores = {
    "Process": process_count,
    "Equipment": fault_count,
    "Material": material_count,
}
ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
analysis_report = "\n".join(f"{index}. {name}: {value}" for index, (name, value) in enumerate(ranked, 1))
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="1. Material: 21\n2. Equipment: 3\n3. Process: 2",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Comparable population review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "process_alarm_count",
                            "fault_event_count",
                            "material_lot_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_runtime_allows_in_place_tuple_list_ranking(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks")
process_count = int(data["process_alarm_count"].sum())
fault_count = int(data["fault_event_count"].sum())
material_count = int(data["material_lot_count"].sum())
scores = [
    ("Process", process_count),
    ("Equipment", fault_count),
    ("Material", material_count),
]
scores.sort(key=lambda item: item[1], reverse=True)
analysis_report = "\n".join(f"{name}: {value}" for name, value in scores)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Material: 21\nEquipment: 3\nProcess: 2",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "process_alarm_count",
                            "fault_event_count",
                            "material_lot_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_report_rejects_tuple_list_dataframe_ranking(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks")
process_count = int(data["process_alarm_count"].sum())
fault_count = int(data["fault_event_count"].sum())
material_count = int(data["material_lot_count"].sum())
evidence_items = [
    ("Process", process_count),
    ("Equipment", fault_count),
    ("Material", material_count),
]
evidence_df = pd.DataFrame(evidence_items, columns=["cause", "evidence_strength"])
evidence_df = evidence_df.sort_values("evidence_strength", ascending=False)
analysis_report = "\n".join(
    f"{row['cause']}: {row['evidence_strength']}"
    for _, row in evidence_df.iterrows()
)
chart = build_chart_result(data=evidence_df, chart_kind="bar", category_field="cause", value_field="evidence_strength")
result = save_analysis_result(analysis_report=analysis_report, charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Material: 21\nEquipment: 3\nProcess: 2",
            charts=[{
                "title": "Evidence ranking",
                "chart_data_contract": {
                    "source_query_key": "q_risks",
                    "encoding": {
                        "category_field": "cause",
                        "value_fields": ["evidence_strength"],
                    },
                },
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT process_alarm_count, fault_event_count, material_lot_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "process_alarm_count",
                            "fault_event_count",
                            "material_lot_count",
                        ],
                    },
                },
            },
        )

        issues = LineageIndexer._heterogeneous_ranking_issues(code)

        self.assertEqual(len(issues), 1)
        self.assertEqual(
            issues[0]["value_field"],
            "evidence_strength",
        )

    def test_runtime_allows_conditionally_appended_tuple_list_ranking(self) -> None:
        code = r'''
process = load_data_with_sql(sql="SELECT batch_id, alarm_flag FROM process_events")
equipment = load_data_with_sql(sql="SELECT batch_id, fault_flag FROM equipment_events")
material = load_data_with_sql(sql="SELECT batch_id, supplier_id FROM material_events")
process_count = process[process["alarm_flag"] == 1]["batch_id"].nunique()
equipment_count = equipment[equipment["fault_flag"] == 1]["batch_id"].nunique()
material_count = material["batch_id"].nunique()
priorities = []
if process_count > 0:
    priorities.append(("Process", process_count))
if equipment_count > 0:
    priorities.append(("Equipment", equipment_count))
if material_count > 0:
    priorities.append(("Material", material_count))
priorities.sort(key=lambda item: item[1], reverse=True)
analysis_report = f"Top direction: {priorities[0][0]} ({priorities[0][1]} batches)."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Top direction: Material (8 batches).",
            charts=[],
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_process": {
                    "implementation": {"sql": "SELECT batch_id, alarm_flag FROM process_events"},
                    "execution": {"status": "success", "columns": ["batch_id", "alarm_flag"]},
                },
                "q_equipment": {
                    "implementation": {"sql": "SELECT batch_id, fault_flag FROM equipment_events"},
                    "execution": {"status": "success", "columns": ["batch_id", "fault_flag"]},
                },
                "q_material": {
                    "implementation": {"sql": "SELECT batch_id, supplier_id FROM material_events"},
                    "execution": {"status": "success", "columns": ["batch_id", "supplier_id"]},
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_report_allows_ranked_rates_with_one_shared_population(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT batch_id, process_alarm_count, fault_event_count FROM risks")
population_count = int(data["batch_id"].nunique())
process_rate = int((data["process_alarm_count"] > 0).sum()) / population_count
fault_rate = int((data["fault_event_count"] > 0).sum()) / population_count
scores = {"Process": process_rate, "Equipment": fault_rate}
ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
analysis_report = "\n".join(f"{name}: {value:.1%}" for name, value in ranked)
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Process: 30.0%\nEquipment: 20.0%",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Shared population rates"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT batch_id, process_alarm_count, fault_event_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "batch_id",
                            "process_alarm_count",
                            "fault_event_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_runtime_allows_rowwise_score_owned_by_the_analysis_agent(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, batch_id, process_alarm_count, fault_event_count FROM risks")
summary = data.groupby("candidate").agg(
    batch_count=("batch_id", "nunique"),
    avg_alarm=("process_alarm_count", "mean"),
    avg_fault=("fault_event_count", "mean"),
).reset_index()
def evidence_score(row):
    return row["batch_count"] + row["avg_alarm"] * 2 + row["avg_fault"] * 3
summary["score"] = summary.apply(evidence_score, axis=1)
summary = summary.sort_values("score", ascending=False)
analysis_report = f"Highest candidate score: {summary.iloc[0]['score']:.1f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Highest candidate score: 15.1.",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT candidate, batch_id, process_alarm_count, fault_event_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "candidate",
                            "batch_id",
                            "process_alarm_count",
                            "fault_event_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_report_allows_vectorized_composite_score_with_normalized_components(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, test_id, process_alarm_count, fault_event_count, material_lot_count FROM risks")
summary = data.groupby("candidate").agg(
    test_count=("test_id", "count"),
    process_alarm=("process_alarm_count", "sum"),
    fault_event=("fault_event_count", "sum"),
    material_lot=("material_lot_count", "sum"),
).reset_index()
summary["process_per_test"] = summary["process_alarm"] / summary["test_count"]
summary["fault_per_test"] = summary["fault_event"] / summary["test_count"]
summary["material_per_test"] = summary["material_lot"] / summary["test_count"]
summary["evidence_strength_score"] = (
    summary["process_per_test"].fillna(0)
    + summary["fault_per_test"].fillna(0)
    + summary["material_per_test"].fillna(0)
)
summary = summary.sort_values("evidence_strength_score", ascending=False)
analysis_report = f"Highest score: {summary.iloc[0]['evidence_strength_score']:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Highest score: 4.14.",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT candidate, test_id, process_alarm_count, fault_event_count, material_lot_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "candidate",
                            "test_id",
                            "process_alarm_count",
                            "fault_event_count",
                            "material_lot_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_report_allows_weighted_score_over_distinct_normalized_components(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, test_id, process_alarm_count, retest_flag FROM risks WHERE event_time >= start_ts AND event_time < end_ts")
summary = data.groupby("candidate").agg(
    candidate_count=("test_id", "count"),
    avg_evidence=("process_alarm_count", "mean"),
    retest_count=("retest_flag", "sum"),
).reset_index()
total_candidates = len(data)
summary["share_score"] = summary["candidate_count"] / total_candidates
max_avg_evidence = summary["avg_evidence"].max()
summary["evidence_score"] = summary["avg_evidence"] / max_avg_evidence
summary["retest_ratio"] = summary["retest_count"] / summary["candidate_count"]
summary["priority_score"] = (
    summary["share_score"] * 0.4
    + summary["evidence_score"] * 0.3
    + summary["retest_ratio"] * 0.3
)
summary = summary.sort_values("priority_score", ascending=False)
analysis_report = f"Highest score: {summary.iloc[0]['priority_score']:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Highest score: 0.82.",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT candidate, test_id, process_alarm_count, retest_flag FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "candidate",
                            "test_id",
                            "process_alarm_count",
                            "retest_flag",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_report_allows_sorted_additive_business_total(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT product, domestic_revenue, export_revenue FROM revenue")
summary = data.groupby("product").agg(
    domestic_revenue=("domestic_revenue", "sum"),
    export_revenue=("export_revenue", "sum"),
).reset_index()
summary["total_revenue"] = summary["domestic_revenue"] + summary["export_revenue"]
summary = summary.sort_values("total_revenue", ascending=False)
analysis_report = f"Top revenue: {summary.iloc[0]['total_revenue']:.2f}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        issues = LineageIndexer._heterogeneous_ranking_issues(code)

        self.assertEqual(issues, [])

    def test_runtime_allows_iterrows_record_score_owned_by_the_analysis_agent(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, batch_id, process_alarm_count, fault_event_count, material_lot_count FROM risks")
summary = data.groupby("candidate").agg(
    test_count=("batch_id", "count"),
    process_alarm=("process_alarm_count", "sum"),
    fault_event=("fault_event_count", "sum"),
    material_lot=("material_lot_count", "sum"),
).reset_index()
priority_rows = []
for _, row in summary.iterrows():
    evidence_score = row["process_alarm"] + row["fault_event"] + row["material_lot"]
    priority_rows.append({
        "candidate": row["candidate"],
        "test_count": row["test_count"],
        "evidence_score": evidence_score,
    })
priority_df = pd.DataFrame(priority_rows).sort_values(
    ["evidence_score", "test_count"], ascending=False
)
analysis_report = f"Highest candidate score: {priority_df.iloc[0]['evidence_score']}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Highest candidate score: 29.",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Candidate review"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT candidate, batch_id, process_alarm_count, fault_event_count, material_lot_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "candidate",
                            "batch_id",
                            "process_alarm_count",
                            "fault_event_count",
                            "material_lot_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_iterrows_record_without_ranking_remains_valid(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, process_alarm_count, fault_event_count FROM risks")
summary = data.groupby("candidate").agg(
    process_alarm=("process_alarm_count", "sum"),
    fault_event=("fault_event_count", "sum"),
).reset_index()
rows = []
for _, row in summary.iterrows():
    total_observations = row["process_alarm"] + row["fault_event"]
    rows.append({"candidate": row["candidate"], "total_observations": total_observations})
table = build_table_result(
    data=pd.DataFrame(rows),
    columns=[
        {"key": "candidate", "label": "Candidate"},
        {"key": "total_observations", "label": "Observation total"},
    ],
)
result = save_analysis_result(analysis_report="Observation totals.", charts=[], tables=[table])
'''

        issues = LineageIndexer._heterogeneous_ranking_issues(code)

        self.assertEqual(issues, [])

    def test_table_rejects_inferred_technical_field_labels(self) -> None:
        result = StructuredResult(
            analysis_report="Batch details are listed below.",
            tables=[{
                "title": "Batch details",
                "columns": [
                    {"key": "batch_id", "label": "batch_id"},
                    {"key": "quality_status", "label": "quality_status"},
                ],
                "rows": [{"batch_id": "B-001", "quality_status": "near_limit"}],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unreadable_table_column_labels",
        )
        self.assertEqual(
            [item["key"] for item in retry.diagnostics["unreadable_table_columns"]],
            ["batch_id", "quality_status"],
        )

    def test_table_allows_explicit_business_column_labels(self) -> None:
        result = StructuredResult(
            analysis_report="Batch details are listed below.",
            tables=[{
                "title": "Batch details",
                "columns": [
                    {"key": "batch_id", "label": "生产批次"},
                    {"key": "quality_status", "label": "质量状态"},
                ],
                "rows": [{"batch_id": "B-001", "quality_status": "near_limit"}],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNone(retry)

    def test_report_allows_rowwise_score_over_shared_population_rates(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT candidate, batch_id, process_alarm_count, fault_event_count FROM risks")
summary = data.groupby("candidate").agg(
    batch_count=("batch_id", "nunique"),
    alarm_batches=("process_alarm_count", "sum"),
    fault_batches=("fault_event_count", "sum"),
).reset_index()
summary["alarm_rate"] = summary["alarm_batches"] / summary["batch_count"]
summary["fault_rate"] = summary["fault_batches"] / summary["batch_count"]
def evidence_score(row):
    return row["alarm_rate"] * 0.5 + row["fault_rate"] * 0.5
summary["score"] = summary.apply(evidence_score, axis=1)
summary = summary.sort_values("score", ascending=False)
analysis_report = f"Highest normalized score: {summary.iloc[0]['score']:.1%}."
result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''
        result = StructuredResult(
            analysis_report="Highest normalized score: 30.0%.",
            tables=[{
                "title": "Scope",
                "columns": [{"key": "note", "label": "Note"}],
                "rows": [{"note": "Shared-population rate score"}],
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT candidate, batch_id, process_alarm_count, fault_event_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": [
                            "candidate",
                            "batch_id",
                            "process_alarm_count",
                            "fault_event_count",
                        ],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_runtime_allows_list_of_dict_ranking_owned_by_the_analysis_agent(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT process_alarm_count, fault_event_count FROM risks")
process_count = int(data["process_alarm_count"].sum())
fault_count = int(data["fault_event_count"].sum())
rows = [
    {"cause": "Process", "score": process_count},
    {"cause": "Equipment", "score": fault_count},
]
ranked = pd.DataFrame(rows).sort_values("score", ascending=False)
chart = build_chart_result(data=ranked, chart_kind="bar", category_field="cause", value_field="score")
result = save_analysis_result(analysis_report="Risk ranking.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Risk ranking.",
            charts=[{
                "title": "Risk ranking",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [
                        {"cause": "Process", "score": 8},
                        {"cause": "Equipment", "score": 3},
                    ]},
                    "encoding": {
                        "category_field": "cause",
                        "value_field": "score",
                    },
                },
                "chart_data_contract": {
                    "source_frame": "ranked",
                    "encoding": {
                        "category_field": "cause",
                        "value_field": "score",
                    },
                },
            }],
            query_snapshots={
                "q_risks": {
                    "implementation": {
                        "sql": "SELECT process_alarm_count, fault_event_count FROM risks"
                    },
                    "execution": {
                        "status": "success",
                        "columns": ["process_alarm_count", "fault_event_count"],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_value_counts_scalar_partitions_remain_one_count_metric(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT pattern FROM process_events")
pattern_counts = data["pattern"].value_counts()
single_count = int(pattern_counts.get("single_burst", 0))
drift_count = int(pattern_counts.get("continuous_drift", 0))
chart_data = pd.DataFrame({
    "pattern": ["Single burst", "Continuous drift"],
    "count": [single_count, drift_count],
})
chart = build_chart_result(
    data=chart_data,
    chart_kind="pie",
    category_field="pattern",
    value_field="count",
)
result = save_analysis_result(analysis_report="Pattern distribution.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Pattern distribution.",
            charts=[{
                "title": "Pattern distribution",
                "chart_type": "echarts",
                "chart_spec": {
                    "series": [{
                        "type": "pie",
                        "data": [
                            {"name": "Single burst", "value": 8},
                            {"name": "Continuous drift", "value": 2},
                        ],
                    }],
                },
                "chart_data_contract": {
                    "encoding": {
                        "category_field": "pattern",
                        "value_field": "count",
                    },
                },
            }],
            query_snapshots={
                "q_process": {
                    "implementation": {"sql": "SELECT pattern FROM process_events"},
                    "execution": {"status": "success", "columns": ["pattern"]},
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_boolean_sum_partitions_remain_one_count_metric(self) -> None:
        code = r'''
data = load_data_with_sql(sql="SELECT alarm_point_count FROM process_events")
single_count = int((data["alarm_point_count"] == 1).sum())
continuous_count = int((data["alarm_point_count"] > 1).sum())
chart_data = pd.DataFrame([
    {"pattern": "Single", "count": single_count},
    {"pattern": "Continuous", "count": continuous_count},
])
chart = build_chart_result(
    data=chart_data,
    chart_kind="pie",
    category_field="pattern",
    value_field="count",
)
result = save_analysis_result(analysis_report="Pattern distribution.", charts=[chart], tables=[])
'''
        result = StructuredResult(
            analysis_report="Pattern distribution.",
            charts=[{
                "title": "Pattern distribution",
                "chart_document": {
                    "chart_kind": "pie",
                    "dataset": {"rows": [
                        {"pattern": "Single", "count": 8},
                        {"pattern": "Continuous", "count": 2},
                    ]},
                    "encoding": {
                        "category_field": "pattern",
                        "value_field": "count",
                    },
                },
            }],
            query_snapshots={
                "q_process": {
                    "implementation": {
                        "sql": "SELECT alarm_point_count FROM process_events"
                    },
                    "execution": {
                        "status": "success",
                        "columns": ["alarm_point_count"],
                    },
                },
            },
        )

        retry = _validate_structured_result_contract(result, generated_code=code)

        self.assertIsNone(retry)

    def test_flat_chart_rejects_additive_total_as_peer_category(self) -> None:
        result = StructuredResult(
            analysis_report="Parameter alarm distribution.",
            charts=[{
                "title": "Parameter alarm distribution",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [
                        {"parameter": "Temperature", "count": 12},
                        {"parameter": "Pressure", "count": 8},
                        {"parameter": "Feed rate", "count": 5},
                        {"parameter": "Process (total)", "count": 25},
                    ]},
                    "encoding": {
                        "category_field": "parameter",
                        "value_field": "count",
                    },
                },
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "overlapping_total_component_categories",
        )

    def test_flat_chart_allows_non_overlapping_peer_categories(self) -> None:
        result = StructuredResult(
            analysis_report="Parameter alarm distribution.",
            charts=[{
                "title": "Parameter alarm distribution",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [
                        {"parameter": "Temperature", "count": 12},
                        {"parameter": "Pressure", "count": 8},
                        {"parameter": "Feed rate", "count": 5},
                    ]},
                    "encoding": {
                        "category_field": "parameter",
                        "value_field": "count",
                    },
                },
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNone(retry)

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
            execution_id=42,
            analysis_report="# report",
            charts=[{"title": "trend"}],
            used_datasource_ids=[12, 13],
            query_snapshots={
                "monthly_profit": {
                    "execution": {
                        "status": "success",
                        "row_count": 3,
                        "columns": ["product", "profit"],
                    },
                },
            },
        )

        payload = json.loads(message.content)
        self.assertEqual(payload["execution_id"], 42)
        self.assertTrue(payload["review_required"])
        self.assertEqual(payload["execution_review"]["used_datasource_ids"], [12, 13])
        self.assertEqual(payload["execution_review"]["queries"], [{
            "query_key": "monthly_profit",
            "status": "success",
            "row_count": 3,
            "columns": ["product", "profit"],
        }])
        self.assertEqual(
            payload["candidate_review_contract"]["required_checks"],
            [
                "question_coverage",
                "population_and_grain",
                "formula_and_unit",
                "artifact_consistency",
                "claim_support",
            ],
        )
        self.assertIn(
            "inference_level",
            payload["candidate_review_contract"]["material_claim_fields"],
        )

        command = _terminal_tool_success(message)

        self.assertTrue(command.update["analysis_complete"])
        self.assertEqual(command.update, {"messages": [message], "analysis_complete": True})
        self.assertEqual(command.goto, END)

    def test_review_claim_requires_qualification_for_descriptive_association(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an explicit qualification"):
            AnalysisClaimAssessment(
                claim_text="报警批次与偏高存在关联",
                inference_level="association",
                support_method="computed_comparison",
                evidence_basis="报警批次偏高 53.5%，无报警批次偏高 52.4%",
                supported_as_written=True,
            )

        assessment = AnalysisClaimAssessment(
            claim_text="报警批次的偏高比例略高",
            inference_level="comparative",
            support_method="computed_comparison",
            evidence_basis="报警批次偏高 53.5%，无报警批次偏高 52.4%，差 1.1 个百分点",
            supported_as_written=True,
        )
        self.assertEqual(assessment.inference_level, "comparative")

    def test_success_message_delivers_bounded_static_advisories_to_review(self) -> None:
        message = _tool_success_message(
            tool_call_id="call-1",
            execution_id=42,
            analysis_report="# report",
            static_analysis_advisories=[{
                "violated_contract": "grouped_visible_dimension_functional_dependency",
                "group_fields": ["product_id"],
                "risky_dimensions": [{"output_field": "company", "reducer": "first"}],
                "code_location": {"line_no": 12, "error_line": "summary = data.groupby(...)"},
            }],
        )

        payload = json.loads(message.content)
        assert payload["static_analysis_advisories"][0]["group_fields"] == ["product_id"]

    def test_static_unproven_risk_is_collected_without_rejecting_program(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"dimensions": ["product_id"]}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT product_id, company, amount FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT product_id, company, amount FROM t")
    summary = data.groupby("product_id", as_index=False).agg({"company": "first", "amount": "sum"})
    table = build_table_result(data=summary, columns=[{"key": "company", "label": "Company"}])
    result = save_analysis_result(analysis_report=f"{len(summary)} products", charts=[], tables=[table])
'''
        advisories = []

        retry = _validate_generated_code_contract(code, advisory_sink=advisories)

        self.assertIsNone(retry)
        grouped = next(
            item for item in advisories
            if item.get("violated_contract") == "grouped_visible_dimension_functional_dependency"
        )
        self.assertEqual(grouped["group_fields"], ["product_id"])
        self.assertEqual(
            grouped["risky_dimensions"][0]["visibility"],
            "confirmed_user_visible",
        )
        self.assertNotIn("context_lines", json.dumps(grouped))

    def test_grouped_first_is_advisory_when_artifact_columns_are_dynamic(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"dimensions": ["product_id"]}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT product_id, company, amount FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT product_id, company, amount FROM t")
    summary = data.groupby("product_id", as_index=False).agg(company=("company", "first"), amount=("amount", "sum"))
    report_columns = {"product_id": "Product", "company": "Company", "amount": "Amount"}
    table = build_table_result(data=summary, columns=[{"key": key, "label": label} for key, label in report_columns.items()])
    result = save_analysis_result(analysis_report=f"{summary.iloc[0]['company']}", charts=[], tables=[table])
'''
        advisories = []

        retry = _validate_generated_code_contract(code, advisory_sink=advisories)

        self.assertIsNone(retry)
        grouped = next(
            item for item in advisories
            if item.get("violated_contract") == "grouped_visible_dimension_functional_dependency"
        )
        self.assertEqual(grouped["group_fields"], ["product_id"])
        self.assertEqual(grouped["risky_dimensions"], [{
            "output_field": "company",
            "source_field": "company",
            "reducer": "first",
            "visibility": "unknown",
        }])

    def test_result_review_packet_reads_normalized_echarts_series(self) -> None:
        chart = build_chart_result(
            data=pd.DataFrame({"product": ["A", "B"], "profit": [12.0, 8.0]}),
            chart_kind="bar",
            category_field="product",
            value_field="profit",
            title="Product profit",
        )
        normalized = _normalize_chart_items([chart])

        message = _tool_success_message(
            tool_call_id="call-1",
            execution_id=42,
            analysis_report="# report",
            charts=normalized,
        )

        review = json.loads(message.content)["artifact_review"]["charts"][0]
        self.assertEqual(review["point_count"], 2)
        self.assertEqual(review["category_count"], 2)
        self.assertEqual(review["sample_categories"], ["A", "B"])
        self.assertEqual(review["series"][0]["sample_values"], [12.0, 8.0])
        self.assertNotIn("row_count", review)

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

    def test_analysis_static_contract_reports_all_missing_timeseries_items_once(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"target_filters": [], "aggregation": {"grain": "day"}}
sql = "SELECT * FROM ts_query('vxbase', 'SELECT * FROM signal LIMIT 100')"
data = load_data_with_sql(sql=sql)
result = save_analysis_result(analysis_report="report", charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "missing_query_constraints")
        self.assertEqual(
            retry.diagnostics["missing_items"],
                [
                    "validate_query_constraints(...) or a composed query helper",
                    "query_constraints.time_series_query",
                ],
        )
        self.assertTrue(any(
            "validate_query_constraints" in item
            for item in retry.repair_instructions
        ))
        self.assertTrue(any(
            "time_series_query" in item
            for item in retry.repair_instructions
        ))

    def test_composed_queries_do_not_require_a_reserved_constraint_variable_name(self) -> None:
        code = '''
execution_intent = "analysis"
cost_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "product", "metric": "unit_cost"},
}
profit_constraints = {
    "time_range": None,
    "target_filters": [],
    "aggregation": {"grain": "product", "metric": "gross_profit"},
}
cost = query_local_file(
    query_key="q_cost",
    query_constraints=cost_constraints,
    file_path="cost.csv",
)
profit = query_local_file(
    query_key="q_profit",
    query_constraints=profit_constraints,
    file_path="profit.csv",
)
cost_total = cost["unit_cost"].sum()
profit_total = profit["gross_profit"].sum()
result = save_analysis_result(
    analysis_report=f"成本 {cost_total}，利润 {profit_total}。",
    charts=[],
    tables=[],
)
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_dedicated_probe_accepts_data_access_without_manual_retry_envelope(self) -> None:
        code = '''
execution_intent = "probe"
probe_data = probe_data_with_sql("SELECT status FROM contracts LIMIT 20")
print(probe_data.to_dict("records"))
'''

        self.assertIsNone(_validate_generated_code_contract(code, allow_implicit_probe_feedback=True))
        self.assertIsNotNone(_validate_generated_code_contract(code))

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

    def test_probe_accepts_observations_from_a_static_outcome_manifest_loop(self) -> None:
        code = '''
execution_intent = "probe"
first = probe_data_with_sql(sql="SELECT status, COUNT(*) AS cnt FROM a GROUP BY status")
second = probe_data_with_sql(sql="SELECT kind, COUNT(*) AS cnt FROM b GROUP BY kind")
observations = []
for name, outcome in [("status", first), ("kind", second)]:
    observation = {
        "name": name,
        "status": outcome["status"],
        "row_count": outcome["row_count"],
    }
    if outcome["status"] == "success" and outcome["row_count"] > 0:
        frame = outcome["data"]
        observation["values"] = frame.head(5).to_dict(orient="records")
    observations.append(observation)
result = request_retry(
    retry_type="probe_feedback",
    message="observed candidate queries",
    diagnostics={"query_observations": observations},
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
        self.assertIsNone(outcome["row_count"])
        self.assertEqual(outcome["error_message"], "bad sql")
        self.assertIsNone(outcome["data"])

    def test_probe_sql_wrapper_keeps_status_envelope_separate_from_dataframe(self) -> None:
        frame = pd.DataFrame({"status": ["open", "closed"], "cnt": [2, 1]})
        token = CURRENT_EXECUTION_INTENT.set("probe")
        try:
            with patch("agent.tools.load_data_with_sql", return_value=frame):
                outcome = probe_data_with_sql("SELECT status, COUNT(*) AS cnt FROM demo")
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)

        self.assertFalse(outcome.data.empty)
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.no_data)
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.row_count, 2)
        self.assertEqual(list(outcome.data.columns), ["status", "cnt"])
        self.assertEqual(outcome.data.to_dict("records"), frame.to_dict("records"))
        self.assertEqual(outcome['status'], 'success')
        self.assertEqual(outcome.data['status'].tolist(), ['open', 'closed'])
        with self.assertRaisesRegex(AttributeError, 'outcome.data'):
            _ = outcome.columns
        self.assertEqual(
            probe_distinct_values(outcome, "status"),
            probe_distinct_values(frame, "status"),
        )

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

    def test_probe_feedback_elevates_query_capacity_risks(self) -> None:
        retry = request_retry(
            retry_type="probe_feedback",
            message="done",
            diagnostics={"query_observations": [{
                "datasource_id": 62,
                "datasource_name": "quality facts",
                "status": "success",
                "row_count": 20419,
            }]},
        )

        self.assertEqual(retry.diagnostics["_query_capacity_risks"], [{
            "datasource_id": 62,
            "datasource_name": "quality facts",
            "observed_row_count": 20419,
            "observed_population_count": 20419,
            "observed_population_counts": {},
            "max_rows_per_physical_query": 10000,
            "required_strategy": "query_side_final_aggregation_or_stable_pagination",
        }])
        self.assertTrue(
            retry.diagnostics["_probe_contract"]["query_execution"][
                "capacity_risks_are_formal_design_inputs"
            ]
        )

    def test_probe_feedback_reads_population_from_aggregate_result(self) -> None:
        retry = request_retry(
            retry_type="probe_feedback",
            message="done",
            diagnostics={"query_observations": [{
                "datasource_id": 62,
                "datasource_name": "quality facts",
                "status": "success",
                "row_count": 1,
                "data": [{"row_count": "20419", "batch_count": 18653}],
            }]},
        )

        summary = retry.diagnostics["_probe_outcome_summary"]["successful"][0]
        self.assertEqual(summary["row_count"], 1)
        self.assertEqual(summary["observed_population_counts"], {
            "row_count": 20419,
            "batch_count": 18653,
        })
        self.assertEqual(
            retry.diagnostics["_query_capacity_risks"][0]["observed_population_count"],
            20419,
        )

    def test_probe_feedback_sums_grouped_count_rows_copied_to_observation(self) -> None:
        retry = request_retry(
            retry_type="probe_feedback",
            message="done",
            diagnostics={"query_observations": [{
                "datasource_id": 76,
                "datasource_name": "process samples",
                "status": "success",
                "row_count": 2,
                "rows": [
                    {"alarm_flag": "0", "cnt": "18234"},
                    {"alarm_flag": "1", "cnt": "195"},
                ],
            }]},
        )

        summary = retry.diagnostics["_probe_outcome_summary"]["successful"][0]
        self.assertEqual(summary["observed_population_counts"], {"cnt": 18429})
        self.assertEqual(
            retry.diagnostics["_query_capacity_risks"][0]["observed_population_count"],
            18429,
        )

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

    def test_analysis_broad_sql_exception_is_static_and_does_not_authorize_scope_loss(self) -> None:
        retry = _validate_generated_code_contract('''
execution_intent = "analysis"
try:
    frame = load_data_with_sql(sql="SELECT * FROM equipment")
except Exception:
    frame = None
result = save_analysis_result(analysis_report="report", charts=[], tables=[])
''')

        self.assertIsNotNone(retry)
        self.assertEqual(retry.diagnostics["violated_contract"], "sql_load_error_handling")
        self.assertEqual(retry.diagnostics["failure_phase"], "static_contract_validation")
        self.assertFalse(retry.diagnostics["queries_executed"])
        self.assertFalse(retry.diagnostics["scope_change_authorized"])
        self.assertTrue(any(
            "保留当前的全部数据源" in item
            for item in retry.repair_instructions
        ))

    def test_technical_repair_rejects_silent_datasource_scope_regression(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[67, 68, 149],
            current_used_datasource_ids=[67],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "retry_type": "contract_error",
                "diagnostics": {
                    "violated_contract": "sql_load_error_handling",
                    "scope_change_authorized": False,
                },
            },
        )

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "analysis_scope_regression_after_technical_repair",
        )
        self.assertEqual(retry.diagnostics["missing_datasource_ids"], [68, 149])

    def test_technical_repair_accepts_preserved_datasource_scope(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[67, 68, 149],
            current_used_datasource_ids=[67, 68, 149],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "retry_type": "contract_error",
                "diagnostics": {
                    "violated_contract": "sql_load_error_handling",
                    "scope_change_authorized": False,
                },
            },
        )

        self.assertIsNone(retry)

    def test_failed_analysis_declaration_does_not_create_scope_anchor(self) -> None:
        history = [
            SimpleNamespace(
                generated_code='execution_intent = "probe"\nresult = request_retry(...)',
                result_payload_json=json.dumps({"error_type": "probe_feedback"}),
                used_datasource_ids_json='[62]',
            ),
            SimpleNamespace(
                generated_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
                result_payload_json=json.dumps({"error_type": "schema_or_column_mismatch"}),
                used_datasource_ids_json='[61, 62, 64, 66, 79, 179]',
            ),
        ]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61, 62],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
        )

        self.assertIsNone(retry)

    def test_rejected_scope_diagnostics_do_not_create_scope_anchor(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
frame = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({
                "retry_type": "contract_error",
                "diagnostics": {
                    "violated_contract": "analysis_scope_regression_after_technical_repair",
                    "previous_used_datasource_ids": [61, 62, 64, 68],
                    "current_used_datasource_ids": [61],
                    "missing_datasource_ids": [62, 64, 68],
                    "scope_change_authorized": False,
                },
            }),
            used_datasource_ids_json='[61]',
        )]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61, 62],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
            selected_datasource_snapshot=[{
                "datasource_id": 61,
                "datasource_identifier": "warehouse.fact_quality",
            }],
        )

        self.assertIsNone(retry)

    def test_probe_scope_is_allowed_to_be_narrower_than_failed_analysis(self) -> None:
        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=[SimpleNamespace(
                generated_code='execution_intent = "analysis"',
                result_payload_json=json.dumps({"error_type": "runtime_error"}),
                used_datasource_ids_json='[61, 62, 64]',
            )],
            current_used_datasource_ids=[62],
            current_code='execution_intent = "probe"\nresult = request_retry(...)',
        )

        self.assertIsNone(retry)

    def test_scope_anchor_ignores_declared_sources_absent_from_failed_code(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
frame = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({"error_type": "schema_or_column_mismatch"}),
            used_datasource_ids_json='[61, 62, 63]',
        )]
        snapshot = [
            {"datasource_id": 61, "datasource_identifier": "warehouse.fact_quality"},
            {"datasource_id": 62, "datasource_identifier": "warehouse.dim_product"},
            {"datasource_id": 63, "datasource_identifier": "warehouse.dim_customer"},
        ]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
            selected_datasource_snapshot=snapshot,
        )

        self.assertIsNone(retry)

    def test_failed_code_references_do_not_create_scope_anchor(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
facts = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
products = load_data_with_sql(sql="SELECT * FROM warehouse.dim_product")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({"error_type": "schema_or_column_mismatch"}),
            used_datasource_ids_json='[61, 62, 63]',
        )]
        snapshot = [
            {"datasource_id": 61, "datasource_identifier": "warehouse.fact_quality"},
            {"datasource_id": 62, "datasource_identifier": "warehouse.dim_product"},
            {"datasource_id": 63, "datasource_identifier": "warehouse.dim_customer"},
        ]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
            selected_datasource_snapshot=snapshot,
        )

        self.assertIsNone(retry)

    def test_scope_anchor_skips_rejected_attempt_without_data_access(self) -> None:
        history = [
            SimpleNamespace(
                generated_code='''execution_intent = "analysis"
query_constraints = {}
result = save_analysis_result(...)
''',
                result_payload_json=json.dumps({"retry_type": "missing_query_constraints"}),
                used_datasource_ids_json='[61, 62, 63]',
            ),
            SimpleNamespace(
                generated_code='''execution_intent = "analysis"
frame = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
result = save_analysis_result(...)
''',
                result_payload_json=json.dumps({"error_type": "query_result_too_large"}),
                used_datasource_ids_json='[61, 62, 63]',
            ),
        ]
        snapshot = [
            {"datasource_id": 61, "datasource_identifier": "warehouse.fact_quality"},
            {"datasource_id": 62, "datasource_identifier": "warehouse.dim_product"},
            {"datasource_id": 63, "datasource_identifier": "warehouse.dim_customer"},
        ]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
            selected_datasource_snapshot=snapshot,
        )

        self.assertIsNone(retry)

    def test_query_size_repair_cannot_drop_an_unrelated_datasource(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[62, 76, 77, 79],
            current_used_datasource_ids=[62, 76, 77],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "error_type": "query_result_too_large",
                "diagnostics": {},
            },
        )

        self.assertIsNotNone(retry)
        self.assertEqual(retry.diagnostics["missing_datasource_ids"], [79])

    def test_result_contract_repair_may_correct_overdeclared_datasource_ids(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[62, 76, 77, 79],
            current_used_datasource_ids=[62, 76, 77],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "retry_type": "result_contract_error",
                "diagnostics": {"declared_but_unread_datasource_ids": [79]},
            },
        )

        self.assertIsNone(retry)

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
            query_snapshots={
                "q_shipping": {
                    "execution": {
                        "status": "success",
                        "rows_before_reduction": 12,
                        "rows_after_reduction": 3,
                    }
                }
            },
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

    def test_unproven_head_does_not_force_a_repair(self) -> None:
        result = StructuredResult(
            analysis_report="All three available products are shown.",
            charts=[build_chart_result(
                chart_kind="bar",
                data=[
                    {"product": "A", "amount": 100},
                    {"product": "B", "amount": 80},
                    {"product": "C", "amount": 60},
                ],
                title="Product revenue",
                category_field="product",
                value_field="amount",
            )],
            tables=[],
        )

        retry = _validate_structured_result_contract(
            result,
            generated_code=(
                "top_n = 10\n"
                "visible = products.head(top_n)\n"
                "charts = [build_chart_result(data=visible)]"
            ),
        )

        self.assertIsNone(retry)

    def test_successful_query_snapshots_create_scope_anchor(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
facts = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
products = load_data_with_sql(sql="SELECT * FROM warehouse.dim_product")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({
                "error_type": "chart_contract_error",
                "query_snapshots": {
                    "q_facts": {
                        "implementation": {
                            "physical_execution": True,
                            "datasource_identifier": "warehouse.fact_quality",
                            "sql": "SELECT * FROM warehouse.fact_quality",
                        },
                        "execution": {"status": "success"},
                    },
                    "q_products": {
                        "implementation": {
                            "physical_execution": True,
                            "datasource_identifier": "warehouse.dim_product",
                            "sql": "SELECT * FROM warehouse.dim_product",
                        },
                        "execution": {"status": "no_data"},
                    },
                },
            }),
            used_datasource_ids_json='[61, 62]',
        )]
        snapshot = [
            {"datasource_id": 61, "datasource_identifier": "warehouse.fact_quality"},
            {"datasource_id": 62, "datasource_identifier": "warehouse.dim_product"},
        ]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='execution_intent = "analysis"\nresult = save_analysis_result(...)',
            selected_datasource_snapshot=snapshot,
        )

        self.assertIsNotNone(retry)
        self.assertEqual(retry.diagnostics["previous_used_datasource_ids"], [61, 62])
        self.assertEqual(retry.diagnostics["missing_datasource_ids"], [62])

    def test_technical_repair_accepts_sources_present_in_generated_query_code(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
trend = load_data_with_sql(sql="SELECT * FROM vxbase.double_signal")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({
                "retry_type": "invalid_query_constraints",
                "diagnostics": {
                    "violated_contract": "missing_time_series_query_contract",
                    "scope_change_authorized": False,
                },
            }),
            used_datasource_ids_json='[61, 62]',
        )]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
trend = load_data_with_sql(sql="SELECT * FROM vxbase.double_signal")
result = save_analysis_result(...)
''',
            selected_datasource_snapshot=[
                {
                    "datasource_id": 61,
                    "datasource_identifier": "warehouse.fact_quality",
                },
                {
                    "datasource_id": 62,
                    "datasource_identifier": "vxbase.double_signal",
                },
            ],
        )

        self.assertIsNone(retry)

    def test_failed_query_code_cannot_prove_missing_source(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
trend = load_data_with_sql(sql="SELECT * FROM vxbase.double_signal")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({
                "retry_type": "invalid_query_constraints",
                "diagnostics": {
                    "violated_contract": "missing_time_series_query_contract",
                    "scope_change_authorized": False,
                },
            }),
            used_datasource_ids_json='[61, 62]',
        )]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
result = save_analysis_result(...)
''',
            selected_datasource_snapshot=[
                {
                    "datasource_id": 61,
                    "datasource_identifier": "warehouse.fact_quality",
                },
                {
                    "datasource_id": 62,
                    "datasource_identifier": "vxbase.double_signal",
                },
            ],
        )

        self.assertIsNone(retry)

    def test_static_implementation_failure_preserves_declared_analysis_scope(self) -> None:
        history = [SimpleNamespace(
            generated_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
products = load_data_with_sql(sql="SELECT * FROM warehouse.dim_product")
result = save_analysis_result(...)
''',
            result_payload_json=json.dumps({
                "error_type": "contract_error",
                "diagnostics": {
                    "violated_contract": "runtime_helper_signature_mismatch",
                    "failure_domain": "implementation",
                    "failure_phase": "static_contract_validation",
                    "scope_change_authorized": False,
                },
            }),
            used_datasource_ids_json='[61, 62]',
        )]

        retry = _validate_analysis_scope_against_execution_history(
            previous_executions=history,
            current_used_datasource_ids=[61],
            current_code='''execution_intent = "analysis"
quality = load_data_with_sql(sql="SELECT * FROM warehouse.fact_quality")
result = save_analysis_result(...)
''',
            selected_datasource_snapshot=[
                {"datasource_id": 61, "datasource_identifier": "warehouse.fact_quality"},
                {"datasource_id": 62, "datasource_identifier": "warehouse.dim_product"},
            ],
        )

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "analysis_scope_regression_after_technical_repair",
        )
        self.assertEqual(retry.diagnostics["missing_datasource_ids"], [62])

    def test_unproven_scope_is_not_preserved_by_direct_validator(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[61, 62],
            current_used_datasource_ids=[61],
            previous_failure_payload={
                "error_type": "chart_contract_error",
                "diagnostics": {"violated_contract": "chart_contract"},
            },
        )

        self.assertIsNone(retry)

    def test_any_static_contract_repair_preserves_datasource_scope_by_default(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[61, 62, 64, 77],
            current_used_datasource_ids=[62],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "retry_type": "contract_error",
                "diagnostics": {
                    "violated_contract": "report_superlative_requires_explicit_extrema",
                },
            },
        )

        self.assertIsNotNone(retry)
        self.assertEqual(retry.diagnostics["missing_datasource_ids"], [61, 64, 77])

    def test_explicitly_authorized_contract_may_normalize_datasource_declaration(self) -> None:
        retry = _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=[61, 62],
            current_used_datasource_ids=[62],
            previous_scope_is_proven=True,
            previous_failure_payload={
                "retry_type": "result_contract_error",
                "diagnostics": {
                    "violated_contract": "declared_datasource_without_physical_access",
                    "scope_change_authorized": True,
                },
            },
        )

        self.assertIsNone(retry)

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

    def test_sql_loader_infers_numeric_aggregates_with_driver_placeholders(self) -> None:
        raw = pd.DataFrame([{
            "quality_status": "near_limit",
            "record_count": "7",
            "avg_abs_deviation": "12.50",
        }])
        with patch("agent.tools._load_data_with_fed_query", return_value=raw):
            data = load_data_with_sql(
                "SELECT quality_status, COUNT(*) AS record_count, "
                "AVG(ABS(target_deviation)) AS avg_abs_deviation FROM t "
                "WHERE quality_status IN (%s, %s) GROUP BY quality_status",
                params=["oos", "near_limit"],
            )

        self.assertEqual(data.loc[0, "record_count"], 7)
        self.assertEqual(data.loc[0, "avg_abs_deviation"], 12.5)

    def test_sql_loader_restores_nested_aggregate_alias_type(self) -> None:
        raw = pd.DataFrame([{
            "batch_id": "B-001",
            "alarm_samples": "3",
            "min_t": "2026-08-10 00:00:00",
        }])
        with patch("agent.tools._load_data_with_fed_query", return_value=raw):
            data = load_data_with_sql(
                "SELECT t.batch_id, t.alarm_samples, t.min_t FROM ("
                "SELECT batch_id, SUM(alarm_flag) AS alarm_samples, "
                "MIN(sample_time) AS min_t FROM samples GROUP BY batch_id"
                ") t ORDER BY t.batch_id LIMIT 100"
            )

        self.assertEqual(data.loc[0, "alarm_samples"], 3)
        self.assertEqual(data.loc[0, "min_t"], "2026-08-10 00:00:00")

    def test_sql_loader_uses_schema_type_for_min_max_aggregate_aliases(self) -> None:
        raw = pd.DataFrame([{
            "max_alarm_count": "12",
            "first_business_id": "00001",
        }])
        token = CURRENT_DATASOURCE_NUMERIC_FIELDS.set([{
            "identifiers": ["dw.process_alarm"],
            "fields": ["alarm_count"],
        }])
        try:
            with patch("agent.tools._load_data_with_fed_query", return_value=raw):
                data = load_data_with_sql(
                    "SELECT MAX(alarm_count) AS max_alarm_count, "
                    "MIN(business_id) AS first_business_id FROM dw.process_alarm"
                )
        finally:
            CURRENT_DATASOURCE_NUMERIC_FIELDS.reset(token)

        self.assertEqual(data.loc[0, "max_alarm_count"], 12)
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

    def test_report_allows_numeric_unit_followed_by_chinese_word(self) -> None:
        self.assertEqual(
            _find_repeated_numeric_display_units(
                "## 分析口径\n\n本周期为2026年年内累计，截至当前日期。"
            ),
            [],
        )

    def test_report_does_not_block_on_ambiguous_duplicate_metric_table_values(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "## Quality summary\n\n"
                "| 指标 | 数值 |\n| --- | --- |\n"
                "| 一次合格率 | 0 |\n| OOS批次率 | 0 |"
            ),
            charts=[],
            tables=[{
                "title": "Quality metrics",
                "columns": [
                    {"key": "first_pass_rate", "label": "一次合格率"},
                    {"key": "oos_rate", "label": "OOS批次率"},
                ],
                "rows": [{"first_pass_rate": 0.9234, "oos_rate": 0.0403}],
            }],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_report_accepts_percent_display_equivalent_to_fraction_table_value(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "## Quality summary\n\n"
                "| 指标 | 数值 |\n| --- | --- |\n"
                "| 一次合格率 | 92.34% |\n| OOS批次率 | 4.03% |"
            ),
            charts=[],
            tables=[{
                "title": "Quality metrics",
                "columns": [
                    {"key": "first_pass_rate", "label": "一次合格率"},
                    {"key": "oos_rate", "label": "OOS批次率"},
                ],
                "rows": [{"first_pass_rate": 0.9234, "oos_rate": 0.0403}],
            }],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_report_accepts_percent_declared_by_metric_label(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "## Quality summary\n\n"
                "| 指标 | 数值 |\n| --- | --- |\n"
                "| 一次合格率(%) | 92.34 |\n| OOS批次率(%) | 4.03 |"
            ),
            charts=[],
            tables=[{
                "title": "Quality metrics",
                "columns": [
                    {"key": "first_pass_rate", "label": "一次合格率(%)"},
                    {"key": "oos_rate", "label": "OOS批次率(%)"},
                ],
                "rows": [{"first_pass_rate": 0.9234, "oos_rate": 0.0403}],
            }],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_report_skips_same_label_from_multiple_structured_boundaries(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "## Regional comparison\n\n"
                "| 指标 | 数值 |\n| --- | --- |\n"
                "| 完成率 | 80% |\n| 订单数 | 10 |"
            ),
            charts=[],
            tables=[
                {
                    "title": "North",
                    "columns": [
                        {"key": "rate", "label": "完成率"},
                        {"key": "orders", "label": "订单数"},
                    ],
                    "rows": [{"rate": 0.8, "orders": 10}],
                },
                {
                    "title": "South",
                    "columns": [
                        {"key": "rate", "label": "完成率"},
                        {"key": "orders", "label": "订单数"},
                    ],
                    "rows": [{"rate": 0.6, "orders": 20}],
                },
            ],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

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
report_sections = [
    "- Range: {start_date} to {end_date[:10]}",
    "- Missing share: {100 - covered_pct:.1f}%",
]
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
        self.assertEqual(retry.code_error_context["line_no"], 6)
        self.assertIn("{start_date}", retry.code_error_context["error_line"])

    def test_report_rejects_unbound_identifier_placeholder(self) -> None:
        result = StructuredResult(
            analysis_report="## Scope\n- Total batches: {total_batches}",
            charts=[{"title": "Trend", "chart_document": {"series": []}}],
            tables=[],
        )
        generated_code = '''
report_sections = [
    "## Scope",
    "- Total batches: {total_batches}",
]
'''

        retry = _validate_structured_result_contract(
            result,
            generated_code=generated_code,
        )

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unrendered_report_placeholder",
        )
        self.assertEqual(
            retry.diagnostics["placeholders"][0]["unbound_identifier"],
            "total_batches",
        )
        self.assertEqual(retry.code_error_context["line_no"], 4)

    def test_code_rejects_literal_unfinished_report_before_execution(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"grain": "overall"}}
validation = validate_query_constraints(query_constraints=query_constraints)
result = save_analysis_result(analysis_report="placeholder", charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "missing_report")
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unfinished_report_sentinel",
        )
        self.assertEqual(retry.code_error_context["line_no"], 5)

    def test_result_rejects_dynamically_produced_unfinished_report(self) -> None:
        result = StructuredResult(
            analysis_report="## TODO",
            charts=[],
            tables=[],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "missing_report")
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "unfinished_report_sentinel",
        )

    def test_result_allows_short_but_complete_report(self) -> None:
        result = StructuredResult(
            analysis_report="当前共有 3 份有效合同。",
            charts=[],
            tables=[{
                "title": "合同概览",
                "columns": [{"key": "status", "label": "状态"}, {"key": "count", "label": "数量"}],
                "data": [{"status": "有效", "count": 3}],
            }],
        )

        self.assertIsNone(_validate_structured_result_contract(result))

    def test_result_rejects_empty_structured_artifact_after_analysis(self) -> None:
        result = StructuredResult(
            analysis_report="统计完成。",
            charts=[],
            tables=[{
                "title": "统计结果",
                "description": "汇总结果",
                "columns": [{"key": "count", "label": "数量"}],
                "rows": [],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.retry_type, "result_contract_error")
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "empty_structured_artifact",
        )

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

    def test_code_does_not_block_unproven_grouped_dimension_semantics(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"grain": "product"}}
validation = validate_query_constraints(query_constraints=query_constraints)
summary = merged.groupby("product_code", as_index=False).agg(
    company_name=("company_name", "first"),
    revenue=("revenue", "sum"),
)
columns = [
    {"key": "product_code", "label": "Product"},
    {"key": "company_name", "label": "Company"},
    {"key": "revenue", "label": "Revenue"},
]
tables = [build_table_result(data=summary, columns=columns)]
result = save_analysis_result(analysis_report="report", tables=tables)
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_code_allows_first_value_when_dimension_is_part_of_grouping_grain(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"grain": "company_product"}}
validation = validate_query_constraints(query_constraints=query_constraints)
summary = merged.groupby(["company_name", "product_code"], as_index=False).agg(
    revenue=("revenue", "sum"),
)
columns = [
    {"key": "company_name", "label": "Company"},
    {"key": "product_code", "label": "Product"},
    {"key": "revenue", "label": "Revenue"},
]
tables = [build_table_result(data=summary, columns=columns)]
result = save_analysis_result(analysis_report="report", tables=tables)
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNone(retry)

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

    def test_generated_code_does_not_block_uncertain_zero_default_semantics(self) -> None:
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

        self.assertIsNone(_validate_generated_code_contract(code))

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

    def test_generated_code_diagnoses_but_does_not_reject_unproven_extremum(self) -> None:
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

        self.assertIsNone(_validate_generated_code_contract(code))
        advisory = _validate_report_extrema_selection_contract(ast.parse(code), code)
        self.assertEqual(advisory.diagnostics['proof_state'], 'unknown')

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

    def test_generated_code_allows_sorted_positional_extrema_row_in_report(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    data = data.sort_values("contract_count", ascending=False).reset_index(drop=True)
    analysis_report = f"合同状态以 {data.iloc[0]['status']} 为主，共 {data.iloc[0]['contract_count']} 个。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_wrapped_metric_from_sorted_extrema_row(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    suspicious = data.sort_values("contract_count", ascending=False).head(10)
    analysis_report = f"Highest status is {suspicious.iloc[0]['status']} with {int(suspicious.iloc[0]['contract_count'])} contracts."
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_inline_sorted_positional_extrema_row(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status")
    analysis_report = f"Highest status is {data.sort_values('contract_count', ascending=False).iloc[0]['status']} with {data.sort_values('contract_count', ascending=False).iloc[0]['contract_count']} contracts."
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_dynamic_sql_order_when_static_proof_is_unknown(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
sql = build_ranked_contract_sql()
validation = validate_query_constraints(query_constraints=query_constraints, sql=sql)
if validation is not None:
    result = validation
else:
    data = load_data_with_sql(sql)
    analysis_report = f"合同状态以 {data.iloc[0]['status']} 为主，共 {data.iloc[0]['contract_count']} 个。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_static_sql_ordered_by_claimed_extremum(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
sql = "SELECT status, COUNT(*) AS contract_count FROM t GROUP BY status ORDER BY contract_count DESC"
validation = validate_query_constraints(query_constraints=query_constraints, sql=sql)
if validation is not None:
    result = validation
else:
    data = load_data_with_sql(sql)
    analysis_report = f"合同状态以 {data.iloc[0]['status']} 为主，共 {data.iloc[0]['contract_count']} 个。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_rejects_reversed_nearest_boundary_direction(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "boundary_distance"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT value, lower_limit, upper_limit FROM tests")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT value, lower_limit, upper_limit FROM tests")
    data["boundary_distance_ratio"] = None
    for idx, row in data.iterrows():
        data.at[idx, "boundary_distance_ratio"] = min(
            row["value"] - row["lower_limit"],
            row["upper_limit"] - row["value"],
        ) / (row["upper_limit"] - row["lower_limit"])
    ranked = data.sort_values("boundary_distance_ratio", ascending=False)
    analysis_report = f"The higher the ratio, the closer it is to the specification limit. Highest risk: {ranked.iloc[0]['boundary_distance_ratio']}."
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "bounded_interval_metric_direction_mismatch",
        )
        self.assertEqual(
            retry.diagnostics["descending_sorts"][0]["field"],
            "boundary_distance_ratio",
        )

    def test_generated_code_allows_nearest_boundary_margin_ranked_ascending(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "boundary_distance"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT value, lower_limit, upper_limit FROM tests")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT value, lower_limit, upper_limit FROM tests")
    data["boundary_distance_ratio"] = None
    for idx, row in data.iterrows():
        data.at[idx, "boundary_distance_ratio"] = min(
            row["value"] - row["lower_limit"],
            row["upper_limit"] - row["value"],
        ) / (row["upper_limit"] - row["lower_limit"])
    ranked = data.sort_values("boundary_distance_ratio", ascending=True)
    analysis_report = f"The lower the ratio, the closer it is to the specification limit. Boundary risk: {ranked.iloc[0]['boundary_distance_ratio']}."
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_explicit_higher_is_riskier_boundary_score(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "boundary_risk"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT value, lower_limit, upper_limit FROM tests")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT value, lower_limit, upper_limit FROM tests")
    data["boundary_risk_score"] = 1 - min(
        data.iloc[0]["value"] - data.iloc[0]["lower_limit"],
        data.iloc[0]["upper_limit"] - data.iloc[0]["value"],
    ) / (data.iloc[0]["upper_limit"] - data.iloc[0]["lower_limit"])
    ranked = data.sort_values("boundary_risk_score", ascending=False)
    analysis_report = f"The higher the risk score, the closer it is to the specification limit: {ranked.iloc[0]['boundary_risk_score']}."
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

    def test_generated_code_diagnoses_unproven_population_without_forcing_repair(self) -> None:
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

        self.assertIsNone(_validate_generated_code_contract(code))
        retry = _validate_report_paired_metric_population_contract(ast.parse(code), code)
        self.assertEqual(retry.diagnostics['proof_state'], 'unknown')
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

    def test_generated_code_allows_bounded_period_when_custom_helper_is_unknown(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "contract_count"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT expiration_time FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT expiration_time FROM t")
    now = pd.Timestamp.now()
    upcoming = apply_bounded_window(data[data["expiration_time"] >= now], months=6)
    upcoming_count = len(upcoming)
    analysis_report = f"未来6个月内有 {upcoming_count} 个合同即将到期。"
    result = save_analysis_result(analysis_report=analysis_report, charts=[], tables=[])
'''

        self.assertIsNone(_validate_generated_code_contract(code))

    def test_generated_code_allows_same_population_with_different_measure_fields(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {"aggregation": {"metric": "pass_rate"}}
validation = validate_query_constraints(query_constraints=query_constraints, sql="SELECT score, sample_id FROM t")
if validation is not None:
    result = validation
else:
    data = load_data_with_sql("SELECT score, sample_id FROM t")
    passed = data[data["score"] >= 60]
    pass_rate = len(passed) / len(data) if len(data) else 0
    pass_count = passed["sample_id"].nunique()
    analysis_report = f"样本合格率 {pass_rate:.1%}（{pass_count} 个）。"
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

    def test_empty_markdown_leaf_sections_are_structural_incompleteness(self) -> None:
        report = """## Report

### Findings

#### Available population

- 7 records were available.

#### Missing population

#### Recommendation

### Method

The method is documented.
"""

        findings = _find_empty_markdown_leaf_sections(report)

        self.assertEqual(
            [item["heading"] for item in findings],
            ["Missing population", "Recommendation"],
        )
        retry = _validate_structured_result_contract(StructuredResult(
            analysis_report=report,
            charts=[{
                "title": "Distribution",
                "chart_spec": {"series": [{"data": [7]}]},
            }],
            tables=[],
        ))
        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "empty_markdown_leaf_sections",
        )

    def test_parent_heading_with_child_sections_is_not_empty(self) -> None:
        report = """## Report

### Findings

#### Distribution

The population is dispersed.
"""

        self.assertEqual(_find_empty_markdown_leaf_sections(report), [])

    def test_static_review_reports_multiple_independent_contracts_once(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {}
validate_query_constraints(query_constraints=query_constraints, sql="SELECT 1")
frame = load_data_with_sql(sql="SELECT group_id, label, value, expiration_time FROM metrics")
summary = frame.groupby("group_id").agg(
    label=("label", "first"),
    value=("value", "sum"),
).reset_index().sort_values("value", ascending=True)
now = pd.Timestamp.now()
upcoming = frame[frame["expiration_time"] >= now]
upcoming_count = len(upcoming)
charts = [build_chart_result(
    title="Distribution",
    data=summary,
    chart_type="bar",
    category_field="label",
    value_fields=["value"],
)]
report = f"## Report\\n\\n### Finding\\n\\n未来6个月内有 {upcoming_count} 个项目。分布以 {summary.iloc[0]['label']} 为主，共 {summary.iloc[0]['value']}。"
result = save_analysis_result(analysis_report=report, charts=charts, tables=[])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "multiple_static_contract_violations",
        )
        contracts = {
            item["diagnostics"].get("violated_contract")
            for item in retry.diagnostics["violations"]
        }
        self.assertIn("report_superlative_requires_explicit_extrema", contracts)
        self.assertIn("bounded_report_period_requires_two_date_boundaries", contracts)

    def test_static_review_allows_point_scoped_metric_chart_and_table(self) -> None:
        code = r'''
execution_intent = "analysis"
query_constraints = {}
validate_query_constraints(query_constraints=query_constraints, sql="SELECT 1")
frame = load_data_with_sql(sql="SELECT pass_rate, oos_rate FROM metrics")
pass_rate = float(frame.iloc[0]["pass_rate"])
oos_rate = float(frame.iloc[0]["oos_rate"])
chart_frame = pd.DataFrame({
    "指标": ["一次合格率", "OOS批次率"],
    "比率(%)": [pass_rate * 100, oos_rate * 100],
})
chart = build_chart_result(
    data=chart_frame,
    chart_kind="bar",
    category_field="指标",
    value_field="比率(%)",
)
table = build_table_result(
    title="指标汇总",
    columns=[{"key": "指标", "label": "指标"}, {"key": "数值", "label": "数值"}],
    rows=[
        {"指标": "一次合格率", "数值": f"{pass_rate * 100:.2f}"},
        {"指标": "OOS批次率", "数值": f"{oos_rate * 100:.2f}"},
    ],
)
result = save_analysis_result(analysis_report="## 结论\n\n质量指标存在差异。", charts=[chart], tables=[table])
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNone(retry)

    def test_validate_query_constraints_accepts_local_file_without_sql(self) -> None:
        retry = validate_query_constraints(
            query_key="q_local_file",
            query_constraints={
                "time_range": None,
                "target_filters": [],
                "aggregation": {"grain": "detail", "metric": "count"},
            },
            sql="",
            filter_expressions=[],
            aggregation_hints=["detail", "count"],
        )

        self.assertIsNone(retry)

    def test_composed_sql_query_runs_validation_before_loader(self) -> None:
        expected = pd.DataFrame([{"metric_value": 12.0}])
        constraints = {
            "time_range": None,
            "target_filters": [],
            "aggregation": {"grain": "overall", "metric": "metric_value"},
        }
        with (
            patch("agent.tools.validate_query_constraints", return_value=None) as validate,
            patch("agent.tools.load_data_with_sql", return_value=expected) as loader,
        ):
            actual = query_data_with_sql(
                query_key="q_metric",
                query_constraints=constraints,
                sql="SELECT SUM(metric_value) AS metric_value FROM metrics",
                params=[2026],
                numeric_columns=["metric_value"],
            )

        self.assertIs(actual, expected)
        validate.assert_called_once_with(
            query_key="q_metric",
            query_constraints=constraints,
            sql="SELECT SUM(metric_value) AS metric_value FROM metrics",
            query_params=[2026],
            filter_expressions=None,
            aggregation_hints=None,
            sql_context=None,
            scope_source_query_key="",
            scope_binding=None,
        )
        loader.assert_called_once_with(
            sql="SELECT SUM(metric_value) AS metric_value FROM metrics",
            params=[2026],
            numeric_columns=["metric_value"],
            text_columns=None,
        )

    def test_composed_sql_query_propagates_structured_retry_without_loading(self) -> None:
        retry = request_retry(
            retry_type="invalid_query_constraints",
            message="invalid",
        )
        with (
            patch("agent.tools.validate_query_constraints", return_value=retry),
            patch("agent.tools.load_data_with_sql") as loader,
        ):
            with self.assertRaises(QueryConstraintRetrySignal) as captured:
                query_data_with_sql(
                    query_key="q_metric",
                    query_constraints={
                        "time_range": None,
                        "target_filters": [],
                        "aggregation": None,
                    },
                    sql="SELECT 1",
                )

        self.assertIs(captured.exception.retry_result, retry)
        loader.assert_not_called()

    def test_composed_local_file_query_runs_validation_before_loader(self) -> None:
        expected = pd.DataFrame([{"category": "A", "metric_value": 12.0}])
        constraints = {
            "time_range": None,
            "target_filters": [{"field_hint": "category", "value": ["A"]}],
            "aggregation": {"grain": "category", "metric": "metric_value"},
        }
        with (
            patch("agent.tools.validate_query_constraints", return_value=None) as validate,
            patch("agent.tools.load_local_file", return_value=expected) as loader,
        ):
            actual = query_local_file(
                query_key="q_file",
                query_constraints=constraints,
                file_path=r"D:\\data\\metrics.csv",
                filter_expressions=["data['category'].isin(['A'])"],
                aggregation_hints=["category", "metric_value", "sum"],
            )

        self.assertIs(actual, expected)
        validate.assert_called_once_with(
            query_key="q_file",
            query_constraints=constraints,
            sql="",
            filter_expressions=["data['category'].isin(['A'])"],
            aggregation_hints=["category", "metric_value", "sum"],
        )
        loader.assert_called_once_with(
            file_path=r"D:\\data\\metrics.csv",
            sheet_name=None,
        )

    def test_composed_local_file_query_accepts_full_scope_detail_without_redundant_hints(self) -> None:
        expected = pd.DataFrame([{"device_id": "A"}])
        constraints = {
            "time_range": None,
            "target_filters": [],
            "aggregation": {"grain": "detail", "metric": "count"},
        }
        with (
            patch("agent.tools.load_local_file", return_value=expected) as loader,
            patch("agent.tools.validate_query_constraints", return_value=None) as validate,
        ):
            actual = query_local_file(
                query_key="q_devices",
                query_constraints=constraints,
                file_path=r"D:\\data\\devices.xlsx",
                filter_expressions=[],
            )

        self.assertIs(actual, expected)
        validate.assert_called_once_with(
            query_key="q_devices",
            query_constraints=constraints,
            sql="",
            filter_expressions=[],
            aggregation_hints=["detail"],
        )
        loader.assert_called_once_with(
            file_path=r"D:\\data\\devices.xlsx",
            sheet_name=None,
        )

    def test_composed_local_file_query_does_not_infer_non_detail_aggregation(self) -> None:
        with self.assertRaises(QueryConstraintRetrySignal) as captured:
            query_local_file(
                query_key="q_summary",
                query_constraints={
                    "time_range": None,
                    "target_filters": [],
                    "aggregation": {"grain": "month", "metric": "revenue"},
                },
                file_path=r"D:\\data\\metrics.csv",
            )

        self.assertEqual(captured.exception.retry_result.retry_type, "query_constraint_violation")

    def test_composed_low_memory_file_query_preserves_streaming_options(self) -> None:
        iterator = iter([pd.DataFrame([{"metric_value": 1.0}])])
        with (
            patch("agent.tools.validate_query_constraints", return_value=None),
            patch("agent.tools.load_local_file_low_memory", return_value=iterator) as loader,
        ):
            actual = query_local_file_low_memory(
                query_key="q_large_file",
                query_constraints={
                    "time_range": None,
                    "target_filters": [],
                    "aggregation": {"grain": "overall", "metric": "metric_value"},
                },
                file_path=r"D:\\data\\large.csv",
                aggregation_hints=["metric_value", "sum"],
                chunk_size=20000,
                usecols=["metric_value"],
                dtype={"metric_value": "float64"},
            )

        self.assertIs(actual, iterator)
        loader.assert_called_once_with(
            file_path=r"D:\\data\\large.csv",
            sheet_name=None,
            chunk_size=20000,
            usecols=["metric_value"],
            dtype={"metric_value": "float64"},
            parse_dates=None,
            low_memory=True,
        )

    def test_composed_local_file_retry_prevents_physical_load(self) -> None:
        retry = request_retry(
            retry_type="invalid_query_constraints",
            message="invalid",
        )
        with (
            patch("agent.tools.validate_query_constraints", return_value=retry),
            patch("agent.tools.load_local_file") as loader,
        ):
            with self.assertRaises(QueryConstraintRetrySignal) as captured:
                query_local_file(
                    query_key="q_file",
                    query_constraints={
                        "time_range": None,
                        "target_filters": [],
                        "aggregation": None,
                    },
                    file_path=r"D:\\data\\metrics.csv",
                )

        self.assertIs(captured.exception.retry_result, retry)
        loader.assert_not_called()

    def test_composed_local_file_keeps_validation_and_load_in_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "metrics.csv"
            file_path.write_text("category,metric_value\nA,12\n", encoding="utf-8")
            snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({})
            try:
                data = query_local_file(
                    query_key="q_file",
                    query_constraints={
                        "time_range": None,
                        "target_filters": [],
                        "aggregation": {"grain": "category", "metric": "metric_value"},
                    },
                    file_path=str(file_path),
                    aggregation_hints=["category", "metric_value", "sum"],
                )
                snapshots = CURRENT_QUERY_SNAPSHOTS.get()
            finally:
                CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)

        self.assertEqual(data.to_dict("records"), [{"category": "A", "metric_value": 12}])
        self.assertEqual(list(snapshots), ["q_file"])
        self.assertEqual(snapshots["q_file"]["execution"]["status"], "success")
        self.assertEqual(snapshots["q_file"]["implementation"]["loader"], "load_local_file")
        self.assertTrue(snapshots["q_file"]["implementation"]["physical_execution"])

    def test_static_time_guard_defers_dynamic_range_to_runtime_validation(self) -> None:
        code = '''
execution_intent = "analysis"
start = analysis_parameters["time_range"]["start"]
end = analysis_parameters["time_range"]["end"]
query_constraints = {
    "time_range": {"start": start, "end": end, "end_exclusive": True},
    "target_filters": [],
    "aggregation": {"grain": "day", "metric": "revenue"},
}
'''

        retry = _validate_explicit_time_range_guard(
            code,
            {"start": "2026-08-01", "end": "2026-08-08"},
            True,
        )

        self.assertIsNone(retry)

    def test_static_time_guard_rejects_proven_literal_drift(self) -> None:
        code = '''
execution_intent = "analysis"
query_constraints = {
    "time_range": {
        "start": "2026-07-01",
        "end": "2026-07-08",
        "end_exclusive": True,
    },
    "target_filters": [],
    "aggregation": {"grain": "day", "metric": "revenue"},
}
'''

        retry = _validate_explicit_time_range_guard(
            code,
            {"start": "2026-08-01", "end": "2026-08-08"},
            True,
        )

        self.assertIsNotNone(retry)

    def test_ranked_report_rows_are_proven_by_explicit_metric_sort(self) -> None:
        code = '''
ranked = summary.sort_values("revenue", ascending=False).reset_index(drop=True)
report = (
    f"营收以 {ranked.iloc[0]['product']} 为主，金额 {ranked.iloc[0]['revenue']}；"
    f"其次是 {ranked.iloc[1]['product']}，金额 {ranked.iloc[1]['revenue']}；"
    f"第三是 {ranked.iloc[2]['product']}，金额 {ranked.iloc[2]['revenue']}。"
)
'''
        tree = ast.parse(code)

        retry = _validate_report_extrema_selection_contract(tree, code)

        self.assertIsNone(retry)

    def test_report_table_consistency_does_not_merge_unit_qualified_labels(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "| 指标 | 数值 |\n"
                "| --- | ---: |\n"
                "| 销售额（亿元） | 1.2 |\n"
                "| 毛利（亿元） | 0.4 |"
            ),
            tables=[{
                "title": "原始金额",
                "columns": [
                    {"key": "sales", "label": "销售额（元）"},
                    {"key": "profit", "label": "毛利（元）"},
                ],
                "rows": [{"sales": 120000000.0, "profit": 40000000.0}],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNone(retry)

    def test_report_table_consistency_skips_explicit_non_percent_units(self) -> None:
        result = StructuredResult(
            analysis_report=(
                "| 指标 | 数值 |\n"
                "| --- | ---: |\n"
                "| 销售额 | 1.2 亿元 |\n"
                "| 毛利 | 0.4 亿元 |"
            ),
            tables=[{
                "title": "原始金额",
                "columns": [
                    {"key": "sales", "label": "销售额"},
                    {"key": "profit", "label": "毛利"},
                ],
                "rows": [{"sales": 120000000.0, "profit": 40000000.0}],
            }],
        )

        retry = _validate_structured_result_contract(result)

        self.assertIsNone(retry)

    def test_static_review_merges_missing_query_validation_with_artifact_shape(self) -> None:
        code = r'''
execution_intent = "analysis"
query_constraints = {"time_range": {"start": "2026-07-01", "end": "2026-08-01"}}
frame = load_data_with_sql(sql="SELECT pass_rate, oos_rate FROM metrics")
pass_rate = float(frame.iloc[0]["pass_rate"])
oos_rate = float(frame.iloc[0]["oos_rate"])
table_frame = pd.DataFrame({
    "指标": ["一次合格率", "OOS批次率"],
    "数值": [pass_rate, oos_rate],
})
table = build_table_result(title="指标汇总", data=table_frame)
result = save_analysis_result(
    analysis_report="## 结论\n\n质量指标存在差异。",
    charts=[],
    tables=[table],
)
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(
            retry.diagnostics["violated_contract"],
            "incomplete_analysis_contract",
        )

    def test_static_review_allows_dataframe_list_of_dict_metric_rows(self) -> None:
        code = r'''
execution_intent = "analysis"
query_constraints = {}
validate_query_constraints(query_constraints=query_constraints, sql="SELECT 1")
frame = load_data_with_sql(sql="SELECT pass_count, total_count FROM metrics")
pass_rate = frame.iloc[0]["pass_count"] / frame.iloc[0]["total_count"]
oos_rate = 1.0 - pass_rate
chart_frame = pd.DataFrame([
    {"指标": "一次合格率", "数值": pass_rate},
    {"指标": "OOS批次率", "数值": oos_rate},
])
chart = build_chart_result(
    data=chart_frame,
    chart_kind="bar",
    category_field="指标",
    value_field="数值",
)
result = save_analysis_result(
    analysis_report="## 结论\n\n质量指标存在差异。",
    charts=[chart],
    tables=[],
)
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNone(retry)

    def test_static_review_allows_table_dataframe_metric_rows(self) -> None:
        code = r'''
execution_intent = "analysis"
query_constraints = {}
validate_query_constraints(query_constraints=query_constraints, sql="SELECT 1")
frame = load_data_with_sql(sql="SELECT pass_count, total_count FROM metrics")
pass_rate = frame.iloc[0]["pass_count"] / frame.iloc[0]["total_count"]
oos_rate = 1.0 - pass_rate
table_frame = pd.DataFrame({
    "指标": ["一次合格率", "OOS批次率"],
    "数值": [pass_rate, oos_rate],
})
table = build_table_result(
    title="指标汇总",
    data=table_frame,
)
result = save_analysis_result(
    analysis_report="## 结论\n\n质量指标存在差异。",
    charts=[],
    tables=[table],
)
'''

        retry = _validate_generated_code_contract(code)

        self.assertIsNone(retry)

    def test_static_review_allows_independent_metric_columns(self) -> None:
        code = r'''
execution_intent = "analysis"
query_constraints = {}
validate_query_constraints(query_constraints=query_constraints, sql="SELECT 1")
frame = load_data_with_sql(sql="SELECT pass_rate, oos_rate FROM metrics")
pass_rate = float(frame.iloc[0]["pass_rate"])
oos_rate = float(frame.iloc[0]["oos_rate"])
summary = pd.DataFrame({
    "期间": ["2026-07"],
    "一次合格率(%)": [pass_rate * 100],
    "OOS批次率(%)": [oos_rate * 100],
})
chart = build_multi_metric_chart_result(
    data=summary,
    chart_kind="bar",
    category_field="期间",
    value_fields=["一次合格率(%)", "OOS批次率(%)"],
)
table = build_table_result(
    title="指标汇总",
    columns=[
        {"key": "一次合格率", "label": "一次合格率(%)"},
        {"key": "OOS批次率", "label": "OOS批次率(%)"},
    ],
    rows=[{"一次合格率": pass_rate * 100, "OOS批次率": oos_rate * 100}],
)
result = save_analysis_result(analysis_report="## 结论\n\n质量指标存在差异。", charts=[chart], tables=[table])
'''

        self.assertIsNone(_validate_generated_code_contract(code))



if __name__ == "__main__":
    unittest.main()
