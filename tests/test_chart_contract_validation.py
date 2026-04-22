import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import StructuredResult, _validate_structured_result_contract  # noqa: E402


class ChartContractValidationTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
