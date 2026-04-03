from flask import Blueprint, jsonify, request

from controller.base_controller import BaseController
from service.insight_ns_rel_knowledge_service import InsightNsRelKnowledgeService
from utils.response import Result


def create_insight_ns_rel_knowledge_controller(service: InsightNsRelKnowledgeService) -> Blueprint:
    """创建会话级知识绑定控制器。"""
    blueprint = Blueprint('insight_ns_rel_knowledge', __name__, url_prefix='/api/insight/conversation/knowledge')
    controller = InsightNsRelKnowledgeController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_conversation)
    blueprint.route('/', methods=['POST'])(controller.add)
    blueprint.route('/', methods=['DELETE'])(controller.remove)
    return blueprint


class InsightNsRelKnowledgeController(BaseController):
    """会话知识绑定的查询与变更控制器。"""

    def __init__(self, blueprint: Blueprint, service: InsightNsRelKnowledgeService):
        super().__init__(blueprint)
        self._service = service

    def get_by_conversation(self):
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        if not insight_conversation_id:
            return self.error_response("缺少 insight_conversation_id 参数")

        rels = self._service.find_by_conversation_id(insight_conversation_id)
        return jsonify(Result.success(data=rels).to_dict())

    def add(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        knowledge_tag = data.get('knowledge_tag')
        knowledge_id = data.get('knowledge_id', 0)
        knowledge_name = data.get('knowledge_name', '')
        file_id = data.get('file_id', '')

        if not insight_conversation_id or not knowledge_tag:
            return self.error_response("缺少必要参数")
        if int(knowledge_id or 0) <= 0 and not file_id:
            return self.error_response("缺少知识资源标识")

        result = self._service.add_knowledge(
            int(insight_conversation_id),
            knowledge_tag=knowledge_tag,
            knowledge_id=int(knowledge_id or 0),
            knowledge_name=knowledge_name,
            file_id=file_id,
        )
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def remove(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        knowledge_tag = data.get('knowledge_tag')
        if not all([insight_conversation_id, knowledge_tag]):
            return self.error_response("缺少必要参数")

        result = self._service.remove_knowledge(int(insight_conversation_id), knowledge_tag)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)
