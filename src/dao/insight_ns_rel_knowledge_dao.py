from typing import Optional, List

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightNsRelKnowledge


class InsightNsRelKnowledgeDAO(BaseDAO[InsightNsRelKnowledge]):
    """洞察空间知识库关联数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_rel_knowledge_dao = self
        super().__init__(InsightNsRelKnowledge, session)

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsRelKnowledge]:
        """根据洞察空间ID查询知识库列表"""
        return self._session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_namespace_id == insight_namespace_id
        ).all()

    def find_by_namespace_id_and_tag(self, insight_namespace_id: int, knowledge_tag: str) -> Optional[InsightNsRelKnowledge]:
        """根据洞察空间ID和知识库tag查询"""
        return self._session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_namespace_id == insight_namespace_id,
            InsightNsRelKnowledge.knowledge_tag == knowledge_tag
        ).first()

    def delete_by_namespace_id(self, insight_namespace_id: int) -> bool:
        """根据洞察空间ID删除所有关联"""
        self._session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_namespace_id == insight_namespace_id
        ).delete()
        self._session.commit()
        return True
