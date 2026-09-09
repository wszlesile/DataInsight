import json

from flask import Blueprint, Response, jsonify

from controller.base_controller import BaseController
from dto import get_current_user_context
from service.analysis_task_runner import get_analysis_task_runner
from service.analysis_stream_queue import AnalysisStreamQueue
from service.analysis_task_service import AnalysisTaskService
from service.conversation_context_service import ConversationContextService
from config import Config
from config.database import SessionLocal
from utils.llm_error_utils import get_user_facing_agent_error
from utils.analysis_task_cancel import CANCELLED_MESSAGE
from utils.redis_client import get_redis_client
from utils.response import Result


def create_agent_controller() -> Blueprint:
    blueprint = Blueprint('agent', __name__, url_prefix='/api/agent')
    controller = AgentController(blueprint)

    blueprint.route('/invoke', methods=['POST'])(controller.invoke)
    blueprint.route('/stream', methods=['POST'])(controller.stream_invoke)
    blueprint.route('/tasks', methods=['POST'])(controller.create_task)
    blueprint.route('/tasks/<task_id>', methods=['GET'])(controller.get_task)
    blueprint.route('/tasks/<task_id>/cancel', methods=['POST'])(controller.cancel_task)
    blueprint.route('/tasks/<task_id>/clarification', methods=['POST'])(
        controller.submit_clarification
    )
    return blueprint


def _get_username() -> str:
    user_context = get_current_user_context()
    return user_context.username if user_context else 'anonymous'


def _build_agent_request(data: dict):
    """
    构造标准化 Agent 请求对象。
    """
    from agent.invoker import AgentRequest

    user_context = get_current_user_context()
    database_conn_info = getattr(user_context, 'database_conn_info', None)
    return AgentRequest(
        # 用户名
        username=user_context.username if user_context else 'anonymous',
        # 命名空间 ID
        namespace_id=data.get('namespace_id', ''),
        # 会话 ID
        conversation_id=data.get('conversation_id', ''),
        # 用户消息
        user_message=(data.get('user_message') or '').strip(),
        platform_authorization=getattr(user_context, 'platform_authorization', '') or '',
        fedquery_authorization=getattr(user_context, 'fedquery_authorization', '') or '',
        selected_llm_model_id=getattr(user_context, 'selected_llm_model_id', '') or '',
        database_conn_info={
            'host': getattr(database_conn_info, 'host', '') or '',
            'port': getattr(database_conn_info, 'port', '') or '',
            'user': getattr(database_conn_info, 'user', '') or '',
            'password': getattr(database_conn_info, 'password', '') or '',
            'lake_rds_database_name': getattr(database_conn_info, 'lake_rds_database_name', '') or '',
            'ts_query_source': getattr(database_conn_info, 'ts_query_source', '') or '',
        } if database_conn_info else {},
        clarification_mode=str(data.get('clarification_mode') or 'same_turn'),
    )


class AgentController(BaseController):
    """负责接收 Agent 调用请求并下发同步或流式响应。"""

    def invoke(self):
        """处理一次同步分析请求。"""
        data = self.get_json_data()
        agent_request = _build_agent_request(data)
        if not agent_request.user_message:
            return self.error_response('user_message 不能为空')

        try:
            from agent.invoker import invoke_agent

            response = invoke_agent(agent_request)
            return jsonify(Result.success(data={
                'username': response.username,
                'message': response.message,
                'conversation_id': response.conversation_id,
                'turn_id': response.turn_id,
                'analysis_report': response.analysis_report,
                'charts': response.charts,
                'tables': response.tables,
                'chart_artifact_id': response.chart_artifact_id,
                'chart_artifact_ids': response.chart_artifact_ids or [],
            }).to_dict())
        except Exception as exc:
            return self.error_response(f'Agent 执行失败: {get_user_facing_agent_error(exc)}')

    def stream_invoke(self):
        """处理一次流式分析请求，并按 SSE 事件持续输出进度。"""
        data = self.get_json_data()
        agent_request = _build_agent_request(data)
        if not agent_request.user_message:
            return self.error_response('user_message 不能为空')

        def generate():
            try:
                from agent.invoker import stream_invoke_agent

                for event in stream_invoke_agent(agent_request):
                    event = {key: value for key, value in event.items() if key != '_resume_context'}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'stage': 'error', 'level': 'error', 'message': f'Agent 执行失败: {get_user_facing_agent_error(exc)}'}, ensure_ascii=False)}\n\n"

        return Response(generate(), mimetype='text/event-stream')

    def create_task(self):
        """提交一次异步分析任务，返回可订阅的轮次信息。"""
        data = self.get_json_data()
        agent_request = _build_agent_request(data)
        if not agent_request.user_message:
            return self.error_response('user_message 不能为空')

        session = SessionLocal()
        try:
            try:
                get_redis_client().ping()
            except Exception as exc:
                return self.error_response(f'Redis 流式队列不可用: {get_user_facing_agent_error(exc)}')

            task_service = AnalysisTaskService(session)
            if task_service.count_active_tasks(agent_request.username) >= Config.ANALYSIS_TASK_MAX_ACTIVE_PER_USER:
                return self.error_response('当前用户正在分析中的任务已达到上限，请等待已有任务完成后再提交', 429)
            if agent_request.conversation_id and task_service.has_active_conversation_task(
                agent_request.username,
                agent_request.conversation_id,
            ):
                return self.error_response('当前会话已有分析任务正在执行，请等待完成后再继续提问', 409)

            context_service = ConversationContextService(session)
            runtime = context_service.start_run(
                username=agent_request.username,
                namespace_id=agent_request.namespace_id,
                conversation_id=agent_request.conversation_id,
                user_message=agent_request.user_message,
            )
            agent_request.conversation_id = str(runtime.conversation.id)
            agent_request.namespace_id = str(runtime.conversation.insight_namespace_id)
            task = task_service.create_task(
                username=agent_request.username,
                namespace_id=runtime.conversation.insight_namespace_id,
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                task_type='new_analysis',
                request_payload={
                    'namespace_id': runtime.conversation.insight_namespace_id,
                    'conversation_id': runtime.conversation.id,
                    'turn_id': runtime.turn.id,
                    'user_message': agent_request.user_message,
                    'clarification_mode': agent_request.clarification_mode,
                },
            )
            session.commit()
            get_analysis_task_runner().submit(
                task_id=task.task_id,
                analysis_task_id=task.id,
                agent_request=agent_request,
                turn_id=runtime.turn.id,
                is_rerun=False,
            )
            return jsonify(Result.success(data=task_service.serialize_task(task), message='分析任务已提交').to_dict())
        except Exception as exc:
            session.rollback()
            return self.error_response(f'提交分析任务失败: {get_user_facing_agent_error(exc)}')
        finally:
            session.close()

    def get_task(self, task_id: str):
        session = SessionLocal()
        try:
            task_service = AnalysisTaskService(session)
            task = task_service.get_task(task_id, username=_get_username())
            if task is None:
                return self.error_response('分析任务不存在', 404)
            return jsonify(Result.success(data=task_service.serialize_task(task)).to_dict())
        finally:
            session.close()

    def cancel_task(self, task_id: str):
        username = _get_username()
        session = SessionLocal()
        try:
            task_service = AnalysisTaskService(session)
            task = task_service.cancel_task(task_id, username=username, reason=CANCELLED_MESSAGE)
            if task is None:
                existing_task = task_service.get_task(task_id, username=username)
                if existing_task is None:
                    return self.error_response('分析任务不存在', 404)
                return jsonify(Result.success(data=task_service.serialize_task(existing_task), message='分析任务已结束').to_dict())

            ConversationContextService(session).fail_run(
                int(task.conversation_id or 0),
                int(task.turn_id or 0),
                CANCELLED_MESSAGE,
                preserve_existing_results=(task.task_type == 'rerun'),
            )
            session.commit()
            get_analysis_task_runner().cancel(task.task_id)
            self._publish_cancelled_event(task)
            return jsonify(Result.success(data=task_service.serialize_task(task), message='分析任务已停止').to_dict())
        except Exception as exc:
            session.rollback()
            return self.error_response(f'停止分析任务失败: {get_user_facing_agent_error(exc)}')
        finally:
            session.close()

    def submit_clarification(self, task_id: str):
        """Resume a suspended analysis inside its existing turn."""
        username = _get_username()
        data = self.get_json_data()
        if not isinstance(data, dict):
            return self.error_response('请求体必须为 JSON 对象', 400), 400
        clarification_id = str(data.get("clarification_id") or "").strip()
        answer_text = str(data.get("answer_text") or "").strip()
        option_id = str(data.get("option_id") or "").strip()
        answers = data.get('answers')
        if answers is not None and (not isinstance(answers, list) or not answers or any(not isinstance(item, dict) for item in answers)):
            return self.error_response('answers 必须为非空回答对象数组', 400), 400
        if not clarification_id:
            return self.error_response("clarification_id 不能为空", 400), 400

        session = SessionLocal()
        try:
            task_service = AnalysisTaskService(session)
            task = task_service.get_task(task_id, username=username)
            if task is None:
                return self.error_response("分析任务不存在", 404), 404
            request_payload = task_service.get_request_payload(task)
            pending = request_payload.get("pending_clarification") or {}
            resumed_task, answer = task_service.resume_from_clarification(
                task_id,
                clarification_id=clarification_id,
                answer_text=answer_text,
                option_id=option_id,
                answers=answers,
            )
            if resumed_task is None:
                return self.error_response("澄清请求已失效或回答不匹配", 409), 409

            ConversationContextService(session).resume_from_clarification(
                conversation_id=int(task.conversation_id or 0),
                turn_id=int(task.turn_id or 0),
                answer=answer,
                analysis_task_id=task.id,
            )
            resume_context = request_payload.get("clarification_resume_context")
            if not isinstance(resume_context, dict):
                resume_context = {}
            agent_request = _build_agent_request({
                "namespace_id": task.namespace_id,
                "conversation_id": task.conversation_id,
                "user_message": str(request_payload.get("user_message") or ""),
                "clarification_mode": str(
                    request_payload.get("clarification_mode") or "same_turn"
                ),
            })
            clarification_history = [
                item
                for item in request_payload.get("clarifications") or []
                if isinstance(item, dict)
            ]
            answer_items = [
                item for item in answer.get("answers") or [] if isinstance(item, dict)
            ] or [answer]
            clarification_history.extend(answer_items)
            agent_request.clarification_context = {
                **resume_context,
                "request": pending,
                "answer_text": answer.get("answer_text"),
                "resolved_value": answer.get("resolved_value"),
                "user_clarifications": clarification_history,
                "target_subjects": [
                    str((item.get("semantic_scope") or {}).get("subject") or "").strip()
                    for item in answer_items
                    if str((item.get("semantic_scope") or {}).get("subject") or "").strip()
                ],
            }
            session.commit()
            try:
                queue = AnalysisStreamQueue(
                    get_redis_client(),
                    max_len=Config.STREAM_QUEUE_MAX_LEN,
                    event_max_bytes=Config.STREAM_EVENT_MAX_BYTES,
                    ttl_seconds=Config.STREAM_QUEUE_TTL_SECONDS,
                )
                queue.clear_turn(int(task.turn_id or 0))
            except Exception:
                pass
            try:
                get_analysis_task_runner().submit(
                    task_id=task.task_id,
                    analysis_task_id=task.id,
                    agent_request=agent_request,
                    turn_id=int(task.turn_id or 0),
                    is_rerun=(task.task_type == "rerun"),
                )
            except Exception:
                if task_service.mark_dispatch_failed(task.task_id, '恢复分析任务派发失败'):
                    ConversationContextService(session).fail_run(
                        int(task.conversation_id or 0), int(task.turn_id or 0), '恢复分析任务派发失败',
                        preserve_existing_results=(task.task_type == 'rerun'),
                    )
                session.commit()
                raise
            return jsonify(Result.success(
                data=task_service.serialize_task(resumed_task),
                message="已补充业务语义，继续分析",
            ).to_dict())
        except Exception as exc:
            session.rollback()
            return self.error_response(
                f"提交澄清回答失败: {get_user_facing_agent_error(exc)}"
            ), 500
        finally:
            session.close()

    def _publish_cancelled_event(self, task) -> None:
        try:
            queue = AnalysisStreamQueue(
                get_redis_client(),
                max_len=Config.STREAM_QUEUE_MAX_LEN,
                event_max_bytes=Config.STREAM_EVENT_MAX_BYTES,
                ttl_seconds=Config.STREAM_QUEUE_TTL_SECONDS,
            )
            queue.push_event(int(task.turn_id or 0), {
                'type': 'error',
                'task_id': task.task_id,
                'turn_id': int(task.turn_id or 0),
                'conversation_id': int(task.conversation_id or 0),
                'stage': 'cancelled',
                'level': 'warning',
                'message': CANCELLED_MESSAGE,
            })
        except Exception:
            pass
