from typing import List, Optional

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightUserCollect


class InsightUserCollectDAO(BaseDAO[InsightUserCollect]):
    """用户收藏数据访问层。"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_user_collect_dao = self
        super().__init__(InsightUserCollect, session)

    def find_by_username(self, username: str) -> List[InsightUserCollect]:
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.is_deleted == 0,
        ).all()

    def find_by_username_and_message_id(self, username: str, insight_message_id: int) -> Optional[InsightUserCollect]:
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_message_id == insight_message_id,
            InsightUserCollect.is_deleted == 0,
        ).first()

    def find_by_username_and_message_id_in(self, username: str, message_ids: List[int]) -> List[InsightUserCollect]:
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_message_id.in_(message_ids),
            InsightUserCollect.is_deleted == 0,
        ).all()

    def delete_by_username_and_message_id(self, username: str, insight_message_id: int) -> bool:
        self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_message_id == insight_message_id,
            InsightUserCollect.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self._session.commit()
        return True
