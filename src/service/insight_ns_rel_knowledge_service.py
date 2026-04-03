from typing import Any

from dao.insight_ns_rel_knowledge_dao import InsightNsRelKnowledgeDAO
from model import InsightKnowledge, InsightNsConversation, InsightNsRelKnowledge
from service.base_service import BaseService


class InsightNsRelKnowledgeService(BaseService[InsightNsRelKnowledgeDAO]):
    """Service for conversation-to-global-knowledge bindings."""

    def __init__(self, dao: InsightNsRelKnowledgeDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_rel_knowledge_service = self
        super().__init__(dao)

    @property
    def rel_knowledge_dao(self) -> InsightNsRelKnowledgeDAO:
        return self._get_dao()

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[dict[str, Any]]:
        session = self.rel_knowledge_dao._session
        rows = session.query(InsightNsRelKnowledge, InsightKnowledge).filter(
            InsightNsRelKnowledge.insight_conversation_id == insight_conversation_id,
            InsightNsRelKnowledge.knowledge_id == InsightKnowledge.id,
            InsightNsRelKnowledge.is_deleted == 0,
            InsightKnowledge.is_deleted == 0,
        ).order_by(InsightNsRelKnowledge.id.asc()).all()
        return [self._to_dict(rel, knowledge) for rel, knowledge in rows]

    def add_knowledge(
        self,
        insight_conversation_id: int,
        knowledge_tag: str,
        knowledge_id: int = 0,
        knowledge_name: str = '',
        file_id: str = '',
    ) -> dict[str, Any]:
        if self.rel_knowledge_dao.find_by_conversation_id_and_tag(insight_conversation_id, knowledge_tag):
            return {"success": False, "message": "该知识标签已存在"}

        session = self.rel_knowledge_dao._session
        conversation = session.query(InsightNsConversation).filter(
            InsightNsConversation.id == insight_conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
        if conversation is None:
            return {"success": False, "message": "会话不存在"}

        knowledge = self._resolve_knowledge(
            session=session,
            knowledge_id=knowledge_id,
            knowledge_name=knowledge_name,
            knowledge_tag=knowledge_tag,
            file_id=file_id,
        )
        if knowledge is None:
            return {"success": False, "message": "知识资源不存在"}

        relation = InsightNsRelKnowledge(
            insight_namespace_id=conversation.insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            knowledge_id=knowledge.id,
        )
        saved = self.rel_knowledge_dao.save(relation)
        return {"success": True, "message": "添加成功", "data": self._to_dict(saved, knowledge)}

    def remove_knowledge(self, insight_conversation_id: int, knowledge_tag: str) -> dict[str, Any]:
        relation = self.rel_knowledge_dao.find_by_conversation_id_and_tag(insight_conversation_id, knowledge_tag)
        if not relation:
            return {"success": False, "message": "知识资源关系不存在"}

        self.rel_knowledge_dao.delete(relation.id)
        return {"success": True, "message": "移除成功"}

    def _resolve_knowledge(
        self,
        session,
        knowledge_id: int,
        knowledge_name: str,
        knowledge_tag: str,
        file_id: str,
    ) -> InsightKnowledge | None:
        if int(knowledge_id or 0) > 0:
            knowledge = session.query(InsightKnowledge).filter(
                InsightKnowledge.id == int(knowledge_id),
                InsightKnowledge.is_deleted == 0,
            ).first()
            if knowledge is not None and knowledge_tag:
                knowledge.knowledge_tag = knowledge_tag
                session.flush()
            return knowledge

        if not file_id:
            return None

        knowledge = session.query(InsightKnowledge).filter(
            InsightKnowledge.file_id == file_id,
            InsightKnowledge.is_deleted == 0,
        ).first()
        if knowledge is not None:
            if knowledge_tag:
                knowledge.knowledge_tag = knowledge_tag
                session.flush()
            return knowledge

        knowledge = InsightKnowledge(
            knowledge_name=(knowledge_name or file_id)[:128],
            knowledge_tag=knowledge_tag or '',
            file_id=file_id,
        )
        session.add(knowledge)
        session.flush()
        return knowledge

    def _to_dict(self, rel: InsightNsRelKnowledge, knowledge: InsightKnowledge | None) -> dict[str, Any]:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "insight_conversation_id": rel.insight_conversation_id,
            "knowledge_id": rel.knowledge_id,
            "knowledge_name": knowledge.knowledge_name if knowledge else '',
            "knowledge_tag": knowledge.knowledge_tag if knowledge else '',
            "file_id": knowledge.file_id if knowledge else '',
            "created_at": rel.created_at.isoformat() if rel.created_at else None,
        }
