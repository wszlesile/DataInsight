import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent import load_system_prompt  # noqa: E402
from agent.tools import (  # noqa: E402
    NoDataFoundError,
    CURRENT_EXECUTION_INTENT,
    CURRENT_QUERY_SNAPSHOTS,
    _augment_repair_instructions,
    _build_repair_instructions,
    _build_no_data_feedback,
    _classify_execution_error,
    _extract_error_signature,
    _inspect_zero_population_aggregate,
    raise_no_data_error,
    load_data_with_sql,
    save_analysis_result,
    save_empty_analysis_result,
)


class ExecutePythonNoDataRetryTestCase(unittest.TestCase):
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
        load_system_prompt.cache_clear()
        prompt = load_system_prompt()

        self.assertIn("raise_no_data_error", prompt)
        self.assertIn("不要继续生成空图表", prompt)
        self.assertIn("返回给 `execute_python` 上层", prompt)

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

    def test_auxiliary_child_query_cannot_mask_empty_root_population(self) -> None:
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
            with self.assertRaises(NoDataFoundError):
                save_analysis_result(
                    analysis_report="## 合同概览\n\n合同为 0，供应链有数据。",
                    charts=[],
                    tables=[],
                )
        finally:
            CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)
            CURRENT_EXECUTION_INTENT.reset(intent_token)


if __name__ == "__main__":
    unittest.main()
