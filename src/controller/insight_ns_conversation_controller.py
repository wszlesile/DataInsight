from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_ns_conversation_service import InsightNsConversationService
from utils.response import Result


def create_insight_ns_conversation_controller(service: InsightNsConversationService) -> Blueprint:
    """创建会话控制器"""
    blueprint = Blueprint('insight_ns_conversation', __name__, url_prefix='/api/insight/conversation')
    controller = InsightNsConversationController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_namespace)
    blueprint.route('/', methods=['POST'])(controller.create)
    blueprint.route('/<int:conversation_id>', methods=['GET'])(controller.get_by_id)
    blueprint.route('/<int:conversation_id>', methods=['PUT'])(controller.update)
    blueprint.route('/<int:conversation_id>', methods=['DELETE'])(controller.delete)

    return blueprint


class InsightNsConversationController(BaseController):
    """会话接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightNsConversationService):
        super().__init__(blueprint)
        self._service = service

    def get_by_namespace(self):
        data = self.get_json_data()
        insight_namespace_id = data.get('insight_namespace_id')

        if not insight_namespace_id:
            return self.error_response("缺少insight_namespace_id参数")

        conversations = self._service.find_by_namespace_id(insight_namespace_id)
        return jsonify(Result.success(data=[self._to_dict(c) for c in conversations]).to_dict())

    def get_by_id(self, conversation_id: int):
        conversation = self._service.find_by_id(conversation_id)
        if conversation:
            return self.success_response(self._to_dict(conversation))
        return self.error_response("会话不存在", 404)

    def create(self):
        data = self.get_json_data()
        username = data.get('username')
        insight_namespace_id = data.get('insight_namespace_id')
        user_message = data.get('user_message')
        insight_result = data.get('insight_result', '')

        if not all([username, insight_namespace_id, user_message]):
            return self.error_response("缺少必要参数")

        result = self._service.create_conversation(username, insight_namespace_id, user_message, insight_result)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def update(self, conversation_id: int):
        data = self.get_json_data()
        user_message = data.get('user_message')
        insight_result = data.get('insight_result')

        result = self._service.update_conversation(conversation_id, user_message, insight_result)
        if result['success']:
            return self.success_response(result['data'], result['message'])
        return self.error_response(result['message'], 400)

    def delete(self, conversation_id: int):
        result = self._service.delete_conversation(conversation_id)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)

    def _to_dict(self, conversation) -> dict:
        return {
            "id": conversation.id,
            "username": conversation.username,
            "insight_namespace_id": conversation.insight_namespace_id,
            "user_message": conversation.user_message,
            "insight_result": conversation.insight_result,
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None
        }
