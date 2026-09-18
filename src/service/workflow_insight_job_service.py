from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent.invocation_policy import InvocationPolicy, normalize_output_contract
from agent.invoker import AgentRequest
from config import Config
from model import (
    InsightAnalysisTask,
    InsightNamespace,
    InsightNsConversation,
    InsightNsConversationPrompt,
    InsightNsExecution,
    InsightNsRelDatasource,
    InsightNsTurn,
    InsightWorkflowJob,
)
from service.analysis_task_runner import get_analysis_task_runner
from service.analysis_task_service import AnalysisTaskService
from service.conversation_context_service import ConversationContextService
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService
from utils.analysis_task_cancel import CANCELLED_MESSAGE
from utils.datasource_utils import dump_json, safe_json_loads


INTERNAL_NAMESPACE_NAME = "__workflow_internal__"
INTERNAL_NAMESPACE_KIND = "workflow_internal"
WORKFLOW_BIND_SOURCE = "workflow_scope"
TERMINAL_STATUSES = {"success", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now()


class WorkflowInsightJobService:
    """Submit and inspect external workflow analyses through the existing Runtime."""

    def __init__(self, session: Session):
        self.session = session

    def submit(
        self,
        *,
        username: str,
        analysis_question: str,
        uns_node_ids: list[Any],
        output_format: Any,
        analysis_preferences: str,
        platform_authorization: str,
        fedquery_authorization: str,
        selected_llm_model_id: str,
        database_conn_info: dict[str, Any],
        idempotency_key: str = "",
        request_id: str = "",
    ) -> tuple[InsightWorkflowJob, bool]:
        if not isinstance(analysis_question, str):
            raise ValueError("analysis_question 必须是字符串")
        question = analysis_question.strip()
        if not question:
            raise ValueError("analysis_question 不能为空")
        if not isinstance(uns_node_ids, list):
            raise ValueError("uns_node_ids 必须是数组")
        if any(
            isinstance(item, (dict, list, tuple, set, bool)) or item is None
            for item in uns_node_ids
        ):
            raise ValueError("uns_node_ids 只能包含 UNS 节点 ID")
        normalized_uns_ids = list(dict.fromkeys(
            str(item or "").strip() for item in uns_node_ids or [] if str(item or "").strip()
        ))
        if not normalized_uns_ids:
            raise ValueError("uns_node_ids 不能为空")
        max_nodes = max(int(getattr(Config, "WORKFLOW_ANALYSIS_MAX_UNS_NODES", 100) or 100), 1)
        if len(normalized_uns_ids) > max_nodes:
            raise ValueError(f"uns_node_ids 不能超过 {max_nodes} 个")

        if not isinstance(output_format, dict):
            raise ValueError("output_format 必须是对象")
        policy = InvocationPolicy.workflow(output_format=output_format)
        if analysis_preferences is not None and not isinstance(analysis_preferences, str):
            raise ValueError("analysis_preferences 必须是字符串")
        preferences = str(analysis_preferences or "").strip()
        database_conn_info = dict(database_conn_info or {})
        if len(preferences) > 20000:
            raise ValueError("analysis_preferences 不能超过 20000 字")
        request_payload = {
            "analysis_question": question,
            "uns_node_ids": normalized_uns_ids,
            "output_format": policy.output.to_dict(),
            "analysis_preferences": preferences,
        }
        fingerprint = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        normalized_idempotency_key = str(idempotency_key or "").strip()[:128]
        if normalized_idempotency_key:
            existing = self.session.query(InsightWorkflowJob).filter(
                InsightWorkflowJob.username == (username or "anonymous"),
                InsightWorkflowJob.idempotency_key == normalized_idempotency_key,
            ).order_by(InsightWorkflowJob.id.desc()).first()
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ValueError("相同 Idempotency-Key 对应了不同的请求内容")
                self._synchronize(existing)
                return existing, False

        datasource_by_node_id = InsightNsRelDatasourceService(
            self.session
        ).materialize_semantic_uns_node_ids(
            uns_node_ids=normalized_uns_ids,
            authorization=platform_authorization,
            lake_rds_database_name=str(database_conn_info.get("lake_rds_database_name") or ""),
            ts_query_source=str(database_conn_info.get("ts_query_source") or ""),
            verify_access=True,
        )
        namespace = self._get_or_create_internal_namespace(username or "anonymous")
        conversation = self._create_internal_conversation(namespace.id)
        self._bind_exact_scope(
            namespace_id=namespace.id,
            conversation_id=conversation.id,
            datasource_ids=[datasource_by_node_id[item] for item in normalized_uns_ids],
        )
        self._set_preferences(namespace.id, conversation.id, preferences)
        self.session.flush()

        runtime = ConversationContextService(self.session).start_run(
            username=username or "anonymous",
            namespace_id=namespace.id,
            conversation_id=conversation.id,
            user_message=question,
            allow_internal_namespace=True,
        )
        task = AnalysisTaskService(self.session).create_task(
            username=username or "anonymous",
            namespace_id=namespace.id,
            conversation_id=conversation.id,
            turn_id=runtime.turn.id,
            task_type="workflow_analysis",
            request_payload={
                "namespace_id": namespace.id,
                "conversation_id": conversation.id,
                "turn_id": runtime.turn.id,
                "user_message": question,
                "output_contract": policy.output.to_dict(),
                "clarification_mode": policy.clarification_mode,
                "evidence_enabled": policy.evidence_enabled,
            },
        )
        job = InsightWorkflowJob(
            job_id=uuid.uuid4().hex,
            username=username or "anonymous",
            namespace_id=namespace.id,
            conversation_id=conversation.id,
            turn_id=runtime.turn.id,
            analysis_task_id=task.id,
            analysis_task_public_id=task.task_id,
            analysis_question=question,
            uns_node_ids_json=dump_json(normalized_uns_ids),
            output_contract_json=dump_json(policy.output.to_dict()),
            analysis_preferences=preferences,
            status="queued",
            idempotency_key=normalized_idempotency_key or None,
            request_id=str(request_id or "").strip()[:128],
            request_fingerprint=fingerprint,
            result_json="{}",
            error_message="",
        )
        self.session.add(job)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if not normalized_idempotency_key:
                raise
            existing = self.session.query(InsightWorkflowJob).filter(
                InsightWorkflowJob.username == (username or "anonymous"),
                InsightWorkflowJob.idempotency_key == normalized_idempotency_key,
            ).first()
            if existing is None or existing.request_fingerprint != fingerprint:
                raise ValueError("相同 Idempotency-Key 对应了不同的请求内容")
            self._synchronize(existing)
            return existing, False
        self.session.refresh(job)

        agent_request = AgentRequest(
            username=username or "anonymous",
            namespace_id=str(namespace.id),
            conversation_id=str(conversation.id),
            user_message=question,
            platform_authorization=platform_authorization,
            fedquery_authorization=fedquery_authorization,
            selected_llm_model_id=selected_llm_model_id,
            database_conn_info=dict(database_conn_info or {}),
            output_contract=policy.output.to_dict(),
            clarification_mode=policy.clarification_mode,
            evidence_enabled=policy.evidence_enabled,
            exact_datasource_scope=True,
        )
        try:
            get_analysis_task_runner().submit(
                task_id=task.task_id,
                analysis_task_id=task.id,
                agent_request=agent_request,
                turn_id=runtime.turn.id,
                is_rerun=False,
            )
        except Exception as exc:
            self._mark_dispatch_failed(job=job, task=task, error_message=str(exc))
            raise RuntimeError(f"洞察作业未能进入执行队列: {exc}") from exc
        return job, True

    def get(self, *, username: str, job_id: str) -> InsightWorkflowJob | None:
        job = self.session.query(InsightWorkflowJob).filter(
            InsightWorkflowJob.job_id == str(job_id or "").strip(),
            InsightWorkflowJob.username == (username or "anonymous"),
        ).first()
        if job is None:
            return None
        self._synchronize(job)
        return job

    def cancel(self, *, username: str, job_id: str) -> InsightWorkflowJob | None:
        job = self.get(username=username, job_id=job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return job
        task = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.id == job.analysis_task_id,
        ).first()
        if task is not None:
            AnalysisTaskService(self.session).cancel_task(
                task.task_id,
                username=username,
                reason=CANCELLED_MESSAGE,
            )
            ConversationContextService(self.session).fail_run(
                job.conversation_id,
                job.turn_id,
                CANCELLED_MESSAGE,
            )
            get_analysis_task_runner().cancel(task.task_id)
        job.status = "cancelled"
        job.error_message = CANCELLED_MESSAGE
        job.finished_at = _now()
        job.updated_at = _now()
        self.session.commit()
        return job

    def serialize(self, job: InsightWorkflowJob) -> dict[str, Any]:
        result = safe_json_loads(job.result_json, {})
        return {
            "job_id": job.job_id,
            "status": job.status,
            "analysis_question": job.analysis_question,
            "uns_node_ids": safe_json_loads(job.uns_node_ids_json, []),
            "output_format": safe_json_loads(job.output_contract_json, {}),
            "result": result if job.status == "success" else None,
            "error": job.error_message if job.status in {"failed", "cancelled"} else "",
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }

    def _synchronize(self, job: InsightWorkflowJob) -> None:
        task = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.id == job.analysis_task_id,
        ).first()
        if task is None:
            return
        job.status = task.status
        job.started_at = task.started_at
        job.finished_at = task.finished_at
        job.error_message = task.error_message or ""
        if task.status == "success" and not safe_json_loads(job.result_json, {}):
            try:
                job.result_json = dump_json(self._build_result(job))
            except ValueError as exc:
                job.status = "failed"
                job.error_message = str(exc)
                job.finished_at = job.finished_at or _now()
        job.updated_at = _now()
        self.session.commit()

    def _build_result(self, job: InsightWorkflowJob) -> dict[str, Any]:
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == job.turn_id,
            InsightNsExecution.execution_status == "success",
            InsightNsExecution.is_deleted == 0,
        ).order_by(InsightNsExecution.id.desc()).first()
        output_contract = normalize_output_contract(
            safe_json_loads(job.output_contract_json, {})
        )
        if execution is None:
            if output_contract.type != "standard":
                raise ValueError("分析没有生成调用方要求的结构化终态结果")
            turn = self.session.query(InsightNsTurn).filter(
                InsightNsTurn.id == job.turn_id,
                InsightNsTurn.is_deleted == 0,
            ).first()
            report = str(getattr(turn, "final_answer", "") or "").strip()
            if not report:
                raise ValueError("工作流任务成功但未找到分析结果")
            return {
                "analysis_report": report,
                "charts": [],
                "tables": [],
            }
        payload = safe_json_loads(execution.result_payload_json, {})
        if output_contract.type == "text":
            return {"content": str(payload.get("output_payload") or execution.analysis_report or "")}
        if output_contract.type == "json":
            return {"data": payload.get("output_payload")}
        return {
            "analysis_report": str(payload.get("analysis_report") or execution.analysis_report or ""),
            "charts": payload.get("charts") if isinstance(payload.get("charts"), list) else [],
            "tables": payload.get("tables") if isinstance(payload.get("tables"), list) else [],
        }

    def _mark_dispatch_failed(
        self,
        *,
        job: InsightWorkflowJob,
        task: InsightAnalysisTask,
        error_message: str,
    ) -> None:
        now = _now()
        message = (error_message or "洞察作业未能进入执行队列").strip()
        task.status = "failed"
        task.error_message = message
        task.finished_at = now
        task.updated_at = now
        job.status = "failed"
        job.error_message = message
        job.finished_at = now
        job.updated_at = now
        ConversationContextService(self.session).fail_run(
            job.conversation_id,
            job.turn_id,
            message,
        )
        self.session.commit()

    def _get_or_create_internal_namespace(self, username: str) -> InsightNamespace:
        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.username == username,
            InsightNamespace.namespace_kind == INTERNAL_NAMESPACE_KIND,
            InsightNamespace.is_deleted == 0,
        ).order_by(InsightNamespace.id.asc()).first()
        if namespace is not None:
            return namespace
        namespace = InsightNamespace(
            username=username,
            name=INTERNAL_NAMESPACE_NAME,
            namespace_kind=INTERNAL_NAMESPACE_KIND,
            is_deleted=0,
            created_at=_now(),
        )
        self.session.add(namespace)
        self.session.flush()
        return namespace

    def _create_internal_conversation(self, namespace_id: int) -> InsightNsConversation:
        now = _now()
        conversation = InsightNsConversation(
            insight_namespace_id=namespace_id,
            title="工作流洞察",
            status="active",
            summary_text="",
            active_datasource_snapshot="{}",
            last_turn_no=0,
            last_message_at=now,
            user_message="",
            insight_result="",
            is_deleted=0,
            created_at=now,
            updated_at=now,
        )
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _bind_exact_scope(
        self,
        *,
        namespace_id: int,
        conversation_id: int,
        datasource_ids: list[int],
    ) -> None:
        for index, datasource_id in enumerate(dict.fromkeys(datasource_ids), start=1):
            self.session.add(InsightNsRelDatasource(
                insight_namespace_id=namespace_id,
                insight_conversation_id=conversation_id,
                datasource_id=int(datasource_id),
                is_active=1,
                sort_no=index,
                bind_source=WORKFLOW_BIND_SOURCE,
                is_deleted=0,
                created_at=_now(),
                updated_at=_now(),
            ))

    def _set_preferences(
        self,
        namespace_id: int,
        conversation_id: int,
        preferences: str,
    ) -> None:
        if not preferences:
            return
        self.session.add(InsightNsConversationPrompt(
            insight_namespace_id=namespace_id,
            insight_conversation_id=conversation_id,
            prompt_content=preferences,
            is_enabled=1,
            is_deleted=0,
            created_at=_now(),
            updated_at=_now(),
        ))
