from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsContext


class InsightNsContextDAO(BaseDAO[InsightNsContext]):
    """会话上下文数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_context_dao = self
        super().__init__(InsightNsContext, session)

    def find_by_conversation_id(self, insight_conversation_id: int) -> List[InsightNsContext]:
        """根据会话ID查询上下文列表"""
        return self._session.query(InsightNsContext).filter(
            InsightNsContext.insight_conversation_id == insight_conversation_id
        ).all()

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsContext]:
        """根据洞察空间ID查询上下文列表"""
        return self._session.query(InsightNsContext).filter(
            InsightNsContext.insight_namespace_id == insight_namespace_id
        ).all()

    def find_by_conversation_id_and_type(self, insight_conversation_id: int, type: int) -> List[InsightNsContext]:
        """根据会话ID和上下文类型查询"""
        return self._session.query(InsightNsContext).filter(
            InsightNsContext.insight_conversation_id == insight_conversation_id,
            InsightNsContext.type == type
        ).all()

    def delete_by_conversation_id(self, insight_conversation_id: int) -> bool:
        """根据会话ID删除所有上下文"""
        self._session.query(InsightNsContext).filter(
            InsightNsContext.insight_conversation_id == insight_conversation_id
        ).delete()
        self._session.commit()
        return True

    def delete_by_namespace_id(self, insight_namespace_id: int) -> bool:
        """根据洞察空间ID删除所有上下文"""
        self._session.query(InsightNsContext).filter(
            InsightNsContext.insight_namespace_id == insight_namespace_id
        ).delete()
        self._session.commit()
        return True