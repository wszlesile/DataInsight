from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from model import InsightNamespace, InsightNsConversation, InsightNsConversationPrompt
from utils import to_int


CONVERSATION_PROMPT_MAX_CHARS = 20000


class InsightNsConversationPromptService:
    """Manage conversation-scoped supplemental system prompts."""

    def __init__(self, session: Session):
        self.session = session

    def get_prompt(
        self,
        username: str,
        namespace_id: Any,
        conversation_id: Any,
    ) -> dict[str, Any] | None:
        conversation = self._get_accessible_conversation(username, namespace_id, conversation_id)
        if conversation is None:
            return None

        prompt = self._get_active_prompt(conversation.insight_namespace_id, conversation.id)
        if prompt is None:
            return {
                "id": 0,
                "insight_namespace_id": conversation.insight_namespace_id,
                "insight_conversation_id": conversation.id,
                "prompt_content": "",
                "is_enabled": 1,
                "is_deleted": 0,
                "created_at": None,
                "updated_at": None,
            }
        return prompt.to_dict()

    def upsert_prompt(
        self,
        username: str,
        namespace_id: Any,
        conversation_id: Any,
        prompt_content: str,
    ) -> dict[str, Any] | None:
        conversation = self._get_accessible_conversation(username, namespace_id, conversation_id)
        if conversation is None:
            return None

        normalized_content = (prompt_content or '').strip()
        if len(normalized_content) > CONVERSATION_PROMPT_MAX_CHARS:
            raise ValueError(f'会话提示词不能超过 {CONVERSATION_PROMPT_MAX_CHARS} 字')

        now = datetime.now()
        prompt = self._get_prompt(conversation.insight_namespace_id, conversation.id)
        if prompt is None:
            prompt = InsightNsConversationPrompt(
                insight_namespace_id=conversation.insight_namespace_id,
                insight_conversation_id=conversation.id,
                prompt_content=normalized_content,
                is_enabled=1,
                is_deleted=0,
                created_at=now,
                updated_at=now,
            )
            self.session.add(prompt)
        else:
            prompt.prompt_content = normalized_content
            prompt.is_enabled = 1
            prompt.is_deleted = 0
            prompt.updated_at = now

        self.session.commit()
        self.session.refresh(prompt)
        return prompt.to_dict()

    def delete_prompt(self, username: str, namespace_id: Any, conversation_id: Any) -> bool | None:
        conversation = self._get_accessible_conversation(username, namespace_id, conversation_id)
        if conversation is None:
            return None

        prompt = self._get_active_prompt(conversation.insight_namespace_id, conversation.id)
        if prompt is None:
            return True

        prompt.prompt_content = ''
        prompt.is_enabled = 0
        prompt.is_deleted = 1
        prompt.updated_at = datetime.now()
        self.session.commit()
        return True

    def get_enabled_prompt_content(self, namespace_id: Any, conversation_id: Any) -> str:
        namespace_id_int = to_int(namespace_id, 0)
        conversation_id_int = to_int(conversation_id, 0)
        if namespace_id_int <= 0 or conversation_id_int <= 0:
            return ''

        prompt = self._get_active_prompt(namespace_id_int, conversation_id_int)
        if prompt is None or not int(prompt.is_enabled or 0):
            return ''
        return (prompt.prompt_content or '').strip()

    def _get_accessible_conversation(
        self,
        username: str,
        namespace_id: Any,
        conversation_id: Any,
    ) -> InsightNsConversation | None:
        namespace_id_int = to_int(namespace_id, 0)
        conversation_id_int = to_int(conversation_id, 0)
        if namespace_id_int <= 0 or conversation_id_int <= 0:
            return None

        return self.session.query(InsightNsConversation).join(
            InsightNamespace,
            InsightNamespace.id == InsightNsConversation.insight_namespace_id,
        ).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.insight_namespace_id == namespace_id_int,
            InsightNsConversation.is_deleted == 0,
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).first()

    def _get_active_prompt(self, namespace_id: int, conversation_id: int) -> InsightNsConversationPrompt | None:
        return self.session.query(InsightNsConversationPrompt).filter(
            InsightNsConversationPrompt.insight_namespace_id == namespace_id,
            InsightNsConversationPrompt.insight_conversation_id == conversation_id,
            InsightNsConversationPrompt.is_deleted == 0,
        ).order_by(InsightNsConversationPrompt.id.desc()).first()

    def _get_prompt(self, namespace_id: int, conversation_id: int) -> InsightNsConversationPrompt | None:
        return self.session.query(InsightNsConversationPrompt).filter(
            InsightNsConversationPrompt.insight_namespace_id == namespace_id,
            InsightNsConversationPrompt.insight_conversation_id == conversation_id,
        ).order_by(InsightNsConversationPrompt.id.desc()).first()
