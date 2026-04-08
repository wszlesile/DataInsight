from flask import Blueprint, jsonify, request

from config.config import Config
from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from service.insight_namespace_service import InsightNamespaceService
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils.response import Result


def create_insight_namespace_controller() -> Blueprint:
    blueprint = Blueprint('insight_namespace', __name__, url_prefix='/api/insight/namespaces')
    controller = InsightNamespaceController(blueprint)

    blueprint.route('', methods=['GET'])(controller.list_namespaces)
    blueprint.route('', methods=['POST'])(controller.create_namespace)
    blueprint.route('/<int:namespace_id>/datasources', methods=['GET'])(controller.list_datasources)
    blueprint.route('/<int:namespace_id>/datasources/upload', methods=['POST'])(controller.upload_datasource_file)
    blueprint.route('/<int:namespace_id>/datasources/<int:datasource_id>', methods=['DELETE'])(controller.delete_datasource)
    blueprint.route('/<int:namespace_id>', methods=['PUT'])(controller.rename_namespace)
    blueprint.route('/<int:namespace_id>', methods=['DELETE'])(controller.delete_namespace)
    return blueprint


class InsightNamespaceController(BaseController):
    """洞察空间接口控制器。"""

    def _get_username(self) -> str:
        user_context = get_current_user_context()
        return user_context.username if user_context else 'anonymous'

    def list_namespaces(self):
        session = SessionLocal()
        try:
            service = InsightNamespaceService(session)
            namespaces = service.list_namespaces(self._get_username())
            return jsonify(Result.success(data=namespaces).to_dict())
        finally:
            session.close()

    def create_namespace(self):
        data = self.get_json_data()
        name = data.get('name', '')
        session = SessionLocal()
        try:
            service = InsightNamespaceService(session)
            result = service.create_namespace(
                username=self._get_username(),
                name=name,
            )
            return jsonify(Result.success(
                data=result,
                message='洞察空间已创建',
                code=201,
            ).to_dict()), 201
        except ValueError as exc:
            session.rollback()
            return jsonify(Result.error(str(exc), 400).to_dict()), 400
        finally:
            session.close()

    def list_datasources(self, namespace_id: int):
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            rows = service.find_by_namespace_id(namespace_id, insight_conversation_id)
            return jsonify(Result.success(data=rows).to_dict())
        finally:
            session.close()

    def upload_datasource_file(self, namespace_id: int):
        upload_file = request.files.get('file')
        if upload_file is None:
            return self.error_response('请选择要上传的文件')

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.upload_file_datasource_to_namespace(
                insight_namespace_id=namespace_id,
                upload_file=upload_file,
                upload_dir=Config.UPLOAD_DIR,
            )
            if result['success']:
                return jsonify(Result.success(data=result['data'], message=result['message'], code=201).to_dict()), 201
            return self.error_response(result['message'], 400)
        finally:
            session.close()

    def delete_datasource(self, namespace_id: int, datasource_id: int):
        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.delete_namespace_datasource(namespace_id, datasource_id)
            if result['success']:
                return jsonify(Result.success(message=result['message']).to_dict())
            return self.error_response(result['message'], 400)
        finally:
            session.close()

    def delete_namespace(self, namespace_id: int):
        session = SessionLocal()
        try:
            service = InsightNamespaceService(session)
            deleted = service.delete_namespace(
                username=self._get_username(),
                namespace_id=namespace_id,
            )
            if not deleted:
                return jsonify(Result.error('洞察空间不存在', 404).to_dict()), 404
            return jsonify(Result.success(message='洞察空间已删除').to_dict())
        finally:
            session.close()

    def rename_namespace(self, namespace_id: int):
        data = self.get_json_data()
        name = data.get('name', '')
        session = SessionLocal()
        try:
            service = InsightNamespaceService(session)
            namespace = service.rename_namespace(
                username=self._get_username(),
                namespace_id=namespace_id,
                name=name,
            )
            if namespace is None:
                return jsonify(Result.error('洞察空间不存在', 404).to_dict()), 404
            return jsonify(Result.success(data=namespace, message='洞察空间已更新').to_dict())
        except ValueError as exc:
            session.rollback()
            return jsonify(Result.error(str(exc), 400).to_dict()), 400
        finally:
            session.close()
