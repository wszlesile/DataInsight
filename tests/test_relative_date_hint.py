import sys
import unittest
from datetime import datetime as real_datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import agent as agent_module


class _FixedDateTime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 4, 17, 12, 0, 0, tzinfo=tz or ZoneInfo('Asia/Shanghai'))


class RelativeDateHintTestCase(unittest.TestCase):
    def test_bare_past_month_defaults_to_current_year(self):
        with patch.object(agent_module, 'datetime', _FixedDateTime):
            message = agent_module._build_relative_date_hint_message(
                '统计一下北方工业2月份的平均单位生产成本'
            )

        self.assertIsNotNone(message)
        self.assertIn('2026 年', message.content)
        self.assertIn('2 月', message.content)

    def test_bare_future_month_defaults_to_previous_year(self):
        with patch.object(agent_module, 'datetime', _FixedDateTime):
            message = agent_module._build_relative_date_hint_message(
                '统计一下北方工业12月份的平均单位生产成本'
            )

        self.assertIsNotNone(message)
        self.assertIn('2025 年', message.content)
        self.assertIn('12 月', message.content)

    def test_explicit_year_is_not_overridden_by_bare_month_rule(self):
        with patch.object(agent_module, 'datetime', _FixedDateTime):
            message = agent_module._build_relative_date_hint_message(
                '统计一下北方工业2025年2月份的平均单位生产成本'
            )

        self.assertIsNone(message)


if __name__ == '__main__':
    unittest.main()
