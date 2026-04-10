from flask import Blueprint, jsonify, request

from api import supos_kernel_api
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
    blueprint.route('/<int:namespace_id>/uns/tree', methods=['POST'])(controller.fetch_uns_tree)
    blueprint.route('/<int:namespace_id>/uns/selections', methods=['GET'])(controller.list_uns_selections)
    blueprint.route('/<int:namespace_id>/uns/selections', methods=['DELETE'])(controller.remove_uns_selection)
    blueprint.route('/<int:namespace_id>/datasources', methods=['GET'])(controller.list_datasources)
    blueprint.route('/<int:namespace_id>/datasources/upload', methods=['POST'])(controller.upload_datasource_file)
    blueprint.route('/<int:namespace_id>/datasources/import-uns', methods=['POST'])(controller.import_uns_datasources)
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
        """查询当前用户的空间列表。"""
        session = SessionLocal()
        try:
            service = InsightNamespaceService(session)
            namespaces = service.list_namespaces(self._get_username())
            return jsonify(Result.success(data=namespaces).to_dict())
        finally:
            session.close()

    def create_namespace(self):
        """创建空间，并同步创建一条默认会话。"""
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
        """
        查询空间级数据源列表。

        如果传入 `insight_conversation_id`，后端会直接在结果中标记当前会话的
        绑定状态，前端无需再拼接第二份绑定列表。
        """
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            rows = service.find_by_namespace_id(namespace_id, insight_conversation_id)
            return jsonify(Result.success(data=rows).to_dict())
        finally:
            session.close()

    def fetch_uns_tree(self, namespace_id: int):
        """代理查询第三方 UNS 树，供前端同源访问。"""
        _ = namespace_id
        user_context = get_current_user_context()
        data = self.get_json_data()

        try:
            result = supos_kernel_api.fetch_uns_tree_nodes(
                authorization=user_context.token if user_context else '',
                parent_id=data.get('parentId', '0'),
                page_no=data.get('pageNo', 1),
                page_size=data.get('pageSize', 100),
                keyword=data.get('keyword', ''),
                search_type=data.get('searchType', 1),
            )
            return jsonify(Result.success(data=result).to_dict())
        except Exception as exc:
            return self.error_response(str(exc), 400)

    def list_uns_selections(self, namespace_id: int):
        """查询当前会话已选择的 UNS 树节点，用于前端回显。"""
        _ = namespace_id
        insight_conversation_id = request.args.get('insight_conversation_id', type=int)
        if not insight_conversation_id:
            return self.error_response('缺少 insight_conversation_id 参数')

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            rows = service.list_uns_selections(insight_conversation_id)
            return jsonify(Result.success(data=rows).to_dict())
        finally:
            session.close()

    def remove_uns_selection(self, namespace_id: int):
        """取消当前会话的 UNS 树节点选择，并同步解绑对应数据源。"""
        data = self.get_json_data()
        insight_conversation_id = int(data.get('insight_conversation_id') or 0)
        uns_node_id = str(data.get('uns_node_id') or '').strip()
        if not insight_conversation_id or not uns_node_id:
            return self.error_response('缺少必要参数')

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.remove_uns_selection_from_conversation(
                insight_namespace_id=namespace_id,
                insight_conversation_id=insight_conversation_id,
                uns_node_id=uns_node_id,
            )
            if result['success']:
                return jsonify(Result.success(data=result.get('data'), message=result['message']).to_dict())
            return self.error_response(result['message'], 400)
        finally:
            session.close()

    def upload_datasource_file(self, namespace_id: int):
        """上传文件到空间，并转换成一条空间级数据源。"""
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

    def import_uns_datasources(self, namespace_id: int):
        """把选中的 UNS 节点批量导入为共享 table 数据源，并绑定到当前会话。"""
        user_context = get_current_user_context()
        data = self.get_json_data()
        nodes = data.get('nodes') or []
        ids = data.get('ids') or []
        insight_conversation_id = int(data.get('insight_conversation_id') or 0)

        session = SessionLocal()
        try:
            service = InsightNsRelDatasourceService(session)
            result = service.import_uns_nodes_to_namespace(
                insight_namespace_id=namespace_id,
                insight_conversation_id=insight_conversation_id,
                nodes=nodes,
                authorization=user_context.token if user_context else '',
                lake_rds_database_name=user_context.lake_rds_database_name if user_context else '',
                ids=ids,
            )
            if result['success']:
                return jsonify(Result.success(data=result['data'], message=result['message']).to_dict())
            return self.error_response(result['message'], 400)
        finally:
            session.close()

    def delete_datasource(self, namespace_id: int, datasource_id: int):
        """删除空间级数据源；若被会话引用则要求先解绑。"""
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
        """删除空间，并一并软删除其下会话和上下文数据。"""
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
        """更新空间名称。"""
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
