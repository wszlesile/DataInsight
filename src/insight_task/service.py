import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from agent.tools import run_generated_code_locally
from analysis_runtime import AnalysisExecutionSnapshot, LocalAnalysisRuntime
from analysis_runtime.parameters import normalize_parameter_schema, validate_analysis_parameters
from config import Config
from model import (
    InsightNamespace,
    InsightDatasource,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsTurn,
    InsightTaskDefinition,
    InsightTaskRevision,
    InsightTaskRun,
)
from insight_task.schedule import next_run_after, normalize_schedule, utc_now
from utils.datasource_utils import dump_json, safe_json_loads


TASK_STATUSES = {'enabled', 'paused'}
RUN_TERMINAL_STATUSES = {'success', 'failed', 'cancelled'}


class InsightTaskService:
    def __init__(self, session: Session):
        self.session = session

    def create_task(
        self,
        *,
        username: str,
        source_execution_id: Any,
        name: str,
        description: str,
        schedule: dict[str, Any],
    ) -> dict[str, Any]:
        execution = self._load_source_execution(username, source_execution_id)
        normalized_schedule = normalize_schedule(schedule)
        task = InsightTaskDefinition(
            task_id=uuid.uuid4().hex,
            username=username,
            namespace_id=self._execution_namespace_id(execution),
            source_conversation_id=execution.conversation_id,
            source_turn_id=execution.turn_id,
            name=str(name or execution.title or '洞察任务').strip()[:260],
            description=str(description or execution.description or '').strip(),
            status='enabled',
            schedule_json=dump_json(normalized_schedule),
            timezone=normalized_schedule['timezone'],
            next_run_at=next_run_after(normalized_schedule),
        )
        if not task.name:
            raise ValueError('任务名称不能为空')
        self.session.add(task)
        self.session.flush()

        snapshot = self._snapshot_from_execution(execution)
        revision = InsightTaskRevision(
            task_definition_id=task.id,
            version=1,
            source_execution_id=execution.id,
            runtime_contract_version=snapshot.runtime_contract_version,
            logic_signature=snapshot.logic_signature or self._snapshot_signature(snapshot),
            snapshot_json=dump_json(snapshot.to_dict()),
        )
        self.session.add(revision)
        self.session.flush()
        task.active_revision_id = revision.id
        self.session.commit()
        return self.get_task(username=username, task_id=task.task_id) or {}

    def list_tasks(self, *, username: str) -> list[dict[str, Any]]:
        tasks = self.session.query(InsightTaskDefinition).filter(
            InsightTaskDefinition.username == username,
            InsightTaskDefinition.is_deleted == 0,
        ).order_by(InsightTaskDefinition.updated_at.desc(), InsightTaskDefinition.id.desc()).all()
        if not tasks:
            return []
        task_ids = [item.id for item in tasks]
        run_counts = dict(
            self.session.query(InsightTaskRun.task_definition_id, func.count(InsightTaskRun.id)).filter(
                InsightTaskRun.task_definition_id.in_(task_ids),
            ).group_by(InsightTaskRun.task_definition_id).all()
        )
        ranked_runs = self.session.query(
            InsightTaskRun.id.label('run_id'),
            InsightTaskRun.task_definition_id.label('task_definition_id'),
            func.row_number().over(
                partition_by=InsightTaskRun.task_definition_id,
                order_by=(InsightTaskRun.scheduled_for.desc(), InsightTaskRun.id.desc()),
            ).label('rank_no'),
        ).filter(InsightTaskRun.task_definition_id.in_(task_ids)).subquery()
        latest_runs = self.session.query(InsightTaskRun).join(
            ranked_runs,
            InsightTaskRun.id == ranked_runs.c.run_id,
        ).filter(ranked_runs.c.rank_no == 1).all()
        latest_by_task = {item.task_definition_id: item for item in latest_runs}
        return [
            self._serialize_task(
                item,
                latest_run=latest_by_task.get(item.id),
                run_count=int(run_counts.get(item.id, 0)),
                latest_run_loaded=True,
            )
            for item in tasks
        ]

    def get_task(self, *, username: str, task_id: str) -> dict[str, Any] | None:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return None
        revision = self._active_revision(task)
        result = self._serialize_task(task)
        result['revision'] = self._serialize_revision(revision) if revision else None
        result['runs'] = self.list_runs(username=username, task_id=task_id, limit=50)
        return result

    def update_task(
        self,
        *,
        username: str,
        task_id: str,
        name: Any = None,
        description: Any = None,
        schedule: Any = None,
        status: Any = None,
    ) -> dict[str, Any] | None:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return None
        if name is not None:
            normalized_name = str(name).strip()
            if not normalized_name:
                raise ValueError('任务名称不能为空')
            task.name = normalized_name[:260]
        if description is not None:
            task.description = str(description).strip()
        if schedule is not None:
            normalized_schedule = normalize_schedule(schedule)
            task.schedule_json = dump_json(normalized_schedule)
            task.timezone = normalized_schedule['timezone']
            task.next_run_at = next_run_after(normalized_schedule) if task.status == 'enabled' else None
        if status is not None:
            normalized_status = str(status).strip().lower()
            if normalized_status not in TASK_STATUSES:
                raise ValueError('任务状态仅支持 enabled 或 paused')
            task.status = normalized_status
            task.next_run_at = (
                next_run_after(safe_json_loads(task.schedule_json, {})) if normalized_status == 'enabled' else None
            )
        self.session.commit()
        return self.get_task(username=username, task_id=task_id)

    def delete_task(self, *, username: str, task_id: str) -> bool:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return False
        task.is_deleted = 1
        task.status = 'paused'
        task.next_run_at = None
        self.session.commit()
        return True

    def enqueue_manual_run(
        self,
        *,
        username: str,
        task_id: str,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return None
        revision = self._active_revision(task)
        if revision is None:
            raise ValueError('任务缺少可执行版本')
        snapshot = AnalysisExecutionSnapshot.from_dict(safe_json_loads(revision.snapshot_json, {}))
        effective_parameters = validate_analysis_parameters(snapshot.parameter_schema, parameters or snapshot.default_parameters)
        run = InsightTaskRun(
            run_id=uuid.uuid4().hex,
            task_definition_id=task.id,
            revision_id=revision.id,
            trigger_type='manual',
            scheduled_for=utc_now(),
            status='queued',
            parameters_json=dump_json(effective_parameters),
        )
        self.session.add(run)
        self.session.commit()
        return run.to_dict()

    def enqueue_due_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        now = utc_now()
        query = self.session.query(InsightTaskDefinition).filter(
            InsightTaskDefinition.status == 'enabled',
            InsightTaskDefinition.is_deleted == 0,
            InsightTaskDefinition.next_run_at.isnot(None),
            InsightTaskDefinition.next_run_at <= now,
        ).order_by(InsightTaskDefinition.next_run_at.asc(), InsightTaskDefinition.id.asc())
        if self.session.bind and self.session.bind.dialect.name == 'postgresql':
            query = query.with_for_update(skip_locked=True)
        tasks = query.limit(max(1, limit)).all()
        runs: list[InsightTaskRun] = []
        for task in tasks:
            revision = self._active_revision(task)
            scheduled_for = task.next_run_at
            schedule = safe_json_loads(task.schedule_json, {})
            # Coalesce missed intervals into one run instead of replaying every
            # historical occurrence after a long service outage.
            task.next_run_at = next_run_after(schedule, after_utc=max(scheduled_for, now))
            if revision is None or scheduled_for is None:
                continue
            snapshot = AnalysisExecutionSnapshot.from_dict(safe_json_loads(revision.snapshot_json, {}))
            existing = self.session.query(InsightTaskRun).filter(
                InsightTaskRun.task_definition_id == task.id,
                InsightTaskRun.scheduled_for == scheduled_for,
            ).first()
            if existing:
                continue
            run = InsightTaskRun(
                run_id=uuid.uuid4().hex,
                task_definition_id=task.id,
                revision_id=revision.id,
                trigger_type='scheduled',
                scheduled_for=scheduled_for,
                status='queued',
                parameters_json=dump_json(snapshot.default_parameters),
            )
            self.session.add(run)
            runs.append(run)
        self.session.commit()
        return [item.to_dict(include_result=False) for item in runs]

    def list_queued_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        now = utc_now()
        runs = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.status == 'queued',
            or_(InsightTaskRun.next_attempt_at.is_(None), InsightTaskRun.next_attempt_at <= now),
        ).order_by(InsightTaskRun.created_at.asc(), InsightTaskRun.id.asc()).limit(max(1, limit)).all()
        return [item.to_dict(include_result=False) for item in runs]

    def recover_expired_runs(self) -> int:
        now = utc_now()
        updated = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.status == 'running',
            or_(InsightTaskRun.lease_until.is_(None), InsightTaskRun.lease_until < now),
        ).update({
            InsightTaskRun.status: 'queued',
            InsightTaskRun.worker_id: '',
            InsightTaskRun.lease_until: None,
            InsightTaskRun.next_attempt_at: None,
            InsightTaskRun.updated_at: now,
        }, synchronize_session=False)
        self.session.commit()
        return int(updated or 0)

    def list_runs(
        self,
        *,
        username: str,
        task_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return []
        runs = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.task_definition_id == task.id,
        ).order_by(InsightTaskRun.scheduled_for.desc(), InsightTaskRun.id.desc()).offset(
            max(0, offset)
        ).limit(min(max(1, limit), 100)).all()
        return [item.to_dict(include_result=False) for item in runs]

    def list_runs_page(
        self,
        *,
        username: str,
        task_id: str,
        page_no: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any] | None:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return None
        normalized_page = max(1, page_no)
        normalized_size = min(max(1, page_size), 100)
        total = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.task_definition_id == task.id,
        ).count()
        items = self.list_runs(
            username=username,
            task_id=task_id,
            limit=normalized_size,
            offset=(normalized_page - 1) * normalized_size,
        )
        return {
            'items': items,
            'total': total,
            'page_no': normalized_page,
            'page_size': normalized_size,
            'has_more': normalized_page * normalized_size < total,
        }

    def get_run(self, *, username: str, task_id: str, run_id: str) -> dict[str, Any] | None:
        task = self._get_task_model(username=username, task_id=task_id)
        if task is None:
            return None
        run = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.run_id == run_id,
            InsightTaskRun.task_definition_id == task.id,
        ).first()
        return run.to_dict() if run else None

    def execute_run(
        self,
        *,
        run_id: str,
        execution_principal: str,
        runtime_user_context: dict[str, Any],
        worker_id: str,
        include_result: bool = True,
    ) -> dict[str, Any] | None:
        run = self.session.query(InsightTaskRun).filter(InsightTaskRun.run_id == run_id).first()
        if run is None:
            return None
        if run.status in RUN_TERMINAL_STATUSES:
            return run.to_dict(include_result=include_result)
        now = utc_now()
        if run.status == 'running' and run.lease_until and run.lease_until > now:
            return run.to_dict(include_result=include_result)
        claim_query = self.session.query(InsightTaskRun).filter(InsightTaskRun.id == run.id)
        if run.status == 'queued':
            claim_query = claim_query.filter(InsightTaskRun.status == 'queued')
        else:
            claim_query = claim_query.filter(
                InsightTaskRun.status == 'running',
                or_(InsightTaskRun.lease_until.is_(None), InsightTaskRun.lease_until <= now),
            )
        lease_seconds = max(
            30,
            Config.INSIGHT_TASK_RUN_LEASE_SECONDS,
            Config.PYTHON_EXEC_TIMEOUT_SECONDS + 60,
            int(Config.INSIGHT_TASK_DISPATCH_TIMEOUT_SECONDS) + 30,
        )
        updated = claim_query.update({
            InsightTaskRun.status: 'running',
            InsightTaskRun.started_at: run.started_at or now,
            InsightTaskRun.worker_id: worker_id,
            InsightTaskRun.lease_until: now + timedelta(seconds=lease_seconds),
            InsightTaskRun.execution_principal: execution_principal,
            InsightTaskRun.next_attempt_at: None,
            InsightTaskRun.updated_at: now,
        }, synchronize_session=False)
        if not updated:
            self.session.rollback()
            return run.to_dict(include_result=include_result)
        self.session.commit()
        self.session.refresh(run)

        revision = self.session.query(InsightTaskRevision).filter(InsightTaskRevision.id == run.revision_id).first()
        if revision is None:
            return self._finish_failed(run, '任务执行版本不存在', include_result=include_result)
        task = self.session.query(InsightTaskDefinition).filter(
            InsightTaskDefinition.id == run.task_definition_id,
            InsightTaskDefinition.is_deleted == 0,
        ).first()
        if task is None:
            return self._finish_failed(run, '洞察任务不存在或已删除', include_result=include_result)
        snapshot = AnalysisExecutionSnapshot.from_dict(safe_json_loads(revision.snapshot_json, {}))
        parameters = safe_json_loads(run.parameters_json, {})
        try:
            runtime_result = LocalAnalysisRuntime(run_generated_code_locally).execute(
                snapshot=snapshot,
                username=task.username,
                analysis_parameters=parameters,
                runtime_user_context=runtime_user_context,
                execution_source='scheduled_task',
            )
        except Exception as exc:
            return self._finish_failed(run, str(exc) or '洞察任务执行异常', include_result=include_result)
        run.execution_id = runtime_result.execution_id or None
        self._disable_internal_execution(runtime_result.execution_id)
        run.execution_seconds = runtime_result.execution_seconds
        run.lease_until = None
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        if runtime_result.status == 'success':
            run.status = 'success'
            run.analysis_report = runtime_result.analysis_report
            run.result_payload_json = dump_json(runtime_result.result_payload())
            run.error_message = ''
        else:
            run.status = 'failed'
            run.error_message = runtime_result.error_message or '洞察任务执行失败'
        task.last_run_at = run.finished_at
        self.session.commit()
        return run.to_dict(include_result=include_result)

    def record_dispatch_failure(self, *, run_id: str, error_message: str, retryable: bool) -> None:
        run = self.session.query(InsightTaskRun).filter(
            InsightTaskRun.run_id == run_id,
            InsightTaskRun.status == 'queued',
        ).first()
        if run is None:
            return
        run.dispatch_attempts = int(run.dispatch_attempts or 0) + 1
        run.error_message = error_message
        max_attempts = max(1, Config.INSIGHT_TASK_DISPATCH_MAX_ATTEMPTS)
        if not retryable or run.dispatch_attempts >= max_attempts:
            run.status = 'failed'
            run.finished_at = utc_now()
            run.next_attempt_at = None
        else:
            delay_seconds = max(1, Config.INSIGHT_TASK_DISPATCH_RETRY_BASE_SECONDS) * (2 ** (run.dispatch_attempts - 1))
            run.next_attempt_at = utc_now() + timedelta(seconds=min(delay_seconds, 300))
        self.session.commit()

    def _load_source_execution(self, username: str, execution_id: Any) -> InsightNsExecution:
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.id == int(execution_id or 0),
            InsightNsExecution.is_deleted == 0,
        ).first()
        if execution is None:
            raise ValueError('执行记录不存在')
        if execution.execution_status != 'success' or not execution.generated_code.strip():
            raise ValueError('只有成功且包含可执行代码的分析结果可以加入任务')
        namespace_id = self._execution_namespace_id(execution)
        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == namespace_id,
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            raise ValueError('无权基于该执行记录创建任务')
        return execution

    def _execution_namespace_id(self, execution: InsightNsExecution) -> int:
        conversation = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == execution.conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
        if conversation is None:
            raise ValueError('来源会话不存在')
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == execution.turn_id,
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            raise ValueError('来源轮次不存在')
        return conversation.insight_namespace_id

    def _snapshot_from_execution(self, execution: InsightNsExecution) -> AnalysisExecutionSnapshot:
        schema = normalize_parameter_schema(safe_json_loads(execution.analysis_parameters_schema_json, {}))
        source_payload = safe_json_loads(execution.result_payload_json, {})
        source_payload = source_payload if isinstance(source_payload, dict) else {}
        turn = self.session.query(InsightNsTurn).filter(InsightNsTurn.id == execution.turn_id).first()
        return AnalysisExecutionSnapshot(
            source_execution_id=execution.id,
            conversation_id=execution.conversation_id,
            turn_id=execution.turn_id,
            code=execution.generated_code,
            analysis_question=turn.user_query if turn else '',
            title=execution.title,
            description=execution.description,
            used_datasource_ids=safe_json_loads(execution.used_datasource_ids_json, []),
            datasource_bindings=self._datasource_bindings(execution),
            query_snapshots=(
                source_payload.get('query_snapshots') if isinstance(source_payload.get('query_snapshots'), dict) else {}
            ),
            parameter_schema=schema,
            default_parameters=validate_analysis_parameters(
                schema,
                safe_json_loads(execution.analysis_parameters_json, {}),
            ),
            logic_signature=execution.analysis_logic_signature or '',
        )

    def _snapshot_signature(self, snapshot: AnalysisExecutionSnapshot) -> str:
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def _datasource_bindings(self, execution: InsightNsExecution) -> list[dict[str, Any]]:
        datasource_ids = [
            int(item)
            for item in safe_json_loads(execution.used_datasource_ids_json, [])
            if str(item).isdigit()
        ]
        if not datasource_ids:
            return []
        rows = self.session.query(InsightDatasource).filter(
            InsightDatasource.id.in_(datasource_ids),
            InsightDatasource.is_deleted == 0,
        ).all()
        by_id = {row.id: row for row in rows}
        return [
            {
                'datasource_id': datasource_id,
                'datasource_name': by_id[datasource_id].datasource_name if datasource_id in by_id else '',
                'datasource_type': by_id[datasource_id].datasource_type if datasource_id in by_id else '',
            }
            for datasource_id in datasource_ids
        ]

    def _serialize_revision(self, revision: InsightTaskRevision) -> dict[str, Any]:
        result = revision.to_dict()
        snapshot = AnalysisExecutionSnapshot.from_dict(safe_json_loads(revision.snapshot_json, {}))
        result['snapshot'] = {
            'source_execution_id': snapshot.source_execution_id,
            'analysis_question': snapshot.analysis_question,
            'title': snapshot.title,
            'description': snapshot.description,
            'used_datasource_ids': snapshot.used_datasource_ids,
            'datasource_bindings': snapshot.datasource_bindings,
            'query_criteria': self._query_criteria(snapshot.query_snapshots),
            'parameter_schema': snapshot.parameter_schema,
            'default_parameters': snapshot.default_parameters,
            'logic_signature': snapshot.logic_signature,
            'runtime_contract_version': snapshot.runtime_contract_version,
        }
        return result

    def _query_criteria(self, query_snapshots: dict[str, Any]) -> list[dict[str, Any]]:
        criteria: list[dict[str, Any]] = []
        for query_key, snapshot in (query_snapshots or {}).items():
            if not isinstance(snapshot, dict):
                continue
            query_scope = snapshot.get('query_scope') if isinstance(snapshot.get('query_scope'), dict) else {}
            execution = snapshot.get('execution') if isinstance(snapshot.get('execution'), dict) else {}
            criteria.append({
                'query_key': str(snapshot.get('query_key') or query_key),
                'query_constraints': (
                    snapshot.get('query_constraints')
                    if isinstance(snapshot.get('query_constraints'), dict)
                    else {}
                ),
                'query_scope': query_scope,
                'datasource_ids': query_scope.get('datasource_ids') if isinstance(query_scope.get('datasource_ids'), list) else [],
                'execution': {
                    'status': execution.get('status'),
                    'row_count': execution.get('row_count'),
                },
            })
        return criteria

    def _get_task_model(self, *, username: str, task_id: str) -> InsightTaskDefinition | None:
        return self.session.query(InsightTaskDefinition).filter(
            InsightTaskDefinition.task_id == task_id,
            InsightTaskDefinition.username == username,
            InsightTaskDefinition.is_deleted == 0,
        ).first()

    def _active_revision(self, task: InsightTaskDefinition) -> InsightTaskRevision | None:
        if not task.active_revision_id:
            return None
        return self.session.query(InsightTaskRevision).filter(
            InsightTaskRevision.id == task.active_revision_id,
            InsightTaskRevision.task_definition_id == task.id,
        ).first()

    def _serialize_task(
        self,
        task: InsightTaskDefinition,
        *,
        latest_run: InsightTaskRun | None = None,
        run_count: int | None = None,
        latest_run_loaded: bool = False,
    ) -> dict[str, Any]:
        result = task.to_dict()
        if not latest_run_loaded:
            latest_run = self.session.query(InsightTaskRun).filter(
                InsightTaskRun.task_definition_id == task.id,
            ).order_by(InsightTaskRun.scheduled_for.desc(), InsightTaskRun.id.desc()).first()
        result['latest_run'] = latest_run.to_dict(include_result=False) if latest_run else None
        result['run_count'] = (
            int(run_count)
            if run_count is not None
            else self.session.query(InsightTaskRun).filter(
                InsightTaskRun.task_definition_id == task.id,
            ).count()
        )
        return result

    def _disable_internal_execution(self, execution_id: int) -> None:
        if not execution_id:
            return
        execution = self.session.query(InsightNsExecution).filter(InsightNsExecution.id == execution_id).first()
        if execution:
            execution.content_enabled = 0

    def _finish_failed(
        self,
        run: InsightTaskRun,
        message: str,
        *,
        include_result: bool = True,
    ) -> dict[str, Any]:
        run.status = 'failed'
        run.error_message = message
        run.finished_at = utc_now()
        run.lease_until = None
        self.session.commit()
        return run.to_dict(include_result=include_result)
