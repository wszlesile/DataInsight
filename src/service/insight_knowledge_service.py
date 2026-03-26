from typing import Optional, Dict, Any

from dao.insight_knowledge_dao import InsightKnowledgeDAO
from model import InsightKnowledge
from service.base_service import BaseService


class InsightKnowledgeService(BaseService[InsightKnowledgeDAO]):
    """知识库业务逻辑层"""

    def __init__(self, dao: InsightKnowledgeDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_knowledge_service = self
        super().__init__(dao)

    @property
    def knowledge_dao(self) -> InsightKnowledgeDAO:
        return self._get_dao()

    def find_by_name(self, knowledge_name: str) -> Optional[InsightKnowledge]:
        return self.knowledge_dao.find_by_name(knowledge_name)

    def find_by_file_id(self, file_id: str) -> Optional[InsightKnowledge]:
        return self.knowledge_dao.find_by_file_id(file_id)

    def create_knowledge(self, knowledge_name: str, file_id: str) -> Dict:
        """创建知识库"""
        existing = self.find_by_name(knowledge_name)
        if existing:
            return {"success": False, "message": "知识库名称已存在"}

        knowledge = InsightKnowledge(knowledge_name=knowledge_name, file_id=file_id)
        saved = self.knowledge_dao.save(knowledge)
        return {"success": True, "message": "创建成功", "data": self._to_dict(saved)}

    def update_knowledge(self, id: int, knowledge_name: str = None, file_id: str = None) -> Dict:
        """更新知识库"""
        knowledge = self.find_by_id(id)
        if not knowledge:
            return {"success": False, "message": "知识库不存在"}

        if knowledge_name:
            knowledge.knowledge_name = knowledge_name
        if file_id:
            knowledge.file_id = file_id

        updated = self.knowledge_dao.update(knowledge)
        return {"success": True, "message": "更新成功", "data": self._to_dict(updated)}

    def _to_dict(self, knowledge: InsightKnowledge) -> dict:
        return {
            "id": knowledge.id,
            "knowledge_name": knowledge.knowledge_name,
            "file_id": knowledge.file_id,
            "created_at": knowledge.created_at.isoformat() if knowledge.created_at else None
        }
