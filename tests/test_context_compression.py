import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.conversation_context_service import ConversationContextService
from utils.context_compression import clip_text, summarize_turns_incrementally


class ContextCompressionUtilsTestCase(unittest.TestCase):
    def test_clip_text_preserves_head_and_tail(self) -> None:
        text = "abcdefghijklmnopqrstuvwxyz0123456789"

        clipped = clip_text(text, 28)

        self.assertTrue(clipped.startswith("abcdefgh"))
        self.assertTrue(clipped.endswith("9"))
        self.assertIn("...[omitted]...", clipped)

    def test_summarize_turns_incrementally_uses_custom_summarizer(self) -> None:
        summary = summarize_turns_incrementally(
            turn_payloads=[
                {"turn_no": 1, "question": "看整体趋势", "answer": "产量上涨"},
                {"turn_no": 2, "question": "看异常点", "answer": "晚班波动最大"},
            ],
            summarizer=lambda text: f"压缩后::{len(text)}",
            max_chars=100,
        )

        self.assertTrue(summary.startswith("压缩后::"))


class ConversationContextCompressionTestCase(unittest.TestCase):
    def test_build_execution_summary_item_applies_length_limits(self) -> None:
        service = ConversationContextService.__new__(ConversationContextService)
        execution = SimpleNamespace(
            id=1,
            turn_id=2,
            tool_call_id="tool-1",
            title="执行标题",
            description="执行描述",
            execution_status="success",
            analysis_report="A" * 1600,
            generated_code="print('x')" * 600,
            error_message="",
            execution_seconds=123,
            finished_at=None,
            result_payload_json='{"charts":[{}],"tables":[{}]}',
        )

        payload = service._build_execution_summary_item(execution, include_code=True)

        self.assertLessEqual(len(payload["analysis_report_preview"]), 800)
        self.assertLessEqual(len(payload["generated_code"]), 3000)
        self.assertEqual(payload["chart_count"], 1)
        self.assertEqual(payload["table_count"], 1)

    def test_build_artifact_summary_item_applies_length_limit(self) -> None:
        service = ConversationContextService.__new__(ConversationContextService)
        artifact = SimpleNamespace(
            id=1,
            turn_id=2,
            execution_id=3,
            artifact_type="chart",
            title="图表",
            summary_text="B" * 1200,
            sort_no=1,
            created_at=None,
            content_json='{"chart_type":"line","chart_spec":{"series":[1,2]}}',
        )

        payload = service._build_artifact_summary_item(artifact)

        self.assertLessEqual(len(payload["summary_text"]), 500)
        self.assertEqual(payload["chart_type"], "line")
        self.assertEqual(payload["chart_series_count"], 2)


if __name__ == "__main__":
    unittest.main()
