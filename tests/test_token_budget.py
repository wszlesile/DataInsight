import sys
import unittest
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils.token_budget import (
    describe_budget_split,
    estimate_messages_tokens,
    shrink_messages_to_budget,
    take_tail_messages_within_budget,
)


class TokenBudgetTestCase(unittest.TestCase):
    def test_describe_budget_split_reserves_history_and_memory(self) -> None:
        budget = describe_budget_split(
            max_prompt_tokens=1000,
            fixed_tokens=200,
            history_ratio=0.5,
            min_history_tokens=200,
            min_memory_tokens=150,
        )

        self.assertEqual(budget["prompt_budget"], 1000)
        self.assertEqual(budget["remaining_tokens"], 800)
        self.assertGreaterEqual(budget["history_budget"], 200)
        self.assertGreaterEqual(budget["memory_budget"], 150)
        self.assertLessEqual(budget["history_budget"] + budget["memory_budget"], 800)

    def test_take_tail_messages_within_budget_keeps_latest_messages(self) -> None:
        messages = [
            HumanMessage("第一轮问题"),
            HumanMessage("第二轮问题"),
            HumanMessage("第三轮问题"),
        ]
        tight_budget = estimate_messages_tokens(messages[-1:])

        trimmed = take_tail_messages_within_budget(messages, tight_budget)

        self.assertEqual(len(trimmed), 1)
        self.assertEqual(trimmed[0].content, "第三轮问题")

    def test_shrink_messages_to_budget_preserves_system_head_and_latest_tail(self) -> None:
        messages = [
            SystemMessage("system context"),
            HumanMessage("历史问题一" * 20),
            HumanMessage("历史问题二" * 20),
            HumanMessage("当前问题"),
        ]
        target_budget = estimate_messages_tokens([messages[0], messages[-1]]) + 8

        trimmed = shrink_messages_to_budget(messages, target_budget)

        self.assertEqual(trimmed[0].content, "system context")
        self.assertEqual(trimmed[-1].content, "当前问题")
        self.assertLessEqual(estimate_messages_tokens(trimmed), target_budget)


if __name__ == "__main__":
    unittest.main()
