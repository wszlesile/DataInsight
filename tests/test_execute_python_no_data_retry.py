import json
import multiprocessing as mp
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

from agent.runtime_contract_registry import load_runtime_contract_text  # noqa: E402
from agent.tools import (  # noqa: E402
    NoDataFoundError,
    CURRENT_CONFIRMED_NO_DATA_RESULT,
    CURRENT_EXECUTION_INTENT,
    CURRENT_PROBE_QUERY_OBSERVATIONS,
    CURRENT_QUERY_SNAPSHOTS,
    _augment_repair_instructions,
    _build_query_capacity_failure_diagnostics,
    _build_implicit_probe_feedback,
    _build_repair_instructions,
    _build_no_data_feedback,
    _classify_execution_error,
    _extract_error_signature,
    _extract_confirmed_no_data_query_snapshots,
    _execute_generated_code_worker,
    ExePythonCodeInput,
    _inspect_zero_population_aggregate,
    _program_requests_probe_feedback,
    _program_uses_probe_api,
    _read_worker_result_payload,
    _remove_worker_result_payload,
    probe_analysis_data,
    probe_data_with_sql,
    ProbeAnalysisDataInput,
    submit_analysis_program,
    _tool_error_message,
    execute_python,
    raise_no_data_error,
    load_data_with_sql,
    save_analysis_result,
    save_empty_analysis_result,
)


class ExecutePythonNoDataRetryTestCase(unittest.TestCase):
    def test_observations_and_repairs_continue_in_the_same_agent_graph(self) -> None:
        self.assertFalse(probe_analysis_data.return_direct)
        self.assertFalse(submit_analysis_program.return_direct)

    def test_formal_submission_detects_probe_feedback_shape(self) -> None:
        self.assertTrue(_program_requests_probe_feedback(
            'result = request_retry(retry_type="probe_feedback", message="observe")'
        ))
        self.assertFalse(_program_requests_probe_feedback(
            'result = request_retry(retry_type="no_data_found", message="empty")'
        ))
        self.assertTrue(_program_uses_probe_api(
            'outcome = probe_data_with_sql("SELECT DISTINCT status FROM quality.batch")'
        ))
        self.assertFalse(_program_uses_probe_api(
            'frame = load_data_with_sql("SELECT status FROM quality.batch")'
        ))

    def test_formal_submission_rejects_probe_source_before_creating_execution(self) -> None:
        runtime = SimpleNamespace(
            context=SimpleNamespace(conversation_id=1, turn_id=2),
            tool_call_id='formal-probe-source',
        )
        with patch('agent.tools._raise_if_runtime_task_cancelled'), patch(
            'agent.tools._create_execution_record'
        ) as create_execution:
            result = execute_python.func(
                runtime=runtime,
                code='outcome = probe_data_with_sql("SELECT DISTINCT status FROM quality.batch")',
                execution_mode='analysis',
            )

        payload = json.loads(result.content)
        self.assertEqual(payload['error_type'], 'contract_error')
        self.assertEqual(
            payload['diagnostics']['violated_contract'],
            'formal_tool_received_probe_program',
        )
        create_execution.assert_not_called()

    def test_composed_query_validation_retry_returns_as_tool_result(self) -> None:
        queue: mp.Queue = mp.Queue()
        code = '''
execution_intent = "analysis"
query_constraints = {
    "time_range": None,
    "target_filters": [{"semantic_role": "primary_entity"}],
    "aggregation": None,
}
data = query_data_with_sql(
    query_key="q_invalid",
    query_constraints=query_constraints,
    sql="SELECT 1",
)
'''

        _execute_generated_code_worker(code, "tester", {}, queue)
        envelope = queue.get(timeout=2)
        result_path = envelope["result_file_path"]
        try:
            payload = _read_worker_result_payload(result_path)
        finally:
            _remove_worker_result_payload(result_path)
            queue.close()

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["exec_result"]["result_kind"], "retry")
        self.assertEqual(
            payload["exec_result"]["payload"]["retry_type"],
            "invalid_query_constraints",
        )

    def test_injected_runtime_helpers_are_also_available_via_runtime_module(self) -> None:
        queue: mp.Queue = mp.Queue()
        code = '''
from runtime import get_analysis_parameters
result = get_analysis_parameters({"window": "last_7_days"})
'''

        try:
            _execute_generated_code_worker(code, "tester", {}, queue)
            envelope = queue.get(timeout=2)
            result_path = envelope["result_file_path"]
            try:
                payload = _read_worker_result_payload(result_path)
            finally:
                _remove_worker_result_payload(result_path)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["exec_result"]["result_kind"], "raw")
        finally:
            queue.close()
            sys.modules.pop("runtime", None)

    def test_execution_tool_inputs_accept_gateway_serialized_datasource_id_arrays(self) -> None:
        self.assertEqual(
            ProbeAnalysisDataInput(code='pass', used_datasource_ids='[62, 76, 79]').used_datasource_ids,
            [62, 76, 79],
        )
        self.assertEqual(
            ProbeAnalysisDataInput(program='pass').code,
            'pass',
        )
        self.assertEqual(
            ProbeAnalysisDataInput(probe_code='pass').code,
            'pass',
        )
        probe_schema = ProbeAnalysisDataInput.model_json_schema()
        self.assertTrue(
            {'code', 'program', 'probe_code', 'probe'}
            <= set(probe_schema['properties'])
        )
        self.assertEqual(
            ExePythonCodeInput(code='pass', used_datasource_ids='[12]').used_datasource_ids,
            [12],
        )

    def test_probe_callable_normalizes_gateway_program_aliases(self) -> None:
        with patch('agent.tools.execute_python.func', return_value='ok') as execute:
            result = probe_analysis_data.func(
                runtime=SimpleNamespace(), probe_code='print(1)',
                used_datasource_ids='[62, 76]',
            )

        self.assertEqual(result, 'ok')
        self.assertEqual(execute.call_args.kwargs['code'], 'print(1)')
        self.assertEqual(execute.call_args.kwargs['used_datasource_ids'], [62, 76])

    def test_probe_helper_promotes_bounded_rows_to_structured_feedback(self) -> None:
        frame = pd.DataFrame({"status": ["open", "closed"], "record_count": [2, 1]})
        intent_token = CURRENT_EXECUTION_INTENT.set("probe")
        observation_token = CURRENT_PROBE_QUERY_OBSERVATIONS.set([])
        try:
            with patch("agent.tools.load_data_with_sql", return_value=frame):
                outcome = probe_data_with_sql(
                    "SELECT status, COUNT(*) AS record_count FROM quality.batch GROUP BY status"
                )
            retry = _build_implicit_probe_feedback(
                {}, "", CURRENT_PROBE_QUERY_OBSERVATIONS.get(),
            )
        finally:
            CURRENT_EXECUTION_INTENT.reset(intent_token)
            CURRENT_PROBE_QUERY_OBSERVATIONS.reset(observation_token)

        self.assertTrue(outcome.success)
        self.assertEqual(retry.diagnostics["query_observations"][0]["value_samples"], [
            {"status": "open", "record_count": 2},
            {"status": "closed", "record_count": 1},
        ])
        payload = json.loads(_tool_error_message(
            tool_call_id="probe", error_type="probe_feedback", execution_id=1,
            error_message="Probe complete", repair_instructions=[], diagnostics=retry.diagnostics,
        ).content)
        ledger = payload["diagnostics"]["probe_fact_ledger"]
        self.assertEqual(ledger["observations"][0]["value_samples"], [
            {"status": "open", "record_count": 2},
            {"status": "closed", "record_count": 1},
        ])

    def test_no_data_feedback_routes_the_next_attempt_to_an_independent_probe(self) -> None:
        feedback = _build_no_data_feedback("筛选后无数据")

        self.assertEqual(
            feedback["error_signature"]["recommended_next_execution_intent"],
            "probe",
        )
        self.assertTrue(any(
            'execution_intent = "probe"' in item
            for item in feedback["repair_instructions"]
        ))

    def test_no_data_feedback_identifies_the_exact_failed_query_scope(self) -> None:
        token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_contracts": {
                "query_constraints": {"target_filters": [{"field": "del_or_not", "value": "0"}]},
                "implementation": {
                    "physical_execution": True,
                    "sql": "SELECT status FROM dw.contracts WHERE del_or_not = '0'",
                },
                "execution": {
                    "status": "success",
                    "row_count": 0,
                    "columns": ["status"],
                },
            },
        })
        try:
            feedback = _build_no_data_feedback("筛选后无数据")
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(token)

        snapshots = feedback["diagnostics"]["executed_query_contexts"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["query_key"], "q_contracts")
        self.assertEqual(snapshots[0]["row_count"], 0)
        self.assertTrue(snapshots[0]["empty_observation"])
        self.assertEqual(
            snapshots[0]["applied_constraints"]["target_filters"],
            [{"field": "del_or_not", "value": "0"}],
        )
        self.assertIn("del_or_not = '0'", snapshots[0]["sql"])
        self.assertEqual(
            feedback["error_signature"]["probe_targets"],
            snapshots,
        )

    def test_raise_no_data_error_is_classified_as_retryable_failure(self) -> None:
        with self.assertRaises(NoDataFoundError) as ctx:
            raise_no_data_error(
                reason="按当前筛选条件过滤后无数据。",
                detail_lines=["时间范围：2026-04-01 ~ 2026-04-19"],
            )

        self.assertEqual(ctx.exception.reason, "按当前筛选条件过滤后无数据。")
        self.assertEqual(
            _classify_execution_error(ctx.exception, str(ctx.exception)),
            "no_data_found",
        )

    def test_system_prompt_mentions_no_data_retry_contract(self) -> None:
        prompt = load_runtime_contract_text(("execution.core",))

        self.assertIn("raise_no_data_error", prompt)
        self.assertIn("不能生成全零报告", prompt)
        self.assertIn("部分路径为空时保留成功路径", prompt)

    def test_mysql_cast_failure_gets_dialect_specific_repair(self) -> None:
        instructions = _augment_repair_instructions(
            "runtime_error",
            'Failed to run text protocol MySQL query "SELECT value::int"',
            ["修复对应代码"],
        )

        self.assertIn("MySQL 源查询", instructions[0])
        self.assertIn("CAST(expression AS SIGNED/DECIMAL)", instructions[0])

    def test_oversized_query_repair_preserves_full_analysis_scope(self) -> None:
        instructions = _build_repair_instructions("query_result_too_large")

        self.assertTrue(any("总体统计口径不变" in item for item in instructions))
        self.assertTrue(any("稳定排序键" in item and "LIMIT/OFFSET" in item for item in instructions))
        self.assertTrue(any("无业务依据的 LIMIT" in item for item in instructions))

    def test_timeout_repair_optimizes_cross_source_scope_without_dropping_it(self) -> None:
        instructions = _build_repair_instructions("execution_timeout")

        self.assertTrue(any("各来源分别完成过滤" in item for item in instructions))
        self.assertTrue(any("不得删除用户问题所需来源" in item for item in instructions))
        self.assertFalse(any("退化成单表分析" in item for item in instructions))

    def test_missing_constraints_repair_matches_composed_query_contract(self) -> None:
        instructions = _build_repair_instructions("missing_query_constraints")

        self.assertTrue(any("每条物理查询" in item for item in instructions))
        self.assertTrue(any("不要求存在固定顶层变量" in item for item in instructions))
        self.assertFalse(any("必须在顶部声明 query_constraints" in item for item in instructions))

    def test_repair_guidance_does_not_forbid_the_advertised_source_patch(self) -> None:
        for error_type in ("schema_or_column_mismatch", "runtime_error", "undefined_name"):
            instructions = _build_repair_instructions(error_type)
            self.assertTrue(any("repair_target.allowed_ranges" in item for item in instructions))
            self.assertFalse(any("不要只修改局部片段" in item for item in instructions))

    def test_oversized_query_has_a_stable_failure_memory_signature(self) -> None:
        signature = _extract_error_signature(
            "query_result_too_large",
            "Query result exceeds 10000 rows",
        )

        self.assertEqual(signature["signature_type"], "query_result_too_large")

    def test_analysis_zero_population_aggregate_enters_no_data_flow(self) -> None:
        token = CURRENT_EXECUTION_INTENT.set("analysis")
        try:
            diagnostics = _inspect_zero_population_aggregate(
                "SELECT COUNT(*) AS contract_count, "
                "COUNT(DISTINCT customer_id) AS customer_count FROM contracts "
                "WHERE status = 'active'",
                pd.DataFrame([{"contract_count": 0, "customer_count": 0}]),
            )
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)
        self.assertTrue(diagnostics["effective_no_data"])

    def test_probe_zero_population_aggregate_remains_observable(self) -> None:
        token = CURRENT_EXECUTION_INTENT.set("probe")
        try:
            diagnostics = _inspect_zero_population_aggregate(
                "SELECT COUNT(*) AS contract_count FROM contracts",
                pd.DataFrame([{"contract_count": 0}]),
            )
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)
        self.assertEqual(diagnostics, {})

    def test_time_series_no_results_is_persisted_as_no_data_query_fact(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("probe")
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({})
        try:
            with patch(
                "agent.tools._load_data_with_fed_query",
                side_effect=RuntimeError(
                    'Failed to fetch result for query "SELECT * FROM vxbase_query(...)": no results'
                ),
            ):
                with self.assertRaises(RuntimeError):
                    load_data_with_sql("SELECT * FROM ts_query('vxbase', 'SELECT * FROM source')")
            snapshots = CURRENT_QUERY_SNAPSHOTS.get()
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)

        self.assertEqual(len(snapshots), 1)
        query_execution = next(iter(snapshots.values()))["execution"]
        self.assertEqual(query_execution["status"], "no_data")
        self.assertEqual(query_execution["row_count"], 0)
        self.assertTrue(query_execution["effective_no_data"])

    def test_nonzero_population_does_not_enter_no_data_flow(self) -> None:
        token = CURRENT_EXECUTION_INTENT.set("analysis")
        try:
            diagnostics = _inspect_zero_population_aggregate(
                "SELECT COUNT(*) AS contract_count, "
                "COUNT(DISTINCT customer_id) AS customer_count FROM contracts",
                pd.DataFrame([{"contract_count": 12, "customer_count": 0}]),
            )
        finally:
            CURRENT_EXECUTION_INTENT.reset(token)
        self.assertEqual(diagnostics, {})

    def test_all_effectively_empty_queries_reject_ordinary_result(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_contracts": {
                "implementation": {"physical_execution": True},
                "execution": {
                    "status": "success",
                    "row_count": 1,
                    "effective_no_data": True,
                },
            },
        })
        try:
            with self.assertRaises(NoDataFoundError):
                save_analysis_result(
                    analysis_report="## 合同概览\n\n当前合同数为 0。",
                    tables=[{"title": "合同指标", "columns": [], "rows": []}],
                )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)

    def test_zero_population_loader_snapshot_rejects_ordinary_result(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({})
        try:
            with patch(
                "agent.tools._load_data_with_fed_query",
                return_value=pd.DataFrame([{"contract_count": 0}]),
            ):
                load_data_with_sql("SELECT COUNT(*) AS contract_count FROM contracts")
            with self.assertRaises(NoDataFoundError):
                save_analysis_result(
                    analysis_report="## 合同概览\n\n当前合同数为 0。",
                    charts=[],
                    tables=[],
                )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)

    def test_partial_data_queries_can_still_save_analysis(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_empty": {
                "implementation": {"physical_execution": True},
                "execution": {"status": "success", "row_count": 0},
            },
            "q_available": {
                "implementation": {"physical_execution": True},
                "execution": {"status": "success", "row_count": 8},
            },
        })
        try:
            result = save_analysis_result(
                analysis_report="## 可用数据分析\n\n其余数据源仍有 8 条记录可供分析。",
                tables=[],
            )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)
        self.assertIn("8 条记录", result.analysis_report)

    def test_positive_implicit_physical_query_preserves_partial_data_result(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_contracts": {
                "implementation": {
                    "physical_execution": True,
                    "sql": "SELECT COUNT(*) AS contract_count FROM dw.contracts",
                },
                "execution": {
                    "status": "success",
                    "row_count": 1,
                    "effective_no_data": True,
                },
            },
            "q_shipping": {
                "implementation": {
                    "physical_execution": True,
                    "sql": "SELECT status, COUNT(*) FROM dw.shipping GROUP BY status",
                    "scope_source_query_key": "q_contracts",
                },
                "execution": {"status": "success", "row_count": 5},
            },
        })
        try:
            result = save_analysis_result(
                analysis_report="## Partial result\n\nThe primary path is empty; another path has data.",
                charts=[],
                tables=[],
            )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)
        self.assertIn("another path has data", result.analysis_report)

    def test_confirmed_no_data_result_can_be_saved_after_probe_limit(self) -> None:
        intent_token = CURRENT_EXECUTION_INTENT.set("analysis")
        confirmed_token = CURRENT_CONFIRMED_NO_DATA_RESULT.set(True)
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_contracts": {
                "implementation": {"physical_execution": True},
                "execution": {"status": "success", "row_count": 0},
            },
        })
        try:
            result = save_analysis_result(
                analysis_report="## No data\n\nNo records matched the confirmed analysis scope.",
                charts=[],
                tables=[],
            )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_CONFIRMED_NO_DATA_RESULT.reset(confirmed_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)
        self.assertIn("No records matched", result.analysis_report)

    def test_confirmed_no_data_reuses_only_executed_empty_query_facts(self) -> None:
        previous = SimpleNamespace(
            id=42,
            result_payload_json=json.dumps({
                "query_snapshots": {
                    "q_empty": {
                        "query_key": "q_empty",
                        "query_scope": {"query_key": "q_empty"},
                        "implementation": {
                            "physical_execution": True,
                            "sql": "SELECT * FROM readings WHERE ts >= :start",
                        },
                        "execution": {"status": "no_data", "row_count": 0},
                    },
                    "q_available": {
                        "implementation": {"physical_execution": True},
                        "execution": {"status": "success", "row_count": 3},
                    },
                    "q_validated": {
                        "implementation": {"physical_execution": False},
                        "execution": {"status": "validated"},
                    },
                },
            }),
        )

        snapshots = _extract_confirmed_no_data_query_snapshots([previous])

        self.assertEqual(len(snapshots), 1)
        query_key, snapshot = next(iter(snapshots.items()))
        self.assertEqual(query_key, "confirmed_no_data_42_q_empty")
        self.assertEqual(snapshot["query_key"], query_key)
        self.assertEqual(snapshot["query_scope"]["query_key"], query_key)
        self.assertEqual(snapshot["execution"]["row_count"], 0)

    def test_query_capacity_diagnostics_identify_failed_physical_query(self) -> None:
        snapshot_token = CURRENT_QUERY_SNAPSHOTS.set({
            "q_summary": {
                "implementation": {
                    "physical_execution": True,
                    "sql": "SELECT status, COUNT(*) FROM contracts GROUP BY status",
                },
                "execution": {"status": "success", "row_count": 4},
            },
            "q_detail": {
                "implementation": {
                    "physical_execution": True,
                    "sql": "SELECT * FROM contracts",
                },
                "execution": {"status": "failed"},
            },
        })
        try:
            diagnostics = _build_query_capacity_failure_diagnostics()
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)

        self.assertEqual(
            diagnostics["query_execution_capability"]["max_rows_per_sql_result"],
            10000,
        )
        self.assertEqual(diagnostics["failed_query"]["query_key"], "q_detail")
        self.assertEqual(diagnostics["failed_query"]["sql"], "SELECT * FROM contracts")


if __name__ == "__main__":
    unittest.main()
