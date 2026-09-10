"""State-driven model request views over a lossless Runtime transcript."""

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field, field_validator

from agent.context_engineering_runtime import (
    CustomContext,
    build_runtime_datasource_working_message,
    runtime_schema_workspace_covers_material_scope,
)
from agent.runtime_contract_registry import (
    infer_query_capabilities_from_snapshot,
    load_runtime_work_contract_text,
    required_runtime_contract_ids,
)
from agent.runtime_schema_memory import merge_schema_sources
from agent.runtime_progress import emit_model_progress
from utils import logger
from utils.token_budget import estimate_messages_tokens, estimate_text_tokens


class WorkingMemoryKind(str, Enum):
    """Producer-owned categories used to manage a Runtime work view.

    These are deliberately not semantic guesses over arbitrary prose. A kind
    exists only when an existing producer gives us a stable identity.
    """

    USER_REQUIREMENT = 'user_requirement'
    ASSISTANT_ANALYTICAL_NOTE = 'assistant_analytical_note'
    INTERACTION_CONSTRAINT = 'interaction_constraint'
    PREPARATION_FACT = 'preparation_fact'
    SCHEMA_SOURCE = 'schema_source'
    PROTOCOL = 'protocol'
    WORK_DECISION = 'work_decision'
    MATERIAL_PLAN = 'material_plan'
    PROGRAM = 'program'
    PROGRAM_PATCH = 'program_patch'
    PROBE_REQUEST = 'probe_request'
    PROTOCOL_REFERENCE = 'protocol_reference'
    QUERY_DEFINITION = 'query_definition'
    QUERY_EXECUTION = 'query_execution'
    PROBE_OBSERVATION = 'probe_observation'
    EXECUTION_FAILURE = 'execution_failure'
    ARTIFACT_RESULT = 'artifact_result'
    RUNTIME_CONTROL = 'runtime_control'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class WorkingMemoryItem:
    """Metadata only; the lossless transcript remains the source of content."""

    kind: WorkingMemoryKind
    logical_key: str
    revision: str
    source_index: int
    source_event_id: str
    scope: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    status: str = 'active'
    superseded_by: str | None = None


@dataclass(frozen=True)
class WorkingMemoryIndex:
    items: tuple[WorkingMemoryItem, ...]

    @property
    def superseded_program_sources(self) -> frozenset[int]:
        return frozenset(
            item.source_index for item in self.items
            if item.kind is WorkingMemoryKind.PROGRAM and item.status == 'superseded'
        )

    @property
    def superseded_program_events(self) -> frozenset[str]:
        return frozenset(
            item.source_event_id for item in self.items
            if item.kind is WorkingMemoryKind.PROGRAM and item.status == 'superseded'
        )

    @property
    def covered_probe_events(self) -> frozenset[str]:
        """Probe calls whose executed facts exist independently of source code."""
        covered = set()
        for item in self.items:
            if item.kind is not WorkingMemoryKind.PROBE_OBSERVATION:
                continue
            if item.status != 'facts_captured':
                continue
            covered.update(
                dependency.removeprefix('call:')
                for dependency in item.dependencies if dependency.startswith('call:')
            )
        return frozenset(covered)

    @property
    def obsolete_probe_events(self) -> frozenset[str]:
        """Completed Probe calls that no longer need replayable source code.

        Probe facts are durable once captured. A failed Probe has no fact
        ledger, but only the latest such failure is actionable: an older
        failure has already been superseded by a newer Probe attempt. Keeping
        every historical Probe program in the next request turns repair
        history into prompt growth without adding a current decision input.
        """
        observations = [
            item for item in self.items
            if item.kind is WorkingMemoryKind.PROBE_OBSERVATION
            or (
                item.kind is WorkingMemoryKind.EXECUTION_FAILURE
                and item.scope == ('probe',)
            )
        ]
        covered = set(self.covered_probe_events)
        unresolved = [
            item for item in observations
            if item.source_event_id not in covered
        ]
        latest_unresolved = max(unresolved, key=lambda item: item.source_index, default=None)
        obsolete = set(covered)
        obsolete.update(
            item.source_event_id
            for item in unresolved
            if latest_unresolved is None or item.source_event_id != latest_unresolved.source_event_id
        )
        latest_observation = max(observations, key=lambda item: item.source_index, default=None)
        if latest_observation is not None:
            # The latest action/observation pair closes the ReAct step. Removing
            # it leaves an old work-selection call as the apparent last action.
            # Keep it until a newer execution supersedes that immediate context.
            if latest_observation.source_index == self.latest_execution_source:
                obsolete.discard(latest_observation.source_event_id)
            if latest_observation.source_event_id in covered:
                obsolete.update(
                    item.source_event_id for item in unresolved
                    if item.source_index < latest_observation.source_index
                )
        return frozenset(obsolete)

    @property
    def latest_execution_source(self) -> int:
        sources = [
            item.source_index for item in self.items
            if item.kind in {
                WorkingMemoryKind.PROBE_OBSERVATION,
                WorkingMemoryKind.EXECUTION_FAILURE,
                WorkingMemoryKind.ARTIFACT_RESULT,
            }
        ]
        return max(sources, default=-1)

    def of_kind(self, kind: WorkingMemoryKind) -> tuple[WorkingMemoryItem, ...]:
        return tuple(item for item in self.items if item.kind is kind)

    def with_items(self, items: tuple[WorkingMemoryItem, ...]) -> 'WorkingMemoryIndex':
        return WorkingMemoryIndex((*self.items, *items))

    @property
    def superseded_control_events(self) -> frozenset[str]:
        return frozenset(
            item.source_event_id for item in self.items
            if item.kind in {
                WorkingMemoryKind.WORK_DECISION,
                WorkingMemoryKind.MATERIAL_PLAN,
            }
            and item.status == 'superseded'
        )


def _stable_revision(value) -> str:
    """Return an opaque content revision without keeping another payload copy."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]


def _execution_source_keys(tool_name: str) -> tuple[str, ...]:
    if tool_name == 'submit_analysis_program':
        return ('code', 'program', 'analysis_code', 'analysis_program')
    if tool_name == 'probe_analysis_data':
        return ('code', 'program', 'probe_code', 'probe')
    return ()


def build_working_memory_index(messages) -> WorkingMemoryIndex:
    """Index producer-owned work memories without changing the transcript.

    `source_index` points back to the original lossless event. The index is a
    small, deterministic control plane: it never becomes a second source of
    truth for code, observations or user facts.
    """
    records = []
    calls = {}
    active_full_program = None

    def add(kind, logical_key, source_index, source_event_id, value, *, scope=(), dependencies=()):
        records.append({
            'kind': kind,
            'logical_key': logical_key,
            'revision': _stable_revision(value),
            'source_index': source_index,
            'source_event_id': source_event_id,
            'scope': tuple(str(item) for item in scope if item is not None),
            'dependencies': tuple(str(item) for item in dependencies if item is not None),
            'status': 'active',
            'superseded_by': None,
        })
        return len(records) - 1

    for index, message in enumerate(messages):
        event_id = str(getattr(message, 'id', '') or f'message:{index}')
        if isinstance(message, HumanMessage):
            add(WorkingMemoryKind.USER_REQUIREMENT, f'user:{event_id}', index, event_id, message.content)
            continue
        if isinstance(message, AIMessage):
            if message.content:
                add(WorkingMemoryKind.ASSISTANT_ANALYTICAL_NOTE, f'assistant-note:{event_id}', index, event_id,
                    message.content)
            for call in message.tool_calls:
                call_id = str(call['id'])
                name = str(call['name'])
                args = call.get('args', {})
                calls[call_id] = (name, args, index)
                if name == 'submit_analysis_program':
                    if any(args.get(key) for key in _execution_source_keys(name)):
                        item_index = add(
                            WorkingMemoryKind.PROGRAM, 'formal-program', index, call_id, args,
                            scope=('formal-analysis',),
                        )
                        if active_full_program is not None:
                            records[active_full_program]['status'] = 'superseded'
                            records[active_full_program]['superseded_by'] = call_id
                        active_full_program = item_index
                    elif args.get('patches'):
                        add(
                            WorkingMemoryKind.PROGRAM_PATCH, f'formal-program-patch:{call_id}', index, call_id, args,
                            scope=('formal-analysis',),
                            dependencies=(f"execution:{args.get('base_execution_id')}",),
                        )
                elif name == 'probe_analysis_data':
                    add(WorkingMemoryKind.PROBE_REQUEST, f'probe:{call_id}', index, call_id, args,
                        scope=('probe',))
            continue
        if not isinstance(message, ToolMessage):
            continue
        payload = _payload(message)
        call_id = str(message.tool_call_id or event_id)
        call_name, call_args, call_source = calls.get(call_id, ('', {}, index))
        if payload is None:
            add(WorkingMemoryKind.UNKNOWN, f'unknown:{event_id}', index, event_id, message.content)
            continue
        # ``execute_python`` is the shared worker implementation behind both
        # public Runtime tools.  The paired AI call owns the semantic role;
        # trusting payload.tool here used to classify every Probe observation
        # as a generic execution result and left its source/stdout replayable.
        owner = call_name or str(payload.get('tool') or '')
        if owner in {'select_runtime_work', 'request_runtime_probe'} and payload.get('status') == 'success':
            add(WorkingMemoryKind.WORK_DECISION, 'runtime-work', index, call_id, payload,
                scope=(payload.get('work'),))
        elif owner == 'plan_runtime_materials' and payload.get('status') == 'success':
            add(WorkingMemoryKind.MATERIAL_PLAN, 'runtime-material-plan', index, call_id, payload)
        elif owner == 'read_runtime_datasource_context':
            for source in payload.get('datasources') or []:
                if isinstance(source, dict):
                    identity = source.get('datasource_id')
                    add(WorkingMemoryKind.SCHEMA_SOURCE, f'schema-read:{call_id}:{identity}',
                        index, call_id, source, scope=(identity,))
            if payload.get('business_context'):
                add(WorkingMemoryKind.PREPARATION_FACT, f'lineage-read:{call_id}',
                    index, call_id, payload['business_context'])
        elif owner == 'read_runtime_query_context':
            for identity, query in (payload.get('queries') or {}).items():
                add(WorkingMemoryKind.QUERY_DEFINITION, f'query-read:{call_id}:{identity}', index, call_id, query,
                    dependencies=(identity,))
        elif owner == 'read_runtime_reference':
            for reference in payload.get('references', []):
                if isinstance(reference, dict) and reference.get('contract_id'):
                    contract_id = str(reference['contract_id'])
                    add(WorkingMemoryKind.PROTOCOL_REFERENCE, f'contract:{contract_id}', index, call_id, reference,
                        scope=(contract_id,))
        elif owner == 'probe_analysis_data' and _is_structured_probe_observation(payload):
            observation_index = add(
                WorkingMemoryKind.PROBE_OBSERVATION, f'probe-observation:{call_id}', index, call_id, payload,
                scope=('probe',), dependencies=(f'call:{call_id}',),
            )
            diagnostics = payload.get('diagnostics')
            has_fact_ledger = isinstance(diagnostics, dict) and isinstance(
                diagnostics.get('probe_fact_ledger'), dict
            )
            if (
                isinstance(payload.get('query_snapshots'), dict) and payload['query_snapshots']
            ) or has_fact_ledger:
                records[observation_index]['status'] = 'facts_captured'
        elif owner == 'probe_analysis_data':
            # A rejected or failed Probe call is execution feedback, not an
            # observation. Treating it as a fact-bearing observation corrupts
            # the work-memory lifecycle and can make later views archive the
            # wrong action/result pair.
            add(
                WorkingMemoryKind.EXECUTION_FAILURE,
                f'probe-failure:{call_id}',
                index,
                call_id,
                payload,
                scope=('probe',),
                dependencies=(f'call:{call_id}',),
            )
        elif owner in {'submit_analysis_program', 'execute_python'}:
            if payload.get('status') == 'failed' or payload.get('error_type'):
                add(WorkingMemoryKind.EXECUTION_FAILURE, f'failure:{call_id}', index, call_id, payload,
                    scope=('formal-analysis',), dependencies=(f'call:{call_id}',))
            elif payload.get('status') == 'success':
                add(WorkingMemoryKind.ARTIFACT_RESULT, f'artifact:{call_id}', index, call_id, payload,
                    scope=('formal-analysis',), dependencies=(f'call:{call_id}',))
        else:
            # A structured payload from a tool not owned here may become a
            # managed type later. Until then retain it verbatim and classify it
            # conservatively, instead of inventing replacement semantics.
            add(WorkingMemoryKind.UNKNOWN, f'unknown:{call_id}', index, call_id, payload)

        snapshots = payload.get('query_snapshots')
        if isinstance(snapshots, dict):
            for snapshot_key, snapshot in snapshots.items():
                if not isinstance(snapshot, dict):
                    continue
                definition = {key: value for key, value in snapshot.items() if key != 'execution'}
                definition_id = 'query:' + _stable_revision(definition)
                add(WorkingMemoryKind.QUERY_DEFINITION, definition_id, index, call_id, definition,
                    scope=(snapshot_key,))
                if 'execution' in snapshot:
                    add(WorkingMemoryKind.QUERY_EXECUTION, f'{definition_id}:execution:{call_id}:{snapshot_key}',
                        index, call_id, snapshot['execution'], scope=(snapshot_key,),
                        dependencies=(definition_id,))

    latest_control_by_key: dict[str, int] = {}
    for index, record in enumerate(records):
        if record['kind'] not in {
            WorkingMemoryKind.WORK_DECISION,
            WorkingMemoryKind.MATERIAL_PLAN,
        }:
            continue
        previous = latest_control_by_key.get(record['logical_key'])
        if previous is not None:
            records[previous]['status'] = 'superseded'
            records[previous]['superseded_by'] = record['source_event_id']
        latest_control_by_key[record['logical_key']] = index
    return WorkingMemoryIndex(tuple(WorkingMemoryItem(**record) for record in records))


def build_short_term_memory_catalog(memory) -> list[dict]:
    """Describe existing Runtime short-term state without copying or mutating it.

    This is an adapter over the legacy dictionary, deliberately not a second
    store. Unknown keys are recorded conservatively so a future migration
    cannot silently erase a recovery dependency.
    """
    if not isinstance(memory, dict):
        return []
    items = []

    def add(kind, logical_key, value, *, scope=(), dependencies=()):
        items.append({
            'kind': kind.value,
            'logical_key': logical_key,
            'revision': _stable_revision(value),
            'source_event_id': f'short-term:{logical_key}',
            'scope': [str(item) for item in scope],
            'dependencies': [str(item) for item in dependencies],
            'status': 'active',
        })

    known = {
        'expected_time_range_contract': (
            WorkingMemoryKind.INTERACTION_CONSTRAINT, 'interaction:expected-time-range', ('time-range',),
        ),
        'explicit_time_range_guard': (
            WorkingMemoryKind.RUNTIME_CONTROL, 'control:explicit-time-range-guard', ('no-data',),
        ),
        'scope_clarification_relation': (
            WorkingMemoryKind.RUNTIME_CONTROL, 'control:scope-clarification', ('clarification',),
        ),
        'probe_no_data_retry': (
            WorkingMemoryKind.RUNTIME_CONTROL, 'control:probe-no-data-retry', ('probe', 'no-data'),
        ),
        'probe_fact_ledger': (
            WorkingMemoryKind.PROBE_OBSERVATION, 'probe:fact-ledger', ('probe',),
        ),
    }
    for key, value in memory.items():
        if key == 'preparation_result':
            # Preparation is indexed from its authoritative snapshot below.
            continue
        if key == 'runtime_schema_detail_workspace':
            for position, source in enumerate(value if isinstance(value, list) else []):
                if not isinstance(source, dict):
                    continue
                datasource_id = source.get('datasource_id') or source.get('id') or f'position:{position}'
                add(
                    WorkingMemoryKind.SCHEMA_SOURCE, f'schema:detail:{datasource_id}', source,
                    scope=(f'datasource:{datasource_id}', 'schema-detail'),
                )
            continue
        definition = known.get(key)
        if definition is not None:
            kind, logical_key, scope = definition
            add(kind, logical_key, value, scope=scope)
            continue
        # Internal dedupe counters do not carry information needed by the
        # model, but are still visible to the control plane for audit.
        add(WorkingMemoryKind.UNKNOWN, f'short-term:{key}', value, scope=('short-term',))
    return items


def build_context_memory_items(context, contract_ids) -> tuple[WorkingMemoryItem, ...]:
    """Describe non-message work memory supplied by the current Runtime context.

    These items are request-scoped metadata. Their bodies stay in the existing
    context envelope or contract registry; this prevents the index itself from
    becoming another large prompt payload.
    """
    snapshot = context.execution_datasource_snapshot or context.active_datasource_snapshot or {}
    items = []
    semantic = snapshot.get('analysis_semantic_context')
    if isinstance(semantic, dict) and semantic:
        items.append(WorkingMemoryItem(
            kind=WorkingMemoryKind.PREPARATION_FACT,
            logical_key='preparation:analysis_semantic_context',
            revision=_stable_revision(semantic),
            source_index=-1,
            source_event_id='runtime-context:preparation',
            scope=('preparation',),
        ))
    for position, datasource in enumerate(snapshot.get('datasources') or []):
        if not isinstance(datasource, dict):
            continue
        datasource_id = (
            datasource.get('datasource_id') or datasource.get('id')
            or (datasource.get('query_contract') or {}).get('datasource_id')
            or f'position:{position}'
        )
        items.append(WorkingMemoryItem(
            kind=WorkingMemoryKind.SCHEMA_SOURCE,
            logical_key=f'schema:{datasource_id}',
            revision=_stable_revision(datasource),
            source_index=-1,
            source_event_id=f'runtime-context:datasource:{datasource_id}',
            scope=(f'datasource:{datasource_id}',),
        ))
    workspace = snapshot.get('runtime_schema_workspace')
    if isinstance(workspace, dict) and workspace:
        items.append(WorkingMemoryItem(
            kind=WorkingMemoryKind.SCHEMA_SOURCE,
            logical_key='schema:runtime-workspace',
            revision=_stable_revision(workspace),
            source_index=-1,
            source_event_id='runtime-context:schema-workspace',
            scope=('schema-workspace',),
        ))
    for raw in getattr(context, 'working_memory_catalog', []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            kind = WorkingMemoryKind(str(raw.get('kind') or WorkingMemoryKind.UNKNOWN.value))
        except ValueError:
            kind = WorkingMemoryKind.UNKNOWN
        logical_key = str(raw.get('logical_key') or 'unknown:short-term')
        items.append(WorkingMemoryItem(
            kind=kind,
            logical_key=logical_key,
            revision=str(raw.get('revision') or ''),
            source_index=-1,
            source_event_id=str(raw.get('source_event_id') or f'runtime-context:{logical_key}'),
            scope=tuple(str(value) for value in raw.get('scope') or ()),
            dependencies=tuple(str(value) for value in raw.get('dependencies') or ()),
            status=str(raw.get('status') or 'active'),
        ))
    for contract_id in contract_ids:
        items.append(WorkingMemoryItem(
            kind=WorkingMemoryKind.PROTOCOL,
            logical_key=f'contract:{contract_id}',
            # Contract source is the current deployed registry. A process
            # cannot load two revisions of the same ID; deployment is the
            # revision boundary, while explicit reads add a content hash.
            revision=f'deployed-registry:{contract_id}',
            source_index=-1,
            source_event_id=f'contract-registry:{contract_id}',
            scope=(contract_id,),
        ))
    return tuple(items)


class RuntimeWorkInput(BaseModel):
    progress_summary: str = Field(default='', max_length=600, description='Brief user-facing work summary: what will be checked and why, in the user language. No private reasoning, code or raw data.')
    work: Literal['plan']
    focus: str = Field(
        default='',
        description=(
            'Current analytical purpose; no metric plan or report template required. '
            'May be omitted when the current user query already states the purpose.'
        ),
    )
    datasource_ids: list[int] = Field(
        default_factory=list,
        description=(
            'Datasource ids whose exact Schema is needed for this immediate work. '
            'This only selects the next material view; it never changes the full analysis scope.'
        ),
    )
    field_names_by_datasource: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            'Exact field names needed now, keyed by datasource id. Prefer named fields '
            'from the datasource index; omit only when the current work needs no exact field definition yet.'
        ),
    )
    @field_validator('datasource_ids', 'field_names_by_datasource', mode='before')
    @classmethod
    def decode_json_container(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                pass
        return value

@tool(args_schema=RuntimeWorkInput)
def select_runtime_work(
    runtime: ToolRuntime[CustomContext],
    work: str,
    focus: str = '',
    datasource_ids: list[int] | None = None,
    field_names_by_datasource: dict[str, list[str]] | None = None,
    progress_summary: str = '',
) -> dict:
    """Select the next focused Runtime work before generating code.

    Start with ``plan`` to organize business facts into a data-access material
    plan. Supply datasource_ids and field_names_by_datasource when the next
    work needs a different local material view; leave them empty to retain
    the current view. A Probe observation resumes its parent work and material
    view. The next model request receives the work-specific contracts.
    This local operation neither queries data nor changes the analysis scope.
    """
    normalized_ids = list(dict.fromkeys(
        parsed for value in datasource_ids or []
        for parsed in (_positive_int(value),) if parsed > 0
    ))
    normalized_fields = {
        str(datasource_id): list(dict.fromkeys(
            str(field).strip() for field in fields if str(field).strip()
        ))
        for datasource_id, fields in (field_names_by_datasource or {}).items()
        if isinstance(fields, list)
    }
    normalized_focus = str(focus or getattr(runtime.context, 'user_query', '') or '').strip()
    return {
        'tool': 'select_runtime_work',
        'progress_summary': progress_summary,
        'status': 'success',
        'work': work,
        'focus': normalized_focus,
        'datasource_ids': normalized_ids,
        'field_names_by_datasource': {
            datasource_id: fields
            for datasource_id, fields in normalized_fields.items() if fields
        },
    }


class RuntimeProbeRequestInput(BaseModel):
    progress_summary: str = Field(
        default='', max_length=600,
        description='Brief user-facing summary of the technical fact to verify and why.',
    )
    focus: str = Field(
        min_length=1, max_length=600,
        description='The unresolved technical fact that must be observed before continuing the current work.',
    )
    datasource_ids: list[int] = Field(
        default_factory=list,
        description='Datasource ids needed for this focused Probe. Empty keeps the current material scope.',
    )
    field_names_by_datasource: dict[str, list[str]] = Field(
        default_factory=dict,
        description='Exact fields needed by the focused Probe, keyed by datasource id.',
    )

    @field_validator('datasource_ids', 'field_names_by_datasource', mode='before')
    @classmethod
    def decode_json_container(cls, value):
        return RuntimeWorkInput.decode_json_container(value)


@tool(args_schema=RuntimeProbeRequestInput)
def request_runtime_probe(
    runtime: ToolRuntime[CustomContext],
    focus: str,
    datasource_ids: list[int] | None = None,
    field_names_by_datasource: dict[str, list[str]] | None = None,
    progress_summary: str = '',
) -> dict:
    """Temporarily enter Probe for a newly discovered technical unknown.

    A successful Probe automatically resumes the calling formal work and its
    material scope. This transition never changes the full analysis scope.
    """
    selected = select_runtime_work.func(
        runtime=runtime,
        work='probe',
        focus=focus,
        datasource_ids=datasource_ids,
        field_names_by_datasource=field_names_by_datasource,
        progress_summary=progress_summary,
    )
    selected['tool'] = 'request_runtime_probe'
    return selected


class RuntimeMaterialUnit(BaseModel):
    question_ids: list[str] = Field(default_factory=list, max_length=8)
    fact_ids: list[str] = Field(default_factory=list, max_length=16)
    datasource_id: int = Field(gt=0)
    # This is a plan for the next focused read, not a fixed-size schema
    # payload.  Capping fields here made a valid source plan fail before the
    # model could choose a narrower implementation view.
    field_names: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list, max_length=12)
    purpose: str = Field(min_length=1, max_length=360)


class RuntimeMaterialPlanInput(BaseModel):
    progress_summary: str = Field(default='', max_length=600, description='Brief user-facing plan summary in the user language; describe the approach, not private reasoning or code.')
    focus: str = Field(min_length=1, max_length=600)
    material_units: list[RuntimeMaterialUnit] = Field(min_length=1, max_length=24)
    probe_required: bool = False
    probe_focus: str = Field(default='', max_length=500)


@tool(args_schema=RuntimeMaterialPlanInput)
def plan_runtime_materials(
    focus: str,
    material_units: list[dict],
    probe_required: bool = False,
    probe_focus: str = '',
    progress_summary: str = '',
) -> dict:
    """Create a structured data-access plan from the current insight semantics.

    This does not define metrics, formulas, charts, report sections, or
    conclusions. It only identifies the business facts, sources, fields and
    confirmed relationships the next Runtime work must understand.
    """
    normalized_units = []
    for item in material_units or []:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        datasource_id = _positive_int(item.get('datasource_id'))
        if datasource_id <= 0:
            continue
        normalized_units.append({
            'question_ids': list(dict.fromkeys(str(value).strip() for value in item.get('question_ids') or [] if str(value).strip())),
            'fact_ids': list(dict.fromkeys(str(value).strip() for value in item.get('fact_ids') or [] if str(value).strip())),
            'datasource_id': datasource_id,
            'field_names': list(dict.fromkeys(str(value).strip() for value in item.get('field_names') or [] if str(value).strip())),
            'relationship_ids': list(dict.fromkeys(str(value).strip() for value in item.get('relationship_ids') or [] if str(value).strip())),
            'purpose': str(item.get('purpose') or '').strip(),
        })
    return {
        'tool': 'plan_runtime_materials',
        'progress_summary': progress_summary,
        'status': 'success',
        'focus': str(focus or '').strip(),
        'material_units': normalized_units,
        'probe_required': bool(probe_required),
        'probe_focus': str(probe_focus or '').strip(),
    }


def _payload(message):
    if not isinstance(message, ToolMessage):
        return None
    if isinstance(message.content, dict):
        return message.content
    if not isinstance(message.content, str):
        return None
    try:
        value = json.loads(message.content)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def estimate_work_messages(messages):
    # The general conversation estimator counts textual content only. Runtime
    # source lives in tool arguments and must be included in work-view audits.
    return estimate_messages_tokens(messages) + sum(
        estimate_text_tokens(json.dumps(message.tool_calls, ensure_ascii=False))
        for message in messages if isinstance(message, AIMessage) and message.tool_calls
    )


@dataclass(frozen=True)
class RuntimeWorkView:
    work: str
    focus: str
    references: tuple[str, ...]
    datasource_ids: tuple[int, ...]
    fields: dict[str, list[str]]


def _material_scope(payload):
    ids = list(dict.fromkeys(
        parsed for value in payload.get('datasource_ids') or []
        for parsed in (_positive_int(value),) if parsed > 0
    ))
    fields = {
        str(key): list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        for key, values in (payload.get('field_names_by_datasource') or {}).items()
        if isinstance(values, list)
    }
    for key in fields:
        value = _positive_int(key)
        if value and value not in ids:
            ids.append(value)
    return ids, fields


def _resolve_work_view(messages):
    work, focus = 'understand', ''
    datasource_ids, fields = [], {}
    calls = {}
    references = []
    probe_return = None
    for message in messages:
        if isinstance(message, AIMessage):
            calls.update({call['id']: call['name'] for call in message.tool_calls})
        payload = _payload(message)
        if payload is None:
            continue
        call_name = calls.get(message.tool_call_id)
        name = call_name or payload.get('tool')
        if name in {'select_runtime_work', 'request_runtime_probe'} and payload.get('status') == 'success':
            if payload.get('work') in {'plan', 'probe', 'implement'}:
                if payload['work'] == 'probe' and work not in {'probe', 'probe_repair'}:
                    probe_return = (
                        work if work in {'implement', 'repair'} else 'implement',
                        focus, copy.deepcopy(datasource_ids), copy.deepcopy(fields),
                    )
                elif payload['work'] != 'probe':
                    probe_return = None
                work, focus = payload['work'], str(payload.get('focus') or '')
                if payload.get('datasource_ids') or payload.get('field_names_by_datasource'):
                    datasource_ids, fields = _material_scope(payload)
        elif name == 'plan_runtime_materials' and payload.get('status') == 'success':
            plan_ids, plan_fields = [], {}
            for unit in payload.get('material_units') or []:
                if not isinstance(unit, dict):
                    continue
                identity = _positive_int(unit.get('datasource_id'))
                if identity:
                    plan_ids.append(identity)
                    plan_fields.setdefault(str(identity), []).extend(unit.get('field_names') or [])
            datasource_ids, fields = _material_scope({
                'datasource_ids': plan_ids, 'field_names_by_datasource': plan_fields,
            })
            probe_return = (
                ('implement', str(payload.get('focus') or ''),
                 copy.deepcopy(datasource_ids), copy.deepcopy(fields))
                if payload.get('probe_required') else None
            )
            work = 'probe' if payload.get('probe_required') else 'implement'
            focus = str(
                payload.get('probe_focus') if payload.get('probe_required') else payload.get('focus')
                or ''
            )
        elif call_name == 'submit_analysis_program':
            if payload.get('status') == 'success':
                work = 'review'
            elif payload.get('status') == 'failed' or payload.get('error_type'):
                diagnostics = payload.get('diagnostics') or {}
                # A model-generated ``execution_intent=probe`` is not itself
                # a state transition.  The tool identity and the current
                # lifecycle decide the work state; otherwise a stale model
                # call can bounce a completed Probe back into probe_repair and
                # repeat the same observation indefinitely.
                if (
                    isinstance(diagnostics, dict)
                    and diagnostics.get('violated_contract')
                    == 'formal_tool_received_probe_program'
                ):
                    if work in {'probe', 'probe_repair'}:
                        work = 'probe_repair'
                    elif work not in {'implement', 'repair'}:
                        # No established implementation lifecycle exists yet;
                        # preserve the old safe recovery path for an initial
                        # formal-tool/probe mismatch.
                        work = 'probe_repair'
                else:
                    work = 'repair'
        elif call_name == 'probe_analysis_data':
            if (
                payload.get('error_type') in {
                    'probe_closed_for_formal_implementation',
                    'duplicate_probe_request',
                }
                or (payload.get('diagnostics') or {}).get('violated_contract')
                in {'probe_closed_for_formal_implementation', 'duplicate_probe_request'}
            ):
                # A stale provider call was blocked at the tool boundary. It
                # must not reopen Probe or replace the current implementation
                # focus with a repair loop.
                if work not in {'implement', 'repair'}:
                    work = 'implement'
            elif _is_structured_probe_observation(payload):
                # Resume the work that requested this observation. Technical
                # success does not declare other business dependencies solved.
                work, focus, datasource_ids, fields = probe_return or (
                    work if work in {'implement', 'repair'} else 'implement',
                    focus, datasource_ids, fields,
                )
                probe_return = None
            else:
                # Probe code can fail before it observes anything (for
                # example, an unsupported API or an unconfirmed relationship
                # used as a direct join).  Treating that as a completed Probe
                # discarded the Probe contract and sent the model forward
                # without either facts or a compatible repair tool.
                work = 'probe_repair'
        elif call_name == 'request_analysis_revision':
            if payload.get('status') == 'revision_required':
                work = 'repair'
        if name == 'read_runtime_reference':
            # A full reference is a one-step implementation aid, not durable
            # Runtime memory. Retaining every read reconstructed the original
            # monolithic prompt after a model reviewed several APIs.
            references = [
                item['contract_id'] for item in payload.get('references', [])
                if isinstance(item, dict) and item.get('contract_id')
            ]
    return RuntimeWorkView(work, focus, tuple(dict.fromkeys(references)), tuple(datasource_ids), fields)


def resolve_work(messages):
    view = _resolve_work_view(messages)
    return view.work, view.focus, list(view.references)


def resolve_work_material_scope(messages) -> tuple[list[int], dict[str, list[str]]]:
    """Use the same work revision and Probe return point as tool selection."""
    view = _resolve_work_view(messages)
    return list(view.datasource_ids), copy.deepcopy(view.fields)


def _resolve_prompt_material_scope(
    messages,
    *,
    work: str,
    material_plan: dict,
) -> tuple[list[int], dict[str, list[str]]]:
    """Build the compact schema projection for the next model request.

    Use the lifecycle's current scope and exact reads as dependencies. A Probe
    return restores its parent's scope; it must not leave implementation with
    the Probe's smaller field set. Unopened planned fields remain an index.
    """
    datasource_ids, fields = resolve_work_material_scope(messages)
    if not material_plan or work not in {'probe', 'probe_repair', 'implement', 'repair'}:
        return datasource_ids, fields

    call_names: dict[str, str] = {}
    opened_fields: dict[str, list[str]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            call_names.update({
                str(call.get('id') or ''): str(call.get('name') or '')
                for call in message.tool_calls
            })
            continue
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        owner = call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
        if owner == 'read_runtime_datasource_context' and payload.get('status') in {'success', 'partial'}:
            for source in payload.get('datasources') or []:
                if not isinstance(source, dict):
                    continue
                identity = str(_positive_int(source.get('datasource_id') or source.get('id')))
                opened_fields.setdefault(identity, []).extend(
                    (source.get('metadata_schema') or {}).get('properties') or {}
                )

    # Use the same state resolver as the execution tool surface. In particular,
    # a completed Probe restores its parent's fields, not the last Probe's
    # narrower explicit selection. Exact reads may add implementation dependencies.
    if opened_fields:
        for identity, values in opened_fields.items():
            if int(identity) in datasource_ids:
                fields[identity] = list(dict.fromkeys([*fields.get(identity, []), *values]))
        return datasource_ids, fields
    # Before a detail read, explicit work selections can request exact fields;
    # planned candidates alone remain an index for the first reader call.
    explicit = any(
        (_payload(message) or {}).get('tool') in {'select_runtime_work', 'request_runtime_probe'}
        and (_payload(message) or {}).get('field_names_by_datasource')
        for message in messages
    )
    return datasource_ids, fields if explicit else {}


def collect_runtime_schema_sources(messages, existing=()):
    """Promote reader observations within the same ReAct call, including resumes."""
    incoming = []
    for message in messages:
        payload = _payload(message)
        if (
            payload and payload.get('tool') == 'read_runtime_datasource_context'
            and payload.get('status') in {'success', 'partial'}
        ):
            incoming.extend(payload.get('datasources') or [])
    return merge_schema_sources(existing, incoming)


def _has_exact_schema_read(messages) -> bool:
    """Whether this working transcript already contains an exact schema read."""
    call_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            call_names.update({
                str(call.get('id') or ''): str(call.get('name') or '')
                for call in message.tool_calls
            })
            continue
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        if (
            call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
            == 'read_runtime_datasource_context'
            and payload.get('status') in {'success', 'partial'}
            and isinstance(payload.get('datasources'), list)
            and payload.get('datasources')
        ):
            return True
    return False


def _has_current_exact_schema_read(messages) -> bool:
    """Whether the current material selection already has a schema read.

    A reader result is reusable until the model deliberately selects another
    implementation/probe scope. This closes the accidental read-read loop
    without preventing a later focused dependency read.
    """
    call_names: dict[str, str] = {}
    latest_plan = -1
    latest_selection = -1
    latest_read = -1
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            call_names.update({
                str(call.get('id') or ''): str(call.get('name') or '')
                for call in message.tool_calls
            })
            continue
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        owner = call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
        if owner == 'plan_runtime_materials' and payload.get('status') == 'success':
            latest_plan = index
        elif (
            owner in {'select_runtime_work', 'request_runtime_probe'}
            and payload.get('status') == 'success'
            and payload.get('work') in {'probe', 'implement'}
        ):
            latest_selection = index
        elif (
            owner == 'read_runtime_datasource_context'
            and payload.get('status') in {'success', 'partial'}
            and payload.get('datasources')
        ):
            latest_read = index
    return latest_read > max(latest_plan, latest_selection)


def resolve_runtime_material_plan(messages) -> dict:
    """Return the latest structured material plan without replaying planning prose."""
    plan = {}
    for message in messages:
        payload = _payload(message)
        if (
            isinstance(payload, dict)
            and payload.get('tool') == 'plan_runtime_materials'
            and payload.get('status') == 'success'
        ):
            plan = {
                key: copy.deepcopy(payload.get(key))
                for key in ('focus', 'material_units', 'probe_required', 'probe_focus')
                if payload.get(key) not in (None, '', [], {})
            }
    return plan


def _is_structured_probe_observation(payload: dict | None) -> bool:
    """Return whether a Probe result contains reusable facts.

    The execution boundary historically used both ``status=observation`` and
    ``status=success`` for a successful Probe.  Working-memory classification
    already treats either a fact ledger or query snapshots as captured facts;
    the state/tool gate must use the same predicate or the Probe tool remains
    visible after a successful observation and the model can loop forever.
    """
    if not isinstance(payload, dict):
        return False
    diagnostics = payload.get('diagnostics')
    has_fact_ledger = (
        isinstance(diagnostics, dict)
        and isinstance(diagnostics.get('probe_fact_ledger'), dict)
    )
    has_query_snapshots = isinstance(payload.get('query_snapshots'), dict) and bool(
        payload.get('query_snapshots')
    )
    # Older Runtime payloads did not set ``status`` on a successful Probe but
    # did carry the same durable facts. Preserve that compatibility while
    # never promoting an explicit failure to an observation.
    return payload.get('status') in {None, 'observation', 'success'} and (
        has_fact_ledger or has_query_snapshots
    )


def _has_completed_probe_observation(messages) -> bool:
    """Whether the current lifecycle already has a structured Probe result."""
    call_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            call_names.update({
                str(call.get('id') or ''): str(call.get('name') or '')
                for call in message.tool_calls
            })
            continue
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        owner = call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
        if owner in {'probe_analysis_data', 'execute_python'} and _is_structured_probe_observation(payload):
            return True
    return False


def _has_open_probe_replan(messages) -> bool:
    """Whether a newer formal failure reopened a technical Probe decision."""
    call_names: dict[str, str] = {}
    latest_observation = -1
    latest_formal_failure = -1
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            call_names.update({
                str(call.get('id') or ''): str(call.get('name') or '')
                for call in message.tool_calls
            })
            continue
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        owner = call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
        if owner in {'probe_analysis_data', 'execute_python'} and _is_structured_probe_observation(payload):
            latest_observation = index
        elif (
            owner == 'submit_analysis_program'
            and (payload.get('status') == 'failed' or payload.get('error_type'))
        ):
            latest_formal_failure = index
    return latest_formal_failure > latest_observation


def _has_runtime_execution(messages) -> bool:
    """Whether this Runtime lifecycle has already entered executable work.

    The first pass must establish a material plan before it can Probe or write
    a program.  Repairs and Probe replans, however, already have execution
    facts and must not be sent back to initial planning.
    """
    calls: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            calls.update(str(call.get('name') or '') for call in message.tool_calls)
    return bool(calls.intersection({'probe_analysis_data', 'submit_analysis_program'}))


def resolve_effective_work(messages) -> tuple[str, str, list[str], dict]:
    """Resolve the active work and enforce the initial material-plan boundary."""
    work, focus, references = resolve_work(messages)
    material_plan = resolve_runtime_material_plan(messages)
    if (
        work in {'probe', 'implement'}
        and not material_plan
        and not _has_runtime_execution(messages)
    ):
        # A direct initial code/probe choice used to bypass the only turn that
        # transforms Preparation into a focused working data model.  Preserve
        # the user's analytical purpose as focus, but make that missing
        # lifecycle step explicit rather than silently accepting a giant view.
        return 'plan', focus or 'Organize the current analysis into a focused material plan.', references, material_plan
    return work, focus, references, material_plan


def _positive_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def work_contracts(context, work, references=()):
    snapshot = context.execution_datasource_snapshot or context.active_datasource_snapshot
    required = required_runtime_contract_ids(
        infer_query_capabilities_from_snapshot(snapshot),
        context_mode=context.context_mode,
        output_format=context.output_contract.get('type', 'standard'),
        clarification_mode=context.clarification_mode,
    )
    if work in {'understand', 'plan'}:
        required = tuple(item for item in required if item.startswith(('context.', 'interaction.')))
        if work == 'plan':
            required = tuple(dict.fromkeys([*required, 'execution.planning']))
    elif work in {'probe', 'probe_repair'}:
        required = tuple(
            'execution.probe' if item == 'execution.core' else item
            for item in required if not item.startswith('result.')
        )
    elif work in {'implement', 'repair'}:
        required = tuple(
            'execution.implementation' if item == 'execution.core' else item
            for item in required
        )
    elif work == 'review':
        required = tuple(
            item for item in required
            if item.startswith(('context.', 'interaction.'))
        )
        required = tuple(dict.fromkeys([*required, 'execution.result_review']))
    # Explicit reads may extend the view, but cannot substitute an incompatible
    # result/interaction mode. All query kinds remain available on request.
    output_id = {'standard': 'result.artifacts', 'text': 'result.text', 'json': 'result.json'}[
        context.output_contract.get('type', 'standard')]
    def compatible_reference(item: str) -> bool:
        if item.startswith('result.') and item != output_id:
            return False
        if item.startswith('interaction.') and item not in required:
            return False
        if work == 'understand':
            # The plan/replan turn decides what work comes next. Mechanical
            # code packs are deliberately queued for that compatible state;
            # otherwise a speculative read silently turns planning back into
            # the full-program prompt it is intended to avoid.
            return item.startswith(('context.', 'interaction.'))
        if work in {'probe', 'probe_repair'}:
            return not item.startswith('result.')
        return True

    extra = [item for item in references if compatible_reference(item)]
    return tuple(dict.fromkeys([*required, *extra]))


def _reference_replayed_source(context, source: str, call_id: str):
    """Reference exact duplicate Python snippets; retain unverified diagnostics."""
    lines = source.splitlines()

    def source_range(snippet):
        fragment = snippet.splitlines() if isinstance(snippet, str) else []
        if not fragment:
            return None
        matches = [index for index in range(len(lines) - len(fragment) + 1)
                   if lines[index:index + len(fragment)] == fragment]
        if len(matches) == 1:
            return matches[0] + 1, matches[0] + len(fragment)
        return None

    def project(value):
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        result = copy.deepcopy(value)
        if value.get('location_domain') not in (None, 'python_source'):
            return result
        start, end = value.get('start_line'), value.get('end_line')
        snippet = value.get('source')
        match = source_range(snippet)
        if match and isinstance(start, int) and isinstance(end, int):
            result.pop('source')
            result['source_ref'] = {'tool_call_id': call_id, 'start_line': match[0], 'end_line': match[1]}
        for key in ('context_lines', 'related_context_lines'):
            if not isinstance(value.get(key), list):
                continue
            projected = []
            for line in value[key]:
                item = copy.deepcopy(line)
                number = line.get('line_no') if isinstance(line, dict) else None
                match = source_range(line.get('code')) if isinstance(line, dict) else None
                if isinstance(number, int) and match:
                    item.pop('code')
                    item['source_ref'] = {'tool_call_id': call_id, 'line_no': match[0]}
                projected.append(item)
            result[key] = projected
        for key in ('related_contexts', 'suggested_patch_ranges'):
            if isinstance(value.get(key), list):
                result[key] = [project(item) for item in value[key]]
        return result

    return project(context)


def _active_formal_program_chain(messages) -> tuple[str | None, frozenset[str]]:
    """Return the current formal call and calls needed to reconstruct it."""
    calls: dict[str, dict] = {}
    execution_owner: dict[int, str] = {}
    ordered: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call.get('name') in {'submit_analysis_program', 'execute_python'}:
                    call_id = str(call.get('id') or '')
                    if call_id:
                        calls[call_id] = call
                        ordered.append(call_id)
        elif isinstance(message, ToolMessage):
            payload = _payload(message)
            if not isinstance(payload, dict):
                continue
            try:
                execution_id = int(payload.get('execution_id') or 0)
            except (TypeError, ValueError):
                execution_id = 0
            if execution_id > 0 and str(message.tool_call_id or '') in calls:
                execution_owner[execution_id] = str(message.tool_call_id)
    if not ordered:
        return None, frozenset()
    latest = ordered[-1]
    active = {latest}
    current = latest
    while current:
        args = calls[current].get('args') or {}
        try:
            base_execution_id = int(args.get('base_execution_id') or 0)
        except (TypeError, ValueError):
            base_execution_id = 0
        parent = execution_owner.get(base_execution_id)
        if not parent or parent in active:
            break
        active.add(parent)
        current = parent
    return latest, frozenset(active)


def _repair_uses_query_context(messages, work: str | None) -> bool:
    """Whether exact executed-query details can help the current work."""
    if work == 'review':
        return True
    if work != 'repair':
        return False
    call_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            call_names.update({str(call.get('id') or ''): str(call.get('name') or '')
                               for call in message.tool_calls})
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        if call_names.get(str(message.tool_call_id or '')) not in {
            'submit_analysis_program', 'execute_python',
        }:
            continue
        payload = _payload(message) or {}
        if not (payload.get('status') == 'failed' or payload.get('error_type')):
            continue
        error_type = str(payload.get('error_type') or payload.get('retry_type') or '').casefold()
        diagnostics = payload.get('diagnostics') if isinstance(payload.get('diagnostics'), dict) else {}
        violation = str(diagnostics.get('violated_contract') or '').casefold()
        if error_type in {
            'query_error', 'query_execution_error', 'query_result_too_large',
            'query_constraint_violation', 'no_data_found', 'probe_feedback',
            'connection_error', 'permission_error',
        }:
            return True
        return any(token in violation for token in (
            'query_', 'sql_', 'datasource_field_ownership', 'physical_execution',
        ))
    return False


def project_history(
    messages,
    *,
    memory: WorkingMemoryIndex | None = None,
    work: str | None = None,
    compact_schema_reader_results: bool = False,
    retain_query_context: bool = True,
):
    """Remove replaced code and repeated protocol material, never business facts."""
    memory = memory or build_working_memory_index(messages)
    superseded_program_events = memory.superseded_program_events
    obsolete_probe_events = set(memory.obsolete_probe_events)
    superseded_control_events = set(memory.superseded_control_events)
    # The latest action/observation must close the visible ReAct step. The
    # memory lifecycle archives it after a newer execution, not merely because
    # the next work is implement. Its facts are independently de-duplicated.
    calls = {}
    call_indices = {}
    program_sources = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call['id']] = call['name']
                source = next((call['args'].get(key) for key in _execution_source_keys(call['name'])
                               if call['args'].get(key)), None)
                if isinstance(source, str):
                    program_sources[call['id']] = source
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_indices[str(call['id'])] = index
    latest_formal_call_id, active_formal_call_ids = _active_formal_program_chain(messages)
    latest_revision_call_id = next(
        (
            str(call['id'])
            for message in reversed(messages)
            if isinstance(message, AIMessage)
            for call in reversed(message.tool_calls)
            if call['name'] == 'request_analysis_revision'
        ),
        None,
    )
    revision_events_to_archive = {
        call_id
        for call_id, call_name in calls.items()
        if call_name == 'request_analysis_revision'
        and (
            (work == 'review' and latest_formal_call_id is not None
             and call_indices.get(str(call_id), -1) < call_indices.get(str(latest_formal_call_id), -1))
            or (work == 'repair' and str(call_id) != latest_revision_call_id)
        )
    }
    latest_results = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage) and _payload(message) is not None:
            owner = calls.get(message.tool_call_id)
            if owner in {'submit_analysis_program', 'execute_python', 'probe_analysis_data'}:
                role = 'probe' if owner == 'probe_analysis_data' else 'formal'
                latest_results[role] = index
    result = []
    archived = 0
    for index, original in enumerate(messages):
        message = copy.deepcopy(original)
        if isinstance(message, AIMessage):
            retained_calls = []
            covered_probe_call_count = 0
            for call in message.tool_calls:
                if call['name'] == 'read_runtime_query_context' and not retain_query_context:
                    archived += 1
                    continue
                if str(call.get('id') or '') in revision_events_to_archive:
                    archived += 1
                    continue
                if str(call.get('id') or '') in superseded_control_events:
                    archived += 1
                    continue
                if (
                    work in {'repair', 'review'}
                    and call['name'] in {'submit_analysis_program', 'execute_python'}
                    and call['id'] not in active_formal_call_ids
                ):
                    # The current failure and current program are the only
                    # formal repair pair. Older programs remain in the audit
                    # store and their compact query facts are indexed below;
                    # replaying every failed program makes the repair view
                    # grow while giving the model competing implementations.
                    archived += 1
                    continue
                # Match the execution tool's accepted spellings and precedence.
                # Only the outbound copy is normalized; audit source is intact.
                source_keys = _execution_source_keys(call['name'])
                source = next((call['args'][key] for key in source_keys if call['args'].get(key)), None)
                if source is not None:
                    for key in source_keys:
                        call['args'].pop(key, None)
                    call['args']['code'] = source
                if call['id'] in superseded_program_events:
                    call['args']['code'] = f"[Superseded program: original retained under tool call {call['id']} in execution history.]"
                    archived += 1
                elif call['name'] == 'probe_analysis_data' and call['id'] in obsolete_probe_events:
                    # Do not leave an inert pseudo-code tool call in the
                    # outbound LangGraph state. The graph may treat it as a
                    # callable action or a model may copy its placeholder as
                    # executable Python. Its canonical facts have already
                    # been promoted into the Probe fact ledger.
                    covered_probe_call_count += 1
                    continue
                retained_calls.append(call)
            if retained_calls:
                # Tool arguments/results are the canonical work memory. The
                # free-form preamble attached to the same AI message is useful
                # for the lossless audit/SSE stream, but replaying it duplicates
                # decisions already represented by the typed action and ledgers.
                message = message.model_copy(update={'content': '', 'tool_calls': retained_calls})
            elif covered_probe_call_count:
                archived += covered_probe_call_count
                message = message.model_copy(update={'content': '', 'tool_calls': []})
            elif work in {'repair', 'review'} and message.tool_calls and not retained_calls:
                message = message.model_copy(update={'content': '', 'tool_calls': []})
            elif message.tool_calls and not retained_calls:
                message = message.model_copy(update={'content': '', 'tool_calls': []})
        if (
            isinstance(message, ToolMessage)
            and calls.get(message.tool_call_id) == 'read_runtime_query_context'
            and not retain_query_context
        ):
            continue
        if (
            isinstance(message, ToolMessage)
            and str(message.tool_call_id or '') in obsolete_probe_events
        ):
            # The paired Probe tool result is represented by the canonical
            # fact ledger rather than another raw transcript entry.
            continue
        if (
            isinstance(message, ToolMessage)
            and str(message.tool_call_id or '') in superseded_control_events
        ):
            continue
        if (
            isinstance(message, ToolMessage)
            and str(message.tool_call_id or '') in revision_events_to_archive
        ):
            continue
        if (
            work in {'repair', 'review'}
            and isinstance(message, ToolMessage)
            and calls.get(message.tool_call_id) in {'submit_analysis_program', 'execute_python'}
            and message.tool_call_id not in active_formal_call_ids
        ):
            # Keep only the current formal failure/result pair in the model
            # view; the complete historical execution remains queryable from
            # the persisted audit records.
            continue
        payload = _payload(message)
        if payload is not None:
            if payload.get('tool') == 'read_runtime_reference':
                for reference in payload.get('references', []):
                    if isinstance(reference, dict):
                        reference.pop('content', None)
                payload['delivery'] = 'Requested contracts are supplied in the current working context.'
            if (
                compact_schema_reader_results
                and calls.get(message.tool_call_id) == 'read_runtime_datasource_context'
                and payload.get('status') in {'success', 'partial'}
            ):
                # Keep the ToolMessage paired with its AI tool call, but do
                # not replay its verbose Schema payload.  The same exact,
                # active source/field view is delivered from the canonical
                # workspace in ``runtime-datasource-envelope``.  This is an
                # outbound prompt projection only; the lossless transcript
                # and execution snapshot remain unchanged.
                payload = {**payload,
                    'tool': 'read_runtime_datasource_context',
                    'status': payload.get('status'),
                    'delivery': (
                        'Exact Schema details for the active material scope are '
                        'available in runtime datasource_details.'
                    ),
                    'datasource_ids': [
                        _positive_int(source.get('datasource_id') or source.get('id'))
                        for source in payload.get('datasources') or []
                        if isinstance(source, dict)
                        and _positive_int(source.get('datasource_id') or source.get('id')) > 0
                    ],
                    'unavailable_datasource_ids': list(payload.get('unavailable_datasource_ids') or []),
                }
                payload.pop('datasources', None)
            if calls.get(message.tool_call_id) in {'probe_analysis_data', 'submit_analysis_program', 'execute_python'}:
                if (message.tool_call_id == latest_formal_call_id
                        and program_sources.get(message.tool_call_id)
                        and isinstance(payload.get('code_error_context'), dict)):
                    payload['code_error_context'] = _reference_replayed_source(
                        payload['code_error_context'], program_sources[message.tool_call_id],
                        message.tool_call_id,
                    )
                diagnostics = payload.get('diagnostics')
                if isinstance(diagnostics, dict):
                    diagnostics.pop('formal_program_work_packet', None)
                # The canonical current program already exists in the paired AI
                # tool call; error payload copies are not another code version.
                payload.pop('current_failed_code', None)
                payload.pop('current_failed_code_truncated', None)
                role = 'probe' if calls.get(message.tool_call_id) == 'probe_analysis_data' else 'formal'
                if index < latest_results.get(role, -1):
                    for key in ('code_error_context', 'repair_target', 'stderr_text',
                                'previous_failure_messages', 'previous_failure_hints'):
                        payload.pop(key, None)
                    payload['history_status'] = 'earlier_attempt_not_proof_of_resolution'
                if (
                    role == 'formal'
                    and message.tool_call_id in active_formal_call_ids
                    and message.tool_call_id != latest_formal_call_id
                ):
                    payload = {
                        'tool': payload.get('tool') or 'execute_python',
                        'status': payload.get('status'),
                        'execution_id': payload.get('execution_id'),
                        'error_type': payload.get('error_type'),
                        'history_status': 'program_derivation_base',
                    }
            message = message.model_copy(update={'content': json.dumps(payload, ensure_ascii=False)})
        result.append(message)
    return result, archived


def _compact_work_value(value, *, depth: int = 0):
    """Bound nested query metadata in the default Runtime work view.

    Query snapshots can legitimately contain a large resolved population (for
    example, hundreds of batch IDs).  The model does not need that population
    duplicated in every repair request: the definition ID points to the exact
    immutable query record through ``read_runtime_query_context``.  Preserve
    the shape and small samples for reasoning while making the default view
    bounded.
    """
    if depth >= 6:
        return '[nested query metadata omitted; read by definition_id]'
    if isinstance(value, dict):
        return {
            str(key): _compact_work_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if len(value) <= 24:
            return [_compact_work_value(item, depth=depth + 1) for item in value]
        return {
            '_truncated': True,
            'count': len(value),
            'head': [_compact_work_value(item, depth=depth + 1) for item in value[:8]],
            'tail': [_compact_work_value(item, depth=depth + 1) for item in value[-2:]],
            'detail_source': 'read_runtime_query_context(definition_id)',
        }
    if isinstance(value, str) and len(value) > 2400:
        return {
            '_truncated': True,
            'length': len(value),
            'head': value[:1600],
            'tail': value[-400:],
            'detail_source': 'read_runtime_query_context(definition_id)',
        }
    return value


def _compact_query_fact(snapshot: dict) -> dict:
    """Index executed query semantics; exact implementation is readable by ID."""
    implementation = snapshot.get('implementation')
    implementation = implementation if isinstance(implementation, dict) else {}
    execution = snapshot.get('execution')
    execution = execution if isinstance(execution, dict) else {}
    projections = implementation.get('projection_semantics')
    return {
        'query_key': snapshot.get('query_key'),
        'query_constraints': _compact_work_value(snapshot.get('query_constraints') or {}),
        'analysis_scope': _compact_work_value(snapshot.get('analysis_scope') or {}),
        'query_scope': _compact_work_value(snapshot.get('query_scope') or {}),
        'implementation': {
            key: implementation.get(key)
            for key in (
                'logical_query_family', 'datasource_identifiers', 'datasource_identifier',
                'filter_expressions', 'file_filters', 'aggregation_hints', 'physical_execution',
            )
            if implementation.get(key) not in (None, '', [], {})
        } | ({'projection_semantics': projections} if isinstance(projections, list) and projections else {}),
        'execution': {
            key: execution.get(key)
            for key in (
                'status', 'row_count', 'family_row_count', 'columns', 'effective_no_data',
                'error_type', 'probe_profile',
            )
            if execution.get(key) not in (None, '', [], {})
        },
    }


def _query_detail(snapshot: dict) -> dict:
    detail = _compact_query_fact(snapshot)
    # The explicit reader is the opt-in path for exact query constraints.  Do
    # not make it inherit the bounded working-view projection above.
    for key in ('query_constraints', 'analysis_scope', 'query_scope'):
        if isinstance(snapshot.get(key), (dict, list)):
            detail[key] = copy.deepcopy(snapshot[key])
    implementation = snapshot.get('implementation')
    detail['implementation'] = copy.deepcopy(implementation) if isinstance(implementation, dict) else {}
    return detail


def _query_detail_id(snapshot: dict) -> str:
    serialized = json.dumps(_query_detail(snapshot), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'query_fact_' + hashlib.sha256(serialized.encode('utf-8')).hexdigest()


class RuntimeQueryContextInput(BaseModel):
    definition_ids: list[str] = Field(min_length=1, max_length=8,
        description='Exact definition_ids from the current executed-query-facts index.')


@tool(args_schema=RuntimeQueryContextInput)
def read_runtime_query_context(runtime: ToolRuntime[CustomContext], definition_ids: list[str]) -> dict:
    """Read exact executed query implementations from this analysis history.

    Use when a query summary is insufficient for implementation or repair.
    Returns recorded query constraints and implementation, not result rows.
    This read-only operation does not execute queries or change working focus.
    """
    requested = set(definition_ids)
    found = {}
    for message in runtime.state.get('messages') or []:
        payload = _payload(message)
        if not isinstance(payload, dict):
            continue
        snapshots = payload.get('query_snapshots')
        if not isinstance(snapshots, dict):
            continue
        for snapshot in snapshots.values():
            if not isinstance(snapshot, dict):
                continue
            identity = _query_detail_id(snapshot)
            if identity in requested:
                found[identity] = _query_detail(snapshot)
    return {'tool': 'read_runtime_query_context', 'status': 'success' if requested <= found.keys() else 'partial',
            'queries': found, 'unavailable_definition_ids': sorted(requested - found.keys())}


def _compact_probe_ledger(ledger: dict) -> dict:
    """Keep execution-derived Probe facts compact and typed for the next turn."""
    observations = []
    for raw in ledger.get('observations') or []:
        if not isinstance(raw, dict):
            continue
        observation = {
            key: raw.get(key)
            for key in (
                'query_key', 'datasource_id', 'datasource_identifiers', 'query_constraints', 'query_scope',
                'status', 'row_count', 'effective_no_data', 'columns', 'error_type',
                'probe_profile', 'value_profile', 'value_samples', 'distinct_values',
                'projection_semantics', 'aggregate_facts', 'capacity_risks',
            )
            if raw.get(key) not in (None, '', [], {})
        }
        if observation:
            observations.append(observation)
    return {
        'schema_version': int(ledger.get('schema_version') or 1),
        'observations': observations,
        'confirmed_facts': list(ledger.get('confirmed_facts') or []),
        'unresolved_facts': list(ledger.get('unresolved_facts') or []),
        'aggregate_facts': list(ledger.get('aggregate_facts') or []),
        'capacity_risks': list(ledger.get('capacity_risks') or []),
    }


def _merge_probe_ledgers(ledgers: list[dict]) -> dict:
    """Merge Probe facts by logical observation identity for the active work view.

    Audit payloads retain every execution.  The model should instead see the
    latest structured observation for each query shape, so repeated Probe calls
    do not recursively grow the next ReAct request.
    """
    observations: dict[str, dict] = {}
    confirmed: dict[str, object] = {}
    unresolved: dict[str, object] = {}
    aggregates: dict[str, object] = {}
    capacity_risks: dict[str, object] = {}
    def fact_identity(value):
        if isinstance(value, dict) and value.get('fact_id'):
            return _stable_revision({
                key: value.get(key) for key in (
                    'fact_id', 'query_key', 'datasource_id', 'datasource_identifiers',
                    'query_constraints', 'query_scope',
                )
            })
        return _stable_revision(value)

    schema_version = 1
    for ledger in ledgers:
        schema_version = max(schema_version, int(ledger.get('schema_version') or 1))
        for observation in ledger.get('observations') or []:
            if not isinstance(observation, dict):
                continue
            identity_source = {
                key: observation.get(key)
                for key in ('query_key', 'datasource_id', 'datasource_identifiers', 'query_constraints', 'query_scope')
                if observation.get(key) not in (None, '', [], {})
            }
            observations[_stable_revision(identity_source or observation)] = observation
        for value in ledger.get('confirmed_facts') or []:
            identity = fact_identity(value)
            confirmed[identity] = value
            unresolved.pop(identity, None)
        for value in ledger.get('unresolved_facts') or []:
            identity = fact_identity(value)
            unresolved[identity] = value
            confirmed.pop(identity, None)
        for value in ledger.get('aggregate_facts') or []:
            aggregates[fact_identity(value)] = value
        for value in ledger.get('capacity_risks') or []:
            capacity_risks[fact_identity(value)] = value
    return {
        'schema_version': schema_version,
        'observations': list(observations.values()),
        'confirmed_facts': list(confirmed.values()),
        'unresolved_facts': list(unresolved.values()),
        'aggregate_facts': list(aggregates.values()),
        'capacity_risks': list(capacity_risks.values()),
    }


def collect_runtime_facts(messages):
    """Project query/probe facts out of tool replay into canonical fact ledgers."""
    query_facts: dict[str, dict] = {}
    probe_facts: dict[str, dict] = {}
    probe_ledgers: list[dict] = []
    probe_payload_positions: list[int] = []
    projected = []
    call_names = {}
    for message in messages:
        if isinstance(message, AIMessage):
            call_names.update({str(call['id']): str(call['name']) for call in message.tool_calls})
    # QuerySnapshot is execution feedback for the currently repairable program,
    # not an accumulating transcript. Each formal retry owns a complete fresh
    # execution context, so retaining snapshots from every replaced formal
    # attempt makes repair prompts grow while supplying stale query paths.
    # Keep the newest formal execution snapshot set; Probe observations have a
    # separate fact ledger and are not affected by this projection.
    formal_snapshot_positions = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage)
        and call_names.get(str(getattr(message, 'tool_call_id', '') or ''))
        in {'submit_analysis_program', 'execute_python'}
        and isinstance((_payload(message) or {}).get('query_snapshots'), dict)
    ]
    latest_formal_snapshot_position = max(formal_snapshot_positions, default=-1)

    for index, original in enumerate(messages):
        payload = _payload(original)
        if payload is None:
            projected.append(original)
            continue
        owner = call_names.get(str(getattr(original, 'tool_call_id', '') or ''))
        snapshots = payload.get('query_snapshots')
        refs = {}
        is_replaced_formal_snapshot = (
            owner in {'submit_analysis_program', 'execute_python'}
            and latest_formal_snapshot_position >= 0
            and index != latest_formal_snapshot_position
        )
        if isinstance(snapshots, dict) and not is_replaced_formal_snapshot:
            for key, snapshot in snapshots.items():
                if not isinstance(snapshot, dict):
                    refs[key] = {'snapshot': snapshot}
                    continue
                fact = _compact_query_fact(snapshot)
                identity = _query_detail_id(snapshot)
                query_facts[identity] = fact
                refs[key] = {'definition_id': identity}
            payload.pop('query_snapshots')
            payload['query_snapshot_refs'] = refs
        elif isinstance(snapshots, dict):
            # The audit record remains persisted unchanged; this outbound
            # working view explicitly marks old execution snapshots as
            # superseded instead of silently pretending they still constrain
            # the latest repair.
            payload.pop('query_snapshots')
            payload['query_snapshot_refs'] = {}
            payload['query_snapshot_history'] = 'superseded_by_latest_formal_execution'

        diagnostics = payload.get('diagnostics')
        if owner == 'probe_analysis_data' and isinstance(diagnostics, dict):
            ledger = diagnostics.get('probe_fact_ledger')
            if isinstance(ledger, dict):
                probe_ledgers.append(_compact_probe_ledger(ledger))
                # The original worker stdout remains in the persisted execution
                # record. The transcript points at the merged current ledger
                # after all completed Probe calls have been projected.
                payload['diagnostics'] = {'probe_fact_refs': []}
                payload.pop('stdout_text', None)
                payload.pop('stderr_text', None)
                probe_payload_positions.append(len(projected))
        projected.append(original.model_copy(update={'content': json.dumps(payload, ensure_ascii=False)}))
    if probe_ledgers:
        fact = _merge_probe_ledgers(probe_ledgers)
        serialized = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        identity = 'probe_fact_' + hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        probe_facts[identity] = fact
        for position in probe_payload_positions:
            payload = _payload(projected[position])
            if payload is None:
                continue
            payload['diagnostics'] = {'probe_fact_refs': [identity]}
            projected[position] = projected[position].model_copy(
                update={'content': json.dumps(payload, ensure_ascii=False)}
            )
    return projected, query_facts, probe_facts


def merge_runtime_probe_facts(
    probe_facts: dict[str, dict],
    context_ledgers: list[dict] | None,
) -> dict[str, dict]:
    """Merge transcript and typed Runtime-memory Probe ledgers once per view."""
    ledgers = [
        ledger for ledger in (context_ledgers or [])
        if isinstance(ledger, dict)
    ]
    ledgers.extend(
        ledger for ledger in probe_facts.values()
        if isinstance(ledger, dict)
    )
    if not ledgers:
        return {}
    merged = _merge_probe_ledgers(ledgers)
    serialized = json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    identity = 'probe_fact_' + hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    return {identity: merged}


def collect_query_facts(messages):
    """Backward-compatible query-only adapter for existing callers/tests."""
    projected, facts, _ = collect_runtime_facts(messages)
    return projected, facts


def _strip_runtime_transient_sections(content: str) -> str:
    """Remove prior request views while retaining the immutable base context."""
    for tag in (
        'runtime-contract-summaries',
        'runtime-contracts',
        'runtime-datasource-envelope',
        'runtime-working-context',
    ):
        content = re.sub(
            # Prompt prose may mention a tag literally (for example,
            # ``<runtime-working-context>`` in an instruction).  Only an
            # envelope tag occupying its own line is mutable request state.
            rf'(?m)^<{tag}>\s*$.*?^</{tag}>\s*$', '', content, flags=re.DOTALL,
        )
    return content.strip()


def _base_system_parts(request, messages) -> list[str]:
    """Return immutable system instructions, never a prior Runtime work view.

    The initial request contains a full datasource envelope and bounded protocol
    summaries so the graph can start.  They are source material, not durable
    outbound context: the working compiler below always rebuilds both from the
    current work state.  Keeping either here would duplicate the focused view
    and make later calls grow with the initial prompt.
    """
    parts = []
    seen = set()
    candidates = [request.system_message, *(
        message for message in messages if isinstance(message, SystemMessage)
    )]
    for candidate in candidates:
        if not isinstance(candidate, SystemMessage) or not isinstance(candidate.content, str):
            continue
        content = _strip_runtime_transient_sections(candidate.content)
        if not content:
            continue
        identity = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if identity not in seen:
            seen.add(identity)
            parts.append(content)
    return parts


def build_working_request(request):
    context = request.runtime.context
    work, focus, references, material_plan = resolve_effective_work(request.messages)
    material_datasource_ids, material_fields = _resolve_prompt_material_scope(
        request.messages,
        work=work,
        material_plan=material_plan,
    )
    contract_ids = work_contracts(context, work, references)
    message_memory = build_working_memory_index(request.messages)
    fact_messages, query_facts, probe_facts = collect_runtime_facts(request.messages)
    probe_facts = merge_runtime_probe_facts(
        probe_facts,
        getattr(context, 'runtime_probe_fact_ledgers', []),
    )
    retain_query_context = _repair_uses_query_context(request.messages, work)
    schema_detail_workspace = collect_runtime_schema_sources(
        request.messages, getattr(context, 'runtime_schema_detail_workspace', []),
    )
    canonical_schema_covered = (
        work in {'probe', 'probe_repair', 'implement', 'repair'}
        and runtime_schema_workspace_covers_material_scope(
            schema_detail_workspace,
            material_datasource_ids,
            material_fields,
        )
    )
    messages, archived = project_history(
        fact_messages,
        memory=message_memory,
        work=work,
        # Result review reasons over the executed program, query facts and
        # candidate artifacts. Replaying exploratory Schema reads adds no new
        # evidence and can make this deliberately focused state larger than
        # implementation itself.
        compact_schema_reader_results=(canonical_schema_covered or work == 'review'),
        retain_query_context=retain_query_context,
    )
    memory = message_memory.with_items(build_context_memory_items(context, contract_ids))
    # The full transcript stays in graph state. This outbound view owns exactly
    # one base context and one state-specific working context; prior views are
    # deliberately removed instead of recursively becoming new base context.
    system_parts = _base_system_parts(request, messages)
    messages = [message for message in messages if not isinstance(message, SystemMessage)]
    datasource_view = build_runtime_datasource_working_message(
        context.active_datasource_snapshot,
        context.execution_datasource_snapshot,
        work=work,
        datasource_ids=material_datasource_ids if work in {'probe', 'probe_repair', 'implement', 'repair'} else (),
        field_names_by_datasource=material_fields if work in {'probe', 'probe_repair', 'implement', 'repair'} else {},
        material_plan=material_plan if work in {'probe', 'probe_repair', 'implement', 'repair'} else None,
        schema_detail_workspace=schema_detail_workspace,
        omit_replayed_details=canonical_schema_covered,
    )
    exact_schema_required = bool(
        material_plan
        and work in {'probe', 'probe_repair', 'implement', 'repair'}
        and not canonical_schema_covered
    )
    work_schema_instruction = (
        '\n当前材料计划的 Schema 明细尚未完整加载；已读取的字段定义仍然有效。'
        '在生成 Probe 或正式代码前，调用 read_runtime_datasource_context 补充当前焦点缺少的数据源和字段明细；'
        '读取结果才是字段类型、格式、枚举、关系和业务描述的依据。'
        if exact_schema_required else
        '\n本轮已经通过 read_runtime_datasource_context 获取了当前工作所需的精确 Schema；'
        '后续只使用该读取结果，不要重复展开全部来源，也不要猜测未读取字段。'
        if material_plan and work in {'probe', 'probe_repair', 'implement', 'repair'} and not material_fields else
        '\n当前已加载本工作声明的精确 Schema。若发现新的字段或关系依赖，读取该精确明细后再继续，不要猜测字段语义。'
    )
    current = SystemMessage(
        '<runtime-working-context>\n'
        + json.dumps({
            'work': work,
            'focus': focus,
            'contract_ids': contract_ids,
            'runtime_material_plan': material_plan,
            'material_datasource_ids': material_datasource_ids if work in {'probe', 'probe_repair', 'implement', 'repair'} else [],
            'material_fields_by_datasource': material_fields if work in {'probe', 'probe_repair', 'implement', 'repair'} else {},
        }, ensure_ascii=False)
        + ('\n理解阶段先选择 plan。计划阶段只组织业务事实、来源、字段和已确认关系，不定义指标、图表或报告。'
           '计划产物是后续 Probe、正式实现和修复共同复用的材料范围，不改变完整分析范围。'
           if work == 'understand' else
           '\n计划阶段使用 plan_runtime_materials 组织当前问题的事实、来源、字段和关系；技术未知才标记为需要 Probe。'
           '计划只固化可读取的材料范围，不定义指标、图表或报告。'
           if work == 'plan' else
           work_schema_instruction)
        + ('每次 Probe 完成后必须先基于事实账本重新选择下一项工作；只有新的、未被账本覆盖的技术未知才选择 Probe。'
           if work == 'understand' and probe_facts else '')
        + '修复保持完整分析目标。若需要进一步探测，可以切换回 probe；探测完成不自动代表全部未知已解决。'
        + '\n' + load_runtime_work_contract_text(
            contract_ids, work=work, output_format=context.output_contract.get('type', 'standard'),
            example_contract_ids=references)
        + ('\n<executed-query-facts>\n'
           + '工具消息中的 query_snapshot_refs 指向下列执行依据索引。不同版本分别保留，不代表旧失败已经解决。\n'
           + '需要实际执行语句或精确查询实现时，用 definition_id 调用 read_runtime_query_context；该工具只读取本轮记录，不执行查询。\n'
           + json.dumps(query_facts, ensure_ascii=False, separators=(',', ':'))
           + '\n</executed-query-facts>' if query_facts and retain_query_context else '')
        + ('\n<probe-fact-ledger>\n'
           + '以下事实来自已执行 Probe 的结构化观察；它们只证明各自记录的查询范围，不能替代正式计算。\n'
           + json.dumps(probe_facts, ensure_ascii=False, separators=(',', ':'))
           + '\n</probe-fact-ledger>' if probe_facts else '')
        + '\n</runtime-working-context>'
    )
    system = SystemMessage('\n\n'.join([*system_parts, datasource_view.content, current.content]))
    allowed_execution = {'probe': 'probe_analysis_data', 'probe_repair': 'probe_analysis_data', 'implement': 'submit_analysis_program',
                         'repair': 'submit_analysis_program'}.get(work)
    def tool_name(item):
        if isinstance(item, dict):
            return item.get('name') or (item.get('function') or {}).get('name')
        return getattr(item, 'name', None)
    def visible_in_work(item) -> bool:
        name = tool_name(item)
        # ``execute_python`` is the legacy shared worker behind the two
        # intent-specific Runtime tools. Leaving it visible lets a model bypass
        # the state-specific tool surface and submit Probe source while the
        # current work is formal implementation. Runtime must expose only the
        # public semantic tools so their execution-mode binding is enforceable.
        if name == 'execute_python':
            return False
        # ``plan`` has one producer: the material-plan tool.  Letting the
        # model select Probe/implementation again at this point made the
        # plan optional, so executable work could start without the focused
        # source/field view that the plan is responsible for producing.
        if name == 'select_runtime_work':
            # Work selection owns only the entry into the material-plan
            # lifecycle. Exposing the generic selector after that point permits
            # same-state transitions such as implement -> implement: they do no
            # analytical work, but become fresh control memory and defer code.
            return work == 'understand'
        if name == 'request_runtime_probe':
            # Executable work has one explicit escape hatch. A newly discovered
            # technical unknown can enter Probe and then resume its caller;
            # Probe and review already have deterministic next states.
            return work in {'implement', 'repair'}
        if name in {'probe_analysis_data', 'submit_analysis_program'}:
            return name == allowed_execution
        if name == 'accept_analysis_result':
            return work == 'review'
        if name == 'request_analysis_revision':
            return work == 'review'
        if name == 'request_analysis_clarification' and work == 'review':
            return False
        if name == 'plan_runtime_materials':
            return work == 'plan'
        # Contract packs are selected locally from the work state and actual
        # datasource capability union. During understand there is no code to
        # write, so a model-directed reference read only creates a loop and
        # cannot add a new business fact. It becomes available once a
        # compatible execution state has been selected.
        if name == 'read_runtime_reference' and work in {'understand', 'plan', 'review'}:
            return False
        if name == 'read_runtime_query_context':
            return bool(query_facts) and retain_query_context
        # A material plan chooses business facts, sources and exact fields.
        # The compact index is sufficient to discover candidates but not to
        # establish a field's formula, enum, format or relationship meaning.
        # Let the plan request that local detail before it commits its typed
        # material units; it remains unable to run Probe or formal code.
        if name == 'read_runtime_datasource_context':
            if work == 'review':
                return False
            if work == 'understand':
                return False
            # A material plan is a working selection, not proof that all
            # implementation dependencies were discovered. Reads preserve
            # the current focus and return exact Schema through tool results.
            # Coverage permits de-duplicating delivered material, not closing
            # discovery of new dependencies during implementation or repair.
        return True

    tools = [item for item in request.tools if visible_in_work(item)]
    logger.debug(
        'Runtime tool surface work=%s available=%s selected=%s',
        work,
        [f'{type(item).__name__}:{tool_name(item)!r}' for item in request.tools],
        [f'{type(item).__name__}:{tool_name(item)!r}' for item in tools],
    )
    memory_counts = {
        kind.value: len(memory.of_kind(kind))
        for kind in WorkingMemoryKind
        if memory.of_kind(kind)
    }
    base_system_tokens = estimate_work_messages([SystemMessage('\n\n'.join(system_parts))]) if system_parts else 0
    datasource_tokens = estimate_work_messages([datasource_view])
    work_context_tokens = estimate_work_messages([current])
    history_tokens = estimate_work_messages(messages)
    tool_schema_chars = sum(
        len(json.dumps(item.args_schema.model_json_schema(), ensure_ascii=False))
        for item in tools
        if getattr(item, 'args_schema', None) is not None
    )
    logger.info('Runtime work view state=%s contracts=%s memory=%s archived_code=%s raw_tokens=%s view_tokens=%s components=%s',
                work, list(contract_ids), memory_counts, archived,
                estimate_work_messages([*([request.system_message] if request.system_message else []), *request.messages]),
                estimate_work_messages([*([system] if system else []), *messages]),
                {
                    'base_system_tokens': base_system_tokens,
                    'datasource_tokens': datasource_tokens,
                    'work_context_tokens': work_context_tokens,
                    'history_tokens': history_tokens,
                    'tool_schema_chars': tool_schema_chars,
                })
    return request.override(messages=messages, system_message=system, tools=tools)


class RuntimeWorkingContextMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        activity = 'model:' + uuid.uuid4().hex
        projected = build_working_request(request)
        work = resolve_effective_work(request.messages)[0]
        emit_model_progress(activity, work, 'running')
        try:
            result = handler(projected)
        except Exception:
            emit_model_progress(activity, work, 'failed')
            raise
        emit_model_progress(activity, work, 'completed')
        return result

    async def awrap_model_call(self, request, handler):
        activity = 'model:' + uuid.uuid4().hex
        projected = build_working_request(request)
        work = resolve_effective_work(request.messages)[0]
        emit_model_progress(activity, work, 'running')
        try:
            result = await handler(projected)
        except Exception:
            emit_model_progress(activity, work, 'failed')
            raise
        emit_model_progress(activity, work, 'completed')
        return result
