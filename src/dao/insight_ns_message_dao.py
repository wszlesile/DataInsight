from typing import List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsMessage


class InsightNsMessageDAO(BaseDAO[InsightNsMessage]):
    """会话消息数据访问层。"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_message_dao = self
        super().__init__(InsightNsMessage, session)

    def find_by_conversation_id(self, insight_conversation_id: int) -> List[InsightNsMessage]:
        return self._session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == insight_conversation_id,
            InsightNsMessage.is_deleted == 0,
        ).all()

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsMessage]:
        return self._session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_namespace_id == insight_namespace_id,
            InsightNsMessage.is_deleted == 0,
        ).all()

    def find_by_conversation_id_and_type(self, insight_conversation_id: int, message_type: int) -> List[InsightNsMessage]:
        return self._session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == insight_conversation_id,
            InsightNsMessage.type == message_type,
            InsightNsMessage.is_deleted == 0,
        ).all()

    def delete_by_conversation_id(self, insight_conversation_id: int) -> bool:
        self._session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == insight_conversation_id,
            InsightNsMessage.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self._session.commit()
        return True

    def delete_by_namespace_id(self, insight_namespace_id: int) -> bool:
        self._session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_namespace_id == insight_namespace_id,
            InsightNsMessage.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self._session.commit()
        return True
