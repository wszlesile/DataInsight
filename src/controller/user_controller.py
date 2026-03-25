from flask import Blueprint,jsonify
from controller.base_controller import BaseController
from service.user_service import UserService
from utils.response import Result


def create_user_controller(user_service: UserService) -> Blueprint:
    """创建用户控制器"""
    blueprint = Blueprint('user', __name__, url_prefix='/api/users')
    controller = UserController(blueprint, user_service)

    blueprint.route('/', methods=['GET'])(controller.get_all_users)
    blueprint.route('/<int:user_id>', methods=['GET'])(controller.get_user)
    blueprint.route('/register', methods=['POST'])(controller.register)
    blueprint.route('/login', methods=['POST'])(controller.login)
    blueprint.route('/<int:user_id>', methods=['PUT'])(controller.update_user)
    blueprint.route('/<int:user_id>', methods=['DELETE'])(controller.delete_user)

    return blueprint


class UserController(BaseController):
    """用户接口控制器"""

    def __init__(self, blueprint: Blueprint, user_service: UserService):
        super().__init__(blueprint)
        self._user_service = user_service

    def get_all_users(self):
        users = self._user_service.find_all()
        return jsonify(Result.success(data = [u.to_dict() for u in users]).to_dict())

    def get_user(self, user_id: int):
        user = self._user_service.find_by_id(user_id)
        if user:
            return self.success_response(user.to_dict())
        return self.error_response("用户不存在", 404)

    def register(self):
        data = self.get_json_data()
        username = data.get('username')
        password = data.get('password')
        email = data.get('email')

        if not all([username, password, email]):
            return self.error_response("缺少必要参数")

        result = self._user_service.register(username, password, email)
        if result['success']:
            return self.success_response(result['data'], result['message'], 201)
        return self.error_response(result['message'], 400)

    def login(self):
        data = self.get_json_data()
        username = data.get('username')
        password = data.get('password')

        if not all([username, password]):
            return self.error_response("缺少必要参数")

        result = self._user_service.login(username, password)
        if result['success']:
            return self.success_response(result['data'], result['message'])
        return self.error_response(result['message'], 401)

    def update_user(self, user_id: int):
        data = self.get_json_data()
        user = self._user_service.find_by_id(user_id)

        if not user:
            return self.error_response("用户不存在", 404)

        if 'username' in data:
            user.username = data['username']
        if 'email' in data:
            user.email = data['email']
        if 'password' in data:
            user.password = data['password']

        updated_user = self._user_service.update(user)
        return self.success_response(updated_user.to_dict(), "更新成功")

    def delete_user(self, user_id: int):
        if self._user_service.delete(user_id):
            return self.success_response(None, "删除成功")
        return self.error_response("用户不存在", 404)
