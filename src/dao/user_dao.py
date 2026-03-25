from typing import Optional
from sqlalchemy.orm import Session
from dao.base_dao import BaseDAO
from model.user import User


class UserDAO(BaseDAO[User]):
    """用户数据访问层"""

    def __init__(self, session: Session):
        super().__init__(User, session)

    def find_by_username(self, username: str) -> Optional[User]:
        """根据用户名查询用户"""
        return self._session.query(User).filter(User.username == username).first()

    def find_by_email(self, email: str) -> Optional[User]:
        """根据邮箱查询用户"""
        return self._session.query(User).filter(User.email == email).first()
