import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.invoker import (  # noqa: E402
    MAX_ANALYSIS_AGENT_ROUNDS,
    _build_failure_reason_reply,
    _promote_assistant_message_to_report,
    _should_use_failure_reason_fallback,
    _should_use_report_only_fallback,
)
from service.conversation_context_service import ConversationRunContext  # noqa: E402


class _FakeService:
    def get_turn_executions(self, turn_id: int, started_at=None):  # noqa: ANN001
        return [
            SimpleNamespace(
                execution_status="failed",
                error_message="No module named 'pytz'",
            )
        ]


class AnalysisReportFallbackTestCase(unittest.TestCase):
    def test_last_round_visible_reply_can_be_promoted_to_report(self) -> None:
        self.assertTrue(
            _should_use_report_only_fallback(
                analysis_request_expected=True,
                round_index=MAX_ANALYSIS_AGENT_ROUNDS - 1,
                has_any_artifact=False,
                assistant_message="这次没有完成分析，但我已经定位到问题了。",
            )
        )
        self.assertEqual(
            _promote_assistant_message_to_report(
                "这次没有完成分析，但我已经定位到问题了。",
                "",
            ),
            "这次没有完成分析，但我已经定位到问题了。",
        )

    def test_failure_reason_fallback_requires_no_visible_reply(self) -> None:
        self.assertTrue(
            _should_use_failure_reason_fallback(
                analysis_request_expected=True,
                round_index=MAX_ANALYSIS_AGENT_ROUNDS - 1,
                has_any_artifact=False,
                assistant_message="",
            )
        )
        self.assertFalse(
            _should_use_failure_reason_fallback(
                analysis_request_expected=True,
                round_index=MAX_ANALYSIS_AGENT_ROUNDS - 1,
                has_any_artifact=False,
                assistant_message="还有最后回复",
            )
        )
        self.assertFalse(
            _should_use_failure_reason_fallback(
                analysis_request_expected=True,
                round_index=0,
                has_any_artifact=False,
                assistant_message="",
            )
        )

    def test_build_failure_reason_reply_uses_latest_execution_error(self) -> None:
        runtime = ConversationRunContext(
            conversation=SimpleNamespace(id=117, insight_namespace_id=18),
            turn=SimpleNamespace(id=191, started_at=None),
            active_datasource_snapshot={
                "selected_datasource_snapshot": [
                    {"datasource_name": "报警记录表"},
                    {"datasource_name": "报警工单表"},
                ]
            },
            is_rerun=False,
            history_turn_limit=None,
        )
        reply = _build_failure_reason_reply(
            service=_FakeService(),
            runtime=runtime,
            user_message="分析最近的汽车维修记录",
        )

        self.assertIn("这次没有顺利完成“分析最近的汽车维修记录”的分析。", reply)
        self.assertIn("No module named 'pytz'", reply)
        self.assertIn("报警记录表", reply)

    def test_existing_report_takes_priority(self) -> None:
        self.assertEqual(
            _promote_assistant_message_to_report("assistant reply", "existing report"),
            "existing report",
        )


if __name__ == "__main__":
    unittest.main()
