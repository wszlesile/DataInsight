import json
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.invoker import AgentRequest, stream_existing_turn_events
from config import Config
from config.database import SessionLocal
from service.analysis_stream_queue import AnalysisStreamQueue
from service.analysis_task_service import AnalysisTaskService
from utils import logger
from utils.redis_client import get_redis_client


class AnalysisTaskRunner:
    """In-process background runner that publishes agent stream events to Redis List."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=max(1, Config.ANALYSIS_TASK_MAX_WORKERS))
        self._worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    def submit(
        self,
        *,
        task_id: str,
        agent_request: AgentRequest,
        turn_id: int,
        is_rerun: bool = False,
    ) -> None:
        self._executor.submit(
            self._run_task,
            task_id,
            agent_request,
            int(turn_id),
            bool(is_rerun),
        )

    def _run_task(
        self,
        task_id: str,
        agent_request: AgentRequest,
        turn_id: int,
        is_rerun: bool,
    ) -> None:
        session = SessionLocal()
        task_service = AnalysisTaskService(session)
        queue: AnalysisStreamQueue | None = None
        final_status = "success"
        error_message = ""
        try:
            redis_client = get_redis_client()
            queue = AnalysisStreamQueue(
                redis_client,
                max_len=Config.STREAM_QUEUE_MAX_LEN,
                event_max_bytes=Config.STREAM_EVENT_MAX_BYTES,
                ttl_seconds=Config.STREAM_QUEUE_TTL_SECONDS,
            )
            task_service.mark_running(task_id, worker_id=self._worker_id)
            session.commit()
            for event in stream_existing_turn_events(
                agent_request,
                turn_id=turn_id,
                is_rerun=is_rerun,
            ):
                event.setdefault("task_id", task_id)
                queue.push_event(turn_id, event)
                task_service.heartbeat(task_id)
                session.commit()
                if event.get("type") == "error":
                    final_status = "failed"
                    error_message = str(event.get("message") or "")
                elif event.get("type") == "done":
                    final_status = "success"
            task_service.mark_finished(task_id, status=final_status, error_message=error_message)
            session.commit()
        except Exception as exc:
            session.rollback()
            final_status = "failed"
            error_message = str(exc)
            logger.error("后台分析任务执行失败: task_id=%s error=%s", task_id, exc, exc_info=True)
            if queue is not None:
                self._publish_error(queue, turn_id, task_id, error_message)
            self._fail_turn_if_possible(session, agent_request, turn_id, error_message)
            task_service.mark_finished(task_id, status="failed", error_message=error_message)
            session.commit()
        finally:
            if queue is not None:
                try:
                    queue.expire_turn(turn_id, Config.STREAM_FINISHED_TTL_SECONDS)
                except Exception:
                    logger.warning("设置流式队列 TTL 失败: turn_id=%s", turn_id, exc_info=True)
            session.close()

    def _publish_error(
        self,
        queue: AnalysisStreamQueue,
        turn_id: int,
        task_id: str,
        message: str,
    ) -> None:
        queue.push_event(turn_id, {
            "type": "error",
            "task_id": task_id,
            "turn_id": turn_id,
            "stage": "error",
            "level": "error",
            "message": message,
        })

    def _fail_turn_if_possible(
        self,
        session,
        agent_request: AgentRequest,
        turn_id: int,
        error_message: str,
    ) -> None:
        try:
            from service.conversation_context_service import ConversationContextService

            ConversationContextService(session).fail_run(
                int(agent_request.conversation_id or 0),
                int(turn_id or 0),
                error_message,
                preserve_existing_results=False,
            )
        except Exception:
            logger.warning("标记分析轮次失败时出错: turn_id=%s", turn_id, exc_info=True)


_runner: AnalysisTaskRunner | None = None


def get_analysis_task_runner() -> AnalysisTaskRunner:
    global _runner
    if _runner is None:
        _runner = AnalysisTaskRunner()
    return _runner


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
