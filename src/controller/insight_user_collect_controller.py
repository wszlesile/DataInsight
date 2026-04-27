from flask import Blueprint, jsonify

from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.insight_user_collect_service import InsightUserCollectService
from utils.response import Result


def create_insight_user_collect_controller() -> Blueprint:
    blueprint = Blueprint('insight_user_collect', __name__, url_prefix='/api/insight/collects')
    controller = InsightUserCollectController(blueprint)

    blueprint.route('', methods=['GET'])(controller.list_collects)
    blueprint.route('', methods=['POST'])(controller.create_collect)
    blueprint.route('', methods=['DELETE'])(controller.remove_collect)
    return blueprint


class InsightUserCollectController(BaseController):
    """用户收藏相关接口控制器。"""

    def _get_username(self) -> str:
        user_context = get_current_user_context()
        return user_context.username if user_context else 'anonymous'

    def _get_authorization(self) -> str:
        user_context = get_current_user_context()
        return user_context.token if user_context else ''

    def list_collects(self):
        """按用户维度查询当前用户全部收藏。"""
        session = SessionLocal()
        try:
            service = InsightUserCollectService(session)
            collects = service.list_collects(username=self._get_username())
            return jsonify(Result.success(data=collects).to_dict())
        finally:
            session.close()

    def create_collect(self):
        """创建一条收藏记录。"""
        data = self.get_json_data()
        collect_type = data.get('collect_type', '')
        target_id = data.get('target_id', 0)
        if not collect_type or not target_id:
            return self.error_response('缺少 collect_type 或 target_id')

        session = SessionLocal()
        try:
            service = InsightUserCollectService(session)
            username = self._get_username()
            collect = service.create_collect(
                username=username,
                collect_type=collect_type,
                target_id=target_id,
                title=data.get('title', ''),
                summary_text=data.get('summary_text', ''),
                namespace_id=data.get('insight_namespace_id', 0),
                conversation_id=data.get('insight_conversation_id', 0),
                message_id=data.get('insight_message_id', data.get('insight_context_id', 0)),
                artifact_id=data.get('insight_artifact_id', 0),
            )
            service.report_collect_statistics(authorization=self._get_authorization())
            return jsonify(Result.success(data=collect, message='收藏成功').to_dict())
        finally:
            session.close()

    def remove_collect(self):
        """取消收藏。"""
        data = self.get_json_data()
        collect_type = data.get('collect_type', '')
        target_id = data.get('target_id', 0)
        if not collect_type or not target_id:
            return self.error_response('缺少 collect_type 或 target_id')

        session = SessionLocal()
        try:
            service = InsightUserCollectService(session)
            username = self._get_username()
            removed = service.remove_collect(
                username=username,
                collect_type=collect_type,
                target_id=target_id,
            )
            if not removed:
                return self.error_response('收藏不存在', 404)
            service.report_collect_statistics(authorization=self._get_authorization())
            return jsonify(Result.success(message='取消收藏成功').to_dict())
        finally:
            session.close()
