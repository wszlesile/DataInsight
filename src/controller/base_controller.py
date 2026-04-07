from flask import Blueprint, jsonify, request

from utils.response import Result


class BaseController:
    """控制器基类。"""

    def __init__(self, blueprint: Blueprint):
        self._blueprint = blueprint

    def success_response(self, data=None, message="success", code=200):
        """返回统一的成功响应结构。"""
        return jsonify(Result.success(data=data, message=message, code=code).to_dict())

    def error_response(self, message="error", code=400):
        """返回统一的失败响应结构。"""
        return jsonify(Result.error(message=message, code=code).to_dict())

    def get_json_data(self) -> dict:
        """获取 JSON 请求体。"""
        return request.get_json() or {}
