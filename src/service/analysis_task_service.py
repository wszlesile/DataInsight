import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from model import InsightAnalysisTask, InsightNsTurn
from utils.datasource_utils import dump_json, safe_json_loads
from config import Config


ACTIVE_TASK_STATUSES = ("queued", "running")
CONVERSATION_BLOCKING_TASK_STATUSES = (
    "queued",
    "running",
    "waiting_clarification",
)
TERMINAL_TASK_STATUSES = ("success", "failed", "cancelled")


def _now() -> datetime:
    return datetime.now()


class AnalysisTaskService:
    """Database-backed analysis task lifecycle service."""

    def __init__(self, session: Session):
        self.session = session

    def create_task(
        self,
        *,
        username: str,
        namespace_id: Any,
        conversation_id: Any,
        turn_id: Any,
        task_type: str,
        request_payload: dict[str, Any],
    ) -> InsightAnalysisTask:
        task = InsightAnalysisTask(
            task_id=uuid.uuid4().hex,
            username=username or "anonymous",
            namespace_id=int(namespace_id or 0),
            conversation_id=int(conversation_id or 0),
            turn_id=int(turn_id or 0),
            task_type=task_type or "new_analysis",
            status="queued",
            request_json=dump_json(request_payload or {}),
            worker_id="",
            error_message="",
        )
        self.session.add(task)
        self.session.flush()
        return task

    def count_active_tasks(self, username: str) -> int:
        self.cleanup_inactive_tasks(username=username)
        return self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.status.in_(ACTIVE_TASK_STATUSES),
        ).count()

    def has_active_conversation_task(self, username: str, conversation_id: Any) -> bool:
        self.cleanup_inactive_tasks(username=username, conversation_id=conversation_id)
        return self.session.query(InsightAnalysisTask.id).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.conversation_id == int(conversation_id or 0),
            InsightAnalysisTask.status.in_(CONVERSATION_BLOCKING_TASK_STATUSES),
        ).first() is not None

    def get_running_turn(self, *, username: str, conversation_id: Any) -> dict[str, Any] | None:
        tasks = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.conversation_id == int(conversation_id or 0),
            InsightAnalysisTask.status.in_(CONVERSATION_BLOCKING_TASK_STATUSES),
        ).order_by(
            InsightAnalysisTask.created_at.desc(),
            InsightAnalysisTask.id.desc(),
        ).all()
        for task in tasks:
            if self._mark_inactive_if_needed(task):
                continue
            return self._serialize_task(task)
        return None

    def get_task(self, task_id: str, username: str | None = None) -> InsightAnalysisTask | None:
        query = self.session.query(InsightAnalysisTask).filter(InsightAnalysisTask.task_id == task_id)
        if username is not None:
            query = query.filter(InsightAnalysisTask.username == (username or "anonymous"))
        return query.first()

    def get_latest_task_for_turn(
        self,
        *,
        username: str,
        conversation_id: Any,
        turn_id: Any,
    ) -> InsightAnalysisTask | None:
        return self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.conversation_id == int(conversation_id or 0),
            InsightAnalysisTask.turn_id == int(turn_id or 0),
        ).order_by(
            InsightAnalysisTask.created_at.desc(),
            InsightAnalysisTask.id.desc(),
        ).first()

    def mark_running(self, task_id: str, worker_id: str = "") -> bool:
        now = _now()
        updated = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status == "queued",
        ).update({
            InsightAnalysisTask.status: "running",
            InsightAnalysisTask.worker_id: worker_id or "",
            InsightAnalysisTask.started_at: now,
            InsightAnalysisTask.heartbeat_at: now,
            InsightAnalysisTask.updated_at: now,
        }, synchronize_session=False)
        self.session.flush()
        return bool(updated)

    def cancel_task(self, task_id: str, username: str | None = None, reason: str = "") -> InsightAnalysisTask | None:
        query = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status.in_(CONVERSATION_BLOCKING_TASK_STATUSES),
        )
        if username is not None:
            query = query.filter(InsightAnalysisTask.username == (username or "anonymous"))
        task = query.first()
        if task is None:
            return None
        now = _now()
        task.status = "cancelled"
        task.error_message = reason or "用户已停止本轮分析。"
        task.finished_at = now
        task.updated_at = now
        task.heartbeat_at = now
        self.session.flush()
        return task

    def is_cancelled(self, task_id: str | None = None, task_pk: int | None = None) -> bool:
        query = self.session.query(InsightAnalysisTask.status)
        if task_pk:
            query = query.filter(InsightAnalysisTask.id == int(task_pk or 0))
        elif task_id:
            query = query.filter(InsightAnalysisTask.task_id == task_id)
        else:
            return False
        row = query.first()
        return bool(row and row[0] == "cancelled")

    def get_task_status(self, task_id: str) -> str:
        row = self.session.query(InsightAnalysisTask.status).filter(
            InsightAnalysisTask.task_id == task_id,
        ).first()
        return str(row[0] or '') if row else ''

    def mark_finished(self, task_id: str, *, status: str, error_message: str = "") -> bool:
        if status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"invalid terminal task status: {status}")
        now = _now()
        updated = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status == "running",
        ).update({
            InsightAnalysisTask.status: status,
            InsightAnalysisTask.error_message: error_message or "",
            InsightAnalysisTask.finished_at: now,
            InsightAnalysisTask.updated_at: now,
            InsightAnalysisTask.heartbeat_at: now,
        }, synchronize_session=False)
        self.session.flush()
        return bool(updated)

    def mark_dispatch_failed(self, task_id: str, error_message: str) -> bool:
        now = _now()
        updated = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status == 'queued',
        ).update({
            InsightAnalysisTask.status: 'failed',
            InsightAnalysisTask.error_message: error_message,
            InsightAnalysisTask.finished_at: now,
            InsightAnalysisTask.updated_at: now,
        }, synchronize_session='fetch')
        self.session.flush()
        return bool(updated)

    def mark_waiting_clarification(
        self,
        task_id: str,
        *,
        clarification: dict[str, Any],
        resume_context: dict[str, Any] | None = None,
    ) -> bool:
        task = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status == "running",
        ).with_for_update().first()
        if task is None:
            return False
        request_payload = safe_json_loads(task.request_json, {})
        if not isinstance(request_payload, dict):
            request_payload = {}
        request_payload["pending_clarification"] = dict(clarification)
        request_payload["clarification_resume_context"] = dict(resume_context or {})
        task.request_json = dump_json(request_payload)
        task.status = "waiting_clarification"
        task.worker_id = ""
        task.error_message = ""
        task.finished_at = None
        task.updated_at = _now()
        task.heartbeat_at = task.updated_at
        self.session.flush()
        return True

    def resume_from_clarification(
        self,
        task_id: str,
        *,
        clarification_id: str,
        answer_text: str,
        option_id: str = "",
        answers: list[dict[str, Any]] | None = None,
    ) -> tuple[InsightAnalysisTask | None, dict[str, Any]]:
        task = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.task_id == task_id,
            InsightAnalysisTask.status == "waiting_clarification",
        ).with_for_update().first()
        if task is None:
            return None, {}
        request_payload = safe_json_loads(task.request_json, {})
        pending = (
            request_payload.get("pending_clarification")
            if isinstance(request_payload, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or str(pending.get("clarification_id") or "")
            != str(clarification_id or "")
        ):
            return None, {}

        pending_items = [
            item for item in pending.get("items") or [] if isinstance(item, dict)
        ] or [pending]
        if answers is not None and (not isinstance(answers, list) or not answers or any(not isinstance(item, dict) for item in answers)):
            return None, {}
        submitted_items = list(answers or [])
        if answers is None:
            if len(pending_items) != 1:
                return None, {}
            submitted_items = [{
                "item_id": str(pending_items[0].get("item_id") or ""),
                "option_id": str(option_id or ""),
                "answer_text": str(answer_text or ""),
            }]
        submitted_by_id = {
            str(item.get("item_id") or ""): item for item in submitted_items
        }
        expected_ids = {str(item.get('item_id') or '') for item in pending_items}
        if len(submitted_by_id) != len(submitted_items) or set(submitted_by_id) != expected_ids:
            return None, {}
        resolved_answers: list[dict[str, Any]] = []
        for index, pending_item in enumerate(pending_items):
            item_id = str(pending_item.get("item_id") or "")
            submitted = submitted_by_id.get(item_id)
            if submitted is None:
                return None, {}
            submitted_option_id = str(submitted.get("option_id") or "")
            selected_option = next(
                (
                    item
                    for item in pending_item.get("options") or []
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == submitted_option_id
                ),
                None,
            )
            if submitted_option_id and selected_option is None:
                return None, {}
            normalized_answer = str(submitted.get("answer_text") or "").strip()
            if selected_option is not None and normalized_answer and normalized_answer not in {
                str(selected_option.get('label') or '').strip(),
                str(selected_option.get('value') or '').strip(),
            }:
                # Do not persist two conflicting answers for the same question.
                return None, {}
            if not normalized_answer and selected_option is not None:
                normalized_answer = str(
                    selected_option.get("label")
                    or selected_option.get("value")
                    or ""
                ).strip()
            if selected_option is None and not bool(pending_item.get("allow_free_text")):
                return None, {}
            if not normalized_answer:
                return None, {}
            resolved_answers.append({
                "clarification_id": str(clarification_id),
                "clarification_item_id": item_id or f"item_{index + 1}",
                "question": str(pending_item.get("question") or ""),
                "semantic_scope": (
                    dict(pending_item.get("semantic_scope") or {})
                    if isinstance(pending_item.get("semantic_scope"), dict)
                    else {}
                ),
                "option_id": submitted_option_id,
                "answer_text": normalized_answer,
                "resolved_value": (
                    selected_option.get("value")
                    if selected_option is not None
                    else normalized_answer
                ),
            })

        answer = resolved_answers[0] if len(resolved_answers) == 1 else {
            "clarification_id": str(clarification_id),
            "answer_text": "\n".join(
                f"{item['question']}：{item['answer_text']}" for item in resolved_answers
            ),
            "answers": resolved_answers,
            "target_subjects": [
                str((item.get("semantic_scope") or {}).get("subject") or "").strip()
                for item in resolved_answers
                if str((item.get("semantic_scope") or {}).get("subject") or "").strip()
            ],
        }
        history = request_payload.get("clarifications")
        if not isinstance(history, list):
            history = []
        history.extend(resolved_answers)
        request_payload["clarifications"] = history
        request_payload.pop("pending_clarification", None)
        task.request_json = dump_json(request_payload)
        task.status = "queued"
        task.worker_id = ""
        task.error_message = ""
        task.finished_at = None
        task.updated_at = _now()
        task.heartbeat_at = task.updated_at
        self.session.flush()
        return task, answer

    def heartbeat(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if task is None or task.status not in ACTIVE_TASK_STATUSES:
            return
        now = _now()
        task.heartbeat_at = now
        task.updated_at = now
        self.session.flush()

    def serialize_task(self, task: InsightAnalysisTask) -> dict[str, Any]:
        return self._serialize_task(task)

    def get_request_payload(self, task: InsightAnalysisTask) -> dict[str, Any]:
        payload = safe_json_loads(task.request_json, {})
        return payload if isinstance(payload, dict) else {}

    def cleanup_inactive_tasks(self, *, username: str, conversation_id: Any | None = None) -> None:
        query = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.status.in_(ACTIVE_TASK_STATUSES),
        )
        if conversation_id is not None:
            query = query.filter(InsightAnalysisTask.conversation_id == int(conversation_id or 0))
        for task in query.all():
            self._mark_inactive_if_needed(task)
        self.session.flush()

    def _mark_inactive_if_needed(self, task: InsightAnalysisTask) -> bool:
        if self._mark_if_turn_terminal(task):
            return True
        return self._mark_stale_if_needed(task)

    def _mark_if_turn_terminal(self, task: InsightAnalysisTask) -> bool:
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == task.turn_id,
            InsightNsTurn.conversation_id == task.conversation_id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None or turn.status not in ("success", "failed"):
            return False
        now = _now()
        task.status = "success" if turn.status == "success" else "failed"
        task.error_message = turn.error_message or task.error_message or ""
        task.finished_at = task.finished_at or turn.finished_at or now
        task.updated_at = now
        self.session.flush()
        return True

    def _mark_stale_if_needed(self, task: InsightAnalysisTask) -> bool:
        now = _now()
        if task.status == "queued":
            last_seen = task.updated_at or task.created_at or now
            stale_seconds = max(5, int(Config.ANALYSIS_TASK_QUEUED_STALE_SECONDS or 30))
        elif task.status == "running":
            last_seen = task.heartbeat_at or task.started_at or task.created_at or now
            stale_seconds = max(60, int(Config.ANALYSIS_TASK_RUNNING_STALE_SECONDS or 1800))
        else:
            return False

        if now - last_seen <= timedelta(seconds=stale_seconds):
            return False

        task.status = "failed"
        task.error_message = "分析任务长时间未更新，已自动标记为失败"
        task.finished_at = now
        task.updated_at = now
        self.session.flush()
        return True

    def _serialize_task(self, task: InsightAnalysisTask) -> dict[str, Any]:
        request_payload = self.get_request_payload(task)
        public_request = dict(request_payload)
        public_request.pop("clarification_resume_context", None)
        return {
            "id": task.id,
            "analysis_task_id": task.id,
            "task_id": task.task_id,
            "username": task.username,
            "namespace_id": task.namespace_id,
            "conversation_id": task.conversation_id,
            "turn_id": task.turn_id,
            "task_type": task.task_type,
            "status": task.status,
            "request": public_request,
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        }
