import json
from io import BytesIO

from flask import Blueprint, Response, jsonify, request, send_file

from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.insight_ns_conversation_service import InsightNsConversationService
from utils.response import Result


def create_insight_ns_conversation_controller() -> Blueprint:
    blueprint = Blueprint('insight_ns_conversation', __name__, url_prefix='/api/insight/conversations')
    controller = InsightNsConversationController(blueprint)

    blueprint.route('', methods=['GET'])(controller.list_conversations)
    blueprint.route('/<int:conversation_id>', methods=['PUT'])(controller.rename_conversation)
    blueprint.route('/<int:conversation_id>/history', methods=['GET'])(controller.get_history)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>', methods=['GET'])(controller.get_turn_detail)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/export/pdf', methods=['POST'])(controller.export_turn_pdf)
    blueprint.route('/<int:conversation_id>/turns/<int:turn_id>/rerun/stream', methods=['POST'])(controller.rerun_turn_stream)
    return blueprint


class InsightNsConversationController(BaseController):
    """会话历史、详情和重命名相关的查询控制器。"""

    def _get_username(self) -> str:
        user_context = get_current_user_context()
        return user_context.username if user_context else 'anonymous'

    def list_conversations(self):
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

    def get_history(self, conversation_id: int):
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
        from agent.invoker import stream_rerun_turn
        username = self._get_username()

        def generate():
            for event in stream_rerun_turn(
                username=username,
                conversation_id=conversation_id,
                turn_id=turn_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return Response(generate(), mimetype='text/event-stream')
