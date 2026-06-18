import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.context_engineering_runtime import classify_analysis_context_mode  # noqa: E402
from agent.invoker import (  # noqa: E402
    _build_history_requirement_recovery_instruction,
    _build_possible_datasource_selection_hints,
)
from service.conversation_context_service import ConversationRunContext  # noqa: E402


class _FakeHistoryService:
    def __init__(self, rows):
        self.rows = rows

    def get_recent_prompt_messages(self, conversation_id, limit_messages=10, max_turn_no=None):  # noqa: ANN001
        return [
            row for row in self.rows
            if max_turn_no is None or int(getattr(row, "turn_no", 0) or 0) <= max_turn_no
        ][-limit_messages:]


class FollowupRecoveryTestCase(unittest.TestCase):
    def _runtime(self, *, turn_no: int = 12) -> ConversationRunContext:
        return ConversationRunContext(
            conversation=SimpleNamespace(id=1, insight_namespace_id=1),
            turn=SimpleNamespace(
                id=12,
                turn_no=turn_no,
                user_query="你看看我前面对话的需求。我不想再重复一遍",
                started_at=None,
            ),
            active_datasource_snapshot={
                "selected_datasource_ids": [3, 4],
                "selected_datasource_snapshot": [
                    {
                        "datasource_id": 3,
                        "datasource_name": "设备开实际停机情况",
                        "datasource_identifier": "public._actual_runtime",
                    },
                    {
                        "datasource_id": 4,
                        "datasource_name": "计划开机时间",
                        "datasource_identifier": "public._planned_runtime",
                    },
                ],
            },
        )

    def test_previous_dialogue_request_is_followup(self) -> None:
        self.assertEqual(
            classify_analysis_context_mode("你看看我前面对话的需求。我不想再重复一遍"),
            "followup",
        )

    def test_history_recovery_instruction_restores_analysis_and_correction(self) -> None:
        rows = [
            SimpleNamespace(
                role="user",
                turn_no=9,
                content="帮我计算5月份1号车间下，风机的平均时间稼动率，结合我表里的计划开机时间算",
            ),
            SimpleNamespace(role="assistant", turn_no=9, content="历史分析结论"),
            SimpleNamespace(
                role="user",
                turn_no=10,
                content="计划开机时间就在“计划开机时间”表里，你用设备名称去匹配一下啊",
            ),
            SimpleNamespace(role="assistant", turn_no=11, content="请重新说明需求"),
        ]

        instruction = _build_history_requirement_recovery_instruction(
            _FakeHistoryService(rows),
            self._runtime(turn_no=12),
            "你看看我前面对话的需求。我不想再重复一遍",
        )

        self.assertIn("最近一个明确分析需求来自第 9 轮", instruction)
        self.assertIn("最近的用户补充/纠错来自第 10 轮", instruction)
        self.assertIn("datasource_identifier=\"public._planned_runtime\"", instruction)
        self.assertIn("不要回答“没有历史对话上下文”", instruction)

    def test_datasource_selection_hint_reports_semantic_omission_without_sql_parsing(self) -> None:
        hints = _build_possible_datasource_selection_hints(
            self._runtime(),
            [
                SimpleNamespace(
                    used_datasource_ids_json="[3]",
                    error_message='Catalog Error: Table with name 计划开机时间 does not exist',
                    generated_code='SELECT * FROM public."计划开机时间"',
                    analysis_report="",
                )
            ],
        )

        self.assertEqual(len(hints), 1)
        self.assertIn("datasource_id=4", hints[0])
        self.assertIn("public._planned_runtime", hints[0])

    def test_datasource_selection_hint_keeps_formal_analysis_omission_after_probe_used_datasource(self) -> None:
        runtime = ConversationRunContext(
            conversation=SimpleNamespace(id=3, insight_namespace_id=2),
            turn=SimpleNamespace(
                id=26,
                turn_no=3,
                user_query="查询1号车间设备开机情况，以柱状图展示",
                started_at=None,
            ),
            active_datasource_snapshot={
                "selected_datasource_ids": [3, 4],
                "selected_datasource_snapshot": [
                    {
                        "datasource_id": 3,
                        "datasource_name": "设备开实际停机情况",
                        "datasource_identifier": "public._actual_runtime",
                    },
                    {
                        "datasource_id": 4,
                        "datasource_name": "计划开机时间",
                        "datasource_identifier": "public._planned_runtime",
                    },
                ],
            },
        )
        hints = _build_possible_datasource_selection_hints(
            runtime,
            [
                SimpleNamespace(
                    used_datasource_ids_json="[3, 4]",
                    generated_code='execution_intent = "probe"',
                    error_message="已完成计划开机表探测",
                    analysis_report="",
                    result_payload_json='{"retry_type":"probe_feedback"}',
                ),
                SimpleNamespace(
                    used_datasource_ids_json="[3]",
                    generated_code='execution_intent = "analysis"',
                    error_message="当前结果提到了未在 used_datasource_ids 中声明的数据源",
                    analysis_report="建议结合计划开机时间计算开机情况。",
                    result_payload_json=(
                        '{"diagnostics":{"untracked_datasource_mentions":['
                        '{"datasource_id":4,"datasource_name":"计划开机时间"}]}}'
                    ),
                ),
            ],
        )

        self.assertEqual(len(hints), 1)
        self.assertIn("datasource_id=4", hints[0])
        self.assertIn("计划/目标", hints[0])
        self.assertIn("加入 used_datasource_ids", hints[0])


if __name__ == "__main__":
    unittest.main()
