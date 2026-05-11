import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from model import InsightAnalysisTask, InsightNsTurn
from utils.datasource_utils import dump_json, safe_json_loads
from config import Config


ACTIVE_TASK_STATUSES = ("queued", "running")
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
            InsightAnalysisTask.status.in_(ACTIVE_TASK_STATUSES),
        ).first() is not None

    def get_running_turn(self, *, username: str, conversation_id: Any) -> dict[str, Any] | None:
        tasks = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.username == (username or "anonymous"),
            InsightAnalysisTask.conversation_id == int(conversation_id or 0),
            InsightAnalysisTask.status.in_(ACTIVE_TASK_STATUSES),
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

    def mark_running(self, task_id: str, worker_id: str = "") -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        now = _now()
        task.status = "running"
        task.worker_id = worker_id or ""
        task.started_at = task.started_at or now
        task.heartbeat_at = now
        task.updated_at = now
        self.session.flush()

    def mark_finished(self, task_id: str, *, status: str, error_message: str = "") -> None:
        if status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"invalid terminal task status: {status}")
        task = self.get_task(task_id)
        if task is None:
            return
        now = _now()
        task.status = status
        task.error_message = error_message or ""
        task.finished_at = now
        task.updated_at = now
        task.heartbeat_at = now
        self.session.flush()

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
            last_seen = task.created_at or now
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
        return {
            "task_id": task.task_id,
            "username": task.username,
            "namespace_id": task.namespace_id,
            "conversation_id": task.conversation_id,
            "turn_id": task.turn_id,
            "task_type": task.task_type,
            "status": task.status,
            "request": safe_json_loads(task.request_json, {}),
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        }
