import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from service.llm_model_service import LlmModelSelectionService  # noqa: E402


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.added = []
        self.committed = False

    def query(self, _model):
        return FakeQuery(self.rows)

    def add(self, entity):
        self.added.append(entity)

    def commit(self):
        self.committed = True

    def refresh(self, _entity):
        pass


class LlmModelSelectionServiceTestCase(unittest.TestCase):
    def test_models_select_saved_user_model(self) -> None:
        session = FakeSession([SimpleNamespace(model_id="model-b", provider="supos_llm_gateway")])
        service = LlmModelSelectionService(session)
        models = [
            {"id": "model-a", "object": "model"},
            {"id": "model-b", "object": "model"},
        ]

        result = service.build_selectable_models("alice", models)

        self.assertFalse(result[0]["selected"])
        self.assertTrue(result[1]["selected"])

    def test_models_default_to_first_when_user_never_selected(self) -> None:
        service = LlmModelSelectionService(FakeSession())
        models = [
            {"id": "model-a", "object": "model"},
            {"id": "model-b", "object": "model"},
        ]

        result = service.build_selectable_models("alice", models)

        self.assertTrue(result[0]["selected"])
        self.assertFalse(result[1]["selected"])

    def test_upsert_user_selection_creates_record(self) -> None:
        session = FakeSession()
        service = LlmModelSelectionService(session)

        selection = service.upsert_user_selection("alice", "model-b")

        self.assertEqual(selection["username"], "alice")
        self.assertEqual(selection["model_id"], "model-b")
        self.assertTrue(session.committed)
        self.assertEqual(len(session.added), 1)


if __name__ == "__main__":
    unittest.main()
