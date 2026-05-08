from flask import Blueprint, jsonify

from api import supos_kernel_api
from config.config import Config
from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.llm_model_service import LlmModelSelectionService
from service.user_auth_service import user_auth_service
from utils import logger
from utils.response import Result


def create_llm_controller() -> Blueprint:
    blueprint = Blueprint('llm', __name__, url_prefix='/api/llm')
    controller = LlmController(blueprint)

    blueprint.route('/models', methods=['GET'])(controller.list_models)
    blueprint.route('/models/selected', methods=['PUT'])(controller.select_model)
    return blueprint


class LlmController(BaseController):
    """LLM gateway proxy endpoints."""

    def _get_llm_gateway_authorization(self, user_context) -> str:
        api_key = (Config.SUPOS_LLM_GATEWAY_API_KEY or '').strip()
        if api_key:
            return api_key if api_key.startswith('Bearer ') else f'Bearer {api_key}'
        return user_context.token if user_context else ''

    def list_models(self):
        user_context = get_current_user_context()
        session = SessionLocal()
        try:
            payload = supos_kernel_api.fetch_llm_gateway_models(
                authorization=self._get_llm_gateway_authorization(user_context),
            )
            models = payload.get('data') if isinstance(payload, dict) else []
            service = LlmModelSelectionService(session)
            result = service.build_selectable_models(
                user_context.username if user_context else '',
                models if isinstance(models, list) else [],
            )
            return jsonify(Result.success(data=result).to_dict())
        except Exception as exc:
            logger.warning("获取 LLM 模型列表失败: %s", exc, exc_info=True)
            return self.error_response(str(exc), 400)
        finally:
            session.close()

    def select_model(self):
        user_context = get_current_user_context()
        data = self.get_json_data()
        model_id = str(data.get('model_id') or '').strip()
        session = SessionLocal()
        try:
            payload = supos_kernel_api.fetch_llm_gateway_models(
                authorization=self._get_llm_gateway_authorization(user_context),
            )
            models = payload.get('data') if isinstance(payload, dict) else []
            if not isinstance(models, list):
                models = []

            service = LlmModelSelectionService(session)
            service.validate_model_id(models, model_id)
            service.upsert_user_selection(
                username=user_context.username if user_context else '',
                model_id=model_id,
            )

            if user_context:
                user_context.selected_llm_model_id = model_id
                user_auth_service.invalidate_user_context(user_context.token)

            result = service.build_selectable_models(
                user_context.username if user_context else '',
                models,
            )
            return jsonify(Result.success(data=result).to_dict())
        except ValueError as exc:
            session.rollback()
            return self.error_response(str(exc), 400)
        except Exception as exc:
            session.rollback()
            logger.warning("切换 LLM 模型失败: %s", exc, exc_info=True)
            return self.error_response(str(exc), 400)
        finally:
            session.close()
