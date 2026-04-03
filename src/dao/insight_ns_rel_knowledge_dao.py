from typing import List, Optional

from sqlalchemy.orm import Session

from dao.base_dao import BaseDAO
from model import InsightKnowledge, InsightNsRelKnowledge


class InsightNsRelKnowledgeDAO(BaseDAO[InsightNsRelKnowledge]):
    """洞察会话与知识资源关系数据访问层"""

    def __init__(self, session: Session, beanFactory=None):
        if beanFactory:
            beanFactory.insight_ns_rel_knowledge_dao = self
        super().__init__(InsightNsRelKnowledge, session)

    def find_by_conversation_id(self, insight_conversation_id: int) -> List[InsightNsRelKnowledge]:
        return self._session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_conversation_id == insight_conversation_id,
            InsightNsRelKnowledge.is_deleted == 0,
        ).all()

    def find_by_conversation_id_and_tag(
        self,
        insight_conversation_id: int,
        knowledge_tag: str,
    ) -> Optional[InsightNsRelKnowledge]:
        return self._session.query(InsightNsRelKnowledge).join(
            InsightKnowledge,
            InsightNsRelKnowledge.knowledge_id == InsightKnowledge.id,
        ).filter(
            InsightNsRelKnowledge.insight_conversation_id == insight_conversation_id,
            InsightKnowledge.knowledge_tag == knowledge_tag,
            InsightNsRelKnowledge.is_deleted == 0,
            InsightKnowledge.is_deleted == 0,
        ).first()

    def delete_by_conversation_id(self, insight_conversation_id: int) -> bool:
        self._session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_conversation_id == insight_conversation_id,
            InsightNsRelKnowledge.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self._session.commit()
        return True
