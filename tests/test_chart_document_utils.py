import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils.chart_document_utils import (
    BACKEND_LAYOUT_LOCK_KEY,
    build_chart_result,
    build_chart_suite,
    compile_chart_document,
    normalize_chart_result_item,
)


class ChartDocumentUtilsTestCase(unittest.TestCase):
    def test_build_chart_result_compiles_multi_series_line(self) -> None:
        chart_item = build_chart_result(
            chart_kind="line",
            data=[
                {"day": "2026-04-01", "region": "East", "sales": 120},
                {"day": "2026-04-01", "region": "West", "sales": 98},
                {"day": "2026-04-02", "region": "East", "sales": 131},
                {"day": "2026-04-02", "region": "West", "sales": 103},
            ],
            title="Daily Sales Trend",
            description="Grouped by region",
            category_field="day",
            value_field="sales",
            series_field="region",
            sort_field="day",
            sort_order="asc",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(normalized_item["chart_type"], "echarts")
        self.assertEqual(normalized_item["chart_spec"]["xAxis"]["data"], ["2026-04-01", "2026-04-02"])
        self.assertEqual(len(normalized_item["chart_spec"]["series"]), 2)
        self.assertEqual({item["name"] for item in normalized_item["chart_spec"]["series"]}, {"East", "West"})

    def test_compile_chart_document_auto_uses_horizontal_bar_for_long_categories(self) -> None:
        chart_document = {
            "chart_kind": "bar",
            "title": "Alarm Count by Device",
            "dataset": {
                "columns": ["device_name", "alarm_count"],
                "rows": [
                    {"device_name": "Device-Name-With-Extremely-Long-Label-01", "alarm_count": 10},
                    {"device_name": "Device-Name-With-Extremely-Long-Label-02", "alarm_count": 8},
                    {"device_name": "Device-Name-With-Extremely-Long-Label-03", "alarm_count": 7},
                ],
            },
            "encoding": {
                "category_field": "device_name",
                "value_field": "alarm_count",
            },
            "presentation": {
                "orientation": "auto",
            },
        }

        chart_spec = compile_chart_document(chart_document)
        self.assertEqual(chart_spec["xAxis"]["type"], "value")
        self.assertEqual(chart_spec["yAxis"]["type"], "category")
        self.assertEqual(chart_spec["yAxis"]["data"][0], "Device-Name-With-Extremely-Long-Label-01")

    def test_normalize_chart_result_item_keeps_legacy_chart_spec_path(self) -> None:
        chart_item = {
            "title": "Legacy Chart",
            "chart_type": "echarts",
            "description": "Existing raw spec should still work",
            "chart_spec": {
                "xAxis": {"type": "category", "data": ["A", "B"]},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "data": [3, 5]}],
            },
        }

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(normalized_item["title"], "Legacy Chart")
        self.assertEqual(normalized_item["chart_spec"]["series"][0]["type"], "bar")

    def test_compile_pie_document_merges_tail(self) -> None:
        chart_item = build_chart_result(
            chart_kind="pie",
            data=[
                {"category": f"Type-{index}", "value": 100 - index}
                for index in range(1, 13)
            ],
            title="Alarm Distribution",
            category_field="category",
            value_field="value",
            top_n=6,
        )

        normalized_item = normalize_chart_result_item(chart_item)
        pie_data = normalized_item["chart_spec"]["series"][0]["data"]
        self.assertLessEqual(len(pie_data), 6)
        self.assertEqual(pie_data[-1]["name"], "其他")

    def test_legacy_pyecharts_line_spec_is_recompiled_to_backend_managed_spec(self) -> None:
        chart_item = {
            "title": "2026年至今每月单位生产成本总和趋势",
            "chart_type": "echarts",
            "description": "展示趋势。",
            "chart_spec": {
                "title": [{"text": "2026年至今每月单位生产成本总和趋势", "subtext": "所有公司汇总"}],
                "legend": [{"data": ["单位生产成本总和（元）"]}],
                "xAxis": [{"type": "category", "name": "年月", "data": ["2026-01", "2026-02", "2026-03"]}],
                "yAxis": [{"type": "value", "name": "单位生产成本总和（元）"}],
                "series": [{
                    "type": "line",
                    "name": "单位生产成本总和（元）",
                    "label": {"show": True},
                    "data": [["2026-01", 2903.7359], ["2026-02", 2539.5568], ["2026-03", 2712.6065]],
                }],
            },
        }

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]
        self.assertTrue(chart_spec.get(BACKEND_LAYOUT_LOCK_KEY))
        self.assertFalse(chart_spec["series"][0]["label"].get("show", True))
        self.assertFalse(chart_spec["legend"].get("show", True))
        self.assertEqual(chart_spec["tooltip"]["trigger"], "axis")

    def test_build_chart_suite_for_temporal_data_returns_line_and_bar(self) -> None:
        charts = build_chart_suite(
            data=[
                {"month": "2026-01", "cost": 2903.73},
                {"month": "2026-02", "cost": 2539.56},
                {"month": "2026-03", "cost": 2712.61},
            ],
            title="单位生产成本分析",
            description="按月汇总单位生产成本",
            category_field="month",
            value_field="cost",
        )

        self.assertEqual(len(charts), 2)
        chart_kinds = [chart["chart_document"]["chart_kind"] for chart in charts]
        self.assertEqual(chart_kinds, ["line", "bar"])

    def test_build_chart_suite_for_categorical_data_returns_bar_and_pie(self) -> None:
        charts = build_chart_suite(
            data=[
                {"company": "A公司", "amount": 120},
                {"company": "B公司", "amount": 98},
                {"company": "C公司", "amount": 87},
            ],
            title="公司销售额分析",
            description="按公司汇总销售额",
            category_field="company",
            value_field="amount",
        )

        self.assertEqual(len(charts), 2)
        chart_kinds = [chart["chart_document"]["chart_kind"] for chart in charts]
        self.assertEqual(chart_kinds, ["bar", "pie"])


if __name__ == "__main__":
    unittest.main()
