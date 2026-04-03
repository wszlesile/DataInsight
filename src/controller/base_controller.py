from flask import Blueprint, jsonify, request


class BaseController:
    """控制器基类。"""

    def __init__(self, blueprint: Blueprint):
        self._blueprint = blueprint

    def success_response(self, data=None, message="success", code=200):
        """返回成功响应。"""
        response = {"code": code, "message": message}
        if data is not None:
            response["data"] = data
        return jsonify(response)

    def error_response(self, message="error", code=400):
        """返回错误响应。"""
        return jsonify({"code": code, "message": message})

    def get_json_data(self) -> dict:
        """获取 JSON 请求体。"""
        return request.get_json() or {}
