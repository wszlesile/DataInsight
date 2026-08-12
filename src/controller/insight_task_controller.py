import hmac
import socket

from flask import Blueprint, jsonify, request

from config import Config
from config.database import SessionLocal
from controller.base_controller import BaseController
from dto import get_current_user_context
from insight_task.service import InsightTaskService
from utils.response import Result


def create_insight_task_controller() -> Blueprint:
    blueprint = Blueprint('insight_task', __name__, url_prefix='/api/insight/tasks')
    controller = InsightTaskController(blueprint)
    blueprint.route('', methods=['GET'])(controller.list_tasks)
    blueprint.route('', methods=['POST'])(controller.create_task)
    blueprint.route('/<string:task_id>', methods=['GET'])(controller.get_task)
    blueprint.route('/<string:task_id>', methods=['PUT'])(controller.update_task)
    blueprint.route('/<string:task_id>', methods=['DELETE'])(controller.delete_task)
    blueprint.route('/<string:task_id>/runs', methods=['GET'])(controller.list_runs)
    blueprint.route('/<string:task_id>/runs', methods=['POST'])(controller.create_run)
    blueprint.route('/<string:task_id>/runs/<string:run_id>', methods=['GET'])(controller.get_run)
    blueprint.route('/internal/runs/<string:run_id>/execute', methods=['POST'])(controller.execute_internal_run)
    return blueprint


class InsightTaskController(BaseController):
    def _username(self) -> str:
        context = get_current_user_context()
        return context.username if context else 'anonymous'

    def list_tasks(self):
        session = SessionLocal()
        try:
            data = InsightTaskService(session).list_tasks(username=self._username())
            return jsonify(Result.success(data=data).to_dict())
        finally:
            session.close()

    def create_task(self):
        payload = self.get_json_data()
        session = SessionLocal()
        try:
            try:
                data = InsightTaskService(session).create_task(
                    username=self._username(),
                    source_execution_id=payload.get('source_execution_id'),
                    name=payload.get('name', ''),
                    description=payload.get('description', ''),
                    schedule=payload.get('schedule') or {},
                )
            except ValueError as exc:
                session.rollback()
                return self.error_response(str(exc), 400)
            return jsonify(Result.success(data=data, message='洞察任务已创建', code=201).to_dict()), 201
        finally:
            session.close()

    def get_task(self, task_id: str):
        session = SessionLocal()
        try:
            data = InsightTaskService(session).get_task(username=self._username(), task_id=task_id)
            if data is None:
                return self.error_response('洞察任务不存在', 404)
            return jsonify(Result.success(data=data).to_dict())
        finally:
            session.close()

    def update_task(self, task_id: str):
        payload = self.get_json_data()
        session = SessionLocal()
        try:
            try:
                data = InsightTaskService(session).update_task(
                    username=self._username(),
                    task_id=task_id,
                    name=payload['name'] if 'name' in payload else None,
                    description=payload['description'] if 'description' in payload else None,
                    schedule=payload['schedule'] if 'schedule' in payload else None,
                    status=payload['status'] if 'status' in payload else None,
                )
            except ValueError as exc:
                session.rollback()
                return self.error_response(str(exc), 400)
            if data is None:
                return self.error_response('洞察任务不存在', 404)
            return jsonify(Result.success(data=data, message='洞察任务已更新').to_dict())
        finally:
            session.close()

    def delete_task(self, task_id: str):
        session = SessionLocal()
        try:
            deleted = InsightTaskService(session).delete_task(username=self._username(), task_id=task_id)
            if not deleted:
                return self.error_response('洞察任务不存在', 404)
            return jsonify(Result.success(message='洞察任务已删除').to_dict())
        finally:
            session.close()

    def list_runs(self, task_id: str):
        session = SessionLocal()
        try:
            data = InsightTaskService(session).list_runs_page(
                username=self._username(),
                task_id=task_id,
                page_no=request.args.get('page_no', 1, type=int) or 1,
                page_size=request.args.get('page_size', 50, type=int) or 50,
            )
            if data is None:
                return self.error_response('洞察任务不存在', 404)
            return jsonify(Result.success(data=data).to_dict())
        finally:
            session.close()

    def create_run(self, task_id: str):
        payload = self.get_json_data()
        context = get_current_user_context()
        if context is None:
            return self.error_response('缺少当前用户上下文', 401)
        session = SessionLocal()
        try:
            try:
                data = InsightTaskService(session).execute_manual_run(
                    username=self._username(),
                    task_id=task_id,
                    parameters=payload.get('analysis_parameters'),
                    runtime_user_context=self._runtime_user_context(context),
                    worker_id=f'{socket.gethostname()}:manual',
                    include_result=False,
                )
            except ValueError as exc:
                session.rollback()
                return self.error_response(str(exc), 400)
            if data is None:
                return self.error_response('洞察任务不存在', 404)
            return jsonify(Result.success(data=data, message='任务运行已完成').to_dict())
        finally:
            session.close()

    def get_run(self, task_id: str, run_id: str):
        session = SessionLocal()
        try:
            data = InsightTaskService(session).get_run(
                username=self._username(),
                task_id=task_id,
                run_id=run_id,
            )
            if data is None:
                return self.error_response('任务运行记录不存在', 404)
            return jsonify(Result.success(data=data).to_dict())
        finally:
            session.close()

    def execute_internal_run(self, run_id: str):
        context = get_current_user_context()
        if not self._is_service_account_request(context):
            return self.error_response('仅允许洞察任务服务账号执行该接口', 403)
        session = SessionLocal()
        try:
            data = InsightTaskService(session).execute_run(
                run_id=run_id,
                execution_principal=getattr(context, 'username', '') or 'DataInsight-Server',
                runtime_user_context=self._runtime_user_context(context),
                worker_id=f'{socket.gethostname()}:internal',
                include_result=False,
            )
            if data is None:
                return self.error_response('任务运行记录不存在', 404)
            return jsonify(Result.success(data=data).to_dict())
        finally:
            session.close()

    def _is_service_account_request(self, context) -> bool:
        if context is None or not Config.SUPOS_DATAINSIGHT_SERVER_APPKEY:
            return False
        actual = str(getattr(context, 'platform_authorization', '') or '').strip()
        expected = Config.SUPOS_DATAINSIGHT_SERVER_APPKEY.strip()
        actual_token = actual[7:] if actual.startswith('Bearer ') else actual
        expected_token = expected[7:] if expected.startswith('Bearer ') else expected
        return bool(actual_token and hmac.compare_digest(actual_token, expected_token))

    @staticmethod
    def _runtime_user_context(context) -> dict:
        database_conn_info = getattr(context, 'database_conn_info', None)
        return {
            'platform_authorization': getattr(context, 'platform_authorization', '') or '',
            'fedquery_authorization': getattr(context, 'fedquery_authorization', '') or '',
            'database_conn_info': {
                'host': getattr(database_conn_info, 'host', '') or '',
                'port': getattr(database_conn_info, 'port', '') or '',
                'user': getattr(database_conn_info, 'user', '') or '',
                'password': getattr(database_conn_info, 'password', '') or '',
                'lake_rds_database_name': getattr(database_conn_info, 'lake_rds_database_name', '') or '',
                'ts_query_source': getattr(database_conn_info, 'ts_query_source', '') or '',
            } if database_conn_info else {},
        }
