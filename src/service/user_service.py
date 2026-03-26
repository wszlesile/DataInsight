from typing import Optional, Dict, Any

from dao.user_dao import UserDAO
from model import User
from service.base_service import BaseService


class UserService(BaseService[UserDAO]):
    """用户业务逻辑层"""

    def __init__(self, user_dao: UserDAO,beanFactory: Any):
        beanFactory.user_service = self
        super().__init__(user_dao)

    @property
    def user_dao(self) -> UserDAO:
        """获取用户DAO"""
        return self._get_dao()

    def register(self, username: str, password: str, email: str) -> Dict:
        """用户注册"""
        if self.user_dao.find_by_username(username):
            return {"success": False, "message": "用户名已存在"}

        if self.user_dao.find_by_email(email):
            return {"success": False, "message": "邮箱已被注册"}

        user = User(username=username, password=password, email=email)
        saved_user = self.user_dao.save(user)

        return {"success": True, "message": "注册成功", "data": saved_user.to_dict()}

    def login(self, username: str, password: str) -> Dict:
        """用户登录"""
        user = self.user_dao.find_by_username(username)

        if not user:
            return {"success": False, "message": "用户不存在"}

        if user.password != password:
            return {"success": False, "message": "密码错误"}

        return {"success": True, "message": "登录成功", "data": user.to_dict()}

    def find_by_username(self, username: str) -> Optional[User]:
        return self.user_dao.find_by_username(username)

    def find_by_email(self, email: str) -> Optional[User]:
        return self.user_dao.find_by_email(email)
