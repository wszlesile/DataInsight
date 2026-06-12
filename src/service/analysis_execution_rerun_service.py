from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from agent.tools import run_generated_code_locally
from model import InsightNsArtifact, InsightNsConversation, InsightNsExecution, InsightNsTurn
from service.analysis_task_service import AnalysisTaskService
from utils.datasource_utils import dump_json, safe_json_loads, to_int


class AnalysisExecutionRerunService:
    """Local rerun service for persisted generated-code executions."""

    def __init__(self, session: Session):
        self.session = session

    def rerun_execution(
        self,
        *,
        username: str,
        conversation_id: Any,
        turn_id: Any,
        execution_id: Any,
        analysis_parameters: dict[str, Any],
        runtime_user_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        conversation = self._get_conversation(conversation_id)
        if conversation is None:
            return None
        turn = self._get_turn(conversation, turn_id)
        if turn is None:
            return None
        original = self._get_execution(conversation, turn, execution_id)
        if original is None:
            return None
        if original.execution_status != 'success':
            raise ValueError('只能基于成功的执行记录进行本地复跑')

        schema = safe_json_loads(original.analysis_parameters_schema_json, {})
        if not isinstance(schema, dict) or not schema:
            raise ValueError('当前执行记录没有可复跑的参数定义，请先重新生成一次参数化分析')
        normalized_parameters = self._validate_parameters(schema, analysis_parameters)

        old_turn_state = {
            'status': turn.status,
            'error_message': turn.error_message,
            'final_answer': turn.final_answer,
            'finished_at': turn.finished_at,
            'started_at': turn.started_at,
        }
        started_at = datetime.now()
        turn.status = 'running'
        turn.error_message = ''
        turn.started_at = started_at
        turn.finished_at = None

        task_service = AnalysisTaskService(self.session)
        task = task_service.create_task(
            username=username,
            namespace_id=conversation.insight_namespace_id,
            conversation_id=conversation.id,
            turn_id=turn.id,
            task_type='local_rerun',
            request_payload={
                'source_execution_id': original.id,
                'analysis_parameters': normalized_parameters,
            },
        )
        self.session.commit()

        task_service.mark_running(task.task_id, worker_id='local')
        self.session.commit()

        run_result = run_generated_code_locally(
            conversation_id=conversation.id,
            turn_id=turn.id,
            username=username,
            code=original.generated_code,
            title=original.title,
            description=original.description,
            used_datasource_ids=safe_json_loads(original.used_datasource_ids_json, []),
            runtime_user_context=runtime_user_context or {},
            analysis_task_id=task.id,
            parent_execution_id=original.id,
            execution_source='manual_param_rerun',
            analysis_parameters_schema=schema,
            analysis_parameters=normalized_parameters,
        )

        if run_result.get('status') != 'success':
            self._disable_execution_content(run_result.get('execution_id'))
            self._restore_turn_state(turn, old_turn_state)
            task_service.mark_finished(
                task.task_id,
                status='failed',
                error_message=str(run_result.get('error_message') or '本地复跑失败'),
            )
            self.session.commit()
            return {
                'task': task_service.serialize_task(task),
                'execution': self._load_execution_dict(run_result.get('execution_id')),
                'status': run_result.get('status'),
                'error_message': run_result.get('error_message') or '',
            }

        self._disable_execution_content(run_result.get('execution_id'))
        self._create_disabled_artifacts(
            conversation=conversation,
            turn=turn,
            execution_id=to_int(run_result.get('execution_id'), 0),
            analysis_report=run_result.get('analysis_report') or '',
            charts=run_result.get('charts') or [],
            tables=run_result.get('tables') or [],
            analysis_task_id=task.id,
        )
        self._restore_turn_state(turn, old_turn_state)
        task_service.mark_finished(task.task_id, status='success')
        self.session.commit()
        return {
            'task': task_service.serialize_task(task),
            'execution': self._load_execution_dict(run_result.get('execution_id')),
            'status': 'success',
        }

    def _get_conversation(self, conversation_id: Any) -> InsightNsConversation | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _get_turn(self, conversation: InsightNsConversation, turn_id: Any) -> InsightNsTurn | None:
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return None
        return self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id_int,
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()

    def _get_execution(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
        execution_id: Any,
    ) -> InsightNsExecution | None:
        execution_id_int = to_int(execution_id, 0)
        if execution_id_int <= 0:
            return None
        return self.session.query(InsightNsExecution).filter(
            InsightNsExecution.id == execution_id_int,
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
        ).first()

    def _load_execution_dict(self, execution_id: Any) -> dict[str, Any] | None:
        execution_id_int = to_int(execution_id, 0)
        if execution_id_int <= 0:
            return None
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.id == execution_id_int,
            InsightNsExecution.is_deleted == 0,
        ).first()
        return execution.to_dict() if execution else None

    def _disable_execution_content(self, execution_id: Any) -> None:
        execution_id_int = to_int(execution_id, 0)
        if execution_id_int <= 0:
            return
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.id == execution_id_int,
            InsightNsExecution.is_deleted == 0,
        ).first()
        if execution is None:
            return
        execution.content_enabled = 0
        execution.updated_at = datetime.now()

    def _restore_turn_state(self, turn: InsightNsTurn, old_turn_state: dict[str, Any]) -> None:
        self.session.refresh(turn)
        turn.status = old_turn_state['status']
        turn.error_message = old_turn_state['error_message']
        turn.final_answer = old_turn_state['final_answer']
        turn.finished_at = old_turn_state['finished_at']
        turn.started_at = old_turn_state['started_at']

    def _create_disabled_artifacts(
        self,
        *,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
        execution_id: int,
        analysis_report: str,
        charts: list[dict[str, Any]],
        tables: list[dict[str, Any]],
        analysis_task_id: int,
    ) -> None:
        for index, chart in enumerate(charts or [], start=1):
            self._create_disabled_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=execution_id,
                artifact_type='chart',
                title=str(chart.get('title') or f"{conversation.title} 图表 {index}").strip(),
                summary_text=str(chart.get('description') or '').strip(),
                content=chart,
                metadata={
                    "turn_no": turn.turn_no,
                    "chart_type": chart.get('chart_type') or '',
                },
                analysis_task_id=analysis_task_id,
                sort_no=index,
            )

        if analysis_report:
            self._create_disabled_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=execution_id,
                artifact_type='report',
                title=f"{conversation.title} 报告",
                summary_text=analysis_report,
                content={"report_markdown": analysis_report},
                metadata={"turn_no": turn.turn_no},
                analysis_task_id=analysis_task_id,
                sort_no=0,
            )

        for index, table in enumerate(tables or [], start=1):
            self._create_disabled_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=execution_id,
                artifact_type='table',
                title=str(table.get('title') or f"{conversation.title} 表格 {index}").strip(),
                summary_text=str(table.get('description') or '').strip(),
                content=table,
                metadata={"turn_no": turn.turn_no},
                analysis_task_id=analysis_task_id,
                sort_no=100 + index,
            )

    def _create_disabled_artifact(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        execution_id: int,
        artifact_type: str,
        title: str,
        summary_text: str,
        content: dict[str, Any],
        metadata: dict[str, Any],
        analysis_task_id: int,
        sort_no: int,
    ) -> None:
        artifact = InsightNsArtifact(
            conversation_id=conversation_id,
            turn_id=turn_id,
            execution_id=execution_id,
            artifact_type=artifact_type,
            title=title,
            summary_text=summary_text or '',
            content_json=dump_json(content or {}),
            metadata_json=dump_json(metadata or {}),
            sort_no=sort_no,
            content_enabled=0,
            analysis_task_id=analysis_task_id,
        )
        self.session.add(artifact)

    def _validate_parameters(self, schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError('analysis_parameters 必须是 JSON 对象')
        defaults = schema.get('defaults') if isinstance(schema.get('defaults'), dict) else {}
        normalized = dict(defaults)
        normalized.update(values)

        parameters = schema.get('parameters')
        if not isinstance(parameters, list):
            return normalized

        for item in parameters:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            required = bool(item.get('required'))
            if required and name not in normalized:
                raise ValueError(f'缺少必填分析参数: {name}')
            if name not in normalized:
                continue
            value = normalized.get(name)
            parameter_type = str(item.get('type') or '').strip().lower()
            self._validate_parameter_type(name, value, parameter_type)
            options = item.get('options')
            if isinstance(options, list) and options:
                self._validate_parameter_options(name, value, parameter_type, options)
        return normalized

    def _validate_parameter_options(
        self,
        name: str,
        value: Any,
        parameter_type: str,
        options: list[Any],
    ) -> None:
        if parameter_type in {'array', 'list', 'multi_select'}:
            if not isinstance(value, list):
                raise ValueError(f'分析参数 {name} 必须是数组')
            invalid_values = [item for item in value if item not in options]
            if invalid_values:
                raise ValueError(f'分析参数 {name} 不在允许选项内: {invalid_values[0]}')
            return

        if value not in options:
            raise ValueError(f'分析参数 {name} 不在允许选项内')

    def _validate_parameter_type(self, name: str, value: Any, parameter_type: str) -> None:
        if value is None or not parameter_type:
            return
        if parameter_type in {'string', 'text', 'date', 'datetime', 'time_range'}:
            if parameter_type == 'time_range' and not isinstance(value, dict):
                raise ValueError(f'分析参数 {name} 必须是对象')
            if parameter_type != 'time_range' and not isinstance(value, str):
                raise ValueError(f'分析参数 {name} 必须是字符串')
        elif parameter_type in {'number', 'integer'}:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f'分析参数 {name} 必须是数字')
        elif parameter_type in {'boolean', 'bool'}:
            if not isinstance(value, bool):
                raise ValueError(f'分析参数 {name} 必须是布尔值')
        elif parameter_type in {'array', 'list', 'multi_select'}:
            if not isinstance(value, list):
                raise ValueError(f'分析参数 {name} 必须是数组')
