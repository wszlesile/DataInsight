from flask import Blueprint, jsonify, request

from config.database import SessionLocal
from controller.base_controller import BaseController
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils.response import Result


def create_insight_ns_rel_datasource_controller() -> Blueprint:
    """创建会话级数据源绑定控制器。"""
    blueprint = Blueprint('insight_ns_rel_datasource', __name__)
    controller = InsightNsRelDatasourceController(blueprint)

    blueprint.route('/api/insight/conversation/datasource/', methods=['GET'])(controller.get_by_conversation)
    blueprint.route('/api/insight/conversation/datasource/', methods=['POST'])(controller.bind_datasource)
    blueprint.route('/api/insight/conversation/datasource/', methods=['DELETE'])(controller.remove)
    return blueprint


class InsightNsRelDatasourceController(BaseController):
    """提供会话数据源绑定的查询与变更接口。"""

    def get_by_conversation(self):
        """查询某个会话当前已绑定的数据源。"""
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        if not insight_conversation_id:
            return self.error_response("缺少 insight_conversation_id 参数")

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            rows = service.find_by_conversation_id(insight_conversation_id)
            return jsonify(Result.success(data=rows).to_dict())
        finally:
            session.close()

    def bind_datasource(self):
        """把空间级数据源绑定到当前会话。"""
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        datasource_id = data.get('datasource_id')
        if not all([insight_conversation_id, datasource_id]):
            return self.error_response("缺少必要参数")

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.bind_existing_datasource(
                insight_conversation_id=int(insight_conversation_id),
                datasource_id=int(datasource_id),
            )
            if result['success']:
                return self.success_response(result['data'], result['message'])
            return self.error_response(result['message'], 400)
        finally:
            session.close()

    def remove(self):
        """从当前会话解绑某个数据源，不删除空间级数据源本体。"""
        data = self.get_json_data()
        insight_conversation_id = data.get('insight_conversation_id')
        datasource_id = data.get('datasource_id')
        if not all([insight_conversation_id, datasource_id]):
            return self.error_response("缺少必要参数")

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.remove_datasource(int(insight_conversation_id), int(datasource_id))
            if result['success']:
                return self.success_response(None, result['message'])
            return self.error_response(result['message'], 400)
        finally:
            session.close()
