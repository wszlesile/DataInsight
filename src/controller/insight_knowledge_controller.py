from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_knowledge_service import InsightKnowledgeService
from utils.response import Result


def create_insight_knowledge_controller(service: InsightKnowledgeService) -> Blueprint:
    """创建知识库控制器"""
    blueprint = Blueprint('insight_knowledge', __name__, url_prefix='/api/insight/knowledge')
    controller = InsightKnowledgeController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_all)
    blueprint.route('/', methods=['POST'])(controller.create)
    blueprint.route('/<int:knowledge_id>', methods=['GET'])(controller.get_by_id)
    blueprint.route('/<int:knowledge_id>', methods=['PUT'])(controller.update)
    blueprint.route('/<int:knowledge_id>', methods=['DELETE'])(controller.delete)

    return blueprint


class InsightKnowledgeController(BaseController):
    """知识库接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightKnowledgeService):
        super().__init__(blueprint)
        self._service = service

    def get_all(self):
        knowledges = self._service.find_all()
        return jsonify(Result.success(data=[self._to_dict(k) for k in knowledges]).to_dict())

    def get_by_id(self, knowledge_id: int):
        knowledge = self._service.find_by_id(knowledge_id)
        if knowledge:
            return self.success_response(self._to_dict(knowledge))
        return self.error_response("知识库不存在", 404)

    def create(self):
        data = self.get_json_data()
        knowledge_name = data.get('knowledge_name')
        file_id = data.get('file_id')

        if not all([knowledge_name, file_id]):
            return self.error_response("缺少必要参数")

        result = self._service.create_knowledge(knowledge_name, file_id)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def update(self, knowledge_id: int):
        data = self.get_json_data()
        knowledge_name = data.get('knowledge_name')
        file_id = data.get('file_id')

        result = self._service.update_knowledge(knowledge_id, knowledge_name, file_id)
        if result['success']:
            return self.success_response(result['data'], result['message'])
        return self.error_response(result['message'], 400)

    def delete(self, knowledge_id: int):
        if self._service.delete(knowledge_id):
            return self.success_response(None, "删除成功")
        return self.error_response("知识库不存在", 404)

    def _to_dict(self, knowledge) -> dict:
        return {
            "id": knowledge.id,
            "knowledge_name": knowledge.knowledge_name,
            "file_id": knowledge.file_id,
            "created_at": knowledge.created_at.isoformat() if knowledge.created_at else None
        }
