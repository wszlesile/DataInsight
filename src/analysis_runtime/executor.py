from collections.abc import Callable
from typing import Any

from analysis_runtime.contracts import AnalysisExecutionSnapshot, AnalysisRuntimeResult
from analysis_runtime.parameters import normalize_parameter_schema, validate_analysis_parameters


class LocalAnalysisRuntime:
    """Execute a persisted analysis snapshot without invoking an LLM."""

    def __init__(self, execute_code: Callable[..., dict[str, Any]]):
        self.execute_code = execute_code

    def execute(
        self,
        *,
        snapshot: AnalysisExecutionSnapshot,
        username: str,
        analysis_parameters: dict[str, Any] | None,
        runtime_user_context: dict[str, Any] | None = None,
        analysis_task_id: int | None = None,
        execution_source: str = 'manual_param_rerun',
    ) -> AnalysisRuntimeResult:
        if not snapshot.code.strip():
            raise ValueError('执行快照缺少可执行分析代码')
        schema = normalize_parameter_schema(snapshot.parameter_schema)
        effective_values = dict(snapshot.default_parameters)
        effective_values.update(analysis_parameters or {})
        normalized_parameters = validate_analysis_parameters(schema, effective_values)
        execution_context = dict(runtime_user_context or {})
        execution_context.setdefault('datasource_bindings', snapshot.datasource_bindings)
        result = self.execute_code(
            conversation_id=snapshot.conversation_id,
            turn_id=snapshot.turn_id,
            username=username,
            code=snapshot.code,
            title=snapshot.title,
            description=snapshot.description,
            used_datasource_ids=snapshot.used_datasource_ids,
            runtime_user_context=execution_context,
            analysis_task_id=analysis_task_id,
            parent_execution_id=snapshot.source_execution_id,
            execution_source=execution_source,
            analysis_parameters_schema=schema,
            analysis_parameters=normalized_parameters,
        )
        return AnalysisRuntimeResult.from_raw(result)
