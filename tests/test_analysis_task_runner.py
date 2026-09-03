import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            patch("config.database.SessionLocal", self.session_factory),
        ):
            runner._run_task(task.task_id, task.id, request, 39, False)

        self.session.expire_all()
        saved_task = self.session.query(InsightAnalysisTask).filter_by(task_id=task.task_id).first()
        saved_turn = self.session.query(InsightNsTurn).filter_by(id=39).first()
        self.assertEqual("failed", saved_task.status)
        self.assertEqual("failed", saved_turn.status)
        self.assertIn("redis down", saved_turn.error_message)

    def test_clarification_event_suspends_task_and_turn_without_failure(self):
        self.session.add(InsightNsConversation(
            id=19,
            insight_namespace_id=7,
            title="test",
            status="active",
            summary_text="",
            active_datasource_snapshot="{}",
            last_turn_no=1,
            user_message="分析批次率",
            insight_result="",
        ))
        self.session.add(InsightNsTurn(
            id=39,
            conversation_id=19,
            turn_no=1,
            user_query="分析批次率",
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
            request_payload={"user_message": "分析批次率"},
        )
        self.session.commit()
        request = AgentRequest(
            username="alice",
            namespace_id="7",
            conversation_id="19",
            user_message="分析批次率",
        )
        queue = MagicMock()
        event = {
            "type": "clarification_required",
            "clarification": {
                "question": "批次月份按哪个时间归属？",
                "options": [],
                "allow_free_text": True,
            },
            "_resume_context": {"preparation": {"intent": {}}},
        }
        runner = AnalysisTaskRunner()
        with (
            patch(
                "service.analysis_task_runner.stream_existing_turn_events",
                return_value=iter([event]),
            ),
            patch("service.analysis_task_runner.get_redis_client"),
            patch(
                "service.analysis_task_runner.AnalysisStreamQueue",
                return_value=queue,
            ),
            patch("config.database.SessionLocal", self.session_factory),
        ):
            runner._run_task(task.task_id, task.id, request, 39, False)

        self.session.expire_all()
        saved_task = self.session.query(InsightAnalysisTask).filter_by(
            task_id=task.task_id
        ).first()
        saved_turn = self.session.query(InsightNsTurn).filter_by(id=39).first()
        self.assertEqual("waiting_clarification", saved_task.status)
        self.assertEqual("waiting_clarification", saved_turn.status)
        pushed_event = queue.push_event.call_args.args[1]
        self.assertEqual("clarification_required", pushed_event["type"])
        self.assertTrue(pushed_event["clarification"]["clarification_id"])
        self.assertNotIn("_resume_context", pushed_event)

    def test_clarification_stream_failure_keeps_durable_waiting_state(self):
        self.session.add(InsightNsConversation(
            id=20,
            insight_namespace_id=7,
            title="test",
            status="active",
            summary_text="",
            active_datasource_snapshot="{}",
            last_turn_no=1,
            user_message="分析批次率",
            insight_result="",
        ))
        self.session.add(InsightNsTurn(
            id=40,
            conversation_id=20,
            turn_no=1,
            user_query="分析批次率",
            selected_datasource_ids_json="[]",
            selected_datasource_snapshot_json="[]",
            final_answer="",
            status="running",
            error_message="",
        ))
        task = AnalysisTaskService(self.session).create_task(
            username="alice",
            namespace_id=7,
            conversation_id=20,
            turn_id=40,
            task_type="new_analysis",
            request_payload={"user_message": "分析批次率"},
        )
        self.session.commit()
        request = AgentRequest(
            username="alice",
            namespace_id="7",
            conversation_id="20",
            user_message="分析批次率",
        )
        queue = MagicMock()
        queue.push_event.side_effect = RuntimeError("redis write failed")
        runner = AnalysisTaskRunner()
        with (
            patch(
                "service.analysis_task_runner.stream_existing_turn_events",
                return_value=iter([{
                    "type": "clarification_required",
                    "clarification": {
                        "question": "批次月份按哪个时间归属？",
                        "options": [],
                        "allow_free_text": True,
                    },
                    "_resume_context": {"preparation": {"intent": {}}},
                }]),
            ),
            patch("service.analysis_task_runner.get_redis_client"),
            patch(
                "service.analysis_task_runner.AnalysisStreamQueue",
                return_value=queue,
            ),
            patch("config.database.SessionLocal", self.session_factory),
        ):
            runner._run_task(task.task_id, task.id, request, 40, False)

        self.session.expire_all()
        saved_task = self.session.query(InsightAnalysisTask).filter_by(
            task_id=task.task_id
        ).first()
        saved_turn = self.session.query(InsightNsTurn).filter_by(id=40).first()
        self.assertEqual("waiting_clarification", saved_task.status)
        self.assertEqual("waiting_clarification", saved_turn.status)


if __name__ == "__main__":
    unittest.main()
