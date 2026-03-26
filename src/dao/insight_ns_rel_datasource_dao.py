from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsRelDatasource


class InsightNsRelDatasourceDAO(BaseDAO[InsightNsRelDatasource]):
    """洞察空间数据源关联数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_rel_datasource_dao = self
        super().__init__(InsightNsRelDatasource, session)

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsRelDatasource]:
        """根据洞察空间ID查询数据源列表"""
        return self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_namespace_id == insight_namespace_id
        ).all()

    def find_by_namespace_id_and_name(self, insight_namespace_id: int, datasource_name: str) -> Optional[InsightNsRelDatasource]:
        """根据洞察空间ID和数据源名称查询"""
        return self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_namespace_id == insight_namespace_id,
            InsightNsRelDatasource.datasource_name == datasource_name
        ).first()

    def delete_by_namespace_id(self, insight_namespace_id: int) -> bool:
        """根据洞察空间ID删除所有关联"""
        self._session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_namespace_id == insight_namespace_id
        ).delete()
        self._session.commit()
        return True
