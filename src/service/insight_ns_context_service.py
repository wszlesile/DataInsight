from typing import Optional, Dict, Any, List

from dao.insight_ns_context_dao import InsightNsContextDAO
from model import InsightNsContext
from service.base_service import BaseService


class InsightNsContextService(BaseService[InsightNsContextDAO]):
    """会话上下文业务逻辑层"""

    def __init__(self, dao: InsightNsContextDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_context_service = self
        super().__init__(dao)

    @property
    def context_dao(self) -> InsightNsContextDAO:
        return self._get_dao()

    def find_by_conversation_id(self, insight_conversation_id: int) -> List[InsightNsContext]:
        return self.context_dao.find_by_conversation_id(insight_conversation_id)

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsContext]:
        return self.context_dao.find_by_namespace_id(insight_namespace_id)

    def create_context(self, username: str, insight_namespace_id: int, insight_conversation_id: int,
                       type: int, content: str, insight_result: str = "") -> Dict:
        """创建上下文"""
        context = InsightNsContext(
            username=username,
            insight_namespace_id=insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            type=type,
            content=content,
            insight_result=insight_result
        )
        saved = self.context_dao.save(context)
        return {"success": True, "message": "创建成功", "data": self._to_dict(saved)}

    def delete_by_conversation_id(self, insight_conversation_id: int) -> Dict:
        """删除会话的所有上下文"""
        self.context_dao.delete_by_conversation_id(insight_conversation_id)
        return {"success": True, "message": "删除成功"}

    def _to_dict(self, context: InsightNsContext) -> dict:
        return {
            "id": context.id,
            "username": context.username,
            "insight_namespace_id": context.insight_namespace_id,
            "insight_conversation_id": context.insight_conversation_id,
            "type": context.type,
            "content": context.content,
            "insight_result": context.insight_result,
            "created_at": context.created_at.isoformat() if context.created_at else None
        }
