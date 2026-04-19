import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent import load_system_prompt  # noqa: E402
from agent.tools import (  # noqa: E402
    NoDataFoundError,
    _classify_execution_error,
    _extract_retryable_no_data_from_result,
    raise_no_data_error,
    save_empty_analysis_result,
)


class ExecutePythonNoDataRetryTestCase(unittest.TestCase):
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

    def test_legacy_empty_analysis_result_is_downgraded_to_no_data_feedback(self) -> None:
        empty_result = save_empty_analysis_result(
            title="分析结果",
            reason="按当前筛选条件过滤后无数据。",
            detail_lines=["时间范围：2026-04-01 ~ 2026-04-19"],
        )

        feedback = _extract_retryable_no_data_from_result(empty_result)

        self.assertIsNotNone(feedback)
        assert feedback is not None
        self.assertEqual(feedback["error_type"], "no_data_found")
        self.assertIn("按当前筛选条件过滤后无数据。", feedback["error_message"])
        self.assertIn("时间范围：2026-04-01 ~ 2026-04-19", feedback["error_message"])

    def test_system_prompt_mentions_no_data_retry_contract(self) -> None:
        load_system_prompt.cache_clear()
        prompt = load_system_prompt()

        self.assertIn("raise_no_data_error", prompt)
        self.assertIn("不要继续生成空图表", prompt)
        self.assertIn("返回给 `execute_python` 上层", prompt)


if __name__ == "__main__":
    unittest.main()
