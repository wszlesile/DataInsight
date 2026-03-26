from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightKnowledge


class InsightKnowledgeDAO(BaseDAO[InsightKnowledge]):
    """知识库数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_knowledge_dao = self
        super().__init__(InsightKnowledge, session)

    def find_by_name(self, knowledge_name: str) -> Optional[InsightKnowledge]:
        """根据知识库名称查询"""
        return self._session.query(InsightKnowledge).filter(
            InsightKnowledge.knowledge_name == knowledge_name
        ).first()

    def find_by_file_id(self, file_id: str) -> Optional[InsightKnowledge]:
        """根据文件ID查询"""
        return self._session.query(InsightKnowledge).filter(
            InsightKnowledge.file_id == file_id
        ).first()
