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
    build_multi_metric_chart_result,
    build_chart_suite,
    compile_chart_document,
    normalize_chart_result_item,
)


class ChartDocumentUtilsTestCase(unittest.TestCase):
    def test_multi_metric_chart_accepts_labels_keyed_by_value_field(self) -> None:
        chart_item = build_multi_metric_chart_result(
            chart_kind="bar",
            data=[{"parameter": "temperature", "alarm": 2, "above": 1}],
            title="Parameter alarms",
            category_field="parameter",
            value_fields=["alarm", "above"],
            value_labels={"above": "Above limit", "alarm": "Alarm count"},
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(
            [item["name"] for item in normalized_item["chart_spec"]["series"]],
            ["Alarm count", "Above limit"],
        )

    def test_multi_metric_chart_rejects_unknown_label_mapping_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "not present in value_fields"):
            build_multi_metric_chart_result(
                data=[{"parameter": "temperature", "alarm": 2, "above": 1}],
                title="Parameter alarms",
                category_field="parameter",
                value_fields=["alarm", "above"],
                value_labels={"alarm": "Alarm count", "missing": "Missing"},
            )

    def test_multi_metric_line_supports_a_business_series_dimension(self) -> None:
        chart_item = build_multi_metric_chart_result(
            chart_kind="line",
            data=[
                {"month": "2026-01", "department": "Sales", "amount": 120, "sets": 8},
                {"month": "2026-01", "department": "Service", "amount": 98, "sets": 6},
                {"month": "2026-02", "department": "Sales", "amount": 131, "sets": 9},
                {"month": "2026-02", "department": "Service", "amount": 103, "sets": 7},
            ],
            title="Monthly performance",
            category_field="month",
            value_fields=["amount", "sets"],
            value_labels=["Amount", "Sets"],
            series_field="department",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]
        self.assertEqual(chart_spec["xAxis"]["data"], ["2026-01", "2026-02"])
        self.assertEqual(
            {item["name"] for item in chart_spec["series"]},
            {"Sales · Amount", "Sales · Sets", "Service · Amount", "Service · Sets"},
        )
        self.assertEqual(chart_spec["series"][0]["data"], [120.0, 131.0])

    def test_multi_metric_helper_degrades_to_single_metric_chart(self) -> None:
        chart_item = build_multi_metric_chart_result(
            chart_kind="line",
            data=[
                {"month": "2026-01", "department": "Sales", "amount": 120},
                {"month": "2026-02", "department": "Sales", "amount": 131},
            ],
            title="Monthly performance",
            category_field="month",
            value_fields=["amount"],
            series_field="department",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(normalized_item["chart_spec"]["xAxis"]["data"], ["2026-01", "2026-02"])
        self.assertEqual(normalized_item["chart_spec"]["series"][0]["data"], [120.0, 131.0])

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

    def test_build_chart_result_accepts_unambiguous_wide_series_data(self) -> None:
        chart_item = build_chart_result(
            chart_kind="line",
            data=[
                {"month": "2026-01", "Sales": 120, "Service": 98},
                {"month": "2026-02", "Sales": 131, "Service": None},
            ],
            title="Monthly performance",
            category_field="month",
            value_field="amount",
            series_field="department",
            sort_field="month",
            sort_order="asc",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]
        self.assertEqual(chart_spec["xAxis"]["data"], ["2026-01", "2026-02"])
        self.assertEqual(
            {item["name"]: item["data"] for item in chart_spec["series"]},
            {"Sales": [120.0, 131.0], "Service": [98.0, None]},
        )

    def test_build_chart_result_does_not_guess_mixed_wide_series_data(self) -> None:
        chart_item = build_chart_result(
            chart_kind="line",
            data=[{"month": "2026-01", "Sales": 120, "note": "estimated"}],
            title="Monthly performance",
            category_field="month",
            value_field="amount",
            series_field="department",
        )

        with self.assertRaisesRegex(ValueError, "field 'amount' is not present"):
            normalize_chart_result_item(chart_item)

    def test_bar_chart_rejects_duplicate_category_without_series(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"workshop": "WS1", "line": "L01", "ratio": 17.23},
                {"workshop": "WS2", "line": "L01", "ratio": 3.23},
            ],
            title="Labor Cost Ratio",
            category_field="line",
            value_field="ratio",
        )

        with self.assertRaisesRegex(ValueError, "chart_semantic_error.*duplicate plotting keys"):
            normalize_chart_result_item(chart_item)

    def test_bar_chart_accepts_duplicate_category_with_unique_series(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"workshop": "WS1", "line": "L01", "ratio": 17.23},
                {"workshop": "WS1", "line": "L02", "ratio": 3.53},
                {"workshop": "WS2", "line": "L01", "ratio": 3.23},
                {"workshop": "WS2", "line": "L02", "ratio": 2.19},
            ],
            title="Labor Cost Ratio",
            category_field="line",
            value_field="ratio",
            series_field="workshop",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]
        self.assertEqual(chart_spec["xAxis"]["data"], ["L01", "L02"])
        self.assertEqual(
            {item["name"]: item["data"] for item in chart_spec["series"]},
            {"WS1": [17.23, 3.53], "WS2": [3.23, 2.19]},
        )

    def test_chart_accepts_metric_categories_in_generic_value_column(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"指标": "上半年签约套数", "套数": 481},
                {"指标": "年度目标套数", "套数": 1130},
            ],
            title="签约套数与年度目标",
            category_field="指标",
            value_field="套数",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(
            normalized_item["chart_spec"]["xAxis"]["data"],
            ["年度目标套数", "上半年签约套数"],
        )
        self.assertEqual(
            normalized_item["chart_spec"]["series"][0]["data"],
            [1130.0, 481.0],
        )

    def test_line_chart_rejects_duplicate_category_and_series_pair(self) -> None:
        chart_item = build_chart_result(
            chart_kind="line",
            data=[
                {"day": "2026-06-01", "region": "East", "sales": 120},
                {"day": "2026-06-01", "region": "East", "sales": 131},
            ],
            title="Daily Sales",
            category_field="day",
            value_field="sales",
            series_field="region",
        )

        with self.assertRaisesRegex(ValueError, "chart_semantic_error.*series_field='region'"):
            normalize_chart_result_item(chart_item)

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

    def test_distribution_chart_rejects_id_as_value_field(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"name": "默认空间", "id": 1},
                {"name": "剩下ASD", "id": 2},
                {"name": "wsz", "id": 3},
            ],
            title="洞察空间创建情况",
            description="展示洞察空间创建时间分布",
            category_field="name",
            value_field="id",
            sort_field="id",
            sort_order="asc",
        )

        with self.assertRaisesRegex(ValueError, "chart_semantic_error"):
            normalize_chart_result_item(chart_item)

    def test_detail_record_chart_rejects_id_as_value_axis(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"name": "wsztest", "id": 5},
                {"name": "wsz", "id": 3},
            ],
            title="名称包含「wsz」的洞察空间",
            description="展示名称中包含「wsz」的 2 个洞察空间",
            category_field="name",
            value_field="id",
            value_precision=0,
        )

        with self.assertRaisesRegex(ValueError, "chart_semantic_error"):
            normalize_chart_result_item(chart_item)

    def test_distribution_chart_accepts_grouped_count_value_field(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"创建日期": "2026-05-19", "洞察空间数量": 1},
                {"创建日期": "2026-05-29", "洞察空间数量": 1},
                {"创建日期": "2026-06-12", "洞察空间数量": 1},
            ],
            title="洞察空间创建时间分布",
            description="按创建日期统计洞察空间数量",
            category_field="创建日期",
            value_field="洞察空间数量",
            sort_field="创建日期",
            sort_order="asc",
            label_mode="always",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(
            normalized_item["chart_spec"]["xAxis"]["data"],
            ["2026-05-19", "2026-05-29", "2026-06-12"],
        )
        self.assertEqual(normalized_item["chart_spec"]["series"][0]["data"], [1, 1, 1])

    def test_label_mode_value_alias_shows_series_labels(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[{"day": "2026-05-19", "count": 1}],
            title="Creation Count",
            category_field="day",
            value_field="count",
            label_mode="value",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertTrue(normalized_item["chart_spec"]["series"][0]["label"]["show"])

    def test_chart_values_default_to_two_decimal_places(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[{"company": "A", "ratio": 19.23808878416981}],
            title="Ratio",
            category_field="company",
            value_field="ratio",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(normalized_item["chart_spec"]["series"][0]["data"], [19.24])
        self.assertNotIn("value_precision", normalized_item["chart_spec"])

    def test_chart_values_honor_custom_value_precision(self) -> None:
        chart_item = build_chart_result(
            chart_kind="bar",
            data=[
                {"company": "A", "ratio": 19.23808878416981},
                {"company": "B", "ratio": 16.65710218501819},
                {"company": "C", "ratio": 11.386771794382815},
            ],
            title="Ratio",
            category_field="company",
            value_field="ratio",
            value_precision=1,
        )

        normalized_item = normalize_chart_result_item(chart_item)
        self.assertEqual(normalized_item["chart_spec"]["series"][0]["data"], [19.2, 16.7, 11.4])
        self.assertNotIn("value_precision", normalized_item["chart_spec"])

    def test_chart_rejects_invalid_value_precision(self) -> None:
        with self.assertRaisesRegex(ValueError, "value_precision must be an integer between 0 and 10"):
            build_chart_result(
                chart_kind="bar",
                data=[{"company": "A", "ratio": 19.2}],
                title="Ratio",
                category_field="company",
                value_field="ratio",
                value_precision=11,
            )

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

    def test_build_chart_result_compiles_boxplot_from_raw_values(self) -> None:
        chart_item = build_chart_result(
            chart_kind="boxplot",
            data=[
                {"metric": "转速[rpm]", "value": 1000},
                {"metric": "转速[rpm]", "value": 1200},
                {"metric": "转速[rpm]", "value": 1400},
                {"metric": "转速[rpm]", "value": 1600},
                {"metric": "转速[rpm]", "value": 1800},
                {"metric": "扭矩[Nm]", "value": 10},
                {"metric": "扭矩[Nm]", "value": 20},
                {"metric": "扭矩[Nm]", "value": 30},
                {"metric": "扭矩[Nm]", "value": 40},
                {"metric": "扭矩[Nm]", "value": 50},
            ],
            title="关键指标分布",
            description="按指标展示五数摘要",
            category_field="metric",
            value_field="value",
        )

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]

        self.assertEqual(chart_spec["series"][0]["type"], "boxplot")
        self.assertEqual(chart_spec["xAxis"]["data"], ["转速[rpm]", "扭矩[Nm]"])
        self.assertEqual(chart_spec["series"][0]["data"][0], [1000.0, 1200.0, 1400.0, 1600.0, 1800.0])
        self.assertTrue(chart_spec.get(BACKEND_LAYOUT_LOCK_KEY))

    def test_compile_boxplot_document_uses_precomputed_five_number_summary(self) -> None:
        chart_spec = compile_chart_document({
            "chart_kind": "boxplot",
            "title": "异常值分布",
            "dataset": {
                "columns": ["指标", "最小值", "Q1", "中位数", "Q3", "最大值"],
                "rows": [
                    {"指标": "转速[rpm]", "最小值": 1168, "Q1": 1423, "中位数": 1503, "Q3": 1612, "最大值": 2886},
                    {"指标": "扭矩[Nm]", "最小值": 3.8, "Q1": 33.2, "中位数": 40.1, "Q3": 46.8, "最大值": 76.6},
                ],
            },
            "encoding": {
                "category_field": "指标",
                "value_fields": ["最小值", "Q1", "中位数", "Q3", "最大值"],
            },
        })

        self.assertEqual(chart_spec["series"][0]["type"], "boxplot")
        self.assertEqual(chart_spec["xAxis"]["data"], ["转速[rpm]", "扭矩[Nm]"])
        self.assertEqual(chart_spec["series"][0]["data"][1], [3.8, 33.2, 40.1, 46.8, 76.6])

    def test_legacy_pyecharts_boxplot_spec_is_recompiled_to_helper_spec(self) -> None:
        chart_item = {
            "title": "Key Metric Boxplot",
            "chart_type": "echarts",
            "description": "Outlier check",
            "chart_spec": {
                "title": [{"text": "Key Metric Boxplot", "subtext": "Outlier check"}],
                "legend": [{"data": ["Value Distribution"], "top": 36}],
                "toolbox": {"show": True},
                "xAxis": [{"type": "category", "data": ["Speed", "Torque"]}],
                "yAxis": [{"type": "value"}],
                "series": [{
                    "type": "boxplot",
                    "name": "Value Distribution",
                    "data": [
                        [1168, 1423, 1503, 1612, 2886],
                        [3.8, 33.2, 40.1, 46.8, 76.6],
                    ],
                }],
            },
        }

        normalized_item = normalize_chart_result_item(chart_item)
        chart_spec = normalized_item["chart_spec"]

        self.assertTrue(chart_spec.get(BACKEND_LAYOUT_LOCK_KEY))
        self.assertEqual(chart_spec["series"][0]["type"], "boxplot")
        self.assertEqual(chart_spec["xAxis"]["data"], ["Speed", "Torque"])
        self.assertEqual(chart_spec["series"][0]["data"][0], [1168.0, 1423.0, 1503.0, 1612.0, 2886.0])
        self.assertFalse(chart_spec["legend"].get("show", True))
        self.assertNotIn("toolbox", chart_spec)
        title = chart_spec["title"][0] if isinstance(chart_spec.get("title"), list) else chart_spec.get("title")
        self.assertNotIn("subtext", title)

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
