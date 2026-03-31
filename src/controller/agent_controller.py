from flask import Blueprint, jsonify, request, Response

from controller.base_controller import BaseController
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

    def invoke(self):
        data = self.get_json_data()
        username = data.get('username', 'anonymous')
        namespace_id = data.get('namespace_id', '')
        conversation_id = data.get('conversation_id', '')
        user_message = data.get('user_message', '')

        if not user_message:
            return self.error_response("user_message 不能为空")
        from agent.invoker import invoke_agent, AgentRequest
        agent_request = AgentRequest(
            username=username,
            namespace_id=namespace_id,
            conversation_id=conversation_id,
            user_message=user_message
        )

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
        username = data.get('username', 'anonymous')
        namespace_id = data.get('namespace_id', '')
        conversation_id = data.get('conversation_id', '')
        user_message = data.get('user_message', '')

        if not user_message:
            return self.error_response("user_message 不能为空")
        from agent.invoker import invoke_agent, AgentRequest

        agent_request = AgentRequest(
            username=username,
            namespace_id=namespace_id,
            conversation_id=conversation_id,
            user_message=user_message
        )

        def generate():
            from agent.invoker import stream_invoke_agent
            for stream_mode, chunk in stream_invoke_agent(agent_request):
                yield f"data: {stream_mode}:{chunk}\n\n"

        return Response(generate(), mimetype='text/event-stream')
