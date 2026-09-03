from __future__ import annotations

from flask import Blueprint, jsonify, request

from controller.base_controller import BaseController
from dto import get_current_user_context
from config.database import SessionLocal
from service.workflow_insight_job_service import WorkflowInsightJobService
from utils.response import Result


def create_workflow_insight_controller() -> Blueprint:
    blueprint = Blueprint(
        "workflow_insight",
        __name__,
        url_prefix="/api/insight/workflow/jobs",
    )
    controller = WorkflowInsightController(blueprint)
    blueprint.route("", methods=["POST"])(controller.submit)
    blueprint.route("/<job_id>", methods=["GET"])(controller.get)
    blueprint.route("/<job_id>/cancel", methods=["POST"])(controller.cancel)
    return blueprint


class WorkflowInsightController(BaseController):
    """External business-workflow facade for asynchronous insight analysis."""

    def _error(self, message: str, status_code: int):
        return self.error_response(message, status_code), status_code

    def submit(self):
        payload = self.get_json_data()
        if not isinstance(payload, dict):
            return self._error("请求体必须是 JSON 对象", 400)
        user_context = get_current_user_context()
        if user_context is None:
            return self._error("未获取到当前调用用户", 401)
        database_conn_info = getattr(user_context, "database_conn_info", None)
        database_payload = {
            "host": getattr(database_conn_info, "host", "") or "",
            "port": getattr(database_conn_info, "port", "") or "",
            "user": getattr(database_conn_info, "user", "") or "",
            "password": getattr(database_conn_info, "password", "") or "",
            "lake_rds_database_name": getattr(
                database_conn_info, "lake_rds_database_name", ""
            ) or "",
            "ts_query_source": getattr(database_conn_info, "ts_query_source", "") or "",
        }
        session = SessionLocal()
        try:
            job, created = WorkflowInsightJobService(session).submit(
                username=user_context.username,
                analysis_question=payload.get("analysis_question"),
                uns_node_ids=payload.get("uns_node_ids"),
                output_format=payload.get("output_format"),
                analysis_preferences=payload.get("analysis_preferences", ""),
                platform_authorization=(
                    getattr(user_context, "platform_authorization", "") or ""
                ),
                fedquery_authorization=(
                    getattr(user_context, "fedquery_authorization", "") or ""
                ),
                selected_llm_model_id=(
                    getattr(user_context, "selected_llm_model_id", "") or ""
                ),
                database_conn_info=database_payload,
                idempotency_key=request.headers.get("Idempotency-Key", ""),
                request_id=request.headers.get("X-Request-ID", ""),
            )
            data = WorkflowInsightJobService(session).serialize(job)
            status_code = 202 if created else 200
            return jsonify(Result.success(
                data=data,
                message="洞察作业已提交" if created else "返回已有洞察作业",
                code=status_code,
            ).to_dict()), status_code
        except ValueError as exc:
            session.rollback()
            return self._error(str(exc), 400)
        except Exception as exc:
            session.rollback()
            return self._error(f"提交洞察作业失败: {exc}", 500)
        finally:
            session.close()

    def get(self, job_id: str):
        user_context = get_current_user_context()
        if user_context is None:
            return self._error("未获取到当前调用用户", 401)
        username = user_context.username
        session = SessionLocal()
        try:
            service = WorkflowInsightJobService(session)
            job = service.get(username=username, job_id=job_id)
            if job is None:
                return self._error("洞察作业不存在", 404)
            return self.success_response(service.serialize(job))
        except Exception as exc:
            session.rollback()
            return self._error(f"查询洞察作业失败: {exc}", 500)
        finally:
            session.close()

    def cancel(self, job_id: str):
        user_context = get_current_user_context()
        if user_context is None:
            return self._error("未获取到当前调用用户", 401)
        username = user_context.username
        session = SessionLocal()
        try:
            service = WorkflowInsightJobService(session)
            job = service.cancel(username=username, job_id=job_id)
            if job is None:
                return self._error("洞察作业不存在", 404)
            return self.success_response(service.serialize(job), message="洞察作业已停止")
        except Exception as exc:
            session.rollback()
            return self._error(f"停止洞察作业失败: {exc}", 500)
        finally:
            session.close()
