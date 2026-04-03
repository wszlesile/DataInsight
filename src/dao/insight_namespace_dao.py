from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNamespace


class InsightNamespaceDAO(BaseDAO[InsightNamespace]):
    """洞察空间数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_namespace_dao = self
        super().__init__(InsightNamespace, session)

    def find_by_username(self, username: str) -> List[InsightNamespace]:
        """根据用户名查询洞察空间列表"""
        return self._session.query(InsightNamespace).filter(
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).all()

    def find_by_username_and_name(self, username: str, name: str) -> Optional[InsightNamespace]:
        """根据用户名和名称查询洞察空间"""
        return self._session.query(InsightNamespace).filter(
            InsightNamespace.username == username,
            InsightNamespace.name == name,
            InsightNamespace.is_deleted == 0,
        ).first()
