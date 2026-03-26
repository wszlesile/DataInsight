from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsConversation


class InsightNsConversationDAO(BaseDAO[InsightNsConversation]):
    """会话数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_conversation_dao = self
        super().__init__(InsightNsConversation, session)

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsConversation]:
        """根据洞察空间ID查询会话列表"""
        return self._session.query(InsightNsConversation).filter(
            InsightNsConversation.insight_namespace_id == insight_namespace_id
        ).all()

    def find_by_username(self, username: str) -> List[InsightNsConversation]:
        """根据用户名查询会话列表"""
        return self._session.query(InsightNsConversation).filter(
            InsightNsConversation.username == username
        ).all()

    def find_by_namespace_id_and_created_at_desc(self, insight_namespace_id: int) -> List[InsightNsConversation]:
        """根据洞察空间ID查询会话列表（按创建时间倒序）"""
        return self._session.query(InsightNsConversation).filter(
            InsightNsConversation.insight_namespace_id == insight_namespace_id
        ).order_by(InsightNsConversation.created_at.desc()).all()

    def delete_by_namespace_id(self, insight_namespace_id: int) -> bool:
        """根据洞察空间ID删除所有会话"""
        self._session.query(InsightNsConversation).filter(
            InsightNsConversation.insight_namespace_id == insight_namespace_id
        ).delete()
        self._session.commit()
        return True
