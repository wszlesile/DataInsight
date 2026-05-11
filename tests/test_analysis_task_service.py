import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import Base
from model import InsightAnalysisTask, InsightNsTurn
from service.analysis_task_service import AnalysisTaskService


class AnalysisTaskServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.session = session_factory()
        self.service = AnalysisTaskService(self.session)

    def tearDown(self) -> None:
        self.session.close()

    def test_create_task_defaults_to_queued_and_can_find_running_turn(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={"user_message": "hello"},
        )
        self.session.commit()

        running = self.service.get_running_turn(username="alice", conversation_id=19)

        self.assertEqual("queued", task.status)
        self.assertEqual(39, running["turn_id"])
        self.assertEqual(task.task_id, running["task_id"])

    def test_count_active_tasks_ignores_terminal_tasks(self):
        self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={},
        )
        finished = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=20,
            turn_id=40,
            task_type="new_analysis",
            request_payload={},
        )
        finished.status = "success"
        self.session.commit()

        self.assertEqual(1, self.service.count_active_tasks("alice"))

    def test_mark_task_running_and_finished_updates_status(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={},
        )
        self.session.commit()

        self.service.mark_running(task.task_id, worker_id="worker-1")
        self.service.mark_finished(task.task_id, status="failed", error_message="boom")
        self.session.commit()
        saved = self.session.query(InsightAnalysisTask).filter_by(task_id=task.task_id).first()

        self.assertEqual("failed", saved.status)
        self.assertEqual("boom", saved.error_message)
        self.assertIsNotNone(saved.started_at)
        self.assertIsNotNone(saved.finished_at)

    def test_active_count_marks_stale_queued_task_failed(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={},
        )
        task.created_at = datetime.now() - timedelta(seconds=600)
        self.session.commit()

        active_count = self.service.count_active_tasks("alice")
        self.session.commit()
        saved = self.session.query(InsightAnalysisTask).filter_by(task_id=task.task_id).first()

        self.assertEqual(0, active_count)
        self.assertEqual("failed", saved.status)

    def test_has_active_conversation_task_ignores_terminal_turn_task(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={},
        )
        self.session.add(InsightNsTurn(
            id=39,
            conversation_id=19,
            turn_no=1,
            user_query="hello",
            selected_datasource_ids_json="[]",
            selected_datasource_snapshot_json="[]",
            final_answer="failed",
            status="failed",
            error_message="redis down",
        ))
        self.session.commit()

        has_active = self.service.has_active_conversation_task("alice", 19)
        self.session.commit()
        saved = self.session.query(InsightAnalysisTask).filter_by(task_id=task.task_id).first()

        self.assertFalse(has_active)
        self.assertEqual("failed", saved.status)


if __name__ == "__main__":
    unittest.main()
