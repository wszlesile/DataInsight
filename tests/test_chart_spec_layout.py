import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils.chart_export_utils import validate_chart_spec_layout
from utils.chart_spec_utils import finalize_chart_spec


class ChartSpecLayoutTestCase(unittest.TestCase):
    def test_finalize_adds_x_axis_datazoom_and_hides_dense_labels(self) -> None:
        raw_spec = {
            "title": {"text": "Monthly Revenue by Channel"},
            "legend": {"data": ["Revenue"]},
            "xAxis": {
                "type": "category",
                "data": [f"2026-01-{day:02d}" for day in range(1, 21)],
            },
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "bar",
                    "name": "Revenue",
                    "label": {"show": True},
                    "data": [100 + day * 7 for day in range(20)],
                }
            ],
        }

        final_spec = finalize_chart_spec(raw_spec)
        data_zoom = final_spec.get("dataZoom") or []
        self.assertTrue(any(item.get("xAxisIndex") == 0 for item in data_zoom if isinstance(item, dict)))

        axis_label = final_spec.get("xAxis", {}).get("axisLabel", {})
        self.assertGreaterEqual(axis_label.get("rotate", 0), 30)
        self.assertFalse(final_spec["series"][0]["label"].get("show", True))

    def test_finalize_adds_y_axis_datazoom_for_dense_horizontal_bar(self) -> None:
        raw_spec = {
            "title": {"text": "Alarm Count by Device"},
            "xAxis": {"type": "value"},
            "yAxis": {
                "type": "category",
                "data": [f"Very-Long-Device-Name-{idx:02d}" for idx in range(1, 16)],
            },
            "series": [
                {
                    "type": "bar",
                    "label": {"show": True},
                    "data": [idx * 3 for idx in range(1, 16)],
                }
            ],
        }

        final_spec = finalize_chart_spec(raw_spec)
        data_zoom = final_spec.get("dataZoom") or []
        self.assertTrue(any(item.get("yAxisIndex") == 0 for item in data_zoom if isinstance(item, dict)))
        axis_label = final_spec.get("yAxis", {}).get("axisLabel", {})
        self.assertGreaterEqual(axis_label.get("width", 0), 96)

    def test_finalize_merges_pie_tail_and_hides_dense_pie_labels(self) -> None:
        raw_spec = {
            "title": {"text": "Alarm Type Distribution"},
            "legend": {"data": [f"Type-{idx}" for idx in range(1, 13)]},
            "series": [
                {
                    "type": "pie",
                    "label": {"show": True},
                    "data": [
                        {"name": f"Type-{idx}", "value": 100 - idx}
                        for idx in range(1, 13)
                    ],
                }
            ],
        }

        final_spec = finalize_chart_spec(raw_spec)
        pie_series = final_spec["series"][0]
        self.assertLessEqual(len(pie_series["data"]), 8)
        self.assertFalse(pie_series["label"].get("show", True))

    def test_browser_validation_smoke_prefers_finalized_spec(self) -> None:
        raw_spec = {
            "title": {"text": "Daily Revenue by Region and Channel"},
            "legend": {"data": [f"Region-{idx}" for idx in range(1, 10)]},
            "xAxis": {
                "type": "category",
                "data": [f"2026-03-{day:02d}" for day in range(1, 25)],
            },
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": f"Region-{idx}",
                    "label": {"show": True},
                    "data": [100 + idx * day for day in range(1, 25)],
                }
                for idx in range(1, 6)
            ],
        }

        final_spec = finalize_chart_spec(raw_spec)
        raw_report = validate_chart_spec_layout(raw_spec)
        final_report = validate_chart_spec_layout(final_spec)

        if raw_report.get("skipped") or final_report.get("skipped"):
            self.skipTest("Playwright chart validation is not available in this environment.")

        raw_metrics = raw_report.get("metrics") or {}
        final_metrics = final_report.get("metrics") or {}
        self.assertLessEqual(
            int(final_metrics.get("overlap_count", 0)),
            int(raw_metrics.get("overlap_count", 0)),
        )
        self.assertLessEqual(
            int(final_metrics.get("overflow_count", 0)),
            int(raw_metrics.get("overflow_count", 0)),
        )


if __name__ == "__main__":
    unittest.main()
