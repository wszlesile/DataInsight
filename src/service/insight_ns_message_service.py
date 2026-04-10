from typing import Any

from dao.insight_ns_message_dao import InsightNsMessageDAO
from model import InsightNsMessage
from service.base_service import BaseService


class InsightNsMessageService(BaseService[InsightNsMessageDAO]):
    """会话消息的增删改查服务。"""

    def __init__(self, dao: InsightNsMessageDAO, beanFactory: Any = None):
        if beanFactory:
            beanFactory.insight_ns_message_service = self
        super().__init__(dao)

    @property
    def message_dao(self) -> InsightNsMessageDAO:
        return self._get_dao()

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[InsightNsMessage]:
        return self.message_dao.find_by_conversation_id(insight_conversation_id)

    def find_by_namespace_id(self, insight_namespace_id: int) -> list[InsightNsMessage]:
        return self.message_dao.find_by_namespace_id(insight_namespace_id)

    def create_message(
        self,
        insight_namespace_id: int,
        insight_conversation_id: int,
        message_type: int,
        content: str,
        insight_result: str = "",
    ) -> dict[str, Any]:
        message = InsightNsMessage(
            insight_namespace_id=insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            type=message_type,
            content=content,
            insight_result=insight_result,
        )
        saved = self.message_dao.save(message)
        return {"success": True, "message": "创建成功", "data": self._to_dict(saved)}

    def delete_by_conversation_id(self, insight_conversation_id: int) -> dict[str, Any]:
        self.message_dao.delete_by_conversation_id(insight_conversation_id)
        return {"success": True, "message": "删除成功"}

    def _to_dict(self, message: InsightNsMessage) -> dict[str, Any]:
        return {
            "id": message.id,
            "insight_namespace_id": message.insight_namespace_id,
            "insight_conversation_id": message.insight_conversation_id,
            "type": message.type,
            "content": message.content,
            "insight_result": message.insight_result,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
