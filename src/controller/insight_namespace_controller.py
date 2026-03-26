from flask import Blueprint, jsonify

from controller.base_controller import BaseController
from service.insight_namespace_service import InsightNamespaceService
from utils.response import Result


def create_insight_namespace_controller(service: InsightNamespaceService) -> Blueprint:
    """创建洞察空间控制器"""
    blueprint = Blueprint('insight_namespace', __name__, url_prefix='/api/insight/namespace')
    controller = InsightNamespaceController(blueprint, service)

    blueprint.route('/', methods=['GET'])(controller.get_by_username)
    blueprint.route('/', methods=['POST'])(controller.create)
    blueprint.route('/<int:namespace_id>', methods=['GET'])(controller.get_by_id)
    blueprint.route('/<int:namespace_id>', methods=['PUT'])(controller.update)
    blueprint.route('/<int:namespace_id>', methods=['DELETE'])(controller.delete)

    return blueprint


class InsightNamespaceController(BaseController):
    """洞察空间接口控制器"""

    def __init__(self, blueprint: Blueprint, service: InsightNamespaceService):
        super().__init__(blueprint)
        self._service = service

    def get_by_username(self):
        username = self.get_json_data().get('username')
        if not username:
            return self.error_response("缺少username参数")

        namespaces = self._service.find_by_username(username)
        return jsonify(Result.success(data=[self._to_dict(ns) for ns in namespaces]).to_dict())

    def get_by_id(self, namespace_id: int):
        namespace = self._service.find_by_id(namespace_id)
        if namespace:
            return self.success_response(self._to_dict(namespace))
        return self.error_response("洞察空间不存在", 404)

    def create(self):
        data = self.get_json_data()
        username = data.get('username')
        name = data.get('name')

        if not all([username, name]):
            return self.error_response("缺少必要参数")

        result = self._service.create_namespace(username, name)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def update(self, namespace_id: int):
        data = self.get_json_data()
        name = data.get('name')

        if not name:
            return self.error_response("缺少name参数")

        result = self._service.update_namespace(namespace_id, name)
        if result['success']:
            return self.success_response(result['data'], result['message'])
        return self.error_response(result['message'], 400)

    def delete(self, namespace_id: int):
        result = self._service.delete_namespace(namespace_id)
        if result['success']:
            return self.success_response(None, result['message'])
        return self.error_response(result['message'], 400)

    def _to_dict(self, namespace) -> dict:
        return {
            "id": namespace.id,
            "username": namespace.username,
            "name": namespace.name,
            "created_at": namespace.created_at.isoformat() if namespace.created_at else None
        }
