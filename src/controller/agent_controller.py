import json

from flask import Blueprint, Response, jsonify

from controller.base_controller import BaseController
from dto import get_current_user_context
from utils.response import Result


def create_agent_controller() -> Blueprint:
    blueprint = Blueprint('agent', __name__, url_prefix='/api/agent')
    controller = AgentController(blueprint)

    blueprint.route('/invoke', methods=['POST'])(controller.invoke)
    blueprint.route('/stream', methods=['POST'])(controller.stream_invoke)
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
    database_context = getattr(user_context, 'database_context', None)
    return AgentRequest(
        # 用户名
        username=user_context.username if user_context else 'anonymous',
        # 命名空间 ID
        namespace_id=data.get('namespace_id', ''),
        # 会话 ID
        conversation_id=data.get('conversation_id', ''),
        # 用户消息
        user_message=(data.get('user_message') or '').strip(),
        auth_token=getattr(user_context, 'token', '') or '',
        database_context={
            'host': getattr(database_context, 'host', '') or '',
            'port': getattr(database_context, 'port', '') or '',
            'user': getattr(database_context, 'user', '') or '',
            'password': getattr(database_context, 'password', '') or '',
            'lake_rds_database_name': getattr(database_context, 'lake_rds_database_name', '') or '',
        } if database_context else {},
    )


class AgentController(BaseController):
    """负责接收 Agent 调用请求并下发同步或流式响应。"""

    def invoke(self):
        """处理一次同步分析请求。"""
        data = self.get_json_data()
        agent_request = _build_agent_request(data)
        if not agent_request.user_message:
            return self.error_response('user_message 不能为空')

        from agent.invoker import invoke_agent

        try:
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
            return self.error_response(f'Agent 执行失败: {str(exc)}')

    def stream_invoke(self):
        """处理一次流式分析请求，并按 SSE 事件持续输出进度。"""
        data = self.get_json_data()
        agent_request = _build_agent_request(data)
        if not agent_request.user_message:
            return self.error_response('user_message 不能为空')

        from agent.invoker import stream_invoke_agent

        def generate():
            for event in stream_invoke_agent(agent_request):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return Response(generate(), mimetype='text/event-stream')
