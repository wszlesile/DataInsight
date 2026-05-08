from typing import Any

from sqlalchemy.orm import Session

from model import InsightUserLlmConfig


DEFAULT_LLM_PROVIDER = 'supos_llm_gateway'


class LlmModelSelectionService:
    """Manage user-level LLM model selection."""

    def __init__(self, session: Session):
        self.session = session

    def get_user_selection(
        self,
        username: str,
        provider: str = DEFAULT_LLM_PROVIDER,
    ) -> InsightUserLlmConfig | None:
        username = (username or '').strip()
        provider = (provider or DEFAULT_LLM_PROVIDER).strip()
        if not username:
            return None
        return self.session.query(InsightUserLlmConfig).filter(
            InsightUserLlmConfig.username == username,
            InsightUserLlmConfig.provider == provider,
            InsightUserLlmConfig.is_deleted == 0,
        ).first()

    def get_user_selected_model_id(
        self,
        username: str,
        provider: str = DEFAULT_LLM_PROVIDER,
    ) -> str:
        selection = self.get_user_selection(username, provider)
        return str(getattr(selection, 'model_id', '') or '').strip() if selection else ''

    def build_selectable_models(
        self,
        username: str,
        models: list[dict[str, Any]],
        provider: str = DEFAULT_LLM_PROVIDER,
    ) -> list[dict[str, Any]]:
        rows = [dict(item) for item in (models or []) if isinstance(item, dict)]
        if not rows:
            return []

        selected_model_id = self.get_user_selected_model_id(username, provider)
        available_ids = {str(item.get('id') or '') for item in rows}
        if selected_model_id not in available_ids:
            selected_model_id = str(rows[0].get('id') or '')

        for item in rows:
            item['selected'] = str(item.get('id') or '') == selected_model_id
        return rows

    def validate_model_id(self, models: list[dict[str, Any]], model_id: str) -> None:
        candidate = (model_id or '').strip()
        if not candidate:
            raise ValueError('model_id 不能为空')
        available_ids = {str(item.get('id') or '') for item in (models or []) if isinstance(item, dict)}
        if candidate not in available_ids:
            raise ValueError('model_id 不在平台可用模型列表中')

    def upsert_user_selection(
        self,
        username: str,
        model_id: str,
        provider: str = DEFAULT_LLM_PROVIDER,
    ) -> dict[str, Any]:
        username = (username or '').strip()
        model_id = (model_id or '').strip()
        provider = (provider or DEFAULT_LLM_PROVIDER).strip()
        if not username:
            raise ValueError('username 不能为空')
        if not model_id:
            raise ValueError('model_id 不能为空')

        selection = self.get_user_selection(username, provider)
        if selection is None:
            selection = InsightUserLlmConfig(
                username=username,
                provider=provider,
                model_id=model_id,
                is_deleted=0,
            )
            self.session.add(selection)
        else:
            selection.model_id = model_id

        self.session.commit()
        self.session.refresh(selection)
        return selection.to_dict()
