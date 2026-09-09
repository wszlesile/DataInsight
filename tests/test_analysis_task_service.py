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
        task.updated_at = task.created_at
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

    def test_waiting_clarification_blocks_conversation_without_counting_worker(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={"user_message": "分析批次率"},
        )
        self.service.mark_running(task.task_id)
        marked = self.service.mark_waiting_clarification(
            task.task_id,
            clarification={
                "clarification_id": "clarify-1",
                "question": "批次月份按哪个时间归属？",
                "options": [],
                "allow_free_text": True,
            },
            resume_context={"preparation": {"intent": {}}},
        )
        self.session.commit()

        self.assertTrue(marked)
        self.assertEqual(0, self.service.count_active_tasks("alice"))
        self.assertTrue(self.service.has_active_conversation_task("alice", 19))
        running = self.service.get_running_turn(username="alice", conversation_id=19)
        self.assertEqual("waiting_clarification", running["status"])
        self.assertEqual(
            "clarify-1",
            running["request"]["pending_clarification"]["clarification_id"],
        )

    def test_resume_clarification_is_atomic_and_uses_selected_semantic_value(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=39,
            task_type="new_analysis",
            request_payload={"user_message": "分析批次率"},
        )
        self.service.mark_running(task.task_id)
        self.service.mark_waiting_clarification(
            task.task_id,
            clarification={
                "clarification_id": "clarify-1",
                "question": "批次月份按哪个时间归属？",
                "options": [{
                    "id": "batch_start",
                    "label": "按批次开始时间",
                    "value": "批次月份按批次开始时间归属",
                }],
                "allow_free_text": True,
            },
        )

        resumed, answer = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-1",
            answer_text="",
            option_id="batch_start",
        )
        second_resume, _ = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-1",
            answer_text="按批次开始时间",
            option_id="batch_start",
        )

        self.assertIsNotNone(resumed)
        self.assertEqual("queued", resumed.status)
        self.assertEqual("批次月份按批次开始时间归属", answer["resolved_value"])
        self.assertIsNone(second_resume)

    def test_batch_clarification_requires_every_answer_and_resumes_once(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=40,
            task_type="new_analysis",
            request_payload={"user_message": "分析质量指标"},
        )
        self.service.mark_running(task.task_id)
        self.service.mark_waiting_clarification(
            task.task_id,
            clarification={
                "clarification_id": "clarify-batch",
                "question": "请确认两项口径",
                "items": [
                    {
                        "item_id": "first_pass",
                        "question": "一次合格率如何定义？",
                        "options": [],
                        "allow_free_text": True,
                        "semantic_scope": {"subject": "一次合格率"},
                    },
                    {
                        "item_id": "oos_rate",
                        "question": "OOS率按记录还是批次？",
                        "options": [{
                            "id": "batch",
                            "label": "按批次",
                            "value": "OOS率按批次计算",
                        }],
                        "allow_free_text": True,
                        "semantic_scope": {"subject": "OOS批次率"},
                    },
                ],
            },
        )

        incomplete, _ = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-batch",
            answer_text="",
            answers=[{
                "item_id": "first_pass",
                "answer_text": "按批次，仅首检且全部首检记录合格",
            }],
        )
        resumed, answer = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-batch",
            answer_text="",
            answers=[
                {
                    "item_id": "first_pass",
                    "answer_text": "按批次，仅首检且全部首检记录合格",
                },
                {"item_id": "oos_rate", "option_id": "batch"},
            ],
        )

        self.assertIsNone(incomplete)
        self.assertIsNotNone(resumed)
        self.assertEqual("queued", resumed.status)
        self.assertEqual(["一次合格率", "OOS批次率"], answer["target_subjects"])
        self.assertEqual(2, len(answer["answers"]))
        self.assertEqual("OOS率按批次计算", answer["answers"][1]["resolved_value"])

    def test_option_only_clarification_rejects_free_text_and_unknown_option(self):
        task = self.service.create_task(
            username="alice",
            namespace_id=7,
            conversation_id=19,
            turn_id=41,
            task_type="new_analysis",
            request_payload={"user_message": "分析批次率"},
        )
        self.service.mark_running(task.task_id)
        self.service.mark_waiting_clarification(
            task.task_id,
            clarification={
                "clarification_id": "clarify-option-only",
                "question": "批次月份按哪个时间归属？",
                "options": [{
                    "id": "batch_start",
                    "label": "按批次开始时间",
                    "value": "批次月份按批次开始时间归属",
                }],
                "allow_free_text": False,
            },
        )

        free_text_resume, _ = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-option-only",
            answer_text="按其他时间",
        )
        unknown_option_resume, _ = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-option-only",
            answer_text="",
            option_id="unknown",
        )
        valid_resume, answer = self.service.resume_from_clarification(
            task.task_id,
            clarification_id="clarify-option-only",
            answer_text="",
            option_id="batch_start",
        )

        self.assertIsNone(free_text_resume)
        self.assertIsNone(unknown_option_resume)
        self.assertIsNotNone(valid_resume)
        self.assertEqual("批次月份按批次开始时间归属", answer["resolved_value"])


if __name__ == "__main__":
    unittest.main()
