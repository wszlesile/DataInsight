from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_ns_rel_knowledge_service import InsightNsRelKnowledgeService
from utils.response import Result


def create_insight_ns_rel_knowledge_controller(service: InsightNsRelKnowledgeService) -> Blueprint:
    """创建洞察空间知识库关联控制器"""
    blueprint = Blueprint('insight_ns_rel_knowledge', __name__, url_prefix='/api/insight/namespace/knowledge')
    controller = InsightNsRelKnowledgeController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_namespace)
    blueprint.route('/', methods=['POST'])(controller.add)
    blueprint.route('/<int:rel_id>', methods=['DELETE'])(controller.remove)

    return blueprint


class InsightNsRelKnowledgeController(BaseController):
    """洞察空间知识库关联接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightNsRelKnowledgeService):
        super().__init__(blueprint)
        self._service = service

    def get_by_namespace(self):
        data = self.get_json_data()
        insight_namespace_id = data.get('insight_namespace_id')

        if not insight_namespace_id:
            return self.error_response("缺少insight_namespace_id参数")

        rels = self._service.find_by_namespace_id(insight_namespace_id)
        return jsonify(Result.success(data=[self._to_dict(rel) for rel in rels]).to_dict())

    def add(self):
        data = self.get_json_data()
        insight_namespace_id = data.get('insight_namespace_id')
        knowledge_name = data.get('knowledge_name')
        knowledge_tag = data.get('knowledge_tag')
        file_id = data.get('file_id')

        if not all([insight_namespace_id, knowledge_name, knowledge_tag, file_id]):
            return self.error_response("缺少必要参数")

        result = self._service.add_knowledge(insight_namespace_id, knowledge_name, knowledge_tag, file_id)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def remove(self, rel_id: int):
        # 需要通过rel_id获取insight_namespace_id和knowledge_tag，这里简化为直接删除
        result = self._service.delete(rel_id)
        if result:
            return self.success_response(None, "移除成功")
        return self.error_response("关联不存在", 404)

    def _to_dict(self, rel) -> dict:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "knowledge_name": rel.knowledge_name,
            "knowledge_tag": rel.knowledge_tag,
            "file_id": rel.file_id,
            "created_at": rel.created_at.isoformat() if rel.created_at else None
        }
