import json
from io import BytesIO
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.analysis_stream_queue import AnalysisStreamQueue
from service.analysis_task_runner import encode_sse, get_analysis_task_runner
from service.analysis_task_service import ACTIVE_TASK_STATUSES, AnalysisTaskService
from service.insight_ns_conversation_service import InsightNsConversationService
from service.conversation_context_service import ConversationContextService
from utils.llm_error_utils import get_user_facing_agent_error
from utils.redis_client import get_redis_client
from utils.response import Result
from config import Config


def create_insight_ns_conversation_controller() -> Blueprint:
    blueprint = Blueprint('insight_ns_conversation', __name__, url_prefix='/api/insight/conversations')
    controller = InsightNsConversationController(blueprint)

    blueprint.route('', methods=['POST'])(controller.create_conversation)
    blueprint.route('', methods=['GET'])(controller.list_conversations)
    blueprint.route('/<int:conversation_id>', methods=['PUT'])(controller.rename_conversation)
    blueprint.route('/<int:conversation_id>', methods=['DELETE'])(controller.delete_conversation)
    blueprint.route('/<int:conversation_id>/history', methods=['GET'])(controller.get_history)
    blueprint.route('/<int:conversation_id>/running-turn', methods=['GET'])(controller.get_running_turn)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>', methods=['GET'])(controller.get_turn_detail)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/stream', methods=['GET'])(controller.stream_turn_events)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/export/pdf', methods=['POST'])(controller.export_turn_pdf)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/rerun/stream', methods=['POST'])(controller.rerun_turn_stream)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/rerun/task', methods=['POST'])(controller.rerun_turn_task)
    return blueprint


class InsightNsConversationController(BaseController):
    """会话列表、历史、详情、导出与原轮重跑接口。"""

    def _get_username(self) -> str:
        user_context = get_current_user_context()
        return user_context.username if user_context else 'anonymous'

    def _build_request_runtime_context(self) -> tuple[str, str, dict[str, Any]]:
        user_context = get_current_user_context()
        database_conn_info = getattr(user_context, 'database_conn_info', None)
        return (
            getattr(user_context, 'token', '') or '',
            getattr(user_context, 'selected_llm_model_id', '') or '',
            {
                'host': getattr(database_conn_info, 'host', '') or '',
                'port': getattr(database_conn_info, 'port', '') or '',
                'user': getattr(database_conn_info, 'user', '') or '',
                'password': getattr(database_conn_info, 'password', '') or '',
                'lake_rds_database_name': getattr(database_conn_info, 'lake_rds_database_name', '') or '',
            } if database_conn_info else {},
        )

    def create_conversation(self):
        """在指定空间下创建一条新的空会话。"""
        data = self.get_json_data()
        namespace_id = data.get('namespace_id', 0)
        title = data.get('title', '')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversation = service.create_conversation(
                username=self._get_username(),
                namespace_id=namespace_id,
                title=title,
            )
            if conversation is None:
                return self.error_response('洞察空间不存在', 404)
            return jsonify(Result.success(data=conversation, message='会话已创建', code=201).to_dict()), 201
        finally:
            session.close()

    def list_conversations(self):
        """按空间查询会话列表。"""
        namespace_id = request.args.get('namespace_id', '0')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversations = service.list_conversations(
                username=self._get_username(),
                namespace_id=namespace_id,
            )
            return jsonify(Result.success(data=conversations).to_dict())
        finally:
            session.close()

    def rename_conversation(self, conversation_id: int):
        """重命名会话。"""
        data = self.get_json_data()
        title = data.get('title', '')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversation = service.rename_conversation(
                username=self._get_username(),
                conversation_id=conversation_id,
                title=title,
            )
            if conversation is None:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(data=conversation, message='会话标题已更新').to_dict())
        finally:
            session.close()

    def delete_conversation(self, conversation_id: int):
        """软删除会话及其会话级上下文数据。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            deleted = service.delete_conversation(
                username=self._get_username(),
                conversation_id=conversation_id,
            )
            if not deleted:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(message='会话已删除').to_dict())
        finally:
            session.close()

    def get_history(self, conversation_id: int):
        """返回主聊天区使用的轮次历史。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            history = service.get_conversation_history(
                username=self._get_username(),
                conversation_id=conversation_id,
            )
            if history is None:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(data=history).to_dict())
        finally:
            session.close()

    def get_turn_detail(self, conversation_id: int, turn_id: int):
        """返回单轮详情，包含消息、执行与产物。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            detail = service.get_turn_detail(
                username=self._get_username(),
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if detail is None:
                return self.error_response('轮次详情不存在', 404)
            return jsonify(Result.success(data=detail).to_dict())
        finally:
            session.close()

    def get_running_turn(self, conversation_id: int):
        """返回当前用户在指定会话下仍在排队或运行中的轮次。"""
        session = SessionLocal()
        try:
            running = AnalysisTaskService(session).get_running_turn(
                username=self._get_username(),
                conversation_id=conversation_id,
            )
            return jsonify(Result.success(data=running).to_dict())
        finally:
            session.close()

    def stream_turn_events(self, conversation_id: int, turn_id: int):
        """从 Redis List 临时队列消费指定轮次的流式事件并输出 SSE。"""
        username = self._get_username()
        session = SessionLocal()
        try:
            task = AnalysisTaskService(session).get_latest_task_for_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if task is None:
                return self.error_response('当前轮次没有可订阅的分析任务', 404)
        finally:
            session.close()

        def generate():
            try:
                redis_client = get_redis_client()
                queue = AnalysisStreamQueue(
                    redis_client,
                    max_len=Config.STREAM_QUEUE_MAX_LEN,
                    event_max_bytes=Config.STREAM_EVENT_MAX_BYTES,
                    ttl_seconds=Config.STREAM_QUEUE_TTL_SECONDS,
                )
            except Exception as exc:
                self._fail_turn_task(username, conversation_id, turn_id, str(exc))
                yield encode_sse({
                    'type': 'error',
                    'stage': 'error',
                    'level': 'error',
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'message': f'Redis 流式队列不可用: {get_user_facing_agent_error(exc)}',
                })
                return

            while True:
                event = queue.pop_event(turn_id, timeout_seconds=Config.STREAM_BLPOP_TIMEOUT_SECONDS)
                if event is None:
                    if self._is_turn_task_finished(username, conversation_id, turn_id):
                        return
                    yield ': heartbeat\n\n'
                    continue

                event.setdefault('conversation_id', conversation_id)
                event.setdefault('turn_id', turn_id)
                yield encode_sse(event)
                if event.get('type') in ('done', 'error'):
                    return

        return Response(generate(), mimetype='text/event-stream')

    def export_turn_pdf(self, conversation_id: int, turn_id: int):
        """导出单轮分析结果 PDF。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            export_result = service.export_turn_pdf(
                username=self._get_username(),
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if export_result is None:
                return self.error_response('轮次详情不存在', 404)
            pdf_bytes, filename = export_result
            return send_file(
                BytesIO(pdf_bytes),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename,
            )
        finally:
            session.close()

    def rerun_turn_stream(self, conversation_id: int, turn_id: int):
        """在同一轮次内流式重跑分析，不新增新轮次。"""
        from agent.invoker import stream_rerun_turn
        username = self._get_username()
        auth_token, selected_llm_model_id, database_conn_info = self._build_request_runtime_context()

        def generate():
            for event in stream_rerun_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
                auth_token=auth_token,
                selected_llm_model_id=selected_llm_model_id,
                database_conn_info=database_conn_info,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return Response(generate(), mimetype='text/event-stream')

    def rerun_turn_task(self, conversation_id: int, turn_id: int):
        """提交原轮次异步重跑任务。"""
        from agent.invoker import AgentRequest

        username = self._get_username()
        auth_token, selected_llm_model_id, database_conn_info = self._build_request_runtime_context()
        session = SessionLocal()
        try:
            try:
                get_redis_client().ping()
            except Exception as exc:
                return self.error_response(f'Redis 流式队列不可用: {get_user_facing_agent_error(exc)}')

            task_service = AnalysisTaskService(session)
            if task_service.count_active_tasks(username) >= Config.ANALYSIS_TASK_MAX_ACTIVE_PER_USER:
                return self.error_response('当前用户正在分析中的任务已达到上限，请等待已有任务完成后再提交', 429)
            if task_service.has_active_conversation_task(username, conversation_id):
                return self.error_response('当前会话已有分析任务正在执行，请等待完成后再刷新分析', 409)

            context_service = ConversationContextService(session)
            runtime = context_service.start_rerun(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if runtime is None:
                return self.error_response('轮次详情不存在', 404)

            agent_request = AgentRequest(
                username=username,
                namespace_id=str(runtime.conversation.insight_namespace_id),
                conversation_id=str(runtime.conversation.id),
                user_message=runtime.turn.user_query,
                auth_token=auth_token,
                selected_llm_model_id=selected_llm_model_id,
                database_conn_info=database_conn_info,
            )
            task = task_service.create_task(
                username=username,
                namespace_id=runtime.conversation.insight_namespace_id,
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                task_type='rerun',
                request_payload={
                    'namespace_id': runtime.conversation.insight_namespace_id,
                    'conversation_id': runtime.conversation.id,
                    'turn_id': runtime.turn.id,
                    'user_message': runtime.turn.user_query,
                },
            )
            session.commit()
            get_analysis_task_runner().submit(
                task_id=task.task_id,
                agent_request=agent_request,
                turn_id=runtime.turn.id,
                is_rerun=True,
            )
            return jsonify(Result.success(data=task_service.serialize_task(task), message='刷新分析任务已提交').to_dict())
        except Exception as exc:
            session.rollback()
            return self.error_response(f'提交刷新分析任务失败: {get_user_facing_agent_error(exc)}')
        finally:
            session.close()

    def _is_turn_task_finished(self, username: str, conversation_id: int, turn_id: int) -> bool:
        session = SessionLocal()
        try:
            task = AnalysisTaskService(session).get_latest_task_for_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            return task is None or task.status not in ACTIVE_TASK_STATUSES
        finally:
            session.close()

    def _fail_turn_task(self, username: str, conversation_id: int, turn_id: int, error_message: str) -> None:
        session = SessionLocal()
        try:
            task_service = AnalysisTaskService(session)
            task = task_service.get_latest_task_for_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if task is not None and task.status in ACTIVE_TASK_STATUSES:
                task_service.mark_finished(task.task_id, status='failed', error_message=error_message)
            ConversationContextService(session).fail_run(
                conversation_id,
                turn_id,
                error_message,
                preserve_existing_results=False,
            )
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
