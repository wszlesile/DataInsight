"""State-driven model request views over a lossless Runtime transcript."""

import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, field_validator, model_validator

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
from agent.runtime_driver_state import (
    RUNTIME_WORK_PACKET_PAYLOAD_KEY,
    RuntimeWorkPacket,
    failed_probe_queries,
    new_runtime_work_packet,
    reduce_runtime_tool_result,
    runtime_phase_from_work,
    runtime_phase_work,
    runtime_work_packet_from_dict,
)
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
    SEMANTIC_PLAN = 'semantic_plan'
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
                WorkingMemoryKind.SEMANTIC_PLAN,
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
                    if args.get('definition_source') or any(args.get(key) for key in _execution_source_keys(name)):
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
        if owner == 'plan_runtime_semantics' and payload.get('status') == 'success':
            add(WorkingMemoryKind.SEMANTIC_PLAN, 'runtime-semantic-plan', index, call_id, payload)
        elif owner in {'select_runtime_work', 'request_runtime_probe'} and payload.get('status') == 'success':
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
            WorkingMemoryKind.SEMANTIC_PLAN,
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
        'runtime_work_packet': (
            WorkingMemoryKind.RUNTIME_CONTROL, 'control:runtime-work-packet', ('runtime',),
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


class RuntimeCalculationRequirement(BaseModel):
    purpose: str = Field(min_length=1, max_length=240, description='The business quantity to calculate, not a report section.')
    definition: str = Field(min_length=1, max_length=600, description='Business meaning and supplied calculation requirements, including selection scope and unresolved meaning. This text is not executable algorithm authority: the formal program owns one native definition or Python function. Retain original user/schema formulas as requirements rather than rewriting them into a second algorithm.')
    basis: Literal['user', 'conversation_rule', 'schema', 'analytical_derivation', 'unresolved']
    input_grain: str = Field(default='', max_length=300, description='What one input row represents; include join multiplicity when combining sources.')
    output_grain: str = Field(default='', max_length=300, description='Dimensions retained in the computed result.')
    unit: str = Field(default='', max_length=160, description='Input/output units and conversion or weighting required by aggregation.')
    result_fields: dict[str, str] = Field(default_factory=dict, description='Business meaning and unit of output fields within output_grain. Distinguish counts, shares, totals and averages. Executable formulas live once in the formal program, not in this description. Legacy formula descriptions remain readable requirements, not independently executed definitions.')


class RuntimeCalculationRule(RuntimeCalculationRequirement):
    """Legacy plan dependencies; new generation declares them in executable definitions."""

    input_fact_ids: list[str] = Field(default_factory=list, description='Existing source fact IDs or previously computed result IDs consumed by this calculation, within the same question.')
    output_fact_ids: list[str] = Field(default_factory=list, description='New result IDs produced directly by this rule. Each ID is unique within the question and distinct from all facts[].fact_id and other calculation results, even when an input column and its aggregate have similar names. Other rules reuse these results through input_fact_ids. Result meaning is defined by this rule purpose, definition, grain and unit; results are not repeated in facts[].')
    group_by_fact_ids: list[str] | None = Field(default=None, description='Exact facts retained as output grouping keys. [] means a single overall result; null means grouping is not settled. Context/display attributes do not become grouping keys.')
    entity_key_fact_ids: list[str] = Field(default_factory=list, description='Facts that identify the counted business entity, when counting distinct entities rather than input records.')


class RuntimeSemanticUnknown(BaseModel):
    kind: Literal['schema', 'probe', 'business_clarification']
    issue: str = Field(min_length=1, max_length=400)
    consequence: str = Field(min_length=1, max_length=400)


class RuntimeCriterionSource(BaseModel):
    source: Literal['user', 'conversation_rule', 'schema']
    reference: str = Field(min_length=1, max_length=240, description='Locate the supplied rule: user question, conversation rule, or datasource ID + field/schema property path.')
    excerpt: str = Field(min_length=1, max_length=500, description='Actual supplied rule text supporting this criterion, not a paraphrased conclusion or an invented numeric cutoff. This is a source citation, not proof of the model interpretation.')


class RuntimeDecisionIntent(BaseModel):
    """Interpretation needs and supplied authority, before executable decisions exist."""
    purpose: str = Field(min_length=1, max_length=240, description='Business comparison or recommendation that needs a runtime decision.')
    basis: Literal['user', 'conversation_rule', 'schema', 'mathematical', 'analytical_derivation', 'unresolved'] = Field(
        description='Basis stated by the analysis plan, not whether the predicate can be calculated and not proof that a business judgement is valid. user/conversation_rule/schema means an actual supplied criterion supports the judgement. mathematical means a direct arithmetic or ordering relation such as equal, greater, lower or tied. analytical_derivation means a transparent computation from supplied facts and does not itself establish a business standard. A chosen numeric cutoff does not establish business risk, health, efficiency or adequacy; absent supplied authority, such interpretations remain unresolved.'
    )
    criterion: str = Field(default='', max_length=500, description='The actual supplied business criterion and where it comes from, or the observable mathematical relation being interpreted. A numeric predicate alone is not a business criterion. When no business criterion is supplied, describe the missing basis and keep the interpretation factual.')
    source_references: list[RuntimeCriterionSource] = Field(default_factory=list, description='Supplied business-rule citations, separate from supporting_fact_ids which locate computed inputs. A Schema field or enumeration alone is not a business judgement rule. No external rule is needed for a mathematical equality/order/difference; without a supplied business criterion, retain factual comparison rather than asserting health/risk.')


class RuntimeDecisionCondition(RuntimeDecisionIntent):
    """Stored-plan reader; retain legacy prose without requesting a second algorithm."""

    condition: str = Field(default='', max_length=500, description='Legacy predicate requirement, not merely whether inputs exist. Executable conditions are defined in the formal program.')
    when_true: str = Field(default='', max_length=400, description='Legacy candidate expression, not an evaluated conclusion.')
    otherwise: str = Field(default='', max_length=400, description='Legacy candidate expression; not just a missing-input message and not proof of health or safety.')
    supporting_fact_ids: list[str] = Field(default_factory=list, description='Facts used to evaluate the predicate, referenced within the same question. This does not supply external benchmarks or business authority.')


class RuntimeFactSourceBinding(BaseModel):
    datasource_id: int = Field(gt=0)
    fields: list[str] = Field(min_length=1)
    role: str = Field(
        min_length=1, max_length=80,
        description='Analytical use of the bound source fields, e.g. measure, dimension, time, join or context. Descriptive metadata, not a work-state or execution permission.'
    )


class RuntimeSemanticFact(BaseModel):
    fact_id: str = Field(min_length=1, description='Unique ID of this source or unresolved fact within the question, distinct from all calculation_rules[].output_fact_ids. A source field and its aggregated result have separate identities.')
    meaning: str = Field(min_length=1, max_length=400)
    status: Literal['direct', 'derived', 'unresolved']
    source_bindings: list[RuntimeFactSourceBinding] = Field(default_factory=list)
    derived_from_fact_ids: list[str] = Field(default_factory=list)
    unresolved_reason: str = Field(default='', max_length=400)

    @model_validator(mode='after')
    def check_grounding(self) -> 'RuntimeSemanticFact':
        if self.status == 'direct' and not self.source_bindings:
            raise ValueError('direct fact requires a source binding')
        if self.status == 'derived' and not (self.derived_from_fact_ids or self.source_bindings):
            raise ValueError('derived fact requires fact dependencies or source bindings')
        if self.status == 'unresolved' and not self.unresolved_reason:
            raise ValueError('unresolved fact requires a reason')
        if self.fact_id in self.derived_from_fact_ids:
            raise ValueError('fact cannot depend on itself')
        return self


class RuntimeQuestionSemantics(BaseModel):
    question_id: str = Field(min_length=1)
    analysis_focus: str = Field(min_length=1, max_length=500)
    population: str = Field(default='', max_length=500)
    time_basis: str = Field(default='', max_length=500)
    grouping_and_comparison: str = Field(default='', max_length=500)
    facts: list[RuntimeSemanticFact] = Field(default_factory=list, description='Source-bound facts and unresolved business meanings needed by the analysis. Results declared in calculation_rules.output_fact_ids need not be declared again here.')
    relationship_ids: list[str] = Field(default_factory=list)
    calculation_rules: list[RuntimeCalculationRule] = Field(default_factory=list, description='Business quantities, supplied requirements and input/output scope for this question. Actual algorithms are defined once in the formal program. Named results may be declared in output_fact_ids; not an exhaustive metric catalogue.')
    decision_conditions: list[RuntimeDecisionCondition] = Field(default_factory=list, description='Concrete Runtime branches for planned comparisons or recommendations; not predicted conclusions. An empty list means no judgement is planned. An unresolved judgement must retain a factual fallback.')
    unknowns: list[RuntimeSemanticUnknown] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_fact_references(self) -> 'RuntimeQuestionSemantics':
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError('fact IDs must be unique within a question')
        output_ids = [identifier for rule in self.calculation_rules for identifier in rule.output_fact_ids]
        if len(output_ids) != len(set(output_ids)):
            producers: dict[str, list[str]] = {}
            for rule_index, rule in enumerate(self.calculation_rules):
                for output_index, identifier in enumerate(rule.output_fact_ids):
                    producers.setdefault(identifier, []).append(
                        f'calculation_rules[{rule_index}].output_fact_ids[{output_index}]'
                    )
            conflicts = {identifier: locations for identifier, locations in producers.items() if len(locations) > 1}
            raise ValueError(
                'each calculation result must have one producer within a question; conflicting declarations: '
                + json.dumps(conflicts, ensure_ascii=False)
            )
        source_ids = {fact.fact_id for fact in self.facts if fact.status != 'derived'}
        if set(output_ids) & source_ids:
            collisions = set(output_ids) & source_ids
            locations = {
                identifier: {
                    'facts': [f'facts[{index}].fact_id' for index, fact in enumerate(self.facts)
                              if fact.fact_id == identifier],
                    'results': [f'calculation_rules[{rule_index}].output_fact_ids[{output_index}]'
                                for rule_index, rule in enumerate(self.calculation_rules)
                                for output_index, value in enumerate(rule.output_fact_ids)
                                if value == identifier],
                }
                for identifier in sorted(collisions)
            }
            raise ValueError('calculation result IDs cannot overwrite source or unresolved facts; '
                             'conflicting declarations: ' + json.dumps(locations, ensure_ascii=False))
        references: dict[str, list[str]] = {}

        def record(values: list[str], path: str) -> None:
            for position, identifier in enumerate(values):
                references.setdefault(identifier, []).append(f'{path}[{position}]')

        for index, fact in enumerate(self.facts):
            record(fact.derived_from_fact_ids, f'facts[{index}].derived_from_fact_ids')
        for index, rule in enumerate(self.calculation_rules):
            for field in ('input_fact_ids', 'group_by_fact_ids', 'entity_key_fact_ids'):
                record(getattr(rule, field) or [], f'calculation_rules[{index}].{field}')
        for index, condition in enumerate(self.decision_conditions):
            record(condition.supporting_fact_ids, f'decision_conditions[{index}].supporting_fact_ids')
        missing = set(references) - (set(fact_ids) | set(output_ids))
        if missing:
            raise ValueError(
                f'unknown fact references within question: {sorted(missing)}; reference locations: '
                + json.dumps({identifier: references[identifier] for identifier in sorted(missing)}, ensure_ascii=False)
            )
        return self


class RuntimeSemanticPlanInput(BaseModel):
    progress_summary: str = Field(default='', max_length=600, description='Brief user-facing approach summary in the user language; no private reasoning or raw data.')
    analysis_goal: str = Field(min_length=1, max_length=600)
    questions: list[RuntimeQuestionSemantics] = Field(min_length=1)

    @field_validator('questions', mode='before')
    @classmethod
    def decode_questions(cls, value):
        if isinstance(value, str):
            return json.loads(value)
        return value


class RuntimeIntentQuestion(RuntimeQuestionSemantics):
    calculation_rules: list['RuntimeIntentCalculationRule'] = Field(
        default_factory=list,
        description='Business quantities and supplied requirements. Actual result identities and dependencies are declared by executable definitions, not a second symbolic graph here.',
    )
    decision_conditions: list['RuntimeIntentDecision'] = Field(
        default_factory=list,
        description='Comparisons or recommendations to investigate and their supplied authority. Executable inputs, predicates and result-dependent prose are defined once in the formal program.',
    )

    @model_validator(mode='before')
    @classmethod
    def discard_legacy_execution_links_from_new_intents(cls, value):
        if not isinstance(value, dict):
            return value
        value = copy.deepcopy(value)
        decisions = value.get('decision_conditions') or []
        legacy_branch_fields = {'condition', 'when_true', 'otherwise'}
        if not any(isinstance(item, dict) and legacy_branch_fields & item.keys() for item in decisions):
            legacy_calculation_fields = {
                'input_fact_ids', 'output_fact_ids', 'group_by_fact_ids', 'entity_key_fact_ids',
            }
            for rule in value.get('calculation_rules') or []:
                if isinstance(rule, dict):
                    for field in legacy_calculation_fields:
                        rule.pop(field, None)
        return value

    @model_validator(mode='after')
    def check_fact_references(self) -> 'RuntimeIntentQuestion':
        # Only source-material references belong to planning. The calculation
        # graph is declared and resolved by the actual execution definitions.
        RuntimeQuestionSemantics.model_validate({
            **self.model_dump(), 'calculation_rules': [], 'decision_conditions': [],
        })
        return self


class RuntimeIntentCalculationRule(RuntimeCalculationRequirement):
    """New requirement schema that can losslessly read older plan payloads."""

    model_config = ConfigDict(extra='allow')

    @model_validator(mode='before')
    @classmethod
    def preserve_only_legacy_execution_links(cls, value):
        if not isinstance(value, dict):
            return value
        legacy_fields = {'input_fact_ids', 'output_fact_ids', 'group_by_fact_ids', 'entity_key_fact_ids'}
        allowed = set(RuntimeCalculationRequirement.model_fields)
        if legacy_fields & value.keys():
            allowed |= legacy_fields
        return {key: item for key, item in value.items() if key in allowed}


class RuntimeIntentDecision(RuntimeDecisionIntent):
    """New intent schema with lossless acceptance of legacy decision fields.

    The old predicate and branch prose are deliberately absent from the public
    schema, but historical callers may still send them. Preserve those extra
    values for snapshot compatibility instead of silently discarding them.
    """

    model_config = ConfigDict(extra='allow')

    @model_validator(mode='before')
    @classmethod
    def preserve_only_legacy_branch_payload(cls, value):
        if not isinstance(value, dict):
            return value
        legacy_branch_fields = {'condition', 'when_true', 'otherwise'}
        allowed = set(RuntimeDecisionIntent.model_fields)
        if legacy_branch_fields & value.keys():
            allowed |= legacy_branch_fields | {'supporting_fact_ids'}
        return {key: item for key, item in value.items() if key in allowed}


class RuntimeSemanticIntentInput(RuntimeSemanticPlanInput):
    """Generation input; legacy executable-looking prose stays in the storage reader."""

    questions: list[RuntimeIntentQuestion] = Field(min_length=1)


def semantic_plan_work_view(plan: dict) -> dict:
    """Compact display defaults, preserving explicit semantic boundaries."""
    if not plan:
        return {}
    try:
        typed = RuntimeSemanticPlanInput.model_validate(plan)
    except ValidationError:
        return copy.deepcopy(plan)
    supplied = {key: value for key, value in plan.items() if key != 'progress_summary'}
    if typed.model_dump(exclude_unset=True, exclude={'progress_summary'}) != supplied:
        return copy.deepcopy(plan)
    view = typed.model_dump(exclude_defaults=True, exclude={'progress_summary'})
    def preserve_boundaries(target: dict, original: dict) -> None:
        # Empty dependencies/citations and unknown grouping are decisions, not
        # reconstructible display defaults for an LLM consumer.
        for key, value in original.items():
            if value is None or value == [] or value == {}:
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                preserve_boundaries(target[key], value)
            elif isinstance(value, list) and isinstance(target.get(key), list):
                for child, supplied_child in zip(target[key], value):
                    if isinstance(child, dict) and isinstance(supplied_child, dict):
                        preserve_boundaries(child, supplied_child)

    preserve_boundaries(view, supplied)
    return view


def semantic_reporting_scope(plan: dict) -> dict:
    """Project planned judgment authority for the post-execution authoring state."""
    questions = plan.get('questions') if isinstance(plan, dict) else []
    scopes = []
    for question in questions or []:
        if not isinstance(question, dict):
            continue
        decisions = question.get('decision_conditions') or []
        scopes.append({
            'question_id': question.get('question_id'),
            'decision_conditions': copy.deepcopy(decisions),
            'judgment_scope': 'planned' if decisions else 'descriptive_only',
        })
    return {'questions': scopes}


def semantic_implementation_bindings(plan: dict) -> list[dict]:
    """Resolve plan identities without copying algorithms or certifying meaning."""
    bindings = []
    for question in plan.get('questions') or []:
        if not isinstance(question, dict):
            continue
        symbols = {}
        for index, fact in enumerate(question.get('facts') or []):
            if isinstance(fact, dict) and fact.get('fact_id'):
                symbols[fact['fact_id']] = f'facts[{index}]'
        for index, rule in enumerate(question.get('calculation_rules') or []):
            if not isinstance(rule, dict):
                continue
            for identifier in rule.get('output_fact_ids') or []:
                symbols[identifier] = f'calculation_rules[{index}]'
        decisions = []
        for index, decision in enumerate(question.get('decision_conditions') or []):
            if not isinstance(decision, dict):
                continue
            basis = decision.get('basis')
            mode = ('mathematical_comparison' if basis == 'mathematical' else
                    'analytical_calculation' if basis == 'analytical_derivation' else
                    'declared_business_rule' if basis in {'user', 'conversation_rule', 'schema'} else
                    'factual_only')
            decisions.append({
                'path': f'decision_conditions[{index}]',
                'mode': mode,
                'inputs': {
                    identifier: symbols.get(identifier)
                    for identifier in decision.get('supporting_fact_ids') or []
                },
            })
        bindings.append({
            'question_id': question.get('question_id'),
            'result_definitions': symbols,
            # New intents deliberately have no second result-ID graph. Keep
            # their calculation requirements addressable independently of the
            # historical output_fact_ids catalogue above.
            'calculation_requirements': [
                {'path': f'calculation_rules[{index}]',
                 'execution_owner': 'formal_program',
                 **{key: copy.deepcopy(rule[key]) for key in
                    RuntimeCalculationRequirement.model_fields if key in rule}}
                for index, rule in enumerate(question.get('calculation_rules') or [])
                if isinstance(rule, dict)
            ],
            'decision_execution': decisions,
        })
    return bindings


def _semantic_plan_input_for_context(context):
    semantic = (getattr(context, 'active_datasource_snapshot', {}) or {}).get('analysis_semantic_context') or {}
    question_ids = list(dict.fromkeys(
        str(item['question_id']) for item in semantic.get('business_questions') or []
        if isinstance(item, dict) and item.get('question_id')
    ))
    if not question_ids:
        return RuntimeSemanticIntentInput
    question_schema = create_model(
        'CurrentRuntimeQuestionSemantics',
        __base__=RuntimeIntentQuestion,
        question_id=(str, Field(
            min_length=1,
            description='Existing business-question identity. Put its analytical facets inside this question, not additional question objects.',
            json_schema_extra={'enum': question_ids},
        )),
    )
    return create_model(
        'CurrentRuntimeSemanticPlanInput',
        __base__=RuntimeSemanticIntentInput,
        questions=(list[question_schema], Field(
            min_length=len(question_ids), max_length=len(question_ids),
            description='One item per existing business question; retain each identity exactly once.',
        )),
    )


@tool(args_schema=RuntimeSemanticIntentInput)
def plan_runtime_semantics(
    runtime: ToolRuntime[CustomContext],
    analysis_goal: str,
    questions: list[dict],
    progress_summary: str = '',
) -> dict:
    """Record the analytical meaning before selecting execution materials.

    The JSON plan owns fact requirements, population, time attribution, supplied
    interpretation authority and consequential unknowns. Executable algorithms,
    predicates and result-dependent expressions belong to the formal program.
    Stored legacy plans remain readable. A Probe may justify revising the plan.
    """
    plan = RuntimeSemanticIntentInput.model_validate({
        'analysis_goal': analysis_goal,
        'questions': questions,
        'progress_summary': progress_summary,
    })
    semantic = (
        (getattr(runtime.context, 'active_datasource_snapshot', {}) or {})
        .get('analysis_semantic_context') or {}
    )
    expected_ids = {
        str(item.get('question_id') or '')
        for item in semantic.get('business_questions') or []
        if isinstance(item, dict) and item.get('question_id')
    }
    actual_ids = [item.question_id for item in plan.questions]
    # A single input and output question have an unambiguous identity even
    # when the model assigns its own label. Never infer multi-question mapping.
    if len(expected_ids) == len(plan.questions) == 1:
        plan.questions[0].question_id = next(iter(expected_ids))
        actual_ids = [plan.questions[0].question_id]
    if len(actual_ids) != len(set(actual_ids)) or (expected_ids and set(actual_ids) != expected_ids):
        return {
            'tool': 'plan_runtime_semantics',
            'status': 'failed',
            'message': 'Semantic plan must cover each current business question exactly once.',
            'expected_question_ids': sorted(expected_ids),
        }
    active_snapshot = getattr(runtime.context, 'active_datasource_snapshot', {}) or {}
    execution_snapshot = getattr(runtime.context, 'execution_datasource_snapshot', {}) or {}
    selected_ids = set(active_snapshot.get('selected_datasource_ids') or [])
    # The outbound Schema projection is a work view, never the authority for
    # field existence. Detail reads may reveal fields absent from that view.
    selected_sources = {
        int(source.get('datasource_id') or source.get('id') or 0): source
        for source in (execution_snapshot.get('selected_datasource_snapshot')
                       or active_snapshot.get('selected_datasource_snapshot') or [])
        if isinstance(source, dict) and source.get('datasource_id', source.get('id'))
        and (not selected_ids or int(source.get('datasource_id') or source.get('id') or 0) in selected_ids)
    }
    available_relationship_ids = {
        str(relationship.get('relationship_id'))
        for source in selected_sources.values()
        for relationship in ((source.get('metadata_schema') or {}).get('relationships') or [])
        if isinstance(relationship, dict) and relationship.get('relationship_id')
    }
    problems = []
    for question in plan.questions:
        # Selection requirements describe coverage, not Runtime fact identities.
        # Requiring identical IDs rejects valid decompositions and encourages a
        # single umbrella fact instead of meaningful calculation dependencies.
        unknown_relationships = set(question.relationship_ids) - available_relationship_ids
        if unknown_relationships and selected_sources:
            problems.append(f'{question.question_id}: unknown relationship IDs {sorted(unknown_relationships)}')
        for fact in question.facts:
            for binding in fact.source_bindings:
                source = selected_sources.get(binding.datasource_id)
                if selected_sources and source is None:
                    problems.append(f'{fact.fact_id}: datasource {binding.datasource_id} is not selected')
                    continue
                properties = ((source or {}).get('metadata_schema') or {}).get('properties') or {}
                if properties:
                    unknown_fields = set(binding.fields) - set(properties)
                    if unknown_fields:
                        problems.append(f'{fact.fact_id}: unknown fields {sorted(unknown_fields)} in datasource {binding.datasource_id}')
    if problems:
        return {
            'tool': 'plan_runtime_semantics',
            'status': 'failed',
            'message': 'Semantic facts must reference the current questions and selected Schema.',
            'problems': problems,
        }
    semantic_plan = plan.model_dump(exclude={'progress_summary'})
    for question, stored_question in zip(plan.questions, semantic_plan['questions']):
        # Do not manufacture empty legacy predicates for a newly generated
        # intent. Retain every explicitly supplied field in an old plan.
        stored_question['decision_conditions'] = [
            decision.model_dump(exclude_unset=True) for decision in question.decision_conditions
        ]
        stored_question['calculation_rules'] = [
            rule.model_dump(exclude_unset=True) for rule in question.calculation_rules
        ]
    return {
        'tool': 'plan_runtime_semantics',
        'status': 'success',
        'progress_summary': progress_summary,
        'semantic_plan': semantic_plan,
    }


def _semantic_plan_argument_failure(request: ToolCallRequest) -> ToolMessage | None:
    if request.tool_call.get('name') != 'plan_runtime_semantics':
        return None
    try:
        RuntimeSemanticIntentInput.model_validate(request.tool_call.get('args') or {})
    except ValidationError as error:
        # The paired call already carries the candidate plan. Return precise
        # errors without duplicating that potentially large object in feedback.
        semantic = ((getattr(request.runtime.context, 'active_datasource_snapshot', {}) or {})
                    .get('analysis_semantic_context') or {})
        expected_ids = sorted({str(item['question_id'])
                               for item in semantic.get('business_questions') or []
                               if isinstance(item, dict) and item.get('question_id')})
        return ToolMessage(
            tool_call_id=request.tool_call['id'], name='plan_runtime_semantics', status='error',
            content=json.dumps({
                'tool': 'plan_runtime_semantics', 'status': 'failed',
                'error_type': 'invalid_arguments',
                'message': 'Correct the indicated fields in the submitted semantic plan.',
                **({'expected_question_ids': expected_ids} if expected_ids else {}),
                'problems': [
                    {'location': list(item['loc']), 'type': item['type'], 'message': item['msg']}
                    for item in error.errors(include_input=False, include_context=False, include_url=False)
                ],
            }, ensure_ascii=False),
        )
    return None


def _program_protocol_argument_failure(request: ToolCallRequest) -> ToolMessage | None:
    """Enforce the advertised authoring protocol at the execution boundary.

    Model tool schemas are guidance; the registered tool retains a legacy
    replay schema. A new malformed call must not create a legacy ancestor.
    """
    if request.tool_call.get('name') != 'submit_analysis_program':
        return None
    messages = (request.state or {}).get('messages') or []
    call_id = request.tool_call.get('id')
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and any(call.get('id') == call_id for call in message.tool_calls):
            messages = messages[:index]
            break
    context = request.runtime.context
    work = runtime_phase_work(_ensure_runtime_work_packet(context, messages).phase)
    if not _uses_native_definition(messages, work):
        return None
    from agent.tools import ExecutableAnalysisProgramInput
    try:
        ExecutableAnalysisProgramInput.model_validate(request.tool_call.get('args') or {})
    except ValidationError as error:
        return ToolMessage(tool_call_id=call_id, name='submit_analysis_program', status='error',
                           content=json.dumps({
                               'tool': 'submit_analysis_program', 'status': 'failed',
                               'error_type': 'invalid_arguments',
                               'message': 'Submit the complete definition_source required by the current tool schema.',
                               'diagnostics': {'new_execution_created': False},
                               'problems': [{'location': list(item['loc']), 'type': item['type'], 'message': item['msg']}
                                            for item in error.errors(include_input=False, include_context=False, include_url=False)],
                           }, ensure_ascii=False))
    return None


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
        if name == 'plan_runtime_semantics' and payload.get('status') == 'success':
            work = 'plan'
            focus = str((payload.get('semantic_plan') or {}).get('analysis_goal') or focus)
            datasource_ids, fields = [], {}
            probe_return = None
        elif name in {'select_runtime_work', 'request_runtime_probe'} and payload.get('status') == 'success':
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
                work = 'repair'
        elif call_name == 'probe_analysis_data':
            if (
                payload.get('error_type') in {
                    'probe_closed_for_formal_implementation',
                    'duplicate_probe_request',
                    'probe_not_authorized_for_current_phase',
                }
                or (payload.get('diagnostics') or {}).get('violated_contract')
                in {
                    'probe_closed_for_formal_implementation',
                    'duplicate_probe_request',
                    'probe_not_authorized_for_current_phase',
                }
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
    packet: RuntimeWorkPacket | None = None,
) -> tuple[list[int], dict[str, list[str]]]:
    """Build the compact schema projection for the next model request.

    Use the lifecycle's current scope and exact reads as dependencies. A Probe
    return restores its parent's scope; it must not leave implementation with
    the Probe's smaller field set. Unopened planned fields remain an index.
    """
    if packet is not None and not packet.workspace_id.startswith('legacy:'):
        datasource_ids = list(packet.active_datasource_ids)
        fields = {
            str(key): list(values)
            for key, values in packet.active_fields_by_datasource.items()
        }
    else:
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


def _merge_semantic_source_scope(
    datasource_ids: list[int],
    fields: dict[str, list[str]],
    semantic_plan: dict,
    selected_ids: set[int],
) -> tuple[list[int], dict[str, list[str]]]:
    """Carry declared bindings into implementation, without inferring any."""
    result_ids = list(datasource_ids)
    result_fields = copy.deepcopy(fields)
    for question in semantic_plan.get('questions') or []:
        if not isinstance(question, dict):
            continue
        for fact in question.get('facts') or []:
            if not isinstance(fact, dict):
                continue
            for binding in fact.get('source_bindings') or []:
                if not isinstance(binding, dict):
                    continue
                identity = _positive_int(binding.get('datasource_id'))
                if identity not in selected_ids:
                    continue
                if identity not in result_ids:
                    result_ids.append(identity)
                key = str(identity)
                result_fields[key] = list(dict.fromkeys([
                    *result_fields.get(key, []),
                    *(field for field in binding.get('fields') or [] if isinstance(field, str)),
                ]))
    return result_ids, result_fields


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
    ledger = diagnostics['probe_fact_ledger'] if has_fact_ledger else {}
    if not has_fact_ledger and has_query_snapshots:
        ledger = {'observations': [snapshot.get('execution', {})
            for snapshot in payload['query_snapshots'].values() if isinstance(snapshot, dict)]}
    if failed_probe_queries(diagnostics if isinstance(diagnostics, dict) else {}, ledger):
        return False
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


def work_contracts(context, work, references=(), *, definition_program=False):
    snapshot = context.execution_datasource_snapshot or context.active_datasource_snapshot
    required = required_runtime_contract_ids(
        infer_query_capabilities_from_snapshot(snapshot),
        context_mode=context.context_mode,
        output_format=context.output_contract.get('type', 'standard'),
        clarification_mode=context.clarification_mode,
    )
    if work in {'understand', 'plan'}:
        time_series_semantics = 'query.time_series' in required
        required = tuple(item for item in required if item.startswith(('context.', 'interaction.')))
        if time_series_semantics:
            required = (*required, 'context.time_series')
        if work == 'understand':
            required = tuple(dict.fromkeys([*required, 'execution.semantic_planning']))
        else:
            required = tuple(dict.fromkeys([*required, 'execution.planning']))
    elif work in {'probe', 'probe_repair'}:
        required = tuple(
            'execution.probe' if item == 'execution.core' else item
            for item in required if not item.startswith('result.')
        )
    elif work in {'implement', 'repair'}:
        required = tuple(
            ('execution.definition' if definition_program else 'execution.implementation')
            if item == 'execution.core' else
            'result.visualizations' if definition_program and item == 'result.artifacts' else item
            for item in required
        )
    elif work == 'express':
        time_series_semantics = 'query.time_series' in required
        required = tuple(
            item for item in required
            if item.startswith(('context.', 'interaction.', 'result.'))
        )
        required = tuple(dict.fromkeys([*required, 'execution.expression']))
        if time_series_semantics:
            required = (*required, 'context.time_series')
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
    if definition_program and work in {'implement', 'repair'} and output_id == 'result.artifacts':
        output_id = 'result.visualizations'
        references = tuple(output_id if item == 'result.artifacts' else item for item in references)
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
            return item.startswith(('context.', 'interaction.')) or item == 'execution.semantic_planning'
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
        for key in ('related_contexts', 'suggested_patch_ranges', 'allowed_ranges'):
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
            diagnostics = payload.get('diagnostics') or {}
            created_execution = not (
                isinstance(diagnostics, dict)
                and diagnostics.get('new_execution_created') is False
            )
            if (created_execution and execution_id > 0
                    and str(message.tool_call_id or '') in calls):
                execution_owner[execution_id] = str(message.tool_call_id)
    if not ordered:
        return None, frozenset()
    latest = ordered[-1]
    active = {latest}
    current = latest
    while current:
        args = calls[current].get('args') or {}
        if args.get('definition_source') or any(str(args.get(key) or '').strip()
               for key in _execution_source_keys(calls[current].get('name', ''))):
            break
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


def _uses_native_definition(messages, work: str) -> bool:
    """A diagnostic does not select a program protocol; its actual producer does."""
    if work == 'implement':
        return True
    _, active_calls = _active_formal_program_chain(messages)
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        for call in reversed(message.tool_calls):
            if str(call.get('id') or '') not in active_calls:
                continue
            args = call.get('args') or {}
            if args.get('definition_source'):
                return True
            if any(str(args.get(key) or '').strip() for key in _execution_source_keys(call.get('name', ''))):
                rejected = any(isinstance(result, ToolMessage) and result.tool_call_id == call.get('id')
                               and ((_payload(result) or {}).get('diagnostics') or {}).get('new_execution_created') is False
                               for result in messages)
                if rejected:
                    continue
                return False
    # A Probe failure or malformed first submission has no legacy program to
    # preserve. Keep the fresh-analysis contract rather than inventing one.
    return True


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
    semantic_plan_available: bool = False,
    discard_invalidated_plans: bool = False,
    material_plan_available: bool = False,
    retain_query_context: bool = True,
):
    """Remove replaced code and repeated protocol material, never business facts."""
    memory = memory or build_working_memory_index(messages)
    superseded_program_events = memory.superseded_program_events
    obsolete_probe_events = set(memory.obsolete_probe_events)
    superseded_control_events = set(memory.superseded_control_events)
    successful_semantic_calls = {
        item.source_event_id for item in memory.of_kind(WorkingMemoryKind.SEMANTIC_PLAN)
    }
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
    pending_presentation_calls = {
        str(message.tool_call_id or '')
        for message in messages
        if isinstance(message, ToolMessage)
        and calls.get(str(message.tool_call_id or '')) == 'submit_analysis_program'
        and (_payload(message) or {}).get('status') == 'presentation_pending'
    }
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_indices[str(call['id'])] = index
    failed_semantic_calls = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        identity = str(message.tool_call_id or '')
        if calls.get(identity) != 'plan_runtime_semantics':
            continue
        payload = _payload(message)
        if message.status == 'error' or (payload and payload.get('status') in {'failed', 'error'}):
            failed_semantic_calls.add(identity)
    latest_semantic_failure_index = max(
        (call_indices.get(identity, -1) for identity in failed_semantic_calls), default=-1,
    )
    # A failed plan revision replaces the earlier failed candidate, not the
    # accepted plan. Keep its complete arguments and latest protocol feedback.
    superseded_control_events.update(
        identity for identity in failed_semantic_calls
        if call_indices.get(identity, -1) < latest_semantic_failure_index
    )
    for kind, owner, available in (
        (WorkingMemoryKind.SEMANTIC_PLAN, 'plan_runtime_semantics', semantic_plan_available),
        (WorkingMemoryKind.MATERIAL_PLAN, 'plan_runtime_materials', material_plan_available),
    ):
        if not available:
            continue
        success_ids = {item.source_event_id for item in memory.of_kind(kind)}
        latest_success_index = max((call_indices.get(identity, -1) for identity in success_ids), default=-1)
        superseded_control_events.update(
            identity for identity, name in calls.items()
            if name == owner and identity not in success_ids
            and call_indices.get(identity, -1) < latest_success_index
        )
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
            completed_actions = []
            covered_probe_call_count = 0
            for call in message.tool_calls:
                if (
                    discard_invalidated_plans
                    and (
                        (call['name'] == 'plan_runtime_semantics' and not semantic_plan_available)
                        or (call['name'] == 'plan_runtime_materials' and not material_plan_available)
                    )
                ):
                    archived += 1
                    continue
                if (
                    semantic_plan_available
                    and call['name'] == 'plan_runtime_semantics'
                    and str(call.get('id') or '') in successful_semantic_calls
                    and str(call.get('id') or '') not in superseded_control_events
                ):
                    completed_actions.append({
                        'action': 'plan_runtime_semantics',
                        'tool_call_id': str(call['id']),
                        'status': 'success',
                        'analysis_goal': str(call['args'].get('analysis_goal') or ''),
                        'question_ids': [
                            str(item.get('question_id') or '')
                            for item in call['args'].get('questions') or []
                            if isinstance(item, dict)
                        ],
                        'delivery': 'Current semantic plan is in runtime_semantic_plan.',
                    })
                    continue
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
                source_port = ('definition_source' if call['name'] == 'submit_analysis_program'
                               and call['args'].get('definition_source') else 'code')
                source = (call['args'].get('definition_source')
                          if call['name'] == 'submit_analysis_program' else None)
                source = source or next((call['args'][key] for key in source_keys if call['args'].get(key)), None)
                if source is not None:
                    if work == 'express' and str(call.get('id') or '') in pending_presentation_calls:
                        # The worker has already consumed this source. The
                        # expression model needs the preserved plan and the
                        # actual candidate facts, not another copy of the
                        # implementation that produced them. Keep the paired
                        # tool-call shape valid while projecting only the
                        # outbound history; the canonical transcript is intact.
                        for key in (*source_keys, 'definition_source'):
                            call['args'].pop(key, None)
                        call['args']['definition_source'] = (
                            '[已执行的定义源码仅保留在原始 Runtime 历史；表达阶段使用下方本次候选事实，不重新执行。]'
                        )
                        archived += 1
                    else:
                        for key in (*source_keys, 'definition_source'):
                            call['args'].pop(key, None)
                        # A definition is host-invoked; a complete script owns
                        # its entry point. History must preserve that boundary
                        # rather than teaching repair to switch execution ports.
                        call['args'][source_port] = source
                if call['id'] in superseded_program_events:
                    call['args'][source_port] = f"[Superseded program: original retained under tool call {call['id']} in execution history.]"
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
            if completed_actions:
                # An assistant JSON completion example invites the model to
                # announce another completed action instead of calling its tool.
                memory = HumanMessage(
                    content=json.dumps({'runtime_recorded_history': completed_actions}, ensure_ascii=False),
                    name='runtime_memory',
                )
                if retained_calls:
                    result.append(memory)
                    message = message.model_copy(update={'content': '', 'tool_calls': retained_calls})
                else:
                    message = memory
            elif retained_calls:
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
            and discard_invalidated_plans
            and (
                (calls.get(message.tool_call_id) == 'plan_runtime_semantics' and not semantic_plan_available)
                or (calls.get(message.tool_call_id) == 'plan_runtime_materials' and not material_plan_available)
            )
        ):
            continue
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
            and semantic_plan_available
            and str(message.tool_call_id or '') in successful_semantic_calls
            and calls.get(message.tool_call_id) == 'plan_runtime_semantics'
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
            if (
                semantic_plan_available
                and calls.get(message.tool_call_id) == 'plan_runtime_semantics'
                and payload.get('status') == 'success'
            ):
                payload = {
                    'tool': 'plan_runtime_semantics',
                    'status': 'success',
                    'delivery': 'Current semantic plan is in runtime_semantic_plan.',
                }
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
                if (message.tool_call_id == latest_formal_call_id
                        and program_sources.get(message.tool_call_id)
                        and isinstance(payload.get('repair_target'), dict)):
                    payload['repair_target'] = _reference_replayed_source(
                        payload['repair_target'], program_sources[message.tool_call_id],
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
                'error_type', 'error_message', 'probe_profile', 'temporal_coverage',
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
                'status', 'row_count', 'effective_no_data', 'columns', 'error_type', 'error_message',
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
        # The work packet travels in the lossless transcript so the outer
        # invocation loop can persist it. It is control-plane state, not an
        # observation for the model.
        payload.pop(RUNTIME_WORK_PACKET_PAYLOAD_KEY, None)
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


def _ensure_runtime_work_packet(context, messages) -> RuntimeWorkPacket:
    """Return the active packet, bootstrapping only legacy in-flight graphs."""
    packet = runtime_work_packet_from_dict(
        getattr(context, 'runtime_work_packet', None)
    )
    if packet is not None and not packet.workspace_id.startswith('legacy:'):
        return packet
    work, focus, _, material_plan = resolve_effective_work(messages)
    if packet is None:
        packet = new_runtime_work_packet(
            workspace_id='legacy:' + uuid.uuid4().hex,
            focus=focus or getattr(context, 'user_query', ''),
        )
    packet = replace(
        packet,
        phase=runtime_phase_from_work(work),
        focus=focus or packet.focus,
        material_manifest=dict(material_plan or {}),
        reason='legacy_transcript_bootstrap',
    )
    context.runtime_work_packet = packet.to_dict()
    return packet


def _command_tool_message(result: Command) -> ToolMessage | None:
    update = result.update if isinstance(result.update, dict) else {}
    for message in reversed(list(update.get('messages') or [])):
        if isinstance(message, ToolMessage):
            return message
    return None


def _tool_result_payload(result) -> dict:
    message = result if isinstance(result, ToolMessage) else (
        _command_tool_message(result) if isinstance(result, Command) else None
    )
    return _payload(message) or {}


def _attach_runtime_work_packet(result, packet: RuntimeWorkPacket):
    """Attach internal state for outer-round persistence, not model context."""
    packet_payload = packet.to_dict()

    def attach(message: ToolMessage) -> ToolMessage:
        payload = _payload(message)
        if payload is None:
            return message
        payload[RUNTIME_WORK_PACKET_PAYLOAD_KEY] = packet_payload
        return message.model_copy(update={
            'content': json.dumps(payload, ensure_ascii=False),
        })

    if isinstance(result, ToolMessage):
        return attach(result)
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result
    update = dict(result.update)
    messages = list(update.get('messages') or [])
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], ToolMessage):
            messages[index] = attach(messages[index])
            break
    update['messages'] = messages
    return replace(result, update=update)


def _reduce_runtime_tool_call(request: ToolCallRequest, result):
    context = request.runtime.context
    state = request.state if isinstance(request.state, dict) else {}
    packet = _ensure_runtime_work_packet(context, state.get('messages') or [])
    payload = _tool_result_payload(result)
    update = result.update if isinstance(result, Command) and isinstance(result.update, dict) else {}
    next_packet = reduce_runtime_tool_result(
        packet,
        tool_name=str(request.tool_call.get('name') or ''),
        tool_call_id=str(request.tool_call.get('id') or ''),
        arguments=request.tool_call.get('args') or {},
        result=payload,
        analysis_complete=bool(update.get('analysis_complete')),
    )
    context.runtime_work_packet = next_packet.to_dict()
    return _attach_runtime_work_packet(result, next_packet)


def build_working_request(request):
    context = request.runtime.context
    packet = _ensure_runtime_work_packet(context, request.messages)
    _, legacy_focus, references, _ = resolve_effective_work(request.messages)
    work = runtime_phase_work(packet.phase)
    focus = packet.focus or legacy_focus
    material_plan = packet.material_manifest
    material_datasource_ids, material_fields = _resolve_prompt_material_scope(
        request.messages,
        work=work,
        material_plan=material_plan,
        packet=packet,
    )
    if work in {'plan', 'implement', 'repair'} and packet.semantic_plan:
        active_snapshot = context.active_datasource_snapshot or {}
        selected_ids = {
            _positive_int(identity) for identity in active_snapshot.get('selected_datasource_ids') or []
        }
        if not selected_ids:
            selected_ids = {
                _positive_int(source.get('datasource_id') or source.get('id'))
                for source in (active_snapshot.get('selected_datasource_snapshot')
                               or (context.execution_datasource_snapshot or {}).get('selected_datasource_snapshot') or [])
                if isinstance(source, dict)
            }
        selected_ids.discard(0)
        material_datasource_ids, material_fields = _merge_semantic_source_scope(
            material_datasource_ids, material_fields, packet.semantic_plan, selected_ids,
        )
    latest_program, _ = _active_formal_program_chain(request.messages)
    # New analysis enters through the host-scheduled definition contract.
    # Repairs keep the protocol of the active persisted program so legacy
    # snapshots remain repairable without rewriting their execution model.
    definition_program = _uses_native_definition(request.messages, work)
    contract_ids = work_contracts(context, work, references, definition_program=definition_program)
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
    execution_schema_sources = (
        (getattr(context, 'execution_datasource_snapshot', {}) or {}).get(
            'selected_datasource_snapshot', []
        )
    )
    schema_scope_available = (
        work in {'probe', 'probe_repair', 'implement', 'repair'}
        and runtime_schema_workspace_covers_material_scope(
            merge_schema_sources(execution_schema_sources, schema_detail_workspace),
            material_datasource_ids,
            material_fields,
        )
    )
    schema_loaded_from_reader = bool(canonical_schema_covered)
    planning_schema_in_view = bool(schema_detail_workspace) and work in {'understand', 'plan'}
    schema_loaded_from_snapshot = bool(
        schema_scope_available and not schema_loaded_from_reader
    )
    messages, archived = project_history(
        fact_messages,
        memory=message_memory,
        work=work,
        # Result review reasons over the executed program, query facts and
        # candidate artifacts. Replaying exploratory Schema reads adds no new
        # evidence and can make this deliberately focused state larger than
        # implementation itself.
        compact_schema_reader_results=(canonical_schema_covered or planning_schema_in_view or work == 'review'),
        semantic_plan_available=bool(packet.semantic_plan),
        discard_invalidated_plans=packet.goal_revision > 1 or bool(packet.semantic_plan),
        material_plan_available=bool(packet.material_manifest),
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
        datasource_ids=material_datasource_ids if work in {'plan', 'probe', 'probe_repair', 'implement', 'repair'} else (),
        field_names_by_datasource=material_fields if work in {'plan', 'probe', 'probe_repair', 'implement', 'repair'} else {},
        material_plan=material_plan if work in {'probe', 'probe_repair', 'implement', 'repair'} else None,
        schema_detail_workspace=schema_detail_workspace,
        omit_replayed_details=canonical_schema_covered or planning_schema_in_view,
    )
    exact_schema_required = bool(
        material_plan
        and work in {'probe', 'probe_repair', 'implement', 'repair'}
        and not schema_scope_available
    )
    if exact_schema_required:
        work_schema_instruction = (
            '\n当前材料计划的 Schema 明细尚未完整加载；已读取的字段定义仍然有效。'
            '在生成 Probe 或正式代码前，调用 read_runtime_datasource_context 补充当前焦点缺少的数据源和字段明细；'
            '读取结果才是字段类型、格式、枚举、关系和业务描述的依据。'
        )
    elif schema_loaded_from_reader:
        work_schema_instruction = (
            '\n当前工作所需的精确 Schema 已由 read_runtime_datasource_context 读取并纳入工作视图；'
            '后续只使用该读取结果，不要重复展开全部来源，也不要猜测未读取字段。'
        )
    elif schema_loaded_from_snapshot:
        work_schema_instruction = (
            '\n当前工作所需的精确 Schema 已由权威数据源快照纳入工作视图；'
            '后续直接使用该快照中的字段定义，不要重复展开全部来源，也不要猜测未读取字段。'
        )
    elif material_plan and work in {'probe', 'probe_repair', 'implement', 'repair'} and not material_fields:
        work_schema_instruction = (
            '\n当前已加载本工作声明的精确 Schema。若发现新的字段或关系依赖，'
            '读取该精确明细后再继续，不要猜测字段语义。'
        )
    else:
        work_schema_instruction = ''
    current = SystemMessage(
        '<runtime-working-context>\n'
        + json.dumps({
            'work': work,
            'focus': focus,
            'contract_ids': contract_ids,
            'runtime_material_plan': material_plan,
            'runtime_semantic_plan': semantic_plan_work_view(packet.semantic_plan),
            **({'expression_scope': semantic_reporting_scope(packet.semantic_plan)}
               if work == 'express' and packet.semantic_plan else {}),
            **({'semantic_implementation_bindings': semantic_implementation_bindings(packet.semantic_plan)}
               if work in {'implement', 'repair'} and packet.semantic_plan else {}),
            'material_datasource_ids': material_datasource_ids if work in {'probe', 'probe_repair', 'implement', 'repair'} else [],
            'material_fields_by_datasource': material_fields if work in {'probe', 'probe_repair', 'implement', 'repair'} else {},
            'pending_review_patch': (
                packet.pending_review_patch if work == 'repair' and packet.pending_review_patch else {}
            ),
        }, ensure_ascii=False)
        + ('\n当前先用 plan_runtime_semantics 提交完整 JSON 语义对象，确认全部问题的事实来源、总体、时间归属、必要计算语义和待核实项。'
           '尚未读取的字段定义可通过 read_runtime_datasource_context 获取；不得预设真实数值或无数据。'
           if work == 'understand' else
           '\n计划阶段依据 runtime_semantic_plan 使用 plan_runtime_materials 组织来源、字段和关系；'
           '技术未知才标记为需要 Probe。材料计划不能改变已经确认的总体与计算语义。'
           if work == 'plan' else
           work_schema_instruction)
        + ('\n计算已在当前 execution_id 完成。请只依据紧邻工具结果中的本次事实和产物撰写完整用户表达，'
           '不得查询或重算；标记为截断的事实只是样本，不能当总体。'
           '这是创作步骤，不是语义复核。只有存在会改变答案的关键业务歧义且上下文无法确定时才澄清。'
           '用 compose_analysis_presentation 一次提交当前输出协议要求的完整内容。'
           if work == 'express' else '')
        + ('每次 Probe 完成后必须先基于事实账本重新选择下一项工作；只有新的、未被账本覆盖的技术未知才选择 Probe。'
           if work == 'understand' and probe_facts else '')
        + ('当前 Review 已授权局部源码修订；必须以 pending_review_patch.base_execution_id 和 allowed_ranges '
           '提交 patches，不能重写完整程序。'
           if work == 'repair' and packet.pending_review_patch else
           '修复保持完整分析目标。若需要进一步探测，可以切换回 probe；探测完成不自动代表全部未知已解决。')
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
    work_protocol = SystemMessage('<runtime-contracts>\n' + load_runtime_work_contract_text(
        contract_ids, work=work, output_format=context.output_contract.get('type', 'standard'),
        example_contract_ids=references,
    ) + '\n</runtime-contracts>')
    system = SystemMessage('\n\n'.join([
        *system_parts, work_protocol.content, datasource_view.content, current.content,
    ]))
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
        if work == 'express':
            return name in {'compose_analysis_presentation', 'request_analysis_clarification'}
        # ``plan`` has one producer: the material-plan tool.  Letting the
        # model select Probe/implementation again at this point made the
        # plan optional, so executable work could start without the focused
        # source/field view that the plan is responsible for producing.
        if name == 'plan_runtime_semantics':
            return work in {'understand', 'plan', 'implement', 'repair'}
        if name == 'select_runtime_work':
            # Work selection owns only the entry into the material-plan
            # lifecycle. Exposing the generic selector after that point permits
            # same-state transitions such as implement -> implement: they do no
            # analytical work, but become fresh control memory and defer code.
            return False
        if name == 'request_runtime_probe':
            # Executable work has one explicit escape hatch. A newly discovered
            # technical unknown can enter Probe and then resume its caller;
            # Probe and review already have deterministic next states.
            return work in {'implement', 'repair'}
        if name in {'probe_analysis_data', 'submit_analysis_program'}:
            return name == allowed_execution
        if name == 'compose_analysis_presentation':
            return work == 'express'
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
            # A material plan is a working selection, not proof that all
            # implementation dependencies were discovered. Reads preserve
            # the current focus and return exact Schema through tool results.
            # Coverage permits de-duplicating delivered material, not closing
            # discovery of new dependencies during implementation or repair.
        return True

    tools = [item for item in request.tools if visible_in_work(item)]
    if work == 'express' and str((getattr(context, 'output_contract', {}) or {}).get('type', 'standard')) in {'standard', 'text'}:
        has_bound_results = any(
            isinstance(message, ToolMessage)
            and (_payload(message) or {}).get('status') == 'presentation_pending'
            and (_payload(message) or {}).get('execution_id') == packet.active_execution_id
            and any(trace.get('findings') or trace.get('facts') for trace in (_payload(message) or {}).get('facts', [])
                    if isinstance(trace, dict))
            for message in request.messages
        )
        if has_bound_results:
            from agent.tools import ComposeBoundPresentationInput
            tools = [item.model_copy(update={'args_schema': ComposeBoundPresentationInput})
                     if tool_name(item) == 'compose_analysis_presentation' and hasattr(item, 'model_copy')
                     else item for item in tools]
    tools = [
        item.model_copy(update={'args_schema': _semantic_plan_input_for_context(context)})
        if tool_name(item) == 'plan_runtime_semantics' and hasattr(item, 'model_copy')
        else item
        for item in tools
    ]
    if work in {'implement', 'repair'}:
        latest_formal_call, _ = _active_formal_program_chain(request.messages)
        current_definition = _uses_native_definition(request.messages, work)
        patch_authorized = False
        for message in reversed(request.messages):
            if isinstance(message, ToolMessage) and message.tool_call_id == latest_formal_call:
                target = (_payload(message) or {}).get('repair_target') or {}
                patch_authorized = (
                    isinstance(target, dict)
                    and target.get('mode') == 'line_patch'
                    and bool(target.get('base_execution_id'))
                )
                break
        if not patch_authorized or current_definition:
            from agent.tools import FullAnalysisProgramInput, ExecutableAnalysisProgramInput

            delivery_schema = (ExecutableAnalysisProgramInput
                               if current_definition else FullAnalysisProgramInput)

            tools = [
                item.model_copy(update={'args_schema': delivery_schema})
                if item.name == 'submit_analysis_program' and hasattr(item, 'model_copy')
                else item
                for item in tools
            ]
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
    protocol_tokens = estimate_work_messages([work_protocol])
    work_context_tokens = estimate_work_messages([current])
    history_tokens = estimate_work_messages(messages)
    tool_schema_chars = sum(
        len(json.dumps(item.args_schema.model_json_schema(), ensure_ascii=False))
        for item in tools
        if getattr(item, 'args_schema', None) is not None
    )
    logger.info('Runtime work view state=%s packet_revision=%s packet_reason=%s contracts=%s memory=%s archived_code=%s raw_tokens=%s view_tokens=%s components=%s',
                work, packet.revision, packet.reason, list(contract_ids), memory_counts, archived,
                estimate_work_messages([*([request.system_message] if request.system_message else []), *request.messages]),
                estimate_work_messages([*([system] if system else []), *messages]),
                {
                    'base_system_tokens': base_system_tokens,
                    'datasource_tokens': datasource_tokens,
                    'protocol_tokens': protocol_tokens,
                    'work_context_tokens': work_context_tokens,
                    'history_tokens': history_tokens,
                    'tool_schema_chars': tool_schema_chars,
                })
    return request.override(messages=messages, system_message=system, tools=tools)


class RuntimeWorkingContextMiddleware(AgentMiddleware):
    @staticmethod
    def _log_model_turn(work: str, started_at: float, result) -> None:
        messages = getattr(result, 'result', None)
        if not isinstance(messages, list):
            messages = [result]
        tool_calls = [call for message in messages
                      for call in (getattr(message, 'tool_calls', None) or [])]
        output_chars = sum(len(str(getattr(message, 'content', '') or '')) for message in messages)
        logger.info(
            'Runtime model turn completed: work=%s elapsed_seconds=%.3f output_chars=%s tool_calls=%s',
            work,
            time.perf_counter() - started_at,
            output_chars,
            [str(call.get('name') or '') for call in tool_calls if isinstance(call, dict)],
        )

    def wrap_model_call(self, request, handler):
        activity = 'model:' + uuid.uuid4().hex
        projected = build_working_request(request)
        work = runtime_phase_work(
            _ensure_runtime_work_packet(request.runtime.context, request.messages).phase
        )
        emit_model_progress(activity, work, 'running')
        started_at = time.perf_counter()
        try:
            result = handler(projected)
        except Exception:
            emit_model_progress(activity, work, 'failed')
            raise
        self._log_model_turn(work, started_at, result)
        emit_model_progress(activity, work, 'completed')
        return result

    async def awrap_model_call(self, request, handler):
        activity = 'model:' + uuid.uuid4().hex
        projected = build_working_request(request)
        work = runtime_phase_work(
            _ensure_runtime_work_packet(request.runtime.context, request.messages).phase
        )
        emit_model_progress(activity, work, 'running')
        started_at = time.perf_counter()
        try:
            result = await handler(projected)
        except Exception:
            emit_model_progress(activity, work, 'failed')
            raise
        self._log_model_turn(work, started_at, result)
        emit_model_progress(activity, work, 'completed')
        return result

    def wrap_tool_call(self, request, handler):
        failure = _semantic_plan_argument_failure(request) or _program_protocol_argument_failure(request)
        return _reduce_runtime_tool_call(request, failure if failure is not None else handler(request))

    async def awrap_tool_call(self, request, handler):
        failure = _semantic_plan_argument_failure(request) or _program_protocol_argument_failure(request)
        return _reduce_runtime_tool_call(request, failure if failure is not None else await handler(request))
