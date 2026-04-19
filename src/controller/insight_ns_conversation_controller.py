import json
from io import BytesIO
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.insight_ns_conversation_service import InsightNsConversationService
from utils.response import Result


def create_insight_ns_conversation_controller() -> Blueprint:
    blueprint = Blueprint('insight_ns_conversation', __name__, url_prefix='/api/insight/conversations')
    controller = InsightNsConversationController(blueprint)

    blueprint.route('', methods=['POST'])(controller.create_conversation)
    blueprint.route('', methods=['GET'])(controller.list_conversations)
    blueprint.route('/<int:conversation_id>', methods=['PUT'])(controller.rename_conversation)
    blueprint.route('/<int:conversation_id>', methods=['DELETE'])(controller.delete_conversation)
    blueprint.route('/<int:conversation_id>/history', methods=['GET'])(controller.get_history)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>', methods=['GET'])(controller.get_turn_detail)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/export/pdf', methods=['POST'])(controller.export_turn_pdf)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/rerun/stream', methods=['POST'])(controller.rerun_turn_stream)
    return blueprint


class InsightNsConversationController(BaseController):
    """会话列表、历史、详情、导出与原轮重跑接口。"""

    def _get_username(self) -> str:
        user_context = get_current_user_context()
        return user_context.username if user_context else 'anonymous'

    def _build_request_runtime_context(self) -> tuple[str, dict[str, Any]]:
        user_context = get_current_user_context()
        database_context = getattr(user_context, 'database_context', None)
        return (
            getattr(user_context, 'token', '') or '',
            {
                'host': getattr(database_context, 'host', '') or '',
                'port': getattr(database_context, 'port', '') or '',
                'user': getattr(database_context, 'user', '') or '',
                'password': getattr(database_context, 'password', '') or '',
                'lake_rds_database_name': getattr(database_context, 'lake_rds_database_name', '') or '',
            } if database_context else {},
        )

    def create_conversation(self):
        """在指定空间下创建一条新的空会话。"""
        data = self.get_json_data()
        namespace_id = data.get('namespace_id', 0)
        title = data.get('title', '')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversation = service.create_conversation(
                username=self._get_username(),
                namespace_id=namespace_id,
                title=title,
            )
            if conversation is None:
                return self.error_response('洞察空间不存在', 404)
            return jsonify(Result.success(data=conversation, message='会话已创建', code=201).to_dict()), 201
        finally:
            session.close()

    def list_conversations(self):
        """按空间查询会话列表。"""
        namespace_id = request.args.get('namespace_id', '0')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversations = service.list_conversations(
                username=self._get_username(),
                namespace_id=namespace_id,
            )
            return jsonify(Result.success(data=conversations).to_dict())
        finally:
            session.close()

    def rename_conversation(self, conversation_id: int):
        """重命名会话。"""
        data = self.get_json_data()
        title = data.get('title', '')
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            conversation = service.rename_conversation(
                username=self._get_username(),
                conversation_id=conversation_id,
                title=title,
            )
            if conversation is None:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(data=conversation, message='会话标题已更新').to_dict())
        finally:
            session.close()

    def delete_conversation(self, conversation_id: int):
        """软删除会话及其会话级上下文数据。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            deleted = service.delete_conversation(
                username=self._get_username(),
                conversation_id=conversation_id,
            )
            if not deleted:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(message='会话已删除').to_dict())
        finally:
            session.close()

    def get_history(self, conversation_id: int):
        """返回主聊天区使用的轮次历史。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            history = service.get_conversation_history(
                username=self._get_username(),
                conversation_id=conversation_id,
            )
            if history is None:
                return self.error_response('会话不存在', 404)
            return jsonify(Result.success(data=history).to_dict())
        finally:
            session.close()

    def get_turn_detail(self, conversation_id: int, turn_id: int):
        """返回单轮详情，包含消息、执行与产物。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            detail = service.get_turn_detail(
                username=self._get_username(),
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if detail is None:
                return self.error_response('轮次详情不存在', 404)
            return jsonify(Result.success(data=detail).to_dict())
        finally:
            session.close()

    def export_turn_pdf(self, conversation_id: int, turn_id: int):
        """导出单轮分析结果 PDF。"""
        session = SessionLocal()
        try:
            service = InsightNsConversationService(session)
            export_result = service.export_turn_pdf(
                username=self._get_username(),
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            if export_result is None:
                return self.error_response('轮次详情不存在', 404)
            pdf_bytes, filename = export_result
            return send_file(
                BytesIO(pdf_bytes),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename,
            )
        finally:
            session.close()

    def rerun_turn_stream(self, conversation_id: int, turn_id: int):
        """在同一轮次内流式重跑分析，不新增新轮次。"""
        from agent.invoker import stream_rerun_turn
        username = self._get_username()
        auth_token, database_context = self._build_request_runtime_context()

        def generate():
            for event in stream_rerun_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
                auth_token=auth_token,
                database_context=database_context,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return Response(generate(), mimetype='text/event-stream')
