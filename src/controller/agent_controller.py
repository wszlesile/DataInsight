import json

from flask import Blueprint, jsonify, request, Response

from controller.base_controller import BaseController
from dto import get_current_user_context
from utils.response import Result


def create_agent_controller() -> Blueprint:
    """创建 Agent 控制器"""
    blueprint = Blueprint('agent', __name__, url_prefix='/api/agent')
    controller = AgentController(blueprint)

    blueprint.route('/invoke', methods=['POST'])(controller.invoke)
    blueprint.route('/stream', methods=['POST'])(controller.stream_invoke)

    return blueprint


class AgentController(BaseController):
    """Agent 接口控制器"""

    def _build_agent_request(self, data: dict) -> tuple:
        """构建 AgentRequest，失败返回 (None, error_response)"""
        user_context = get_current_user_context()
        username = user_context.username if user_context else 'anonymous'
        namespace_id = data.get('namespace_id', '')
        conversation_id = data.get('conversation_id', '')
        user_message = data.get('user_message', '')

        if not user_message:
            return None, self.error_response("user_message 不能为空")

        from agent.invoker import AgentRequest
        agent_request = AgentRequest(
            username=username,
            namespace_id=namespace_id,
            conversation_id=conversation_id,
            user_message=user_message
        )
        return agent_request, None

    def invoke(self):
        data = self.get_json_data()
        agent_request, error = self._build_agent_request(data)
        if error:
            return error
        from agent.invoker import invoke_agent

        try:
            response = invoke_agent(agent_request)
            return jsonify(Result.success(data={
                'username': response.username,
                'message': response.message,
                'file_id': response.file_id,
                'analysis_report': response.analysis_report,
            }).to_dict())
        except Exception as e:
            return self.error_response(f"Agent 执行失败: {str(e)}")

    def stream_invoke(self):
        data = request.get_json()
        agent_request, error = self._build_agent_request(data)
        if error:
            return error

        def generate():
            from agent.invoker import stream_invoke_agent
            try:
                for event in stream_invoke_agent(agent_request):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                error_event = {
                    'type': 'error',
                    'stage': 'error',
                    'level': 'error',
                    'message': f'流式分析失败: {str(e)}'
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        return Response(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
