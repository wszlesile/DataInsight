import sys
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.tools import probe_distinct_values, probe_text_candidates


class NoDataProbeHelpersTestCase(unittest.TestCase):
    def test_probe_distinct_values_returns_top_counts(self) -> None:
        dataframe = pd.DataFrame({
            "产线名称": ["产线A", "产线A", "产线 B", "产线C", "产线 B"],
        })

        result = probe_distinct_values(dataframe, "产线名称", top_n=2)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["value"], "产线A")
        self.assertEqual(result[0]["count"], 2)
        self.assertEqual(result[1]["value"], "产线 B")
        self.assertEqual(result[1]["normalized_value"], "产线b")

    def test_probe_text_candidates_prefers_normalized_exact_match(self) -> None:
        dataframe = pd.DataFrame({
            "产线名称": ["产线 A", "产线B", "一车间", "二车间"],
        })

        result = probe_text_candidates(dataframe, "产线名称", "产线A", top_n=3)

        self.assertGreaterEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "产线 A")
        self.assertEqual(result[0]["match_type"], "exact")
        self.assertGreaterEqual(result[0]["similarity"], 0.99)


if __name__ == "__main__":
    unittest.main()
