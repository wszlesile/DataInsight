import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import StructuredResult, _validate_generated_code_contract, _validate_structured_result_contract  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
