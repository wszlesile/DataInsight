import json
import socket
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any

from agent.invoker import AgentRequest, stream_existing_turn_events
from config import Config
from config.database import session_scope
from service.analysis_stream_queue import AnalysisStreamQueue
from service.analysis_task_service import AnalysisTaskService
from utils import logger
from utils.analysis_task_cancel import AnalysisTaskCancelled, CANCELLED_MESSAGE
from utils.redis_client import get_redis_client


class AnalysisTaskRunner:
    """In-process background runner that publishes agent stream events to Redis List."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=max(1, Config.ANALYSIS_TASK_MAX_WORKERS))
        self._worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._futures: dict[str, Future] = {}
        self._futures_lock = Lock()

    def submit(
        self,
        *,
        task_id: str,
        analysis_task_id: int | None = None,
        agent_request: AgentRequest,
        turn_id: int,
        is_rerun: bool = False,
    ) -> None:
        future = self._executor.submit(
            self._run_task,
            task_id,
            analysis_task_id,
            agent_request,
            int(turn_id),
            bool(is_rerun),
        )
        with self._futures_lock:
            self._futures[task_id] = future

    def cancel(self, task_id: str) -> bool:
        """Best-effort cancellation for tasks that are still waiting in this process."""
        with self._futures_lock:
            future = self._futures.get(task_id)
        if future is None:
            return False
        return future.cancel()

    def _run_task(
        self,
        task_id: str,
        analysis_task_id: int | None,
        agent_request: AgentRequest,
        turn_id: int,
        is_rerun: bool,
    ) -> None:
        queue: AnalysisStreamQueue | None = None
        final_status = "success"
        error_message = ""
        try:
            if not self._mark_task_running(task_id):
                return
            redis_client = get_redis_client()
            queue = AnalysisStreamQueue(
                redis_client,
                max_len=Config.STREAM_QUEUE_MAX_LEN,
                event_max_bytes=Config.STREAM_EVENT_MAX_BYTES,
                ttl_seconds=Config.STREAM_QUEUE_TTL_SECONDS,
            )
            for event in stream_existing_turn_events(
                agent_request,
                turn_id=turn_id,
                is_rerun=is_rerun,
                analysis_task_id=analysis_task_id,
            ):
                event.setdefault("task_id", task_id)
                if event.get("type") == "clarification_required":
                    clarification = (
                        dict(event.get("clarification") or {})
                        if isinstance(event.get("clarification"), dict)
                        else {}
                    )
                    clarification.setdefault("clarification_id", uuid.uuid4().hex)
                    resume_context = (
                        event.pop("_resume_context", {})
                        if isinstance(event.get("_resume_context"), dict)
                        else {}
                    )
                    event["clarification"] = clarification
                    if not self._mark_task_waiting_clarification(
                        task_id=task_id,
                        analysis_task_id=analysis_task_id,
                        agent_request=agent_request,
                        turn_id=turn_id,
                        clarification=clarification,
                        resume_context=resume_context,
                    ):
                        raise RuntimeError("analysis task could not enter waiting_clarification")
                    try:
                        queue.push_event(turn_id, event)
                    except Exception:
                        # The clarification request is already durable in the
                        # task and turn. A transient stream failure must not
                        # convert that suspended turn into a failed turn;
                        # reconnecting clients recover the request from DB.
                        logger.warning(
                            "发布澄清事件失败，等待客户端从任务快照恢复: task_id=%s turn_id=%s",
                            task_id,
                            turn_id,
                            exc_info=True,
                        )
                    final_status = "waiting_clarification"
                    break
                queue.push_event(turn_id, event)
                self._heartbeat_task(task_id)
                if self._is_task_cancelled(task_id):
                    raise AnalysisTaskCancelled(CANCELLED_MESSAGE)
                if event.get("type") == "error":
                    final_status = "failed"
                    error_message = str(event.get("message") or "")
                elif event.get("type") == "done":
                    final_status = "success"
            if final_status != "waiting_clarification":
                self._mark_task_finished(task_id, status=final_status, error_message=error_message)
        except AnalysisTaskCancelled:
            final_status = "cancelled"
            error_message = CANCELLED_MESSAGE
            if queue is not None:
                self._publish_cancelled(queue, turn_id, task_id)
            self._fail_turn_if_possible(
                agent_request,
                turn_id,
                error_message,
                preserve_existing_results=is_rerun,
            )
        except Exception as exc:
            final_status = "failed"
            error_message = str(exc)
            logger.error("后台分析任务执行失败: task_id=%s error=%s", task_id, exc, exc_info=True)
            if self._recover_successful_turn_if_possible(
                task_id=task_id,
                analysis_task_id=analysis_task_id,
                agent_request=agent_request,
                turn_id=turn_id,
                is_rerun=is_rerun,
            ):
                if queue is not None:
                    queue.push_event(turn_id, {
                        "type": "done",
                        "task_id": task_id,
                        "turn_id": turn_id,
                    })
                return
            if queue is not None:
                self._publish_error(queue, turn_id, task_id, error_message)
            self._fail_turn_if_possible(
                agent_request,
                turn_id,
                error_message,
                preserve_existing_results=is_rerun,
            )
            self._mark_task_finished(task_id, status="failed", error_message=error_message)
        finally:
            with self._futures_lock:
                self._futures.pop(task_id, None)
            if queue is not None:
                try:
                    queue.expire_turn(turn_id, Config.STREAM_FINISHED_TTL_SECONDS)
                except Exception:
                    logger.warning("设置流式队列 TTL 失败: turn_id=%s", turn_id, exc_info=True)

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

    def _publish_cancelled(self, queue: AnalysisStreamQueue, turn_id: int, task_id: str) -> None:
        queue.push_event(turn_id, {
            "type": "error",
            "task_id": task_id,
            "turn_id": turn_id,
            "stage": "cancelled",
            "level": "warning",
            "message": CANCELLED_MESSAGE,
        })

    def _mark_task_running(self, task_id: str) -> bool:
        with session_scope(expire_on_commit=False) as session:
            marked = AnalysisTaskService(session).mark_running(task_id, worker_id=self._worker_id)
            session.commit()
            return marked

    def _heartbeat_task(self, task_id: str) -> None:
        with session_scope(expire_on_commit=False) as session:
            AnalysisTaskService(session).heartbeat(task_id)
            session.commit()

    def _is_task_cancelled(self, task_id: str) -> bool:
        with session_scope(expire_on_commit=False) as session:
            return AnalysisTaskService(session).is_cancelled(task_id=task_id)

    def _mark_task_finished(self, task_id: str, *, status: str, error_message: str = "") -> None:
        with session_scope(expire_on_commit=False) as session:
            AnalysisTaskService(session).mark_finished(task_id, status=status, error_message=error_message)
            session.commit()

    def _mark_task_waiting_clarification(
        self,
        *,
        task_id: str,
        analysis_task_id: int | None,
        agent_request: AgentRequest,
        turn_id: int,
        clarification: dict[str, Any],
        resume_context: dict[str, Any],
    ) -> bool:
        from service.conversation_context_service import ConversationContextService

        with session_scope(expire_on_commit=False) as session:
            task_service = AnalysisTaskService(session)
            marked = task_service.mark_waiting_clarification(
                task_id,
                clarification=clarification,
                resume_context=resume_context,
            )
            if not marked:
                return False
            ConversationContextService(session).suspend_for_clarification(
                conversation_id=int(agent_request.conversation_id or 0),
                turn_id=int(turn_id or 0),
                clarification=clarification,
                analysis_task_id=analysis_task_id,
            )
            session.commit()
            return True

    def _fail_turn_if_possible(
        self,
        agent_request: AgentRequest,
        turn_id: int,
        error_message: str,
        preserve_existing_results: bool = False,
    ) -> None:
        try:
            from service.conversation_context_service import ConversationContextService

            with session_scope(expire_on_commit=False) as session:
                ConversationContextService(session).fail_run(
                    int(agent_request.conversation_id or 0),
                    int(turn_id or 0),
                    error_message,
                    preserve_existing_results=preserve_existing_results,
                )

        except Exception:
            logger.warning("标记分析轮次失败时出错: turn_id=%s", turn_id, exc_info=True)

    def _recover_successful_turn_if_possible(
        self,
        *,
        task_id: str,
        analysis_task_id: int | None,
        agent_request: AgentRequest,
        turn_id: int,
        is_rerun: bool,
    ) -> bool:
        def _operation(recovery_session):
            from service.conversation_context_service import ConversationContextService
            from model import InsightNsTurn

            service = ConversationContextService(recovery_session)
            turn = recovery_session.query(InsightNsTurn).filter(
                InsightNsTurn.id == int(turn_id or 0),
                InsightNsTurn.conversation_id == int(agent_request.conversation_id or 0),
                InsightNsTurn.is_deleted == 0,
            ).first()
            if turn is not None and turn.status == 'success' and (turn.final_answer or '').strip():
                AnalysisTaskService(recovery_session).mark_finished(task_id, status="success", error_message="")
                recovery_session.commit()
                return True

            latest_execution = service.get_latest_execution(
                int(turn_id or 0),
                analysis_task_id=analysis_task_id,
            )
            if latest_execution is None or latest_execution.execution_status != 'success':
                return False
            try:
                payload = json.loads(latest_execution.result_payload_json or '{}')
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            analysis_report = str(payload.get('analysis_report') or latest_execution.analysis_report or '').strip()
            charts = payload.get('charts') if isinstance(payload.get('charts'), list) else []
            tables = payload.get('tables') if isinstance(payload.get('tables'), list) else []
            if not analysis_report and not charts and not tables:
                return False

            service.complete_run(
                conversation_id=int(agent_request.conversation_id or 0),
                turn_id=int(turn_id or 0),
                assistant_message='',
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
                replace_existing_results=bool(is_rerun),
                analysis_task_id=analysis_task_id,
                evidence_enabled=bool(agent_request.evidence_enabled),
            )
            AnalysisTaskService(recovery_session).mark_finished(task_id, status="success", error_message="")
            recovery_session.commit()
            logger.info(
                "已从最新成功执行记录恢复分析轮次: task_id=%s turn_id=%s execution_id=%s",
                task_id,
                turn_id,
                latest_execution.id,
            )
            return True

        try:
            with session_scope(expire_on_commit=False) as session:
                return _operation(session)
        except Exception:
            logger.warning("从最新成功执行记录恢复分析轮次失败: task_id=%s turn_id=%s", task_id, turn_id, exc_info=True)
            return False


_runner: AnalysisTaskRunner | None = None


def get_analysis_task_runner() -> AnalysisTaskRunner:
    global _runner
    if _runner is None:
        _runner = AnalysisTaskRunner()
    return _runner


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
