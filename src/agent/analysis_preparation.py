import copy
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from config import Config
from service.semantic_datasource_selection_service import (
    DatasourceSelectionProviderRouter,
    SemanticDatasourceSelectionError,
    SemanticDatasourceSelectionService,
)
from utils import logger
from utils.analysis_task_cancel import AnalysisTaskCancelled
from utils.llm_model_factory import (
    LlmRuntimeConfig,
    create_data_insight_model,
    create_runtime_config,
)


PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / "preparation"
QUERY_PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / "query_capabilities"


def _normalize_time_series_datasource_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Align advisory Schema fields with each time-series backend's query API."""
    normalized = copy.deepcopy(snapshot)
    rows = normalized.get('selected_datasource_snapshot')
    if not isinstance(rows, list):
        return normalized
    for item in rows:
        if not isinstance(item, dict):
            continue
        backend = str(item.get('ts_query_source') or '').strip().lower()
        is_time_series = (
            str(item.get('table_engine') or '').strip().lower() == 'time_series'
            or backend in {'vxbase', 'timescale'}
        )
        if not is_time_series:
            continue
        physical_time_field = 'ts' if backend == 'vxbase' else 'timeStamp'
        stale_time_field = 'timeStamp' if physical_time_field == 'ts' else 'ts'
        item['time_field'] = physical_time_field
        schema = item.get('metadata_schema')
        if not isinstance(schema, dict):
            continue
        properties = schema.get('properties')
        if not isinstance(properties, dict):
            continue
        stale_definition = properties.pop(stale_time_field, None)
        definition = properties.get(physical_time_field)
        if not isinstance(definition, dict):
            definition = (
                copy.deepcopy(stale_definition)
                if isinstance(stale_definition, dict)
                else {'type': 'string'}
            )
            properties[physical_time_field] = definition
        definition['description'] = '时序数据物理查询与返回时间字段'
    return normalized


def _requires_datasource_shortlist(catalog: list[dict[str, Any]]) -> bool:
    trigger_count = max(
        int(
            getattr(
                Config,
                "DATASOURCE_SELECTION_SHORTLIST_TRIGGER_COUNT",
                8,
            )
            or 8
        ),
        1,
    )
    trigger_chars = max(
        int(
            getattr(
                Config,
                "DATASOURCE_SELECTION_SHORTLIST_TRIGGER_CHARS",
                24000,
            )
            or 24000
        ),
        1,
    )
    catalog_chars = len(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    return len(catalog) > trigger_count or catalog_chars > trigger_chars


class BusinessFactRequirementContract(BaseModel):
    """Canonical business meaning required by one planned question."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    business_fact: str = Field(min_length=1)
    semantic_definition: str = Field(min_length=1)


class BusinessQuestionContract(BaseModel):
    """One user-requested business question and its indispensable selection facts."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    business_question: str = Field(min_length=1)
    required_business_facts: list[BusinessFactRequirementContract] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def ensure_unique_fact_ids(self) -> "BusinessQuestionContract":
        fact_ids = [item.fact_id for item in self.required_business_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("required business fact IDs must be unique per question")
        return self


class IntentContract(BaseModel):
    """Explicit selection intent plus advisory facets for later insight analysis."""

    # Input context echoed by the model is not part of the canonical intent. The
    # semantic body remains typed, while harmless top-level annotations are dropped.
    model_config = ConfigDict(extra="ignore")

    business_goal: str = Field(min_length=1)
    business_subjects: list[str] = Field(default_factory=list)
    analytical_facets: list[str] = Field(default_factory=list)
    business_questions: list[BusinessQuestionContract] = Field(min_length=1)

    @model_validator(mode="after")
    def ensure_unique_business_questions(self) -> "IntentContract":
        question_ids = [item.question_id for item in self.business_questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("business question IDs must be unique")
        fact_ids = [
            fact.fact_id
            for question in self.business_questions
            for fact in question.required_business_facts
        ]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("business fact IDs must be unique in the intent")
        normalized_facets = [item.strip() for item in self.analytical_facets if item.strip()]
        self.analytical_facets = list(dict.fromkeys(normalized_facets))
        return self


def _canonicalize_intent_selection_facts(intent: IntentContract) -> IntentContract:
    """Keep source-selection obligations identical to user-requested questions.

    The source-free intent stage may suggest analytical facets, but it cannot know
    whether an amount, lifecycle event, comparison baseline, or other ideal field
    exists. Selection therefore receives one canonical obligation per explicit
    business question and decides coverage from the real schemas.
    """
    questions = [
        question.model_copy(update={
            "required_business_facts": [BusinessFactRequirementContract(
                fact_id=f"{question.question_id}_fact_1",
                business_fact=question.business_question,
                semantic_definition=(
                    "完整回答该业务问题所需的业务事实范围；具体可用维度与口径"
                    "由 Selection 基于真实 Schema 核验，并由正式分析自主判断。"
                ),
            )],
        })
        for question in intent.business_questions
    ]
    return intent.model_copy(update={"business_questions": questions})


class DatasourceShortlistContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shortlisted_datasource_ids: list[int] = Field(min_length=1)
    coverage_reason: str


class BusinessFactCoverageContract(BaseModel):
    """Datasource support for one canonical fact from the business intent."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    coverage_status: Literal["direct", "partial", "unresolved"]
    datasource_ids: list[int] = Field(default_factory=list)
    supporting_datasource_ids: list[int] = Field(default_factory=list)
    semantic_reference_datasource_ids: list[int] = Field(default_factory=list)
    derived_from_fact_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical fact IDs that provide the factual basis for this analytical "
            "output. Use this for calculations and for synthesis such as comparison, "
            "ranking, interpretation, recommendations, or evidence-gap assessment; "
            "the final analytical output does not need to be stored as a datasource field."
        ),
    )
    coverage_reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_fact_id_echo_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.pop("business_fact", None)
        normalized.pop("semantic_definition", None)
        if "coverage_id" not in normalized:
            return normalized
        alias = str(normalized.get("coverage_id") or "").strip()
        fact_id = str(normalized.get("fact_id") or "").strip()
        if fact_id and alias and fact_id != alias:
            return value
        normalized["fact_id"] = fact_id or alias
        normalized.pop("coverage_id", None)
        return normalized

    @model_validator(mode="after")
    def normalize_ids(self) -> "BusinessFactCoverageContract":
        self.datasource_ids = list(dict.fromkeys(
            int(item)
            for item in self.datasource_ids
            if int(item or 0) > 0
        ))
        self.supporting_datasource_ids = list(dict.fromkeys(
            int(item)
            for item in self.supporting_datasource_ids
            if int(item or 0) > 0 and int(item) not in self.datasource_ids
        ))
        physical_ids = {*self.datasource_ids, *self.supporting_datasource_ids}
        self.semantic_reference_datasource_ids = list(dict.fromkeys(
            int(item)
            for item in self.semantic_reference_datasource_ids
            if int(item or 0) > 0 and int(item) not in physical_ids
        ))
        self.derived_from_fact_ids = list(dict.fromkeys(
            str(item).strip()
            for item in self.derived_from_fact_ids
            if str(item).strip()
        ))
        if (
            self.coverage_status in {"direct", "partial"}
            and not self.datasource_ids
            and not self.derived_from_fact_ids
        ):
            raise ValueError(f"{self.coverage_status} fact coverage requires datasources")
        return self


class BusinessQuestionCoverageContract(BaseModel):
    """Datasource coverage for one planned business question."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    coverage_status: Literal["direct", "partial", "unresolved"] = "direct"
    datasource_ids: list[int] = Field(default_factory=list)
    fact_coverage: list[BusinessFactCoverageContract] = Field(min_length=1)
    coverage_reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def discard_read_only_question_echo(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.pop("business_question", None)
        normalized.pop("business_fact", None)
        # Intent names the requested inputs ``required_business_facts``. If a
        # Selection response preserves that label while adding coverage fields,
        # normalize it at this boundary; downstream state remains canonical.
        if (
            "fact_coverage" not in normalized
            and isinstance(normalized.get("required_business_facts"), list)
        ):
            normalized["fact_coverage"] = normalized.pop("required_business_facts")
        return normalized

    @model_validator(mode="after")
    def normalize_ids(self) -> "BusinessQuestionCoverageContract":
        fact_ids = [item.fact_id for item in self.fact_coverage]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_coverage must contain unique fact IDs")
        fact_datasource_ids = {
            datasource_id
            for item in self.fact_coverage
            for datasource_id in [
                *item.datasource_ids,
                *item.supporting_datasource_ids,
            ]
        }
        self.datasource_ids = sorted(fact_datasource_ids)
        fact_statuses = {item.coverage_status for item in self.fact_coverage}
        derived_status = (
            "direct"
            if fact_statuses == {"direct"}
            else "unresolved"
            if fact_statuses == {"unresolved"}
            else "partial"
        )
        self.coverage_status = derived_status
        return self


class DatasourceSelectionContract(BaseModel):
    """Complete datasource coverage for the planned business intent."""

    model_config = ConfigDict(extra="forbid")

    selected_datasource_ids: list[int] = Field(default_factory=list)
    question_coverage: list[BusinessQuestionCoverageContract] = Field(min_length=1)
    selection_reason: str

    @model_validator(mode="after")
    def normalize_ids(self) -> "DatasourceSelectionContract":
        coverage_ids = [item.question_id for item in self.question_coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("question_coverage must contain unique question IDs")
        coverage_by_fact = {
            fact.fact_id: fact
            for question in self.question_coverage
            for fact in question.fact_coverage
        }
        if len(coverage_by_fact) != sum(
            len(question.fact_coverage) for question in self.question_coverage
        ):
            raise ValueError("fact coverage IDs must be globally unique")

        resolved: set[str] = set()
        resolving: set[str] = set()

        def resolve_fact(fact_id: str) -> BusinessFactCoverageContract:
            fact = coverage_by_fact[fact_id]
            if fact_id in resolved:
                return fact
            if fact_id in resolving:
                raise ValueError("derived fact dependencies must not contain cycles")
            resolving.add(fact_id)
            dependencies = set(fact.derived_from_fact_ids)
            if fact_id in dependencies or not dependencies.issubset(coverage_by_fact):
                raise ValueError(
                    f"derived fact {fact_id} must reference another selected business fact"
                )
            dependency_items = [resolve_fact(item) for item in dependencies]
            if dependency_items:
                if not fact.datasource_ids:
                    fact.datasource_ids = sorted({
                        datasource_id
                        for dependency in dependency_items
                        for datasource_id in dependency.datasource_ids
                    })
                if not fact.supporting_datasource_ids:
                    fact.supporting_datasource_ids = sorted({
                        datasource_id
                        for dependency in dependency_items
                        for datasource_id in dependency.supporting_datasource_ids
                        if datasource_id not in fact.datasource_ids
                    })
                if not fact.semantic_reference_datasource_ids:
                    fact.semantic_reference_datasource_ids = sorted({
                        datasource_id
                        for dependency in dependency_items
                        for datasource_id in dependency.semantic_reference_datasource_ids
                        if datasource_id not in fact.datasource_ids
                        and datasource_id not in fact.supporting_datasource_ids
                    })
                dependency_statuses = {
                    dependency.coverage_status for dependency in dependency_items
                }
                fact.coverage_status = (
                    "direct"
                    if dependency_statuses == {"direct"}
                    else "unresolved"
                    if dependency_statuses == {"unresolved"}
                    else "partial"
                )
            resolving.remove(fact_id)
            resolved.add(fact_id)
            return fact

        for fact_id in coverage_by_fact:
            resolve_fact(fact_id)
        for question in self.question_coverage:
            question.datasource_ids = sorted({
                datasource_id
                for fact in question.fact_coverage
                for datasource_id in [
                    *fact.datasource_ids,
                    *fact.supporting_datasource_ids,
                ]
            })
            fact_statuses = {fact.coverage_status for fact in question.fact_coverage}
            question.coverage_status = (
                "direct"
                if fact_statuses == {"direct"}
                else "unresolved"
                if fact_statuses == {"unresolved"}
                else "partial"
            )
        covered_ids = {
            datasource_id
            for question in self.question_coverage
            for fact in question.fact_coverage
            for datasource_id in [
                *fact.datasource_ids,
                *fact.supporting_datasource_ids,
                *fact.semantic_reference_datasource_ids,
            ]
        }
        if not covered_ids:
            raise ValueError("selection requires at least one supported datasource")
        self.selected_datasource_ids = sorted(covered_ids)
        return self


def _semantic_reference_datasource_ids(
    selection: DatasourceSelectionContract,
) -> set[int]:
    return {
        datasource_id
        for question in selection.question_coverage
        for fact in question.fact_coverage
        for datasource_id in fact.semantic_reference_datasource_ids
    }


class BusinessRelationshipContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    left_datasource_id: int
    right_datasource_id: int
    relationship_type: Literal[
        "same_entity",
        "parent_child",
        "event_of_entity",
        "dimension_enrichment",
        "bridge",
        "comparable",
        "independent",
        "unknown",
    ]
    joinability: Literal["joinable", "alignable", "not_joinable", "unknown"]
    left_fields: list[str] = Field(default_factory=list)
    right_fields: list[str] = Field(default_factory=list)
    business_meaning: str
    evidence_basis: Literal["schema", "probe", "unresolved"] = "unresolved"
    observation_id: str = ""
    probe_facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keep_unknown_relationship_unresolved(self) -> "BusinessRelationshipContract":
        if self.relationship_type == "unknown" and self.joinability != "unknown":
            raise ValueError("unknown relationship type must keep joinability unknown")
        return self


class BusinessFactBindingContract(BaseModel):
    """Schema-grounded location of one business fact, without calculation rules."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    datasource_id: int = Field(default=0, ge=0)
    fields: list[str] = Field(default_factory=list)
    derived_from_fact_ids: list[str] = Field(default_factory=list)

    @field_validator("datasource_id", mode="before")
    @classmethod
    def normalize_logical_datasource_id(cls, value: Any) -> Any:
        return 0 if value is None else value

    @model_validator(mode="after")
    def require_physical_or_derived_lineage(self) -> "BusinessFactBindingContract":
        self.fields = list(dict.fromkeys(
            str(item).strip() for item in self.fields if str(item).strip()
        ))
        self.derived_from_fact_ids = list(dict.fromkeys(
            str(item).strip()
            for item in self.derived_from_fact_ids
            if str(item).strip()
        ))
        if not self.fields and not self.derived_from_fact_ids:
            raise ValueError("fact binding requires fields or derived fact dependencies")
        if self.fields and self.datasource_id <= 0:
            raise ValueError("physical fact binding requires a datasource")
        if self.derived_from_fact_ids and not self.fields:
            self.datasource_id = 0
        if self.fact_id in self.derived_from_fact_ids:
            raise ValueError("derived fact binding cannot reference itself")
        return self


class UnsupportedBusinessFieldBindingContract(BaseModel):
    """Schema fields that were checked but cannot represent a required fact."""

    model_config = ConfigDict(extra="forbid")

    datasource_id: int = Field(gt=0)
    fields: list[str] = Field(min_length=1)
    business_reason: str = Field(min_length=1)


class UnresolvedBusinessFactContract(BaseModel):
    """A canonical business fact that remains unsupported after Schema review."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    business_impact: str = ""
    unsupported_field_bindings: list[UnsupportedBusinessFieldBindingContract] = Field(
        default_factory=list
    )

    @model_validator(mode="before")
    @classmethod
    def discard_empty_unsupported_bindings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        bindings = value.get("unsupported_field_bindings")
        if not isinstance(bindings, list):
            return value
        normalized = dict(value)
        normalized["unsupported_field_bindings"] = [
            item
            for item in bindings
            if not isinstance(item, dict) or any(
                str(field).strip() for field in item.get("fields") or []
            )
        ]
        return normalized


class BusinessContextProbeRequest(BaseModel):
    """Bounded Schema fact requested to finish business-lineage resolution."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    datasource_id: int = Field(gt=0)
    field: str = Field(min_length=1)
    probe_kind: Literal["distinct_values", "field_profile"]
    purpose: str = Field(min_length=1)
    related_fact_ids: list[str] = Field(default_factory=list)
    expected_values: list[str] = Field(default_factory=list)
    top_n: int = Field(default=20, ge=1, le=50)

    @field_validator("related_fact_ids", "expected_values", mode="before")
    @classmethod
    def normalize_optional_lists(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        return [
            str(item)
            if isinstance(item, (int, float, bool))
            else item
            for item in value
        ]

    @field_validator("top_n", mode="before")
    @classmethod
    def normalize_optional_top_n(cls, value: Any) -> Any:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return 20
        return min(normalized, 50) if normalized > 0 else 20


class InsightLineageContract(BaseModel):
    """How selected facts form one coherent answerable business question."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    business_question: str = Field(min_length=1)
    lineage_status: Literal["direct", "partial", "unresolved"] = "direct"
    datasource_ids: list[int] = Field(default_factory=list)
    fact_bindings: list[BusinessFactBindingContract] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    unresolved_business_facts: list[UnresolvedBusinessFactContract] = Field(
        default_factory=list
    )
    synthesis_path: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_redundant_unresolved_bindings(cls, value: Any) -> Any:
        """Canonicalize one binding per fact and datasource.

        Models sometimes repeat an unresolved fact as an empty fact binding. That
        duplicate carries no lineage information and makes an otherwise complete
        draft fail structural validation. They can also split fields for the same
        fact/source across several bindings; those are one lineage boundary and are
        merged without weakening validation of conflicting sources.
        """
        if not isinstance(value, dict):
            return value
        normalized_value = dict(value)
        for read_only_key in (
            "verified_enums",
            "probe_observations",
            "field_profiles",
        ):
            normalized_value.pop(read_only_key, None)
        unresolved_ids = {
            str(item.get("fact_id") or "").strip()
            for item in normalized_value.get("unresolved_business_facts") or []
            if isinstance(item, dict) and str(item.get("fact_id") or "").strip()
        }
        bindings = normalized_value.get("fact_bindings")
        if not isinstance(bindings, list):
            return normalized_value

        normalized_bindings: list[Any] = []
        positions: dict[tuple[str, int], int] = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                normalized_bindings.append(binding)
                continue
            fact_id = str(binding.get("fact_id") or "").strip()
            has_lineage = bool(
                binding.get("fields")
                or binding.get("derived_from_fact_ids")
            )
            if fact_id in unresolved_ids and not has_lineage:
                continue
            try:
                datasource_id = int(binding.get("datasource_id") or 0)
            except (TypeError, ValueError):
                normalized_bindings.append(binding)
                continue
            key = (fact_id, datasource_id)
            if fact_id and key in positions:
                existing = normalized_bindings[positions[key]]
                if isinstance(existing, dict):
                    existing["fields"] = list(dict.fromkeys([
                        *(existing.get("fields") or []),
                        *(binding.get("fields") or []),
                    ]))
                    existing["derived_from_fact_ids"] = list(dict.fromkeys([
                        *(existing.get("derived_from_fact_ids") or []),
                        *(binding.get("derived_from_fact_ids") or []),
                    ]))
                    continue
            if fact_id:
                positions[key] = len(normalized_bindings)
            normalized_bindings.append(binding)
        return {**normalized_value, "fact_bindings": normalized_bindings}

    @field_validator("lineage_status", mode="before")
    @classmethod
    def normalize_lineage_status(cls, value: Any) -> Any:
        normalized = str(value or "").strip().lower()
        return {
            "confirmed": "direct",
            "verified": "direct",
            "supported": "direct",
            "resolved": "direct",
        }.get(normalized, value)


class BusinessContextContract(BaseModel):
    """Question-oriented business lineage over the selected datasource schemas."""

    # The contract body remains strict; top-level explanatory annotations from the
    # model are not part of the canonical context and can be discarded safely.
    model_config = ConfigDict(extra="ignore")

    relationships: list[BusinessRelationshipContract] = Field(default_factory=list)
    insight_lineage: list[InsightLineageContract] = Field(default_factory=list)
    unresolved_relationships: list[str] = Field(default_factory=list)
    probe_requests: list[BusinessContextProbeRequest] = Field(default_factory=list)

    @field_validator("unresolved_relationships", mode="before")
    @classmethod
    def normalize_unresolved_relationships(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(
                    item.get("description")
                    or item.get("business_meaning")
                    or item.get("reason")
                    or json.dumps(item, ensure_ascii=False, default=str)
                ).strip()
            else:
                text = str(item or "").strip()
            if text:
                normalized.append(text)
        return normalized

    @model_validator(mode="after")
    def validate_relationship_ids(self) -> "BusinessContextContract":
        relationship_ids = [item.relationship_id for item in self.relationships]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("relationship_id must be unique")
        lineage_ids = [item.question_id for item in self.insight_lineage]
        if len(lineage_ids) != len(set(lineage_ids)):
            raise ValueError("insight_lineage must contain unique question IDs")
        request_ids = [item.request_id for item in self.probe_requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("probe request_id must be unique")
        return self


class BusinessContextDraftContract(BaseModel):
    """Pre-Probe lineage draft; semantic resolutions require observed values."""

    model_config = ConfigDict(extra="ignore")

    relationships: list[BusinessRelationshipContract] = Field(default_factory=list)
    insight_lineage: list[InsightLineageContract] = Field(default_factory=list)
    unresolved_relationships: list[str] = Field(default_factory=list)
    probe_requests: list[BusinessContextProbeRequest] = Field(default_factory=list)


class BusinessFactProbeResolutionContract(BaseModel):
    """A delta decision for one fact affected by a Schema Probe."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    resolution_status: Literal["supported", "unsupported", "unchanged"]
    observation_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    business_impact: str = ""

    @field_validator("observation_ids", mode="before")
    @classmethod
    def normalize_observation_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        return value


class BusinessContextProbeResolutionContract(BaseModel):
    """Compact semantic decisions after bounded Schema observations."""

    model_config = ConfigDict(extra="forbid")

    fact_resolutions: list[BusinessFactProbeResolutionContract] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_unique_fact_ids(self) -> "BusinessContextProbeResolutionContract":
        fact_ids = [item.fact_id for item in self.fact_resolutions]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_resolutions must contain unique fact_ids")
        return self


@dataclass(frozen=True)
class PreparationResult:
    intent: IntentContract
    selection: DatasourceSelectionContract
    business_context: BusinessContextContract
    selected_snapshot: dict[str, Any]
    query_capabilities: tuple[str, ...]
    probe_observations: tuple[dict[str, Any], ...] = ()
    explicit_time_range: dict[str, Any] | None = None

    def resume_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent.model_dump(),
            "selection": self.selection.model_dump(),
            "business_context": self.business_context.model_dump(),
            "selected_snapshot": self.selected_snapshot,
            "query_capabilities": list(self.query_capabilities),
            "probe_observations": list(self.probe_observations),
            "explicit_time_range": self.explicit_time_range,
        }

    @classmethod
    def from_resume_payload(
        cls,
        payload: dict[str, Any] | None,
    ) -> "PreparationResult | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                intent=IntentContract.model_validate(payload["intent"]),
                selection=DatasourceSelectionContract.model_validate(
                    payload["selection"]
                ),
                business_context=BusinessContextContract.model_validate(
                    payload["business_context"]
                ),
                selected_snapshot=dict(payload.get("selected_snapshot") or {}),
                query_capabilities=tuple(payload.get("query_capabilities") or ()),
                probe_observations=tuple(
                    item
                    for item in payload.get("probe_observations") or ()
                    if isinstance(item, dict)
                ),
                explicit_time_range=(
                    dict(payload.get("explicit_time_range") or {}) or None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def prompt_snapshot(self) -> dict[str, Any]:
        snapshot = compact_datasource_snapshot(self.selected_snapshot)
        semantic_reference_ids = _semantic_reference_datasource_ids(
            self.selection
        )
        semantic_references = [
            {
                "datasource_id": int(
                    datasource.get("datasource_id")
                    or datasource.get("id")
                    or 0
                ),
                "datasource_name": str(
                    datasource.get("datasource_name") or ""
                ),
                "description": str(
                    datasource.get("description")
                    or (datasource.get("metadata_schema") or {}).get(
                        "description"
                    )
                    or ""
                ),
            }
            for datasource in snapshot.get("selected_datasource_snapshot") or []
            if int(
                datasource.get("datasource_id")
                or datasource.get("id")
                or 0
            ) in semantic_reference_ids
        ]
        _apply_probe_field_profiles(snapshot, self.probe_observations)
        _apply_relationship_execution_semantics(snapshot, self.business_context)
        business_fact_names = {
            fact.fact_id: fact.business_fact
            for question in self.intent.business_questions
            for fact in question.required_business_facts
        }

        runtime_lineage: list[dict[str, Any]] = []
        for lineage in self.business_context.insight_lineage:
            item = lineage.model_dump(exclude={"unresolved_business_facts"})
            unsupported_by_fact: dict[str, dict[int, set[str]]] = {}
            for unresolved_fact in lineage.unresolved_business_facts:
                datasource_fields = unsupported_by_fact.setdefault(
                    unresolved_fact.fact_id,
                    {},
                )
                for unsupported in unresolved_fact.unsupported_field_bindings:
                    datasource_fields.setdefault(
                        unsupported.datasource_id,
                        set(),
                    ).update(unsupported.fields)
            item["fact_bindings"] = []
            for binding in lineage.fact_bindings:
                unsupported_fields = unsupported_by_fact.get(
                    binding.fact_id,
                    {},
                ).get(binding.datasource_id, set())
                supported_fields = [
                    field for field in binding.fields
                    if field not in unsupported_fields
                ]
                if not supported_fields:
                    continue
                binding_item = binding.model_dump()
                binding_item["fields"] = supported_fields
                item["fact_bindings"].append(binding_item)
            item["unresolved_business_facts"] = [
                {
                    "fact_id": fact.fact_id,
                    "unsupported_field_bindings": [
                        item.model_dump()
                        for item in fact.unsupported_field_bindings
                    ],
                    "business_impact": (
                        fact.business_impact.strip()
                        or (
                            f"当前数据无法可靠支持“{business_fact_names[fact.fact_id]}”，"
                            "该部分不纳入结论。"
                            if fact.fact_id in business_fact_names
                            else "当前数据无法可靠支持该业务事实，该部分不纳入结论。"
                        )
                    ),
                }
                for fact in lineage.unresolved_business_facts
            ]
            runtime_lineage.append(item)

        lineage_datasource_ids = {
            int(binding.get("datasource_id") or 0)
            for lineage in runtime_lineage
            for binding in lineage.get("fact_bindings") or []
            if int(binding.get("datasource_id") or 0) > 0
        }
        runtime_datasource_ids = {
            int(datasource_id)
            for datasource_id in self.selection.selected_datasource_ids
            if int(datasource_id or 0) > 0
            and int(datasource_id) not in semantic_reference_ids
        }
        referenced_relationship_ids = {
            str(relationship_id)
            for lineage in runtime_lineage
            for relationship_id in lineage.get("relationship_ids") or []
            if str(relationship_id).strip()
        }

        runtime_relationships: list[dict[str, Any]] = []
        for relationship in self.business_context.relationships:
            if (
                relationship.relationship_id not in referenced_relationship_ids
                or relationship.left_datasource_id not in runtime_datasource_ids
                or relationship.right_datasource_id not in runtime_datasource_ids
            ):
                continue
            item = relationship.model_dump(exclude={
                "evidence_basis",
                "observation_id",
                "probe_facts",
            })
            for key in (
                "cardinality",
                "left_measure_aggregation_safe",
                "right_measure_aggregation_safe",
                "key_alignment",
                "key_normalization",
                "join_execution_strategy",
            ):
                if key in relationship.probe_facts:
                    item[key] = relationship.probe_facts[key]
            if (
                relationship.evidence_basis == "probe"
                and relationship.probe_facts.get("join_supported") is True
                and str(relationship.probe_facts.get("cardinality") or "unknown")
                != "unknown"
            ):
                item["joinability"] = "joinable"
            aggregation_safety = _relationship_aggregation_safety(
                relationship.probe_facts
            )
            if aggregation_safety:
                item["aggregation_safety"] = {
                    "left_measure_after_join": aggregation_safety[
                        "local_measure_after_join"
                    ],
                    "right_measure_after_join": aggregation_safety[
                        "target_measure_after_join"
                    ],
                    "key_domain_coverage": aggregation_safety[
                        "key_domain_coverage"
                    ],
                }
            runtime_relationships.append(item)

        runtime_relationship_ids = {
            item["relationship_id"] for item in runtime_relationships
        }
        for lineage in runtime_lineage:
            lineage["datasource_ids"] = [
                datasource_id
                for datasource_id in lineage.get("datasource_ids") or []
                if datasource_id in lineage_datasource_ids
            ]
            lineage["relationship_ids"] = [
                relationship_id
                for relationship_id in lineage.get("relationship_ids") or []
                if relationship_id in runtime_relationship_ids
            ]

        if runtime_datasource_ids:
            snapshot["selected_datasource_ids"] = [
                datasource_id
                for datasource_id in snapshot.get("selected_datasource_ids") or []
                if datasource_id in runtime_datasource_ids
            ]
            snapshot["selected_datasource_snapshot"] = [
                datasource
                for datasource in snapshot.get("selected_datasource_snapshot") or []
                if int(datasource.get("datasource_id") or datasource.get("id") or 0)
                in runtime_datasource_ids
            ]
            for datasource in snapshot["selected_datasource_snapshot"]:
                schema = datasource.get("metadata_schema")
                relationships = schema.get("relationships") if isinstance(schema, dict) else None
                if isinstance(relationships, list):
                    schema["relationships"] = [
                        relationship
                        for relationship in relationships
                        if int(relationship.get("target_datasource_id") or 0)
                        in runtime_datasource_ids
                    ]

        snapshot["analysis_semantic_context"] = {
            "analysis_as_of_date": datetime.now(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat(),
            "explicit_time_range": self.explicit_time_range,
            "business_goal": self.intent.business_goal,
            "business_subjects": self.intent.business_subjects,
            "analytical_facets": self.intent.analytical_facets,
            "semantic_references": semantic_references,
            "business_questions": [
                item.model_dump()
                for item in self.intent.business_questions
            ],
            "relationships": runtime_relationships,
            "insight_lineage": runtime_lineage,
        }
        return snapshot


def _apply_probe_field_profiles(
    snapshot: dict[str, Any],
    observations: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> None:
    """Fold value-format conclusions into Schema without forwarding sample data."""
    rows_by_id = {
        int(item.get("datasource_id") or item.get("id") or 0): item
        for item in snapshot.get("selected_datasource_snapshot") or []
        if isinstance(item, dict)
    }
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        if (
            observation.get("kind") != "distinct_values"
            or observation.get("status") != "verified"
            or not bool(observation.get("complete"))
        ):
            continue
        datasource_id = int(observation.get("datasource_id") or 0)
        field = str(observation.get("field") or "").strip()
        datasource = rows_by_id.get(datasource_id)
        if datasource is None or not field:
            continue
        schema = datasource.get("metadata_schema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        definition = properties.get(field) if isinstance(properties, dict) else None
        if not isinstance(definition, dict):
            continue
        facts = observation.get("facts") if isinstance(observation.get("facts"), dict) else {}
        raw_values = facts.get("values") or []
        values = [
            item.get("value") if isinstance(item, dict) else item
            for item in raw_values
        ] if isinstance(raw_values, list) else []
        non_null_values = [item for item in values if item is not None and str(item).strip()]
        if not non_null_values:
            availability = "empty"
            readability = "unknown"
        else:
            availability = "non_empty"
            normalized = [str(item).strip() for item in non_null_values]
            opaque_count = sum(
                bool(
                    re.fullmatch(r"[-+]?\d{8,}", item)
                    or re.fullmatch(
                        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                        item,
                    )
                    or re.fullmatch(r"[0-9a-fA-F]{24,}", item)
                )
                for item in normalized
            )
            readability = (
                "opaque_identifier"
                if opaque_count == len(normalized)
                else "categorical_values"
            )
        definition["observed_value_profile"] = {
            "availability": availability,
            "readability": readability,
            "observed_distinct_value_count": len(non_null_values),
        }
        if readability == "categorical_values":
            definition["observed_value_profile"]["observed_values"] = [
                str(item).strip()
                for item in non_null_values[:50]
            ]
            resolutions = facts.get("requested_value_resolutions")
            if isinstance(resolutions, list) and resolutions:
                definition["observed_value_profile"]["requested_value_resolutions"] = [
                    {
                        "requested_value": str(item.get("requested_value") or "").strip(),
                        "status": str(item.get("status") or "unresolved").strip(),
                        "observed_value": str(item.get("observed_value") or "").strip(),
                    }
                    for item in resolutions
                    if isinstance(item, dict)
                    and str(item.get("requested_value") or "").strip()
                ]


def _relationship_aggregation_safety(
    probe_facts: dict[str, Any] | None,
    *,
    reverse: bool = False,
) -> dict[str, Any]:
    """Project cardinality facts into an orientation-aware execution policy."""
    facts = probe_facts if isinstance(probe_facts, dict) else {}
    if not facts or "cardinality" not in facts:
        return {}

    local_safe_key = (
        "right_measure_aggregation_safe"
        if reverse
        else "left_measure_aggregation_safe"
    )
    target_safe_key = (
        "left_measure_aggregation_safe"
        if reverse
        else "right_measure_aggregation_safe"
    )

    def side_policy(safe: Any, side: str) -> dict[str, str]:
        if safe is True:
            return {
                "status": "safe",
                "strategy": "may_aggregate_after_join",
            }
        if safe is False:
            return {
                "status": "unsafe_repeated_by_join",
                "strategy": (
                    f"do_not_attribute_{side}_measure_to_the_other_side_dimensions_"
                    "without_an_explicit_allocation_or_functional_dependency"
                ),
            }
        return {
            "status": "unknown",
            "strategy": "verify_cardinality_before_aggregating_after_join",
        }

    cardinality = str(facts.get("cardinality") or "unknown")
    if reverse:
        cardinality = {
            "one_to_many": "many_to_one",
            "many_to_one": "one_to_many",
        }.get(cardinality, cardinality)
    return {
        "cardinality": cardinality,
        "local_measure_after_join": side_policy(facts.get(local_safe_key), "local"),
        "target_measure_after_join": side_policy(
            facts.get(target_safe_key), "target"
        ),
        "key_domain_coverage": {
            "numerator": "distinct_local_keys_intersect_distinct_target_keys",
            "denominator": "distinct_local_population_keys",
            "policy": (
                "coverage_of_local_entities_by_target_rows_must_use_a_semi_join_"
                "or_key_intersection;_never_count_all_target_keys_as_local_coverage"
            ),
        },
    }


def _relationship_key_cardinality(
    probe_facts: dict[str, Any] | None,
    *,
    reverse: bool = False,
) -> dict[str, int]:
    """Project verified key populations into a datasource-local orientation."""
    facts = probe_facts if isinstance(probe_facts, dict) else {}
    local_key = "right_distinct_key_count" if reverse else "left_distinct_key_count"
    target_key = "left_distinct_key_count" if reverse else "right_distinct_key_count"

    def non_negative_int(key: str) -> int:
        try:
            return max(int(facts.get(key) or 0), 0)
        except (TypeError, ValueError):
            return 0

    result = {
        "local_distinct_key_count": non_negative_int(local_key),
        "target_distinct_key_count": non_negative_int(target_key),
        "matched_distinct_key_count": non_negative_int("matched_distinct_key_count"),
    }
    return result if any(result.values()) else {}


def _apply_relationship_execution_semantics(
    snapshot: dict[str, Any],
    business_context: BusinessContextContract,
) -> None:
    """Reproject canonical relation facts for fresh and resumed Preparation data."""
    relationships_by_id = {
        item.relationship_id: item for item in business_context.relationships
    }
    for datasource in snapshot.get("selected_datasource_snapshot") or []:
        if not isinstance(datasource, dict):
            continue
        datasource_id = int(datasource.get("datasource_id") or 0)
        schema = datasource.get("metadata_schema")
        relationships = schema.get("relationships") if isinstance(schema, dict) else None
        if not isinstance(relationships, list):
            continue
        for item in relationships:
            if not isinstance(item, dict):
                continue
            canonical = relationships_by_id.get(str(item.get("relationship_id") or ""))
            if canonical is None:
                continue
            facts = canonical.probe_facts
            if (
                canonical.evidence_basis == "probe"
                and facts.get("join_supported") is True
                and str(facts.get("cardinality") or "unknown") != "unknown"
            ):
                item["joinability"] = "joinable"
            for key in (
                "key_alignment",
                "key_normalization",
                "join_execution_strategy",
            ):
                if key in facts:
                    item[key] = facts[key]
            aggregation_safety = _relationship_aggregation_safety(
                facts,
                reverse=datasource_id == canonical.right_datasource_id,
            )
            key_cardinality = _relationship_key_cardinality(
                facts,
                reverse=datasource_id == canonical.right_datasource_id,
            )
            if key_cardinality:
                item["observed_key_cardinality"] = key_cardinality
            if not aggregation_safety:
                continue
            item["cardinality"] = aggregation_safety["cardinality"]
            item["aggregation_safety"] = {
                "local_measure_after_join": aggregation_safety[
                    "local_measure_after_join"
                ],
                "target_measure_after_join": aggregation_safety[
                    "target_measure_after_join"
                ],
                "key_domain_coverage": aggregation_safety[
                    "key_domain_coverage"
                ],
            }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    return str(content or "")


def _extract_json(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", source, re.DOTALL)
    if fenced:
        source = fenced.group(1)
    else:
        start = source.find("{")
        end = source.rfind("}")
        if start >= 0 and end > start:
            source = source[start:end + 1]
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("stage output must be one JSON object")
    return value


@lru_cache(maxsize=16)
def _load_prompt(name: str) -> str:
    return (PROMPT_ROOT / f"{name}.md").read_text(encoding="utf-8").strip()


def _invoke_contract(
    *,
    stage: str,
    payload: dict[str, Any],
    output_model: type[BaseModel],
    runtime_config: LlmRuntimeConfig | None,
    cancelled: Callable[[], None] | None = None,
) -> BaseModel:
    contract_runtime = runtime_config or create_runtime_config()
    contract_runtime = replace(
        contract_runtime,
        temperature=float(Config.ANALYSIS_PREPARATION_TEMPERATURE),
    )
    model = create_data_insight_model(contract_runtime)
    messages = [
        SystemMessage(_load_prompt(stage)),
        HumanMessage(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        ),
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        if cancelled is not None:
            cancelled()
        response_text = ""
        started_at = time.perf_counter()
        logger.info(
            "preparation LLM request stage=%s attempt=%s system_chars=%s payload_chars=%s",
            stage,
            attempt + 1,
            len(_load_prompt(stage)),
            len(str(messages[1].content or "")),
        )
        try:
            response = model.invoke(messages)
            response_text = _message_text(getattr(response, "content", response))
            logger.info(
                "preparation LLM response stage=%s attempt=%s elapsed=%.2fs output_chars=%s",
                stage,
                attempt + 1,
                time.perf_counter() - started_at,
                len(response_text),
            )
            parsed = _extract_json(response_text)
            return output_model.model_validate(parsed)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "preparation contract rejected stage=%s attempt=%s error=%s",
                stage,
                attempt + 1,
                str(exc)[:1600],
            )
            if attempt == 0:
                if response_text:
                    messages.append(AIMessage(response_text))
                messages.append(HumanMessage(
                    "上一输出不符合唯一 JSON 契约。保持业务判断不变，只修正结构或引用错误，"
                    "重新输出完整 JSON：" + str(exc)[:1200]
                ))
                continue
            raise
    raise RuntimeError(f"{stage} failed: {last_error}")


def _schema_property_items(
    item: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    schema = item.get("metadata_schema")
    if not isinstance(schema, dict):
        return []
    raw_fields = schema.get("properties")
    if not raw_fields:
        raw_fields = schema.get("fields") or schema.get("columns") or []
    if isinstance(raw_fields, dict):
        return [
            (
                str(name),
                copy.deepcopy(definition)
                if isinstance(definition, dict)
                else {},
            )
            for name, definition in raw_fields.items()
            if str(name)
        ]
    iterable = raw_fields if isinstance(raw_fields, list) else []
    properties: list[tuple[str, dict[str, Any]]] = []
    for field in iterable:
        if isinstance(field, str):
            name = field
            definition: dict[str, Any] = {}
        elif isinstance(field, dict):
            name = str(
                field.get("name")
                or field.get("field")
                or field.get("column")
                or ""
            )
            definition = copy.deepcopy(field)
        else:
            continue
        if not name:
            continue
        properties.append((name, definition))
    return properties


def _schema_field_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for name, definition in _schema_property_items(item):
        details.append({
            "name": name,
            "type": str(
                definition.get("type")
                or definition.get("property_type")
                or definition.get("data_type")
                or definition.get("dtype")
                or ""
            ),
            "description": str(
                definition.get("description")
                or definition.get("comment")
                or definition.get("title")
                or (
                    definition.get("name")
                    if str(definition.get("name") or "") != name
                    else ""
                )
                or ""
            ),
            **(
                {"schema_enrichment": definition["schema_enrichment"]}
                if isinstance(definition.get("schema_enrichment"), dict)
                else {}
            ),
        })
    return details


def _schema_fields(item: dict[str, Any]) -> list[str]:
    return [detail["name"] for detail in _schema_field_details(item)]


def _schema_field_capabilities(item: dict[str, Any]) -> dict[str, list[str]]:
    queryable = _schema_fields(item)
    described: list[str] = []
    schema = item.get("metadata_schema")
    if isinstance(schema, dict):
        for key in ("derived_fields", "descriptive_fields", "field_descriptions"):
            value = schema.get(key)
            if isinstance(value, dict):
                described.extend(str(item) for item in value)
            elif isinstance(value, list):
                described.extend(
                    str(item.get("name") if isinstance(item, dict) else item)
                    for item in value
                )
    return {
        "queryable_fields": list(dict.fromkeys(queryable)),
        "descriptive_or_derived_fields": [
            field
            for field in dict.fromkeys(described)
            if field and field not in queryable
        ],
    }


def _relationship_fields(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if str(item or "").strip()
        ]
    return []


def _declared_schema_relationships(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize only explicit datasource relationship metadata.

    Field-name similarity is intentionally excluded: it is a Probe candidate,
    not schema authority.
    """
    identifiers = {
        str(
            item.get("datasource_identifier")
            or item.get("identifier")
            or item.get("table_name")
            or ""
        ): int(item.get("datasource_id") or item.get("id") or 0)
        for item in rows
        if isinstance(item, dict)
    }
    normalized: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        source_id = int(item.get("datasource_id") or item.get("id") or 0)
        schema = item.get("metadata_schema")
        if source_id <= 0 or not isinstance(schema, dict):
            continue
        raw_relationships: list[Any] = []
        for key in ("relationships", "foreign_keys"):
            value = schema.get(key)
            if isinstance(value, list):
                raw_relationships.extend(value)
            elif isinstance(value, dict):
                raw_relationships.extend(
                    {"name": name, **(definition if isinstance(definition, dict) else {})}
                    for name, definition in value.items()
                )
        for raw in raw_relationships:
            if not isinstance(raw, dict):
                continue
            target_id = int(
                raw.get("target_datasource_id")
                or raw.get("referenced_datasource_id")
                or raw.get("right_datasource_id")
                or raw.get("to_datasource_id")
                or 0
            )
            if target_id <= 0:
                target_identifier = str(
                    raw.get("target_datasource_identifier")
                    or raw.get("referenced_table")
                    or raw.get("target_table")
                    or raw.get("to")
                    or ""
                )
                target_id = identifiers.get(target_identifier, 0)
            left_fields = _relationship_fields(
                raw.get("left_fields")
                or raw.get("source_fields")
                or raw.get("local_fields")
                or raw.get("columns")
                or raw.get("column")
                or raw.get("field")
            )
            right_fields = _relationship_fields(
                raw.get("right_fields")
                or raw.get("target_fields")
                or raw.get("referenced_fields")
                or raw.get("referenced_columns")
                or raw.get("referenced_column")
                or raw.get("target_field")
            )
            if (
                target_id <= 0
                or not left_fields
                or len(left_fields) != len(right_fields)
            ):
                continue
            normalized.append({
                "left_datasource_id": source_id,
                "right_datasource_id": target_id,
                "left_fields": left_fields,
                "right_fields": right_fields,
            })
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for relationship in normalized:
        signature = (
            relationship["left_datasource_id"],
            relationship["right_datasource_id"],
            tuple(relationship["left_fields"]),
            tuple(relationship["right_fields"]),
        )
        unique[signature] = relationship
    return list(unique.values())


def _schema_declares_relationship(
    declared_relationships: list[dict[str, Any]],
    *,
    left_datasource_id: int,
    right_datasource_id: int,
    left_fields: list[str],
    right_fields: list[str],
) -> bool:
    expected = (
        left_datasource_id,
        right_datasource_id,
        tuple(left_fields),
        tuple(right_fields),
    )
    reverse = (
        right_datasource_id,
        left_datasource_id,
        tuple(right_fields),
        tuple(left_fields),
    )
    return any(
        (
            int(item.get("left_datasource_id") or 0),
            int(item.get("right_datasource_id") or 0),
            tuple(item.get("left_fields") or ()),
            tuple(item.get("right_fields") or ()),
        )
        in {expected, reverse}
        for item in declared_relationships
    )


def build_datasource_catalog(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in snapshot.get("selected_datasource_snapshot") or []:
        if not isinstance(item, dict):
            continue
        datasource_id = int(item.get("datasource_id") or item.get("id") or 0)
        if datasource_id <= 0:
            continue
        schema = item.get("metadata_schema")
        capabilities = _schema_field_capabilities(item)
        result.append({
            "datasource_id": datasource_id,
            "datasource_name": str(
                item.get("datasource_name") or item.get("name") or ""
            ),
            "datasource_type": str(
                item.get("datasource_type") or item.get("type") or ""
            ),
            "description": str(
                item.get("description")
                or (schema.get("description") if isinstance(schema, dict) else "")
                or ""
            ),
            "datasource_identifier": item.get("datasource_identifier"),
            "table_engine": item.get("table_engine"),
            "ts_query_source": item.get("ts_query_source"),
            "relationships": (
                {
                    key: schema.get(key)
                    for key in ("relationships", "foreign_keys")
                    if schema.get(key)
                }
                if isinstance(schema, dict)
                else {}
            ),
            "fields": capabilities["queryable_fields"],
            "field_details": _schema_field_details(item),
            "field_capabilities": capabilities,
        })
    return result


def _search_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for segment in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", str(value or "")):
        normalized = segment.strip().lower()
        if len(normalized) < 2:
            continue
        tokens.add(normalized)
        if re.fullmatch(r"[\u4e00-\u9fff]+", normalized) and len(normalized) > 2:
            tokens.update(
                normalized[index:index + 2]
                for index in range(len(normalized) - 1)
            )
    return tokens


def build_datasource_discovery_catalog(
    catalog: list[dict[str, Any]],
    *,
    search_text: str,
) -> list[dict[str, Any]]:
    tokens = _search_tokens(search_text)
    result: list[dict[str, Any]] = []
    for item in catalog:
        ranked_fields: list[tuple[int, int, dict[str, str]]] = []
        for index, detail in enumerate(item.get("field_details") or []):
            name = str(detail.get("name") or "")
            description = str(detail.get("description") or "")
            name_text = name.lower()
            description_text = description.lower()
            score = sum(
                4 * int(token in name_text) + int(token in description_text)
                for token in tokens
            )
            if score > 0:
                ranked_fields.append((score, -index, {
                    "name": str(detail.get("name") or ""),
                    "type": str(detail.get("type") or ""),
                    "description": description[:100],
                }))
        ranked_fields.sort(reverse=True, key=lambda item: (item[0], item[1]))
        matched_fields = [item[2] for item in ranked_fields[:5]]
        result.append({
            "datasource_id": item["datasource_id"],
            "datasource_name": item.get("datasource_name"),
            "datasource_type": item.get("datasource_type"),
            "description": str(item.get("description") or "")[:180],
            "field_count": len(item.get("fields") or []),
            "matched_fields": matched_fields,
        })
    return result


def recall_datasources_for_business_facts(
    catalog: list[dict[str, Any]],
    intent: IntentContract,
    *,
    per_fact: int = 2,
) -> list[int]:
    """Keep the strongest Schema candidates for every canonical business fact."""
    recalled: list[int] = []
    for question in intent.business_questions:
        for fact in question.required_business_facts:
            tokens = _search_tokens(
                " ".join([
                    fact.business_fact,
                    fact.semantic_definition,
                    question.business_question,
                ])
            )
            ranked: list[tuple[int, int]] = []
            for item in catalog:
                datasource_id = int(item.get("datasource_id") or 0)
                datasource_text = " ".join([
                    str(item.get("datasource_name") or ""),
                    str(item.get("description") or ""),
                ]).lower()
                score = sum(3 * int(token in datasource_text) for token in tokens)
                for detail in item.get("field_details") or []:
                    name = str(detail.get("name") or "").lower()
                    description = str(detail.get("description") or "").lower()
                    score += sum(
                        4 * int(token in name) + int(token in description)
                        for token in tokens
                    )
                if datasource_id > 0 and score > 0:
                    ranked.append((score, datasource_id))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            recalled.extend(
                datasource_id for _, datasource_id in ranked[:max(per_fact, 1)]
            )
    return list(dict.fromkeys(recalled))


def filter_datasource_snapshot(
    snapshot: dict[str, Any],
    datasource_ids: list[int],
) -> dict[str, Any]:
    allowed = {int(item) for item in datasource_ids if int(item or 0) > 0}
    rows = [
        dict(item)
        for item in snapshot.get("selected_datasource_snapshot") or []
        if isinstance(item, dict)
        and int(item.get("datasource_id") or item.get("id") or 0) in allowed
    ]
    return {
        **snapshot,
        "selected_datasource_ids": [
            int(item.get("datasource_id") or item.get("id"))
            for item in rows
        ],
        "selected_datasource_snapshot": rows,
    }


def filter_relationship_hints(
    relationship_hints: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    datasource_ids: list[int],
) -> tuple[dict[str, Any], ...]:
    allowed = {int(item) for item in datasource_ids if int(item or 0) > 0}
    return tuple(
        dict(item)
        for item in relationship_hints
        if isinstance(item, dict)
        and int(item.get('source_datasource_id') or 0) in allowed
        and int(item.get('target_datasource_id') or 0) in allowed
    )


def validate_semantic_relationship_hints(
    relationship_hints: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    schemas: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Keep provider-asserted relations whose endpoints match the current Schema."""
    fields_by_id = {
        int(item.get("datasource_id") or item.get("id") or 0): set(
            (item.get("field_capabilities") or {}).get("queryable_fields")
            or _schema_fields(item)
        )
        for item in schemas
        if isinstance(item, dict)
    }
    validated: list[dict[str, Any]] = []
    for hint in relationship_hints:
        if (
            not isinstance(hint, dict)
            or hint.get("source") != "semantic_center"
            or hint.get("verification_status") != "provider_asserted"
        ):
            continue
        source_id = int(hint.get("source_datasource_id") or 0)
        target_id = int(hint.get("target_datasource_id") or 0)
        source_fields = [
            str(item).strip()
            for item in hint.get("source_fields") or []
            if str(item).strip()
        ]
        target_fields = [
            str(item).strip()
            for item in hint.get("target_fields") or []
            if str(item).strip()
        ]
        if (
            source_id not in fields_by_id
            or target_id not in fields_by_id
            or not source_fields
            or len(source_fields) != len(target_fields)
            or any(field not in fields_by_id[source_id] for field in source_fields)
            or any(field not in fields_by_id[target_id] for field in target_fields)
        ):
            continue
        validated.append({
            **dict(hint),
            "source_datasource_id": source_id,
            "target_datasource_id": target_id,
            "source_fields": source_fields,
            "target_fields": target_fields,
        })
    return tuple(validated)


def enrich_snapshot_with_semantic_relationships(
    snapshot: dict[str, Any],
    relationship_hints: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Adapt provider ER facts into the existing datasource Schema contract."""
    enriched = copy.deepcopy(snapshot)
    rows = [
        item
        for item in enriched.get("selected_datasource_snapshot") or []
        if isinstance(item, dict)
    ]
    validated = validate_semantic_relationship_hints(relationship_hints, rows)
    rows_by_id = {
        int(item.get("datasource_id") or item.get("id") or 0): item
        for item in rows
    }
    for hint in validated:
        source_id = int(hint["source_datasource_id"])
        source = rows_by_id.get(source_id)
        if source is None:
            continue
        metadata_schema = source.get("metadata_schema")
        if not isinstance(metadata_schema, dict):
            metadata_schema = {}
            source["metadata_schema"] = metadata_schema
        relationships = metadata_schema.get("relationships")
        if not isinstance(relationships, list):
            relationships = []
            metadata_schema["relationships"] = relationships
        candidate = {
            "target_datasource_id": int(hint["target_datasource_id"]),
            "source_fields": list(hint["source_fields"]),
            "target_fields": list(hint["target_fields"]),
            "description": str(hint.get("description") or "").strip(),
            "source": "semantic_center",
        }
        signature = (
            candidate["target_datasource_id"],
            tuple(candidate["source_fields"]),
            tuple(candidate["target_fields"]),
        )
        if any(
            (
                int(item.get("target_datasource_id") or 0),
                tuple(_relationship_fields(
                    item.get("source_fields")
                    or item.get("left_fields")
                    or item.get("local_fields")
                )),
                tuple(_relationship_fields(
                    item.get("target_fields")
                    or item.get("right_fields")
                    or item.get("referenced_fields")
                )),
            ) == signature
            for item in relationships
            if isinstance(item, dict)
        ):
            continue
        relationships.append(candidate)
    return enriched


def compact_datasource_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add one canonical property view without removing source schema semantics."""
    rows: list[dict[str, Any]] = []
    for item in snapshot.get("selected_datasource_snapshot") or []:
        if not isinstance(item, dict):
            continue
        property_items = _schema_property_items(item)
        schema = item.get("metadata_schema")
        compact = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"metadata_schema", "field_capabilities"}
        }
        compact_schema = copy.deepcopy(schema) if isinstance(schema, dict) else {}
        if not isinstance(compact_schema.get("properties"), dict):
            compact_schema["properties"] = {
                name: definition
                for name, definition in property_items
            }
        compact["metadata_schema"] = compact_schema
        compact["field_capabilities"] = _schema_field_capabilities(item)
        rows.append(compact)
    return {
        **snapshot,
        "selected_datasource_snapshot": rows,
    }


def enrich_selected_datasource_schemas(
    snapshot: dict[str, Any],
    *,
    selection: DatasourceSelectionContract,
    business_context: BusinessContextContract,
) -> dict[str, Any]:
    """Return the selected schemas with one canonical, in-schema ER graph."""
    enriched = compact_datasource_snapshot(snapshot)
    rows = [
        item
        for item in enriched.get("selected_datasource_snapshot") or []
        if isinstance(item, dict)
    ]
    rows_by_id = {
        int(item.get("datasource_id") or item.get("id") or 0): item
        for item in rows
        if int(item.get("datasource_id") or item.get("id") or 0) > 0
    }
    names_by_id = {
        datasource_id: str(
            item.get("datasource_name")
            or item.get("name")
            or f"数据源{datasource_id}"
        )
        for datasource_id, item in rows_by_id.items()
    }
    relationships_by_datasource: dict[int, list[dict[str, Any]]] = {
        datasource_id: []
        for datasource_id in rows_by_id
    }

    def add_relationship(
        *,
        relationship_id: str,
        left_datasource_id: int,
        right_datasource_id: int,
        left_fields: list[str],
        right_fields: list[str],
        relationship_type: str,
        joinability: str,
        business_meaning: str,
        evidence_basis: str,
        observation_id: str = "",
        probe_facts: dict[str, Any] | None = None,
    ) -> None:
        if (
            left_datasource_id not in rows_by_id
            or right_datasource_id not in rows_by_id
            or not left_fields
            or len(left_fields) != len(right_fields)
        ):
            return
        orientations = (
            (
                left_datasource_id,
                right_datasource_id,
                left_fields,
                right_fields,
            ),
            (
                right_datasource_id,
                left_datasource_id,
                right_fields,
                left_fields,
            ),
        )
        for orientation_index, (
            source_id,
            target_id,
            local_fields,
            target_fields,
        ) in enumerate(orientations):
            item = {
                "relationship_id": relationship_id,
                "target_datasource_id": target_id,
                "target_datasource_name": names_by_id[target_id],
                "local_fields": list(local_fields),
                "target_fields": list(target_fields),
                "relationship_type": relationship_type,
                "joinability": joinability,
                "business_meaning": business_meaning,
                "evidence_basis": evidence_basis,
            }
            if observation_id:
                item["observation_id"] = observation_id
            if probe_facts:
                item["probe_facts"] = dict(probe_facts)
                for key in (
                    "key_alignment",
                    "key_normalization",
                    "join_execution_strategy",
                ):
                    if key in probe_facts:
                        item[key] = probe_facts[key]
                aggregation_safety = _relationship_aggregation_safety(
                    probe_facts,
                    reverse=orientation_index == 1,
                )
                key_cardinality = _relationship_key_cardinality(
                    probe_facts,
                    reverse=orientation_index == 1,
                )
                if key_cardinality:
                    item["observed_key_cardinality"] = key_cardinality
                if aggregation_safety:
                    item["cardinality"] = aggregation_safety["cardinality"]
                    item["aggregation_safety"] = {
                        "local_measure_after_join": aggregation_safety[
                            "local_measure_after_join"
                        ],
                        "target_measure_after_join": aggregation_safety[
                            "target_measure_after_join"
                        ],
                        "key_domain_coverage": aggregation_safety[
                            "key_domain_coverage"
                        ],
                    }
            relationships_by_datasource[source_id].append(item)

    known_signatures: set[tuple[int, int, tuple[str, ...], tuple[str, ...]]] = set()
    for relationship in business_context.relationships:
        signature = (
            relationship.left_datasource_id,
            relationship.right_datasource_id,
            tuple(relationship.left_fields),
            tuple(relationship.right_fields),
        )
        reverse_signature = (
            relationship.right_datasource_id,
            relationship.left_datasource_id,
            tuple(relationship.right_fields),
            tuple(relationship.left_fields),
        )
        known_signatures.update({signature, reverse_signature})
        add_relationship(
            relationship_id=relationship.relationship_id,
            left_datasource_id=relationship.left_datasource_id,
            right_datasource_id=relationship.right_datasource_id,
            left_fields=relationship.left_fields,
            right_fields=relationship.right_fields,
            relationship_type=relationship.relationship_type,
            joinability=relationship.joinability,
            business_meaning=relationship.business_meaning,
            evidence_basis=relationship.evidence_basis,
            observation_id=relationship.observation_id,
            probe_facts=relationship.probe_facts,
        )

    # Preserve explicit physical FK metadata even when the advisory LLM stage
    # could not finalize a richer business description.
    for index, relationship in enumerate(_declared_schema_relationships(rows), start=1):
        signature = (
            relationship["left_datasource_id"],
            relationship["right_datasource_id"],
            tuple(relationship["left_fields"]),
            tuple(relationship["right_fields"]),
        )
        if signature in known_signatures:
            continue
        add_relationship(
            relationship_id=f"schema_relationship_{index}",
            left_datasource_id=relationship["left_datasource_id"],
            right_datasource_id=relationship["right_datasource_id"],
            left_fields=relationship["left_fields"],
            right_fields=relationship["right_fields"],
            relationship_type="same_entity",
            joinability="joinable",
            business_meaning="数据源 Schema 显式声明的字段关系",
            evidence_basis="schema",
        )

    for datasource_id, row in rows_by_id.items():
        schema = row.get("metadata_schema")
        if not isinstance(schema, dict):
            schema = {}
            row["metadata_schema"] = schema
        schema["relationships"] = relationships_by_datasource[datasource_id]
        properties = schema.get("properties")
        if isinstance(properties, dict):
            relationship_ids_by_field: dict[str, list[str]] = {}
            for relationship in relationships_by_datasource[datasource_id]:
                relationship_id = str(relationship.get("relationship_id") or "")
                for field_name in relationship.get("local_fields") or []:
                    if relationship_id:
                        relationship_ids_by_field.setdefault(
                            str(field_name),
                            [],
                        ).append(relationship_id)
            for field_name, relationship_ids in relationship_ids_by_field.items():
                property_schema = properties.get(field_name)
                if not isinstance(property_schema, dict):
                    continue
                property_schema["relationship_ids"] = list(
                    dict.fromkeys(relationship_ids)
                )

    return {
        **enriched,
        "selected_datasource_ids": [
            datasource_id
            for datasource_id in selection.selected_datasource_ids
            if datasource_id in rows_by_id
        ],
        "selected_datasource_snapshot": rows,
    }


def infer_query_capabilities(snapshot: dict[str, Any]) -> tuple[str, ...]:
    capabilities: set[str] = set()
    for item in snapshot.get("selected_datasource_snapshot") or []:
        if not isinstance(item, dict):
            continue
        source_type = str(
            item.get("datasource_type") or item.get("type") or ""
        ).lower()
        if source_type in {"local_file", "minio_file"}:
            capabilities.add("local_file")
        elif source_type == "api":
            capabilities.add("api")
        else:
            capabilities.add("sql")
        metadata_text = json.dumps(
            item.get("metadata_schema") or {},
            ensure_ascii=False,
        ).lower()
        if (
            str(item.get("table_engine") or "").lower() == "time_series"
            or str(item.get("ts_query_source") or "").lower() in {"timescale", "vxbase"}
            or any(
                token in metadata_text
                for token in ("timescale", "time_series", "ts_query", "时序")
            )
        ):
            capabilities.add("time_series")
    return tuple(sorted(capabilities))


def build_query_guidance(capabilities: tuple[str, ...]) -> str:
    parts = []
    for capability in capabilities:
        path = QUERY_PROMPT_ROOT / f"{capability}.md"
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def validate_business_context(
    contract: BusinessContextContract,
    schemas: list[dict[str, Any]],
    *,
    selection: DatasourceSelectionContract,
    intent: IntentContract | None = None,
    probe_observations: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> None:
    fields_by_id = {
        int(item.get("datasource_id") or item.get("id") or 0): set(
            (item.get("field_capabilities") or {}).get("queryable_fields")
            or _schema_fields(item)
        )
        for item in schemas
        if isinstance(item, dict)
    }
    selected_ids = set(selection.selected_datasource_ids)
    observations_by_id = {
        str(item.get("observation_id") or ""): item
        for item in probe_observations
        if isinstance(item, dict) and str(item.get("observation_id") or "")
    }
    declared_relationships = _declared_schema_relationships(schemas)
    for item in schemas:
        for relationship in item.get("declared_relationships") or []:
            if isinstance(relationship, dict):
                declared_relationships.append(relationship)
    for request in contract.probe_requests:
        if request.datasource_id not in selected_ids:
            raise ValueError(
                f"Probe request {request.request_id} references an unselected datasource"
            )
        if request.field not in fields_by_id.get(request.datasource_id, set()):
            raise ValueError(
                f"Probe request {request.request_id} references an unavailable field"
            )
    for relationship in contract.relationships:
        if (
            relationship.left_datasource_id not in selected_ids
            or relationship.right_datasource_id not in selected_ids
        ):
            raise ValueError(
                f"relationship {relationship.relationship_id} references an "
                "unselected datasource"
            )
        invalid_left = set(relationship.left_fields) - fields_by_id.get(
            relationship.left_datasource_id,
            set(),
        )
        invalid_right = set(relationship.right_fields) - fields_by_id.get(
            relationship.right_datasource_id,
            set(),
        )
        if invalid_left or invalid_right:
            raise ValueError(
                f"relationship {relationship.relationship_id} references unavailable fields"
            )
        if (
            relationship.relationship_type in {
                "same_entity",
                "parent_child",
                "event_of_entity",
                "dimension_enrichment",
                "bridge",
            }
            and relationship.joinability == "joinable"
            and len(relationship.left_fields) != len(relationship.right_fields)
        ):
            raise ValueError(
                f"relationship {relationship.relationship_id} must map the same number "
                "of left and right key fields"
            )
        if relationship.joinability == "joinable":
            if relationship.evidence_basis not in {"schema", "probe"}:
                raise ValueError(
                    f"joinable relationship {relationship.relationship_id} lacks evidence"
                )
            if not relationship.left_fields or not relationship.right_fields:
                raise ValueError(
                    f"joinable relationship {relationship.relationship_id} lacks keys"
                )
        if (
            relationship.relationship_type in {
                "same_entity",
                "parent_child",
                "event_of_entity",
                "dimension_enrichment",
                "bridge",
            }
            and relationship.evidence_basis == "schema"
            and relationship.joinability in {"joinable", "alignable"}
            and not _schema_declares_relationship(
                declared_relationships,
                left_datasource_id=relationship.left_datasource_id,
                right_datasource_id=relationship.right_datasource_id,
                left_fields=relationship.left_fields,
                right_fields=relationship.right_fields,
            )
        ):
            raise ValueError(
                f"relationship {relationship.relationship_id} claims schema evidence "
                "without matching explicit relationship metadata; Probe it or leave it unresolved"
            )
        if relationship.evidence_basis == "probe" and not relationship.observation_id:
            raise ValueError(
                f"relationship {relationship.relationship_id} claims Probe evidence "
                "without an observation_id"
            )
        if relationship.evidence_basis == "probe":
            observation = observations_by_id.get(relationship.observation_id)
            if observation is None:
                raise ValueError(
                    f"relationship {relationship.relationship_id} references an "
                    "unavailable Probe observation"
                )
            if (
                observation.get("kind") != "relationship_coverage"
                or observation.get("status") != "verified"
                or not bool(observation.get("complete"))
            ):
                raise ValueError(
                    f"relationship {relationship.relationship_id} requires a complete "
                    "verified relationship_coverage observation"
                )

    available_relationship_ids = {
        item.relationship_id
        for item in contract.relationships
    }
    relationships_by_id = {
        item.relationship_id: item
        for item in contract.relationships
    }
    coverage_by_question = {
        item.question_id: item
        for item in selection.question_coverage
    }
    expected_question_ids = (
        {item.question_id for item in intent.business_questions}
        if intent is not None
        else set(coverage_by_question)
    )
    actual_question_ids = {
        item.question_id
        for item in contract.insight_lineage
    }
    if actual_question_ids != expected_question_ids:
        raise ValueError(
            "insight_lineage must cover every planned business question exactly"
        )
    required_fact_ids_by_question = {
        item.question_id: {
            fact.fact_id for fact in item.required_business_facts
        }
        for item in (intent.business_questions if intent is not None else [])
    }
    all_required_fact_ids = {
        fact_id
        for fact_ids in required_fact_ids_by_question.values()
        for fact_id in fact_ids
    }
    for lineage in contract.insight_lineage:
        lineage_datasource_ids = set(lineage.datasource_ids)
        if not lineage_datasource_ids.issubset(selected_ids):
            raise ValueError(
                f"lineage {lineage.question_id} references an unselected datasource"
            )
        coverage = coverage_by_question.get(lineage.question_id)
        if coverage is None or not lineage_datasource_ids.issubset(
            set(coverage.datasource_ids)
        ):
            raise ValueError(
                f"lineage {lineage.question_id} uses a datasource outside question coverage"
            )
        if not set(lineage.relationship_ids).issubset(available_relationship_ids):
            raise ValueError(
                f"lineage {lineage.question_id} references an unavailable relationship"
            )
        bound_fact_ids: set[str] = set()
        binding_keys: set[tuple[str, int]] = set()
        for binding in lineage.fact_bindings:
            binding_key = (binding.fact_id, binding.datasource_id)
            if binding_key in binding_keys:
                raise ValueError(
                    f"lineage {lineage.question_id} repeats a fact binding"
                )
            binding_keys.add(binding_key)
            bound_fact_ids.add(binding.fact_id)
            if (
                binding.datasource_id > 0
                and binding.datasource_id not in lineage_datasource_ids
            ):
                raise ValueError(
                    f"lineage {lineage.question_id} binds a fact outside its datasource set"
                )
            if (
                binding.datasource_id > 0
                and set(binding.fields) - fields_by_id.get(binding.datasource_id, set())
            ):
                raise ValueError(
                    f"lineage {lineage.question_id} references unavailable fact fields"
                )
            if all_required_fact_ids and not set(
                binding.derived_from_fact_ids
            ).issubset(all_required_fact_ids):
                raise ValueError(
                    f"lineage {lineage.question_id} derives a fact from an unavailable fact ID"
                )
        unresolved_fact_ids = {
            item.fact_id for item in lineage.unresolved_business_facts
        }
        if len(unresolved_fact_ids) != len(lineage.unresolved_business_facts):
            raise ValueError(
                f"lineage {lineage.question_id} repeats unresolved fact IDs"
            )
        for unresolved_fact in lineage.unresolved_business_facts:
            for unsupported in unresolved_fact.unsupported_field_bindings:
                if unsupported.datasource_id not in selected_ids:
                    raise ValueError(
                        f"lineage {lineage.question_id} excludes a field from an "
                        "unselected datasource"
                    )
                if set(unsupported.fields) - fields_by_id.get(
                    unsupported.datasource_id,
                    set(),
                ):
                    raise ValueError(
                        f"lineage {lineage.question_id} excludes unavailable fields"
                    )
        required_fact_ids = required_fact_ids_by_question.get(
            lineage.question_id,
            bound_fact_ids | unresolved_fact_ids,
        )
        if bound_fact_ids | unresolved_fact_ids != required_fact_ids:
            raise ValueError(
                f"lineage {lineage.question_id} must account for every required fact ID"
            )
        if lineage.lineage_status == "direct" and (
            not lineage_datasource_ids
            or bound_fact_ids != required_fact_ids
            or unresolved_fact_ids
        ):
            raise ValueError(
                f"direct lineage {lineage.question_id} requires every fact to be grounded"
            )
        unresolved_join_relationships = [
            relationship_id
            for relationship_id in lineage.relationship_ids
            for relationship in [relationships_by_id.get(relationship_id)]
            if relationship is not None
            and relationship.relationship_type in {
                "same_entity",
                "parent_child",
                "event_of_entity",
                "dimension_enrichment",
                "bridge",
            }
            and relationship.joinability != "joinable"
        ]
        if (
            lineage.lineage_status == "direct"
            and unresolved_join_relationships
        ):
            raise ValueError(
                f"direct lineage {lineage.question_id} depends on unresolved "
                "cross-source relationships"
            )
        if lineage.lineage_status in {"partial", "unresolved"} and not (
            lineage.unresolved_business_facts
        ):
            raise ValueError(
                f"lineage {lineage.question_id} must state unresolved business facts"
            )


class AnalysisPreparationState(TypedDict, total=False):
    catalog: list[dict[str, Any]]
    intent: IntentContract
    selection: DatasourceSelectionContract
    selected_snapshot: dict[str, Any]
    selected_schemas: list[dict[str, Any]]
    business_context: BusinessContextContract
    probe_observations: tuple[dict[str, Any], ...]
    query_capabilities: tuple[str, ...]


class AnalysisPreparationWorkflow:
    """Advisory LangGraph preparation; no node can terminate formal analysis."""

    def __init__(
        self,
        *,
        user_query: str,
        context_mode: str,
        expected_time_range: dict[str, Any],
        datasource_snapshot: dict[str, Any],
        conversation_context: dict[str, Any] | None,
        runtime_config: LlmRuntimeConfig | None,
        execution_context: dict[str, Any] | None,
        cancelled: Callable[[], None] | None,
        stage_callback: Callable[[str], None] | None,
    ) -> None:
        self.user_query = user_query
        self.context_mode = context_mode
        self.expected_time_range = expected_time_range
        self.datasource_snapshot = _normalize_time_series_datasource_snapshot(
            datasource_snapshot
        )
        self.conversation_context = conversation_context or {}
        self.runtime_config = runtime_config
        self.execution_context = dict(execution_context or {})
        self.cancelled = cancelled
        self.stage_callback = stage_callback

    def _start_stage(self, stage: str) -> None:
        if self.cancelled is not None:
            self.cancelled()
        if self.stage_callback is not None:
            self.stage_callback(stage)

    def _fallback_intent(self) -> IntentContract:
        query = self.user_query.strip() or "完成本轮数据洞察分析"
        return IntentContract(
            business_goal=query,
            business_questions=[BusinessQuestionContract(
                question_id="question_1",
                business_question=query,
                required_business_facts=[BusinessFactRequirementContract(
                    fact_id="question_1_fact_1",
                    business_fact=query,
                    semantic_definition="用户当前明确要求理解的业务事实",
                )],
            )],
        )

    @staticmethod
    def _fallback_selection(
        catalog: list[dict[str, Any]],
        intent: IntentContract,
    ) -> DatasourceSelectionContract:
        datasource_ids = [
            int(item.get("datasource_id") or 0)
            for item in catalog
            if int(item.get("datasource_id") or 0) > 0
        ]
        if not datasource_ids:
            raise ValueError("no datasource is available for analysis")
        return DatasourceSelectionContract(
            selected_datasource_ids=datasource_ids,
            question_coverage=[
                BusinessQuestionCoverageContract(
                    question_id=question.question_id,
                    datasource_ids=datasource_ids,
                    fact_coverage=[
                        BusinessFactCoverageContract(
                            fact_id=fact.fact_id,
                            coverage_status="direct",
                            datasource_ids=datasource_ids,
                            coverage_reason="保留当前候选集合供正式业务血缘核验",
                        )
                        for fact in question.required_business_facts
                    ],
                    coverage_reason="保留当前候选集合以覆盖该业务问题",
                )
                for question in intent.business_questions
            ],
            selection_reason="选源判断未完全定稿，保留候选来源交由统一洞察阶段复核",
        )

    @staticmethod
    def _normalize_selection(
        selection: DatasourceSelectionContract,
        catalog: list[dict[str, Any]],
        intent: IntentContract,
    ) -> DatasourceSelectionContract:
        available_ids = {
            int(item.get("datasource_id") or 0)
            for item in catalog
            if int(item.get("datasource_id") or 0) > 0
        }
        selected_ids = set(selection.selected_datasource_ids)
        if not selected_ids or not selected_ids.issubset(available_ids):
            raise ValueError("selection references unavailable datasource IDs")
        coverage_by_question = {
            item.question_id: item
            for item in selection.question_coverage
        }
        expected_question_ids = {
            item.question_id for item in intent.business_questions
        }
        if set(coverage_by_question) != expected_question_ids:
            raise ValueError("question_coverage must exactly match intent business questions")
        for question in intent.business_questions:
            coverage = coverage_by_question[question.question_id]
            expected_facts = {
                item.fact_id for item in question.required_business_facts
            }
            actual_facts = {
                item.fact_id for item in coverage.fact_coverage
            }
            if actual_facts != expected_facts:
                raise ValueError(
                    f"fact_coverage for {question.question_id} must exactly match "
                    "intent required_business_facts"
                )
        return selection

    @staticmethod
    def _complete_derived_fact_lineage(
        candidate: BusinessContextContract,
        selection: DatasourceSelectionContract,
    ) -> BusinessContextContract:
        """Project Selection-owned derived facts into Business Context lineage."""
        coverage_by_question = {
            item.question_id: item
            for item in selection.question_coverage
        }
        normalized_lineage: list[InsightLineageContract] = []
        for lineage in candidate.insight_lineage:
            coverage = coverage_by_question.get(lineage.question_id)
            if coverage is None:
                normalized_lineage.append(lineage)
                continue
            bindings = list(lineage.fact_bindings)
            bound_fact_ids = {item.fact_id for item in bindings}
            unresolved = list(lineage.unresolved_business_facts)
            unresolved_by_id = {item.fact_id: item for item in unresolved}
            for fact in coverage.fact_coverage:
                if not fact.derived_from_fact_ids or fact.fact_id in bound_fact_ids:
                    continue
                if fact.coverage_status == "unresolved":
                    continue
                bindings.append(BusinessFactBindingContract(
                    fact_id=fact.fact_id,
                    datasource_id=0,
                    derived_from_fact_ids=fact.derived_from_fact_ids,
                ))
                bound_fact_ids.add(fact.fact_id)
                unresolved_by_id.pop(fact.fact_id, None)
            required_fact_ids = {
                fact.fact_id for fact in coverage.fact_coverage
            }
            relationship_by_id = {
                item.relationship_id: item for item in candidate.relationships
            }
            unresolved_join = any(
                relationship is not None
                and relationship.relationship_type in {
                    "same_entity",
                    "parent_child",
                    "event_of_entity",
                    "dimension_enrichment",
                    "bridge",
                }
                and relationship.joinability != "joinable"
                for relationship_id in lineage.relationship_ids
                for relationship in [relationship_by_id.get(relationship_id)]
            )
            if not unresolved_join:
                # A bound fact with a fully resolved relationship path is
                # analysis-ready. Requiring aggregation, ranking, comparison or
                # interpretation does not make the business fact unavailable;
                # those are Runtime responsibilities. Preserve unresolved facts
                # only when Preparation identified a concrete unsupported field
                # binding or an actual missing lineage path.
                unresolved_by_id = {
                    fact_id: unresolved_fact
                    for fact_id, unresolved_fact in unresolved_by_id.items()
                    if fact_id not in bound_fact_ids
                }
            lineage_status = lineage.lineage_status
            if (
                bound_fact_ids == required_fact_ids
                and not unresolved_by_id
                and not unresolved_join
            ):
                lineage_status = "direct"
            elif unresolved_join:
                # A partially verified relationship graph must not invalidate the
                # verified edges or the rest of Preparation. Keep the grounded
                # facts, but make the affected question explicitly advisory until
                # Runtime can avoid or resolve the remaining relationship.
                lineage_status = "partial"
                if not unresolved_by_id:
                    affected_fact_id = next(
                        iter(required_fact_ids or bound_fact_ids),
                        "",
                    )
                    if affected_fact_id:
                        unresolved_by_id[affected_fact_id] = (
                            UnresolvedBusinessFactContract(
                                fact_id=affected_fact_id,
                                reason="回答该问题所需的数据源关系仍有部分未确认",
                                business_impact=(
                                    "相关来源暂不能全部可靠合并，综合判断需要保留范围说明"
                                ),
                            )
                        )
            normalized_lineage.append(lineage.model_copy(update={
                "fact_bindings": bindings,
                "unresolved_business_facts": list(unresolved_by_id.values()),
                "lineage_status": lineage_status,
            }))
        return candidate.model_copy(update={"insight_lineage": normalized_lineage})

    @staticmethod
    def _normalize_fact_binding_fields(
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
    ) -> BusinessContextContract:
        """Retain grounded lineage when only part of a binding is invalid.

        Business Context owns the fact-to-schema lineage.  A hallucinated field
        must not make a separately grounded field or relationship disappear;
        instead, keep valid bindings and explicitly mark facts with no remaining
        physical or derived anchor as unresolved.
        """
        fields_by_id = {
            int(item.get("datasource_id") or item.get("id") or 0): set(
                (item.get("field_capabilities") or {}).get("queryable_fields")
                or _schema_fields(item)
            )
            for item in schemas
            if isinstance(item, dict)
        }
        normalized_lineage: list[InsightLineageContract] = []
        for lineage in candidate.insight_lineage:
            bindings: list[BusinessFactBindingContract] = []
            affected_fact_ids: set[str] = set()
            for binding in lineage.fact_bindings:
                if not binding.fields:
                    bindings.append(binding)
                    continue
                available = fields_by_id.get(binding.datasource_id, set())
                valid_fields = [
                    field for field in binding.fields if field in available
                ]
                if len(valid_fields) != len(binding.fields):
                    affected_fact_ids.add(binding.fact_id)
                if valid_fields:
                    bindings.append(binding.model_copy(update={"fields": valid_fields}))

            bound_fact_ids = {binding.fact_id for binding in bindings}
            unresolved_by_id = {
                item.fact_id: item for item in lineage.unresolved_business_facts
            }
            for fact_id in affected_fact_ids - bound_fact_ids:
                unresolved_by_id.setdefault(
                    fact_id,
                    UnresolvedBusinessFactContract(
                        fact_id=fact_id,
                        reason="已选 Schema 中没有可承载该事实的有效字段",
                        business_impact="该事实当前缺少可核验的数据落点",
                    ),
                )
            unresolved = list(unresolved_by_id.values())
            status = lineage.lineage_status
            if unresolved and status == "direct":
                status = "partial"
            normalized_lineage.append(lineage.model_copy(update={
                "lineage_status": status,
                "fact_bindings": bindings,
                "unresolved_business_facts": unresolved,
            }))
        return candidate.model_copy(update={"insight_lineage": normalized_lineage})

    @staticmethod
    def _scope_lineage_relationships(
        candidate: BusinessContextContract,
    ) -> BusinessContextContract:
        """Keep only relationships that participate in an actual question lineage."""
        relationships_by_id = {
            item.relationship_id: item for item in candidate.relationships
        }
        normalized_lineage: list[InsightLineageContract] = []
        for lineage in candidate.insight_lineage:
            datasource_ids = set(lineage.datasource_ids)
            relationship_ids = [
                relationship_id
                for relationship_id in lineage.relationship_ids
                for relationship in [relationships_by_id.get(relationship_id)]
                if relationship is not None
                and {
                    relationship.left_datasource_id,
                    relationship.right_datasource_id,
                }.issubset(datasource_ids)
            ]
            normalized_lineage.append(lineage.model_copy(update={
                "relationship_ids": list(dict.fromkeys(relationship_ids)),
            }))
        referenced_ids = {
            relationship_id
            for lineage in normalized_lineage
            for relationship_id in lineage.relationship_ids
        }
        return candidate.model_copy(update={
            "relationships": [
                item for item in candidate.relationships
                if item.relationship_id in referenced_ids
            ],
            "insight_lineage": normalized_lineage,
        })

    @staticmethod
    def _remove_intrasource_relationships(
        candidate: BusinessContextContract,
    ) -> BusinessContextContract:
        """ER lineage contains edges between sources, not fields inside one source."""
        removed_ids = {
            item.relationship_id
            for item in candidate.relationships
            if item.left_datasource_id == item.right_datasource_id
        }
        if not removed_ids:
            return candidate
        return candidate.model_copy(update={
            "relationships": [
                item for item in candidate.relationships
                if item.relationship_id not in removed_ids
            ],
            "insight_lineage": [
                lineage.model_copy(update={
                    "relationship_ids": [
                        item for item in lineage.relationship_ids
                        if item not in removed_ids
                    ],
                })
                for lineage in candidate.insight_lineage
            ],
        })

    @staticmethod
    def _fallback_business_context(
        selection: DatasourceSelectionContract,
    ) -> BusinessContextContract:
        return BusinessContextContract(
            unresolved_relationships=(
                ["Business Context 的有界 Probe 未能确认跨源关系"]
                if len(selection.selected_datasource_ids) > 1
                else []
            ),
        )

    def _recover_business_context_candidate(
        self,
        *,
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
        selection: DatasourceSelectionContract,
        intent: IntentContract,
        probe_observations: list[dict[str, Any]],
    ) -> BusinessContextContract | None:
        """Preserve valid advisory lineage when one candidate relation is invalid."""
        try:
            recovered = self._normalize_unverified_schema_relationships(
                candidate,
                schemas,
                probe_observations=probe_observations,
            )
            recovered = self._normalize_fact_binding_fields(recovered, schemas)
            recovered = self._normalize_probe_backed_relationships(
                recovered,
                probe_observations,
            )
            recovered = self._complete_derived_fact_lineage(recovered, selection)
            recovered = self._remove_intrasource_relationships(recovered)
            recovered = self._scope_lineage_relationships(recovered)
            if recovered.probe_requests:
                recovered = recovered.model_copy(update={
                    "probe_requests": [],
                    "unresolved_relationships": list(dict.fromkeys([
                        *recovered.unresolved_relationships,
                        *(
                            f"Schema fact Probe request {item.request_id} remains unresolved"
                            for item in recovered.probe_requests
                        ),
                    ])),
                })
            validate_business_context(
                recovered,
                schemas,
                selection=selection,
                intent=intent,
                probe_observations=probe_observations,
            )
            return recovered
        except Exception as exc:
            logger.warning(
                "business context candidate recovery unavailable: %s",
                exc,
            )
            return None

    def _probe_relationship_candidates(
        self,
        *,
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
        selection: DatasourceSelectionContract,
    ) -> tuple[BusinessContextContract, list[dict[str, Any]]]:
        """Verify unresolved ER edges with the bounded Preparation Probe API."""
        from agent.tools import execute_preparation_relationship_probe

        referenced_relationship_ids = {
            relationship_id
            for lineage in candidate.insight_lineage
            for relationship_id in lineage.relationship_ids
        }
        probe_candidates = [
            relationship
            for relationship in candidate.relationships
            if relationship.relationship_id in referenced_relationship_ids
            and relationship.joinability in {"unknown", "alignable"}
            and relationship.relationship_type in {
                "same_entity",
                "parent_child",
                "event_of_entity",
                "dimension_enrichment",
                "bridge",
            }
            and relationship.left_fields
            and len(relationship.left_fields) == len(relationship.right_fields)
        ]
        if not probe_candidates:
            return candidate, []

        try:
            typed_observations = execute_preparation_relationship_probe(
                relationships=[item.model_dump() for item in probe_candidates],
                datasources=schemas,
                runtime_user_context={
                    **self.execution_context,
                    "allowed_datasource_ids": selection.selected_datasource_ids,
                },
                timeout_seconds=max(
                    int(Config.PYTHON_EXEC_TIMEOUT_SECONDS or 90),
                    1,
                ),
                cancelled=self.cancelled,
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "business context relationship Probe unavailable; preserving "
                "unresolved candidates: %s",
                exc,
            )
            unresolved = list(candidate.unresolved_relationships)
            unresolved.append(f"关系 Probe 未完成：{exc}")
            return candidate.model_copy(update={
                "unresolved_relationships": list(dict.fromkeys(unresolved)),
            }), []
        observations_by_id = {
            str(item.get("observation_id") or ""): item
            for item in typed_observations
        }
        probe_candidate_ids = {
            item.relationship_id for item in probe_candidates
        }
        resolved_relationships: list[BusinessRelationshipContract] = []
        unresolved: list[str] = []
        for relationship in candidate.relationships:
            if relationship.relationship_id not in probe_candidate_ids:
                resolved_relationships.append(relationship)
                if relationship.joinability == "unknown":
                    unresolved.append(
                        f"关系 {relationship.relationship_id} 仍缺少可验证的关系键"
                    )
                continue
            observation_id = f"probe:{relationship.relationship_id}"
            observation = observations_by_id.get(observation_id)
            if (
                observation is None
                or observation.get("status") != "verified"
                or not bool(observation.get("complete"))
            ):
                resolved_relationships.append(relationship.model_copy(update={
                    "joinability": "unknown",
                    "evidence_basis": "unresolved",
                    "observation_id": "",
                }))
                unresolved.append(
                    f"关系 {relationship.relationship_id} 未完成全量关系键核验"
                )
                continue
            facts = observation.get("facts") or {}
            cardinality = str(facts.get("cardinality") or "unknown")
            join_supported = bool(facts.get("join_supported"))
            resolved_relationships.append(relationship.model_copy(update={
                "joinability": (
                    "joinable" if join_supported and cardinality != "unknown"
                    else "alignable" if join_supported
                    else "not_joinable"
                ),
                "evidence_basis": "probe",
                "observation_id": observation_id,
                "probe_facts": dict(facts),
            }))
        return (
            candidate.model_copy(update={
                "relationships": resolved_relationships,
                "unresolved_relationships": list(dict.fromkeys(unresolved)),
            }),
            typed_observations,
        )

    def _probe_schema_requests(
        self,
        *,
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
        selection: DatasourceSelectionContract,
    ) -> list[dict[str, Any]]:
        """Resolve only the bounded Schema facts explicitly requested by Business Context."""
        if not candidate.probe_requests:
            return []
        from agent.tools import execute_preparation_schema_probe

        try:
            observations = execute_preparation_schema_probe(
                requests=[item.model_dump() for item in candidate.probe_requests],
                datasources=schemas,
                runtime_user_context={
                    **self.execution_context,
                    "allowed_datasource_ids": selection.selected_datasource_ids,
                },
                timeout_seconds=max(
                    int(Config.PYTHON_EXEC_TIMEOUT_SECONDS or 90),
                    1,
                ),
                cancelled=self.cancelled,
            )
            requests_by_observation_id = {
                f"probe:{item.request_id}": item
                for item in candidate.probe_requests
            }
            normalized: list[dict[str, Any]] = []
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                item = dict(observation)
                request = requests_by_observation_id.get(
                    str(item.get("observation_id") or "")
                )
                if request is not None:
                    item.setdefault("datasource_id", request.datasource_id)
                    item.setdefault("field", request.field)
                    item.setdefault("purpose", request.purpose)
                    item["related_fact_ids"] = list(request.related_fact_ids)
                normalized.append(item)
            return normalized
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning("business context Schema Probe unavailable: %s", exc)
            return [
                {
                    "observation_id": f"probe:{item.request_id}",
                    "kind": item.probe_kind,
                    "status": "unavailable",
                    "complete": False,
                    "datasource_id": item.datasource_id,
                    "field": item.field,
                    "purpose": item.purpose,
                    "related_fact_ids": item.related_fact_ids,
                    "error": str(exc),
                }
                for item in candidate.probe_requests
            ]

    @staticmethod
    def _has_consumable_probe_observations(
        observations: list[dict[str, Any]],
    ) -> bool:
        """Return whether Probe produced facts that require semantic finalization.

        Relationship coverage is already projected onto the canonical graph by
        ``_probe_relationship_candidates``. Re-sending relation-only facts to the
        LLM duplicates the most expensive Business Context step and can make a
        verified graph probabilistic again. Only field-value/profile observations
        require another semantic decision.
        """
        return any(
            isinstance(observation, dict)
            and observation.get("status") == "verified"
            and bool(observation.get("complete"))
            and observation.get("kind") in {"distinct_values", "field_profile"}
            for observation in observations
        )

    def _probe_resolution_payload(
        self,
        *,
        candidate: BusinessContextContract,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the smallest semantic context needed after Schema Probe."""
        schema_observations = [
            item
            for item in observations
            if isinstance(item, dict)
            and item.get("kind") in {"distinct_values", "field_profile"}
            and item.get("status") == "verified"
            and bool(item.get("complete"))
        ]
        affected_fact_ids = {
            str(fact_id).strip()
            for observation in schema_observations
            for fact_id in observation.get("related_fact_ids") or []
            if str(fact_id).strip()
        }
        affected_facts: list[dict[str, Any]] = []
        for lineage in candidate.insight_lineage:
            bindings_by_fact: dict[str, list[dict[str, Any]]] = {}
            for binding in lineage.fact_bindings:
                if binding.fact_id in affected_fact_ids:
                    bindings_by_fact.setdefault(binding.fact_id, []).append(
                        binding.model_dump()
                    )
            unresolved_by_fact = {
                item.fact_id: item.model_dump()
                for item in lineage.unresolved_business_facts
                if item.fact_id in affected_fact_ids
            }
            for fact_id in sorted(affected_fact_ids):
                if fact_id not in bindings_by_fact and fact_id not in unresolved_by_fact:
                    continue
                affected_facts.append({
                    "question_id": lineage.question_id,
                    "business_question": lineage.business_question,
                    "fact_id": fact_id,
                    "fact_bindings": bindings_by_fact.get(fact_id, []),
                    "unresolved_business_fact": unresolved_by_fact.get(fact_id),
                })
        return {
            "user_query": self.user_query,
            "affected_facts": affected_facts,
            "probe_observations": schema_observations,
        }

    @staticmethod
    def _apply_probe_fact_resolutions(
        candidate: BusinessContextContract,
        resolution: BusinessContextProbeResolutionContract,
        observations: list[dict[str, Any]],
    ) -> BusinessContextContract:
        """Merge bounded semantic decisions without regenerating the ER graph."""
        by_fact = {
            item.fact_id: item
            for item in resolution.fact_resolutions
        }
        if not by_fact:
            return candidate
        observations_by_id = {
            str(item.get("observation_id") or ""): item
            for item in observations
            if isinstance(item, dict)
            and str(item.get("observation_id") or "").strip()
        }
        observations_by_fact: dict[str, list[dict[str, Any]]] = {}
        for observation in observations_by_id.values():
            for fact_id in observation.get("related_fact_ids") or []:
                observations_by_fact.setdefault(str(fact_id), []).append(observation)
        lineages: list[InsightLineageContract] = []
        for lineage in candidate.insight_lineage:
            relevant = {
                fact_id: item
                for fact_id, item in by_fact.items()
                if any(
                    binding.fact_id == fact_id
                    for binding in lineage.fact_bindings
                )
                or any(
                    unresolved.fact_id == fact_id
                    for unresolved in lineage.unresolved_business_facts
                )
            }
            if not relevant:
                lineages.append(lineage)
                continue
            bindings = [
                binding
                for binding in lineage.fact_bindings
                if binding.fact_id not in relevant
            ]
            unresolved = [
                item
                for item in lineage.unresolved_business_facts
                if item.fact_id not in relevant
            ]
            for fact_id, item in relevant.items():
                if item.resolution_status == "unchanged":
                    bindings.extend(
                        binding
                        for binding in lineage.fact_bindings
                        if binding.fact_id == fact_id
                    )
                    unresolved.extend(
                        fact
                        for fact in lineage.unresolved_business_facts
                        if fact.fact_id == fact_id
                    )
                    continue
                selected_observations = [
                    observations_by_id[observation_id]
                    for observation_id in item.observation_ids
                    if observation_id in observations_by_id
                ] or observations_by_fact.get(fact_id, [])
                observed_fields = {
                    (
                        int(observation.get("datasource_id") or 0),
                        str(observation.get("field") or "").strip(),
                    )
                    for observation in selected_observations
                    if int(observation.get("datasource_id") or 0) > 0
                    and str(observation.get("field") or "").strip()
                }
                existing_bindings = [
                    binding
                    for binding in lineage.fact_bindings
                    if binding.fact_id == fact_id
                ]
                if item.resolution_status == "supported":
                    bindings.extend(existing_bindings)
                    existing_fields = {
                        (binding.datasource_id, field)
                        for binding in existing_bindings
                        for field in binding.fields
                    }
                    for datasource_id, field in sorted(observed_fields - existing_fields):
                        bindings.append(BusinessFactBindingContract(
                            fact_id=fact_id,
                            datasource_id=datasource_id,
                            fields=[field],
                        ))
                    continue

                for binding in existing_bindings:
                    remaining_fields = [
                        field for field in binding.fields
                        if (binding.datasource_id, field) not in observed_fields
                    ]
                    if remaining_fields or binding.derived_from_fact_ids:
                        bindings.append(binding.model_copy(update={
                            "fields": remaining_fields,
                        }))
                previous = next((
                    fact for fact in lineage.unresolved_business_facts
                    if fact.fact_id == fact_id
                ), None)
                reason = item.reason.strip() or (
                    previous.reason if previous is not None
                    else "Schema Probe 未能确认该字段可承载所需业务事实"
                )
                business_impact = item.business_impact.strip() or (
                    previous.business_impact if previous is not None else ""
                )
                unresolved.append(UnresolvedBusinessFactContract(
                    fact_id=fact_id,
                    reason=reason,
                    business_impact=business_impact,
                    unsupported_field_bindings=[
                        UnsupportedBusinessFieldBindingContract(
                            datasource_id=datasource_id,
                            fields=[field],
                            business_reason=reason,
                        )
                        for datasource_id, field in sorted(observed_fields)
                    ],
                ))
            lineages.append(lineage.model_copy(update={
                "fact_bindings": bindings,
                "unresolved_business_facts": unresolved,
                "lineage_status": "partial" if unresolved else "direct",
            }))
        return candidate.model_copy(update={"insight_lineage": lineages})

    @staticmethod
    def _remove_relationship_resolved_schema_probes(
        candidate: BusinessContextContract,
    ) -> BusinessContextContract:
        """Do not re-probe a key whose semantic path is already verified.

        A distinct-values observation on an identifier naturally returns IDs;
        it cannot overturn a separately verified relation to a label source.
        """
        resolved_key_fields = {
            (datasource_id, field)
            for relationship in candidate.relationships
            if relationship.joinability == "joinable"
            and relationship.evidence_basis in {"schema", "probe"}
            for datasource_id, fields in (
                (relationship.left_datasource_id, relationship.left_fields),
                (relationship.right_datasource_id, relationship.right_fields),
            )
            for field in fields
        }
        if not resolved_key_fields:
            return candidate
        return candidate.model_copy(update={
            "probe_requests": [
                request
                for request in candidate.probe_requests
                if (request.datasource_id, request.field) not in resolved_key_fields
            ],
        })

    @staticmethod
    def _normalize_probe_backed_relationships(
        candidate: BusinessContextContract,
        probe_observations: list[dict[str, Any]],
    ) -> BusinessContextContract:
        """Project verified technical Probe facts onto ER joinability."""
        observations_by_id = {
            str(item.get("observation_id") or ""): item
            for item in probe_observations
            if isinstance(item, dict)
        }
        normalized: list[BusinessRelationshipContract] = []
        for relationship in candidate.relationships:
            expected_observation_id = f"probe:{relationship.relationship_id}"
            observation = observations_by_id.get(expected_observation_id)
            if (
                observation is None
                or observation.get("status") != "verified"
                or not bool(observation.get("complete"))
            ):
                normalized.append(relationship)
                continue
            facts = observation.get("facts") or {}
            cardinality = str(facts.get("cardinality") or "unknown")
            join_supported = bool(facts.get("join_supported"))
            normalized.append(relationship.model_copy(update={
                "joinability": (
                    "joinable" if join_supported and cardinality != "unknown"
                    else "alignable" if join_supported
                    else "not_joinable"
                ),
                "probe_facts": dict(facts),
                "evidence_basis": "probe",
                "observation_id": str(observation.get("observation_id") or ""),
            }))
        return candidate.model_copy(update={"relationships": normalized})

    @staticmethod
    def _reconcile_finalized_relationships(
        candidate: BusinessContextContract,
        draft: BusinessContextContract,
    ) -> BusinessContextContract:
        """Keep relation identity and evidence binding owned by the pre-Probe draft."""
        draft_by_id = {
            item.relationship_id: item for item in draft.relationships
        }
        finalized_by_id = {
            item.relationship_id: item for item in candidate.relationships
        }
        referenced_ids = {
            relationship_id
            for lineage in candidate.insight_lineage
            for relationship_id in lineage.relationship_ids
            if relationship_id in draft_by_id
        }
        relationships = []
        for draft_relationship in draft.relationships:
            relationship_id = draft_relationship.relationship_id
            if relationship_id not in referenced_ids:
                continue
            finalized_relationship = finalized_by_id.get(relationship_id)
            relationships.append(draft_relationship.model_copy(update={
                "business_meaning": (
                    finalized_relationship.business_meaning
                    if finalized_relationship is not None
                    else draft_relationship.business_meaning
                ),
            }))
        lineage = [
            item.model_copy(update={
                "relationship_ids": [
                    relationship_id
                    for relationship_id in item.relationship_ids
                    if relationship_id in referenced_ids
                ],
            })
            for item in candidate.insight_lineage
        ]
        return candidate.model_copy(update={
            "relationships": relationships,
            "insight_lineage": lineage,
        })

    @staticmethod
    def _normalize_unverified_schema_relationships(
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
        *,
        probe_observations: list[dict[str, Any]] | None = None,
    ) -> BusinessContextContract:
        """Keep semantic ER candidates while reserving evidence for verification."""
        declared = _declared_schema_relationships(schemas)
        verified_probe_observation_ids = {
            str(item.get("observation_id") or "")
            for item in probe_observations or []
            if isinstance(item, dict)
            and item.get("kind") == "relationship_coverage"
            and item.get("status") == "verified"
            and bool(item.get("complete"))
        }
        fields_by_id = {
            int(item.get("datasource_id") or item.get("id") or 0): set(
                (item.get("field_capabilities") or {}).get("queryable_fields")
                or _schema_fields(item)
            )
            for item in schemas
            if isinstance(item, dict)
        }
        normalized: list[BusinessRelationshipContract] = []
        removed_ids: set[str] = set()
        unresolved = list(candidate.unresolved_relationships)
        for relationship in candidate.relationships:
            if relationship.relationship_type in {"comparable", "independent"}:
                normalized.append(relationship)
                continue
            if relationship.relationship_type == "unknown":
                normalized.append(relationship.model_copy(update={
                    "joinability": "unknown",
                    "evidence_basis": "unresolved",
                    "observation_id": "",
                }))
                continue
            valid_field_pairs = [
                (left_field, right_field)
                for left_field, right_field in zip(
                    relationship.left_fields,
                    relationship.right_fields,
                )
                if left_field in fields_by_id.get(relationship.left_datasource_id, set())
                and right_field in fields_by_id.get(relationship.right_datasource_id, set())
            ]
            original_pair_count = max(
                len(relationship.left_fields),
                len(relationship.right_fields),
            )
            if not valid_field_pairs:
                removed_ids.add(relationship.relationship_id)
                unresolved.append(
                    f"关系 {relationship.relationship_id} 的候选键未同时存在于两侧 Schema"
                )
                continue
            if len(valid_field_pairs) != original_pair_count:
                relationship = relationship.model_copy(update={
                    "left_fields": [item[0] for item in valid_field_pairs],
                    "right_fields": [item[1] for item in valid_field_pairs],
                    "joinability": "unknown",
                    "evidence_basis": "unresolved",
                    "observation_id": "",
                })
                unresolved.append(
                    f"关系 {relationship.relationship_id} 已忽略两侧 Schema 不完整的候选键"
                )
            schema_declared = _schema_declares_relationship(
                declared,
                left_datasource_id=relationship.left_datasource_id,
                right_datasource_id=relationship.right_datasource_id,
                left_fields=relationship.left_fields,
                right_fields=relationship.right_fields,
            )
            if schema_declared:
                normalized.append(relationship.model_copy(update={
                    "joinability": "joinable",
                    "evidence_basis": "schema",
                    "observation_id": "",
                    "probe_facts": {},
                }))
                continue
            verified_probe_claim = bool(
                relationship.evidence_basis == "probe"
                and relationship.observation_id in verified_probe_observation_ids
                and relationship.probe_facts
            )
            if not verified_probe_claim and (
                relationship.joinability in {"joinable", "alignable"}
                or relationship.evidence_basis in {"schema", "probe"}
            ):
                normalized.append(relationship.model_copy(update={
                    "joinability": "unknown",
                    "evidence_basis": "unresolved",
                    "observation_id": "",
                    "probe_facts": {},
                }))
            else:
                normalized.append(relationship)
        normalized_lineage = [
            lineage.model_copy(update={
                "relationship_ids": [
                    relationship_id
                    for relationship_id in lineage.relationship_ids
                    if relationship_id not in removed_ids
                ],
            })
            for lineage in candidate.insight_lineage
        ]
        return candidate.model_copy(update={
            "relationships": normalized,
            "insight_lineage": normalized_lineage,
            "unresolved_relationships": list(dict.fromkeys(unresolved)),
        })

    def intent_analysis(
        self,
        _: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        self._start_stage("intent_analysis")
        try:
            intent = _invoke_contract(
                stage="intent_analysis",
                payload={
                    "user_query": self.user_query,
                    "context_mode": self.context_mode,
                    "analysis_as_of_date": datetime.now(
                        ZoneInfo("Asia/Shanghai")
                    ).date().isoformat(),
                    "expected_time_range": self.expected_time_range,
                    "conversation_context": self.conversation_context,
                },
                output_model=IntentContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
            intent = _canonicalize_intent_selection_facts(intent)
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "intent unresolved; carrying raw user goal to formal analysis: %s",
                exc,
            )
            intent = self._fallback_intent()
        logger.info(
            "analysis intent resolved goal=%s questions=%s",
            intent.business_goal,
            [
                {
                    "question_id": item.question_id,
                    "business_question": item.business_question,
                    "required_business_facts": item.required_business_facts,
                }
                for item in intent.business_questions
            ],
        )
        return {"intent": intent}

    def semantic_selection(
        self,
        state: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        """Use semantic retrieval as a provider for canonical datasource selection."""
        self._start_stage("semantic_selection")
        intent = state["intent"]
        try:
            semantic_query = "\n".join(filter(None, [
                intent.business_goal,
                *intent.business_subjects,
                *(item.business_question for item in intent.business_questions),
                *(
                    fact.business_fact
                    for item in intent.business_questions
                    for fact in item.required_business_facts
                ),
            ]))
            semantic_result = SemanticDatasourceSelectionService().retrieve(
                query=semantic_query,
                authorization=str(self.execution_context.get("token") or ""),
                namespace_id=int(self.execution_context.get("namespace_id") or 0),
                database_conn_info=dict(
                    self.execution_context.get("database_conn_info") or {}
                ),
            )
            semantic_snapshot = _normalize_time_series_datasource_snapshot(
                semantic_result.datasource_snapshot
            )
            catalog = build_datasource_catalog(semantic_snapshot)
            if not catalog:
                raise SemanticDatasourceSelectionError(
                    "semantic selection returned no usable datasource candidates"
                )
            relationship_hints = semantic_result.relationship_hints
            discovery_catalog = build_datasource_discovery_catalog(
                catalog,
                search_text=semantic_query,
            )
            selection = _invoke_contract(
                stage="semantic_selection",
                payload={
                    "intent_context": intent.model_dump(),
                    "semantic_candidate_catalog": discovery_catalog,
                    "relationship_hints": list(relationship_hints),
                },
                output_model=DatasourceSelectionContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
            selection = self._normalize_selection(selection, catalog, intent)
            semantic_reference_ids = _semantic_reference_datasource_ids(
                selection
            )
            physical_datasource_ids = [
                datasource_id
                for datasource_id in selection.selected_datasource_ids
                if datasource_id not in semantic_reference_ids
            ]
            selected_snapshot = filter_datasource_snapshot(
                semantic_snapshot,
                selection.selected_datasource_ids,
            )
            selected_snapshot = enrich_snapshot_with_semantic_relationships(
                selected_snapshot,
                filter_relationship_hints(
                    relationship_hints,
                    physical_datasource_ids,
                ),
            )
            selected_schemas = (
                compact_datasource_snapshot(selected_snapshot)
                .get("selected_datasource_snapshot")
                or []
            )
            logger.info(
                "semantic selection canonicalized candidate_count=%s selected_ids=%s relationships=%s",
                len(catalog),
                selection.selected_datasource_ids,
                len(relationship_hints),
            )
            return {
                "selection": selection,
                "selected_snapshot": selected_snapshot,
                "selected_schemas": [
                    item
                    for item in selected_schemas
                    if int(item.get("datasource_id") or item.get("id") or 0)
                    not in semantic_reference_ids
                ],
                "query_capabilities": infer_query_capabilities(selected_snapshot),
            }
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "semantic datasource selection unavailable; using local selection "
                "with the existing intent: %s",
                exc,
            )
            if not state.get("catalog"):
                raise
            return self.select_datasources(state)


    def select_datasources(
        self,
        state: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        self._start_stage("select_datasources")
        catalog = state["catalog"]
        intent = state["intent"]
        if len(catalog) == 1:
            datasource_id = int(catalog[0]["datasource_id"])
            selection = DatasourceSelectionContract(
                selected_datasource_ids=[datasource_id],
                question_coverage=[
                    BusinessQuestionCoverageContract(
                        question_id=question.question_id,
                        datasource_ids=[datasource_id],
                        fact_coverage=[
                            BusinessFactCoverageContract(
                                fact_id=fact.fact_id,
                                coverage_status="direct",
                                datasource_ids=[datasource_id],
                                coverage_reason="当前唯一数据源提供可核验的事实入口",
                            )
                            for fact in question.required_business_facts
                        ],
                        coverage_reason="当前唯一数据源承载该业务问题的可用事实",
                    )
                    for question in intent.business_questions
                ],
                selection_reason="当前会话只有一个可用数据源",
            )
            selected_snapshot = filter_datasource_snapshot(
                self.datasource_snapshot,
                selection.selected_datasource_ids,
            )
            return {
                "selection": selection,
                "selected_snapshot": selected_snapshot,
                "selected_schemas": (
                    compact_datasource_snapshot(selected_snapshot)
                    .get("selected_datasource_snapshot")
                    or []
                ),
                "query_capabilities": infer_query_capabilities(selected_snapshot),
            }
        if _requires_datasource_shortlist(catalog):
            try:
                discovery_catalog = build_datasource_discovery_catalog(
                    catalog,
                    search_text=" ".join([
                        self.user_query,
                        intent.business_goal,
                        *intent.business_subjects,
                        *(
                            question.business_question
                            for question in intent.business_questions
                        ),
                        *(
                            fact.business_fact
                            for question in intent.business_questions
                            for fact in question.required_business_facts
                        ),
                    ]),
                )
                logger.info(
                    "datasource discovery started catalog_count=%s discovery_chars=%s",
                    len(catalog),
                    len(json.dumps(
                        discovery_catalog,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )),
                )
                shortlist = _invoke_contract(
                    stage="datasource_shortlist",
                    payload={
                        "user_query": self.user_query,
                        "intent_context": intent.model_dump(),
                        "datasource_discovery_catalog": discovery_catalog,
                    },
                    output_model=DatasourceShortlistContract,
                    runtime_config=self.runtime_config,
                    cancelled=self.cancelled,
                )
                by_id = {item["datasource_id"]: item for item in catalog}
                recalled_ids = recall_datasources_for_business_facts(
                    catalog,
                    intent,
                )
                shortlist_ids = list(dict.fromkeys([
                    *shortlist.shortlisted_datasource_ids,
                    *recalled_ids,
                ]))
                shortlisted = [
                    by_id[datasource_id]
                    for datasource_id in shortlist_ids
                    if datasource_id in by_id
                ]
                if shortlisted:
                    logger.info(
                        "datasource discovery resolved shortlisted_count=%s shortlisted_ids=%s full_schema_chars=%s reason=%s",
                        len(shortlisted),
                        [item["datasource_id"] for item in shortlisted],
                        len(json.dumps(
                            shortlisted,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        )),
                        shortlist.coverage_reason,
                    )
                    catalog = shortlisted
            except AnalysisTaskCancelled:
                raise
            except Exception as exc:
                logger.warning(
                    "shortlist unresolved; preserving the full candidate catalog: %s",
                    exc,
                )
        try:
            selection = _invoke_contract(
                stage="select_datasources",
                payload={
                    "user_query": self.user_query,
                    "intent_context": intent.model_dump(),
                    "datasource_catalog": catalog,
                },
                output_model=DatasourceSelectionContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
            selection = self._normalize_selection(selection, catalog, intent)
            logger.info(
                "datasource selection resolved selected_count=%s selected_ids=%s coverage=%s",
                len(selection.selected_datasource_ids),
                selection.selected_datasource_ids,
                [
                    item.model_dump()
                    for item in selection.question_coverage
                ],
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "selection unresolved; preserving candidate sources: %s",
                exc,
            )
            selection = self._fallback_selection(catalog, intent)
        selected_snapshot = filter_datasource_snapshot(
            self.datasource_snapshot,
            selection.selected_datasource_ids,
        )
        selected_schemas = (
            compact_datasource_snapshot(selected_snapshot)
            .get("selected_datasource_snapshot")
            or []
        )
        semantic_reference_ids = _semantic_reference_datasource_ids(selection)
        return {
            "selection": selection,
            "selected_snapshot": selected_snapshot,
            "selected_schemas": [
                item
                for item in selected_schemas
                if int(item.get("datasource_id") or item.get("id") or 0)
                not in semantic_reference_ids
            ],
            "query_capabilities": infer_query_capabilities(selected_snapshot),
        }

    def business_context(
        self,
        state: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        self._start_stage("business_context")
        selection = state["selection"]
        payload = {
            "user_query": self.user_query,
            # Business Context resolves fact placement and ER lineage. Open-ended
            # analytical facets belong to Runtime and must not be reinterpreted as
            # required fields or enum contracts here.
            "intent_context": state["intent"].model_dump(
                exclude={"analytical_facets"}
            ),
            "datasource_selection": selection.model_dump(),
            "selected_datasources": state["selected_schemas"],
            "query_guidance": build_query_guidance(
                state.get("query_capabilities") or ()
            ),
        }
        candidate: BusinessContextContract | None = None
        probe_observations: list[dict[str, Any]] = []
        try:
            draft = _invoke_contract(
                stage="business_context",
                payload=payload,
                output_model=BusinessContextDraftContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
            candidate = BusinessContextContract.model_validate(draft.model_dump())
            candidate = self._normalize_unverified_schema_relationships(
                candidate,
                state["selected_schemas"],
            )
            candidate = self._normalize_fact_binding_fields(
                candidate,
                state["selected_schemas"],
            )
            candidate = self._complete_derived_fact_lineage(
                candidate,
                selection,
            )
            candidate = self._remove_intrasource_relationships(candidate)
            candidate = self._scope_lineage_relationships(candidate)
            candidate, relationship_observations = self._probe_relationship_candidates(
                candidate=candidate,
                schemas=state["selected_schemas"],
                selection=selection,
            )
            candidate = self._remove_relationship_resolved_schema_probes(candidate)
            # Relationship Probe updates the canonical graph locally. Re-project
            # that verified graph onto question lineage before deciding whether a
            # second semantic finalization is necessary; otherwise the relation
            # can be joinable while its fact remains stale and unresolved.
            candidate = self._complete_derived_fact_lineage(
                candidate,
                selection,
            )
            schema_observations = self._probe_schema_requests(
                candidate=candidate,
                schemas=state["selected_schemas"],
                selection=selection,
            )
            current_observations = [
                *relationship_observations,
                *schema_observations,
            ]
            probe_observations.extend(current_observations)
            resolution_payload = self._probe_resolution_payload(
                candidate=candidate,
                observations=current_observations,
            )
            if (
                self._has_consumable_probe_observations(current_observations)
                and resolution_payload["affected_facts"]
            ):
                try:
                    resolution = _invoke_contract(
                        stage="business_context_probe_resolution",
                        payload=resolution_payload,
                        output_model=BusinessContextProbeResolutionContract,
                        runtime_config=self.runtime_config,
                        cancelled=self.cancelled,
                    )
                    candidate = self._apply_probe_fact_resolutions(
                        candidate,
                        resolution,
                        current_observations,
                    )
                except AnalysisTaskCancelled:
                    raise
                except Exception as exc:
                    logger.warning(
                        "business context Probe delta unavailable; preserving "
                        "the verified graph: %s",
                        exc,
                    )
                    candidate = candidate.model_copy(update={
                        "unresolved_relationships": list(dict.fromkeys([
                            *candidate.unresolved_relationships,
                            "Schema Probe 观察已保留，但受影响业务事实尚未完成语义定稿",
                        ])),
                    })
            candidate = self._normalize_unverified_schema_relationships(
                candidate,
                state["selected_schemas"],
                probe_observations=probe_observations,
            )
            candidate = self._normalize_fact_binding_fields(
                candidate,
                state["selected_schemas"],
            )
            candidate = self._normalize_probe_backed_relationships(
                candidate,
                relationship_observations,
            )
            candidate = self._complete_derived_fact_lineage(
                candidate,
                selection,
            )
            candidate = self._remove_intrasource_relationships(candidate)
            candidate = self._scope_lineage_relationships(candidate)
            if candidate.probe_requests:
                remaining = [
                    f"Schema fact Probe request {item.request_id} remains unresolved"
                    for item in candidate.probe_requests
                ]
                candidate = candidate.model_copy(update={
                    "probe_requests": [],
                    "unresolved_relationships": list(dict.fromkeys([
                        *candidate.unresolved_relationships,
                        *remaining,
                    ])),
                })
            validate_business_context(
                candidate,
                state["selected_schemas"],
                selection=selection,
                intent=state["intent"],
                probe_observations=probe_observations,
            )
            contract = candidate
            logger.info(
                "business context resolved relationships=%s lineage=%s unresolved=%s",
                [
                    {
                        "left_datasource_id": item.left_datasource_id,
                        "right_datasource_id": item.right_datasource_id,
                        "type": item.relationship_type,
                        "joinability": item.joinability,
                        "left_fields": item.left_fields,
                        "right_fields": item.right_fields,
                        "basis": item.evidence_basis,
                    }
                    for item in contract.relationships
                ],
                [
                    {
                        "question_id": item.question_id,
                        "status": item.lineage_status,
                        "datasource_ids": item.datasource_ids,
                        "business_facts": [
                            binding.fact_id
                            for binding in item.fact_bindings
                        ],
                        "unresolved_business_facts": (
                            item.unresolved_business_facts
                        ),
                    }
                    for item in contract.insight_lineage
                ],
                contract.unresolved_relationships,
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "business context unresolved; carrying relationship uncertainty: %s",
                exc,
            )
            recovered = (
                self._recover_business_context_candidate(
                    candidate=candidate,
                    schemas=state["selected_schemas"],
                    selection=selection,
                    intent=state["intent"],
                    probe_observations=probe_observations,
                )
                if candidate is not None
                else None
            )
            contract = recovered or self._fallback_business_context(selection)
        return {
            "business_context": contract,
            "probe_observations": tuple(probe_observations),
            "selected_snapshot": enrich_selected_datasource_schemas(
                state["selected_snapshot"],
                selection=selection,
                business_context=contract,
            ),
        }

    def compile(self, *, provider: str = "local"):
        graph = StateGraph(AnalysisPreparationState)
        graph.add_node("business_context", self.business_context)
        graph.add_node("intent_analysis", self.intent_analysis)
        if provider == "semantic_center":
            graph.add_node("semantic_selection", self.semantic_selection)
            graph.add_edge(START, "intent_analysis")
            graph.add_edge("intent_analysis", "semantic_selection")
            graph.add_edge("semantic_selection", "business_context")
        else:
            graph.add_node("select_datasources", self.select_datasources)
            graph.add_edge(START, "intent_analysis")
            graph.add_edge("intent_analysis", "select_datasources")
            graph.add_edge("select_datasources", "business_context")
        graph.add_edge("business_context", END)
        return graph.compile()

    def run(self) -> PreparationResult:
        provider = DatasourceSelectionProviderRouter.resolve(self.datasource_snapshot)
        try:
            state = self.compile(provider=provider).invoke({
                "catalog": build_datasource_catalog(self.datasource_snapshot),
            })
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            if provider != "semantic_center":
                raise
            logger.warning(
                "semantic Preparation unavailable; falling back to local Preparation: %s",
                exc,
            )
            if not build_datasource_catalog(self.datasource_snapshot):
                raise
            state = self.compile(provider="local").invoke({
                "catalog": build_datasource_catalog(self.datasource_snapshot),
            })
        return PreparationResult(
            intent=state["intent"],
            selection=state["selection"],
            business_context=state["business_context"],
            selected_snapshot=(
                state.get("selected_snapshot") or dict(self.datasource_snapshot)
            ),
            query_capabilities=state.get("query_capabilities") or (),
            probe_observations=state.get("probe_observations") or (),
            explicit_time_range=self.expected_time_range or None,
        )


def prepare_analysis(
    *,
    user_query: str,
    context_mode: str,
    expected_time_range: dict[str, Any],
    datasource_snapshot: dict[str, Any],
    conversation_context: dict[str, Any] | None = None,
    runtime_config: LlmRuntimeConfig | None = None,
    execution_context: dict[str, Any] | None = None,
    cancelled: Callable[[], None] | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PreparationResult:
    return AnalysisPreparationWorkflow(
        user_query=user_query,
        context_mode=context_mode,
        expected_time_range=expected_time_range,
        datasource_snapshot=datasource_snapshot,
        conversation_context=conversation_context,
        runtime_config=runtime_config,
        execution_context=execution_context,
        cancelled=cancelled,
        stage_callback=stage_callback,
    ).run()
