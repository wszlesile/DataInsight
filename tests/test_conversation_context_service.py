import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.conversation_context_service import _attach_chart_artifact_refs


class ConversationContextServiceTestCase(unittest.TestCase):
    def test_attach_chart_artifact_refs_backfills_chart_ids(self) -> None:
        charts = [
            {"title": "图表1", "chart_type": "echarts", "chart_spec": {"series": []}},
            {"title": "图表2", "chart_type": "echarts", "chart_spec": {"series": []}},
        ]
        artifacts = [
            {"id": 101, "artifact_type": "chart"},
            {"id": 102, "artifact_type": "chart"},
        ]

        merged = _attach_chart_artifact_refs(charts, artifacts)

        self.assertEqual(merged[0]["id"], 101)
        self.assertEqual(merged[1]["id"], 102)
        self.assertNotIn("chart_artifact_id", merged[0])
        self.assertNotIn("chart_artifact_id", merged[1])

    def test_attach_chart_artifact_refs_preserves_existing_chart_payload(self) -> None:
        charts = [
            {"title": "图表1", "description": "desc", "chart_type": "echarts", "chart_spec": {"series": [1]}},
        ]
        artifacts = [{"id": 201, "artifact_type": "chart"}]

        merged = _attach_chart_artifact_refs(charts, artifacts)

        self.assertEqual(merged[0]["title"], "图表1")
        self.assertEqual(merged[0]["description"], "desc")
        self.assertEqual(merged[0]["chart_spec"], {"series": [1]})
        self.assertEqual(merged[0]["id"], 201)


if __name__ == "__main__":
    unittest.main()
