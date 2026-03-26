from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils.response import Result


def create_insight_ns_rel_datasource_controller(service: InsightNsRelDatasourceService) -> Blueprint:
    """创建洞察空间数据源关联控制器"""
    blueprint = Blueprint('insight_ns_rel_datasource', __name__, url_prefix='/api/insight/namespace/datasource')
    controller = InsightNsRelDatasourceController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_namespace)
    blueprint.route('/', methods=['POST'])(controller.add)
    blueprint.route('/', methods=['DELETE'])(controller.remove)

    return blueprint


class InsightNsRelDatasourceController(BaseController):
    """洞察空间数据源关联接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightNsRelDatasourceService):
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
        datasource_type = data.get('datasource_type')
        datasource_name = data.get('datasource_name')
        knowledge_tag = data.get('knowledge_tag')
        uns_node_alias = data.get('uns_node_alias')
        file_type = data.get('file_type')
        file_id = data.get('file_id')

        if not all([insight_namespace_id, datasource_type is not None, datasource_name, file_id]):
            return self.error_response("缺少必要参数")

        result = self._service.add_datasource(
            insight_namespace_id, datasource_type, datasource_name,
            knowledge_tag or '', uns_node_alias or '',
            file_type or 0, file_id
        )
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def remove(self):
        data = self.get_json_data()
        insight_namespace_id = data.get('insight_namespace_id')
        datasource_name = data.get('datasource_name')

        if not all([insight_namespace_id, datasource_name]):
            return self.error_response("缺少必要参数")

        result = self._service.remove_datasource(insight_namespace_id, datasource_name)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)

    def _to_dict(self, rel) -> dict:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "datasource_type": rel.datasource_type,
            "datasource_name": rel.datasource_name,
            "knowledge_tag": rel.knowledge_tag,
            "uns_node_alias": rel.uns_node_alias,
            "file_type": rel.file_type,
            "file_id": rel.file_id,
            "created_at": rel.created_at.isoformat() if rel.created_at else None
        }
