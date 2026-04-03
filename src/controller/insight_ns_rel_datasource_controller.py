from flask import Blueprint, jsonify, request

from controller.base_controller import BaseController
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils.response import Result


def create_insight_ns_rel_datasource_controller(service: InsightNsRelDatasourceService) -> Blueprint:
    """创建会话级数据源绑定控制器。"""
    blueprint = Blueprint('insight_ns_rel_datasource', __name__, url_prefix='/api/insight/conversation/datasource')
    controller = InsightNsRelDatasourceController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_conversation)
    blueprint.route('/', methods=['POST'])(controller.add)
    blueprint.route('/', methods=['DELETE'])(controller.remove)
    return blueprint


class InsightNsRelDatasourceController(BaseController):
    """提供会话数据源绑定的查询与变更接口。"""

    def __init__(self, blueprint: Blueprint, service: InsightNsRelDatasourceService):
        super().__init__(blueprint)
        self._service = service

    def get_by_conversation(self):
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        if not insight_conversation_id:
            return self.error_response("缺少 insight_conversation_id 参数")

        rows = self._service.find_by_conversation_id(insight_conversation_id)
        return jsonify(Result.success(data=rows).to_dict())

    def add(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        datasource_type = data.get('datasource_type')
        datasource_name = data.get('datasource_name')
        knowledge_tag = data.get('knowledge_tag')

        if not all([insight_conversation_id, datasource_type is not None, datasource_name]):
            return self.error_response("缺少必要参数")

        result = self._service.add_datasource(
            insight_conversation_id=int(insight_conversation_id),
            datasource_type=str(datasource_type),
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag or '',
            datasource_schema=data.get('datasource_schema', ''),
            datasource_config_json=data.get('datasource_config_json', '{}'),
        )
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def remove(self):
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        datasource_name = data.get('datasource_name')
        if not all([insight_conversation_id, datasource_name]):
            return self.error_response("缺少必要参数")

        result = self._service.remove_datasource(int(insight_conversation_id), datasource_name)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)
