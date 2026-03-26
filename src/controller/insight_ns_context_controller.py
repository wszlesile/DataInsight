from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_ns_context_service import InsightNsContextService
from utils.response import Result


def create_insight_ns_context_controller(service: InsightNsContextService) -> Blueprint:
    """创建会话上下文控制器"""
    blueprint = Blueprint('insight_ns_context', __name__, url_prefix='/api/insight/context')
    controller = InsightNsContextController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_conversation)
    blueprint.route('/', methods=['POST'])(controller.create)
    blueprint.route('/', methods=['DELETE'])(controller.delete_by_conversation)

    return blueprint


class InsightNsContextController(BaseController):
    """会话上下文接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightNsContextService):
        super().__init__(blueprint)
        self._service = service

    def get_by_conversation(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')

        if not insight_conversation_id:
            return self.error_response("缺少insight_conversation_id参数")

        contexts = self._service.find_by_conversation_id(insight_conversation_id)
        return jsonify(Result.success(data=[self._to_dict(c) for c in contexts]).to_dict())

    def create(self):
        data = self.get_json_data()
        username = data.get('username')
        insight_namespace_id = data.get('insight_namespace_id')
        insight_conversation_id = data.get('insight_conversation_id')
        type = data.get('type')
        content = data.get('content')
        insight_result = data.get('insight_result', '')

        if not all([username, insight_namespace_id, insight_conversation_id, type is not None, content]):
            return self.error_response("缺少必要参数")

        result = self._service.create_context(
            username, insight_namespace_id, insight_conversation_id,
            type, content, insight_result
        )
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def delete_by_conversation(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')

        if not insight_conversation_id:
            return self.error_response("缺少insight_conversation_id参数")

        result = self._service.delete_by_conversation_id(insight_conversation_id)
        return self.success_response(None, result['message'])

    def _to_dict(self, context) -> dict:
        return {
            "id": context.id,
            "username": context.username,
            "insight_namespace_id": context.insight_namespace_id,
            "insight_conversation_id": context.insight_conversation_id,
            "type": context.type,
            "content": context.content,
            "insight_result": context.insight_result,
            "created_at": context.created_at.isoformat() if context.created_at else None
        }
