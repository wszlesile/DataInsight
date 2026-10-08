import unittest

from service.report_presentation_service import normalize_report_presentation


class ReportPresentationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bindings = [
            {"datasource_id": 1, "datasource_name": "\u4ea7\u54c1\u751f\u4ea7\u6210\u672c_\u516c\u53f8\u4ea7\u7ebf"},
            {"datasource_id": 2, "datasource_name": "\u4ea7\u54c1\u6708\u5ea6\u5229\u6da6_\u516c\u53f8\u4ea7\u7ebf"},
            {"datasource_id": 18, "datasource_name": "\u4ea7\u54c1\u6708\u5ea6\u5229\u6da6_LARGE_1GB"},
        ]

    def test_replaces_explicit_and_compound_datasource_references(self) -> None:
        report = (
            "\u6570\u636e\u6e901\u4e0e2\u53e3\u5f84\u4e00\u81f4\uff0c\u6570\u636e\u6e901/2\u53ef\u8054\u5408\u5206\u6790\uff0c"
            "\u6570\u636e\u6e9018\u4f5c\u4e3a\u957f\u5468\u671f\u8865\u5145\u3002"
        )

        result = normalize_report_presentation(report, self.bindings)

        self.assertEqual(
            result,
            "\u4ea7\u54c1\u751f\u4ea7\u6210\u672c_\u516c\u53f8\u4ea7\u7ebf\u4e0e\u4ea7\u54c1\u6708\u5ea6\u5229\u6da6_\u516c\u53f8\u4ea7\u7ebf\u53e3\u5f84\u4e00\u81f4\uff0c"
            "\u4ea7\u54c1\u751f\u4ea7\u6210\u672c_\u516c\u53f8\u4ea7\u7ebf/\u4ea7\u54c1\u6708\u5ea6\u5229\u6da6_\u516c\u53f8\u4ea7\u7ebf\u53ef\u8054\u5408\u5206\u6790\uff0c"
            "\u4ea7\u54c1\u6708\u5ea6\u5229\u6da6_LARGE_1GB\u4f5c\u4e3a\u957f\u5468\u671f\u8865\u5145\u3002",
        )

    def test_preserves_unknown_datasource_references(self) -> None:
        result = normalize_report_presentation("\u6570\u636e\u6e901\u4e0e99\u5bf9\u6bd4", self.bindings)

        self.assertEqual(result, "\u4ea7\u54c1\u751f\u4ea7\u6210\u672c_\u516c\u53f8\u4ea7\u7ebf\u4e0e\u6570\u636e\u6e9099\u5bf9\u6bd4")

    def test_makes_numeric_ranges_safe_for_markdown(self) -> None:
        report = "\u65f6\u95f4\u8303\u56f4 2024-01~2026-06\uff0c\u5dee\u5f02\u7387 10 ~ 20%\u3002"

        result = normalize_report_presentation(report, self.bindings)

        self.assertEqual(result, "\u65f6\u95f4\u8303\u56f4 2024-01\uff5e2026-06\uff0c\u5dee\u5f02\u7387 10\uff5e20%\u3002")
        self.assertNotIn("~", result)

    def test_replaces_unsupported_markdown_image_artifacts_with_text(self) -> None:
        report = (
            "## \u53ef\u89c6\u5316\u56fe\u8868\n\n"
            "![\u8bbe\u5907\u7c7b\u578b\u5206\u5e03](chart:\u8bbe\u5907\u7c7b\u578b\u5206\u5e03)\n"
            "![\u73b0\u573a\u7167\u7247](https://example.invalid/device.png)\n"
            "![](chart:\u72b6\u6001\u56fe)"
        )

        result = normalize_report_presentation(report, self.bindings)

        self.assertEqual(
            result,
            "## \u53ef\u89c6\u5316\u56fe\u8868\n\n"
            "（\u56fe\u8868：\u8bbe\u5907\u7c7b\u578b\u5206\u5e03）\n"
            "\u73b0\u573a\u7167\u7247\n"
            "（\u56fe\u8868：\u72b6\u6001\u56fe）",
        )
        self.assertNotIn("![", result)

    def test_escapes_markup_from_unsupported_image_alt_text(self) -> None:
        result = normalize_report_presentation("![<img src=x onerror=alert(1)>](chart:unsafe)", self.bindings)

        self.assertEqual(result, "（\u56fe\u8868：&lt;img src=x onerror=alert(1)&gt;）")


if __name__ == "__main__":
    unittest.main()
