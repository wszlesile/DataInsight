from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class AnalysisExecutionSnapshot:
    """Immutable input required to repeat one successful analysis locally."""

    source_execution_id: int
    conversation_id: int
    turn_id: int
    code: str
    analysis_question: str = ''
    title: str = ''
    description: str = ''
    used_datasource_ids: list[int] = field(default_factory=list)
    datasource_bindings: list[dict[str, Any]] = field(default_factory=list)
    query_snapshots: dict[str, Any] = field(default_factory=dict)
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    default_parameters: dict[str, Any] = field(default_factory=dict)
    logic_signature: str = ''
    runtime_contract_version: str = 'v1'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'AnalysisExecutionSnapshot':
        return cls(
            source_execution_id=int(value.get('source_execution_id') or 0),
            conversation_id=int(value.get('conversation_id') or 0),
            turn_id=int(value.get('turn_id') or 0),
            code=str(value.get('code') or ''),
            analysis_question=str(value.get('analysis_question') or ''),
            title=str(value.get('title') or ''),
            description=str(value.get('description') or ''),
            used_datasource_ids=[
                int(item) for item in (value.get('used_datasource_ids') or []) if str(item).isdigit()
            ],
            datasource_bindings=(
                value.get('datasource_bindings') if isinstance(value.get('datasource_bindings'), list) else []
            ),
            query_snapshots=(
                value.get('query_snapshots') if isinstance(value.get('query_snapshots'), dict) else {}
            ),
            parameter_schema=value.get('parameter_schema') if isinstance(value.get('parameter_schema'), dict) else {},
            default_parameters=(
                value.get('default_parameters') if isinstance(value.get('default_parameters'), dict) else {}
            ),
            logic_signature=str(value.get('logic_signature') or ''),
            runtime_contract_version=str(value.get('runtime_contract_version') or 'v1'),
        )


@dataclass(frozen=True)
class AnalysisRuntimeResult:
    status: str
    execution_id: int = 0
    analysis_report: str = ''
    charts: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    query_snapshots: dict[str, Any] = field(default_factory=dict)
    stdout_text: str = ''
    stderr_text: str = ''
    execution_seconds: int = 0
    error_type: str = ''
    error_message: str = ''

    @classmethod
    def from_raw(cls, value: dict[str, Any]) -> 'AnalysisRuntimeResult':
        return cls(
            status=str(value.get('status') or 'failed'),
            execution_id=int(value.get('execution_id') or 0),
            analysis_report=str(value.get('analysis_report') or ''),
            charts=value.get('charts') if isinstance(value.get('charts'), list) else [],
            tables=value.get('tables') if isinstance(value.get('tables'), list) else [],
            parameters=value.get('analysis_parameters') if isinstance(value.get('analysis_parameters'), dict) else {},
            parameter_schema=(
                value.get('analysis_parameters_schema')
                if isinstance(value.get('analysis_parameters_schema'), dict)
                else {}
            ),
            query_snapshots=value.get('query_snapshots') if isinstance(value.get('query_snapshots'), dict) else {},
            stdout_text=str(value.get('stdout_text') or ''),
            stderr_text=str(value.get('stderr_text') or ''),
            execution_seconds=int(value.get('execution_seconds') or 0),
            error_type=str(value.get('error_type') or ''),
            error_message=str(value.get('error_message') or ''),
        )

    def result_payload(self) -> dict[str, Any]:
        return {
            'analysis_report': self.analysis_report,
            'charts': self.charts,
            'tables': self.tables,
            'analysis_parameters_schema': self.parameter_schema,
            'analysis_parameters': self.parameters,
            'query_snapshots': self.query_snapshots,
        }
