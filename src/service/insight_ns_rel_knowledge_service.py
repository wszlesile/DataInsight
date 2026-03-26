from typing import Optional, Dict, Any, List

from dao.insight_ns_rel_knowledge_dao import InsightNsRelKnowledgeDAO
from model import InsightNsRelKnowledge
from service.base_service import BaseService


class InsightNsRelKnowledgeService(BaseService[InsightNsRelKnowledgeDAO]):
    """洞察空间知识库关联业务逻辑层"""

    def __init__(self, dao: InsightNsRelKnowledgeDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_rel_knowledge_service = self
        super().__init__(dao)

    @property
    def rel_knowledge_dao(self) -> InsightNsRelKnowledgeDAO:
        return self._get_dao()

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsRelKnowledge]:
        return self.rel_knowledge_dao.find_by_namespace_id(insight_namespace_id)

    def add_knowledge(self, insight_namespace_id: int, knowledge_name: str, knowledge_tag: str, file_id: str) -> Dict:
        """添加知识库到洞察空间"""
        existing = self.rel_knowledge_dao.find_by_namespace_id_and_tag(insight_namespace_id, knowledge_tag)
        if existing:
            return {"success": False, "message": "该知识库tag已存在"}

        rel = InsightNsRelKnowledge(
            insight_namespace_id=insight_namespace_id,
            knowledge_name=knowledge_name,
            knowledge_tag=knowledge_tag,
            file_id=file_id
        )
        saved = self.rel_knowledge_dao.save(rel)
        return {"success": True, "message": "添加成功", "data": self._to_dict(saved)}

    def remove_knowledge(self, insight_namespace_id: int, knowledge_tag: str) -> Dict:
        """从洞察空间移除知识库"""
        rel = self.rel_knowledge_dao.find_by_namespace_id_and_tag(insight_namespace_id, knowledge_tag)
        if not rel:
            return {"success": False, "message": "知识库关联不存在"}

        self.rel_knowledge_dao.delete(rel.id)
        return {"success": True, "message": "移除成功"}

    def _to_dict(self, rel: InsightNsRelKnowledge) -> dict:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "knowledge_name": rel.knowledge_name,
            "knowledge_tag": rel.knowledge_tag,
            "file_id": rel.file_id,
            "created_at": rel.created_at.isoformat() if rel.created_at else None
        }
