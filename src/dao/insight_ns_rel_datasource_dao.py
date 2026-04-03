from typing import Optional

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsRelDatasource


class InsightNsRelDatasourceDAO(BaseDAO[InsightNsRelDatasource]):
    """洞察空间数据源关系数据访问层。"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_rel_datasource_dao = self
        super().__init__(InsightNsRelDatasource, session)

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[InsightNsRelDatasource]:
        return self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).order_by(InsightNsRelDatasource.sort_no.asc(), InsightNsRelDatasource.id.asc()).all()

    def find_by_conversation_id_and_datasource_id(
        self,
        insight_conversation_id: int,
        datasource_id: int,
    ) -> Optional[InsightNsRelDatasource]:
        return self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == datasource_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).first()

    def delete_by_conversation_id(self, insight_conversation_id: int) -> bool:
        self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self._session.commit()
        return True
