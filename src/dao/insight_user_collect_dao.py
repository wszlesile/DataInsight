from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightUserCollect


class InsightUserCollectDAO(BaseDAO[InsightUserCollect]):
    """用户收藏数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_user_collect_dao = self
        super().__init__(InsightUserCollect, session)

    def find_by_username(self, username: str) -> List[InsightUserCollect]:
        """根据用户名查询收藏列表"""
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username
        ).all()

    def find_by_username_and_context_id(self, username: str, insight_context_id: int) -> Optional[InsightUserCollect]:
        """根据用户名和上下文ID查询"""
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_context_id == insight_context_id
        ).first()

    def find_by_username_and_context_id_in(self, username: str, context_ids: List[int]) -> List[InsightUserCollect]:
        """根据用户名和上下文ID列表批量查询"""
        return self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_context_id.in_(context_ids)
        ).all()

    def delete_by_username_and_context_id(self, username: str, insight_context_id: int) -> bool:
        """根据用户名和上下文ID删除收藏"""
        self._session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.insight_context_id == insight_context_id
        ).delete()
        self._session.commit()
        return True
