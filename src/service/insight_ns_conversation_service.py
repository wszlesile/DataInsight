from typing import Optional, Dict, Any, List

from dao.insight_ns_conversation_dao import InsightNsConversationDAO
from model import InsightNsConversation
from service.base_service import BaseService


class InsightNsConversationService(BaseService[InsightNsConversationDAO]):
    """会话业务逻辑层"""

    def __init__(self, dao: InsightNsConversationDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_conversation_service = self
        super().__init__(dao)

    @property
    def conversation_dao(self) -> InsightNsConversationDAO:
        return self._get_dao()

    def find_by_namespace_id(self, insight_namespace_id: int) -> List[InsightNsConversation]:
        return self.conversation_dao.find_by_namespace_id(insight_namespace_id)

    def find_by_username(self, username: str) -> List[InsightNsConversation]:
        return self.conversation_dao.find_by_username(username)

    def create_conversation(self, username: str, insight_namespace_id: int, user_message: str, insight_result: str) -> Dict:
        """创建会话"""
        conversation = InsightNsConversation(
            username=username,
            insight_namespace_id=insight_namespace_id,
            user_message=user_message,
            insight_result=insight_result
        )
        saved = self.conversation_dao.save(conversation)
        return {"success": True, "message": "创建成功", "data": self._to_dict(saved)}

    def update_conversation(self, id: int, user_message: str = None, insight_result: str = None) -> Dict:
        """更新会话"""
        conversation = self.find_by_id(id)
        if not conversation:
            return {"success": False, "message": "会话不存在"}

        if user_message:
            conversation.user_message = user_message
        if insight_result:
            conversation.insight_result = insight_result

        updated = self.conversation_dao.update(conversation)
        return {"success": True, "message": "更新成功", "data": self._to_dict(updated)}

    def delete_conversation(self, id: int) -> Dict:
        """删除会话"""
        if self.delete(id):
            return {"success": True, "message": "删除成功"}
        return {"success": False, "message": "会话不存在"}

    def _to_dict(self, conversation: InsightNsConversation) -> dict:
        return {
            "id": conversation.id,
            "username": conversation.username,
            "insight_namespace_id": conversation.insight_namespace_id,
            "user_message": conversation.user_message,
            "insight_result": conversation.insight_result,
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None
        }