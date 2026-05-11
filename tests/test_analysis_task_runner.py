import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.invoker import AgentRequest
from config.database import Base
from model import InsightAnalysisTask, InsightNsConversation, InsightNsTurn
from service.analysis_task_runner import AnalysisTaskRunner
from service.analysis_task_service import AnalysisTaskService


class AnalysisTaskRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.session = self.session_factory()

    def tearDown(self) -> None:
        self.session.close()

    def test_redis_initialization_failure_marks_task_and_turn_failed(self):
        self.session.add(InsightNsConversation(
            id=19,
            insight_namespace_id=7,
            title="test",
            status="active",
            summary_text="",
            active_datasource_snapshot="{}",
            last_turn_no=1,
            user_message="hello",
            insight_result="",
        ))
        self.session.add(InsightNsTurn(
            id=39,
            conversation_id=19,
            turn_no=1,
            user_query="hello",
            selected_datasource_ids_json="[]",
            selected_datasource_snapshot_json="[]",
            final_answer="",
            status="running",
            error_message="",
        ))
        task = AnalysisTaskService(self.session).create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={"user_message": "hello"},
        )
        self.session.commit()

        request = AgentRequest(
            username="alice",
            namespace_id="7",
            conversation_id="19",
            user_message="hello",
        )
        runner = AnalysisTaskRunner()
        with (
            patch("service.analysis_task_runner.get_redis_client", side_effect=RuntimeError("redis down")),
            patch("service.analysis_task_runner.SessionLocal", self.session_factory),
        ):
            runner._run_task(task.task_id, request, 39, False)

        self.session.expire_all()
        saved_task = self.session.query(InsightAnalysisTask).filter_by(task_id=task.task_id).first()
        saved_turn = self.session.query(InsightNsTurn).filter_by(id=39).first()
        self.assertEqual("failed", saved_task.status)
        self.assertEqual("failed", saved_turn.status)
        self.assertIn("redis down", saved_turn.error_message)


if __name__ == "__main__":
    unittest.main()
