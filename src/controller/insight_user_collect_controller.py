from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_user_collect_service import InsightUserCollectService
from utils.response import Result


def create_insight_user_collect_controller(service: InsightUserCollectService) -> Blueprint:
    """创建用户收藏控制器"""
    blueprint = Blueprint('insight_user_collect', __name__, url_prefix='/api/insight/collect')
    controller = InsightUserCollectController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_username)
    blueprint.route('/', methods=['POST'])(controller.add)
    blueprint.route('/', methods=['DELETE'])(controller.remove)

    return blueprint


class InsightUserCollectController(BaseController):
    """用户收藏接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightUserCollectService):
        super().__init__(blueprint)
        self._service = service

    def get_by_username(self):
        data = self.get_json_data()
        username = data.get('username')

        if not username:
            return self.error_response("缺少username参数")

        collects = self._service.find_by_username(username)
        return jsonify(Result.success(data=[self._to_dict(c) for c in collects]).to_dict())

    def add(self):
        data = self.get_json_data()
        username = data.get('username')
        insight_context_id = data.get('insight_context_id')

        if not all([username, insight_context_id]):
            return self.error_response("缺少必要参数")

        result = self._service.add_collect(username, insight_context_id)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def remove(self):
        data = self.get_json_data()
        username = data.get('username')
        insight_context_id = data.get('insight_context_id')

        if not all([username, insight_context_id]):
            return self.error_response("缺少必要参数")

        result = self._service.remove_collect(username, insight_context_id)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)

    def _to_dict(self, collect) -> dict:
        return {
            "id": collect.id,
            "username": collect.username,
            "insight_context_id": collect.insight_context_id,
            "created_at": collect.created_at.isoformat() if collect.created_at else None
        }
