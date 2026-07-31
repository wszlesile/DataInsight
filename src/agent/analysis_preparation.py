import copy
import json
import re
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from config import Config
from utils import logger
from utils.analysis_task_cancel import AnalysisTaskCancelled
from utils.llm_model_factory import (
    LlmRuntimeConfig,
    create_data_insight_model,
    create_runtime_config,
)


PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / "preparation"
QUERY_PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / "query_capabilities"


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


class IntentContract(BaseModel):
    """Business intent only; formal analysis owns all analytical semantics."""

    model_config = ConfigDict(extra="forbid")

    business_goal: str = Field(min_length=1)
    business_subjects: list[str] = Field(default_factory=list)
    business_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_business_questions(self) -> "IntentContract":
        if not self.business_questions:
            self.business_questions = [self.business_goal]
        return self


class DatasourceShortlistContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shortlisted_datasource_ids: list[int] = Field(min_length=1)
    coverage_reason: str


class DatasourceSelectionContract(BaseModel):
    """Selected datasource set only; formal analysis owns role and grain."""

    model_config = ConfigDict(extra="forbid")

    selected_datasource_ids: list[int] = Field(min_length=1)
    selection_reason: str

    @model_validator(mode="after")
    def normalize_ids(self) -> "DatasourceSelectionContract":
        self.selected_datasource_ids = list(dict.fromkeys(
            int(item)
            for item in self.selected_datasource_ids
            if int(item or 0) > 0
        ))
        if not self.selected_datasource_ids:
            raise ValueError("selection requires at least one datasource")
        return self


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
    ]
    joinability: Literal["joinable", "alignable", "not_joinable", "unknown"]
    left_fields: list[str] = Field(default_factory=list)
    right_fields: list[str] = Field(default_factory=list)
    business_meaning: str
    evidence_basis: Literal["schema", "probe", "unresolved"] = "unresolved"
    observation_id: str = ""


class BusinessContextContract(BaseModel):
    """ER graph only; no source roles, grain, metrics, or report plan."""

    model_config = ConfigDict(extra="forbid")

    relationships: list[BusinessRelationshipContract] = Field(default_factory=list)
    unresolved_relationships: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_relationship_ids(self) -> "BusinessContextContract":
        relationship_ids = [item.relationship_id for item in self.relationships]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("relationship_id must be unique")
        return self


@dataclass(frozen=True)
class PreparationResult:
    intent: IntentContract
    selection: DatasourceSelectionContract
    business_context: BusinessContextContract
    selected_snapshot: dict[str, Any]
    query_capabilities: tuple[str, ...]
    probe_observations: tuple[dict[str, Any], ...] = ()

    def resume_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent.model_dump(),
            "selection": self.selection.model_dump(),
            "business_context": self.business_context.model_dump(),
            "selected_snapshot": self.selected_snapshot,
            "query_capabilities": list(self.query_capabilities),
            "probe_observations": list(self.probe_observations),
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
            )
        except (KeyError, TypeError, ValueError):
            return None

    def prompt_snapshot(self) -> dict[str, Any]:
        return compact_datasource_snapshot(self.selected_snapshot)


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
                    "description": description[:160],
                }))
        ranked_fields.sort(reverse=True, key=lambda item: (item[0], item[1]))
        matched_fields = [item[2] for item in ranked_fields[:8]]
        result.append({
            "datasource_id": item["datasource_id"],
            "datasource_name": item.get("datasource_name"),
            "datasource_type": item.get("datasource_type"),
            "description": str(item.get("description") or "")[:500],
            "field_count": len(item.get("fields") or []),
            "matched_fields": matched_fields,
        })
    return result


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
        for source_id, target_id, local_fields, target_fields in orientations:
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
        if len(relationship.left_fields) != len(relationship.right_fields):
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
            relationship.evidence_basis == "schema"
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
        self.datasource_snapshot = datasource_snapshot
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
            business_questions=[query],
        )

    @staticmethod
    def _fallback_selection(
        catalog: list[dict[str, Any]],
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
            selection_reason="选源判断未完全定稿，保留候选来源交由统一洞察阶段复核",
        )

    @staticmethod
    def _normalize_selection(
        selection: DatasourceSelectionContract,
        catalog: list[dict[str, Any]],
    ) -> DatasourceSelectionContract:
        available_ids = {
            int(item.get("datasource_id") or 0)
            for item in catalog
            if int(item.get("datasource_id") or 0) > 0
        }
        selected_ids = [
            datasource_id
            for datasource_id in selection.selected_datasource_ids
            if datasource_id in available_ids
        ]
        if not selected_ids:
            return AnalysisPreparationWorkflow._fallback_selection(catalog)
        return selection.model_copy(update={
            "selected_datasource_ids": selected_ids,
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

    @staticmethod
    def _compile_relationship_probe_program(
        relationships: list[BusinessRelationshipContract],
        schemas: list[dict[str, Any]],
    ) -> str:
        """Compile semantic relationship candidates into a bounded Probe program."""
        schemas_by_id = {
            int(item.get("datasource_id") or item.get("id") or 0): item
            for item in schemas
            if isinstance(item, dict)
        }
        lines = ['analysis = begin_analysis(execution_mode="probe")']
        observation_names: list[str] = []
        safe_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for index, relationship in enumerate(relationships):
            left_schema = schemas_by_id.get(relationship.left_datasource_id)
            right_schema = schemas_by_id.get(relationship.right_datasource_id)
            if left_schema is None or right_schema is None:
                raise ValueError(
                    f"relationship {relationship.relationship_id} references "
                    "an unavailable datasource"
                )
            if (
                not relationship.left_fields
                or len(relationship.left_fields) != len(relationship.right_fields)
                or any(
                    not safe_name.fullmatch(field)
                    for field in [
                        *relationship.left_fields,
                        *relationship.right_fields,
                    ]
                )
            ):
                raise ValueError(
                    f"relationship {relationship.relationship_id} has invalid Probe keys"
                )

            def add_query(
                *,
                variable: str,
                query_key: str,
                schema: dict[str, Any],
                fields: list[str],
            ) -> None:
                datasource_id = int(
                    schema.get("datasource_id") or schema.get("id") or 0
                )
                identifier = str(
                    schema.get("datasource_identifier") or ""
                ).strip()
                if not identifier:
                    raise ValueError(
                        f"datasource {datasource_id} lacks a query identifier"
                    )
                columns = ", ".join(fields)
                non_null = " AND ".join(
                    f"{field} IS NOT NULL" for field in fields
                )
                sql = (
                    f"SELECT {columns} FROM {identifier} "
                    f"WHERE {non_null}"
                )
                lines.extend([
                    f"{variable} = analysis.query.sql(",
                    f"    key={json.dumps(query_key)},",
                    f"    datasource_id={datasource_id},",
                    f"    sql={json.dumps(sql)},",
                    f"    description={json.dumps('验证数据源关系键')},",
                    "    sql_context={\"fragments\": [{",
                    f"        \"datasource_id\": {datasource_id},",
                    f"        \"datasource_identifier\": {json.dumps(identifier)},",
                    "    }]},",
                    ")",
                ])

            left_name = f"left_keys_{index}"
            right_name = f"right_keys_{index}"
            observation_name = f"relationship_{index}"
            add_query(
                variable=left_name,
                query_key=f"bc_left_{index}",
                schema=left_schema,
                fields=relationship.left_fields,
            )
            add_query(
                variable=right_name,
                query_key=f"bc_right_{index}",
                schema=right_schema,
                fields=relationship.right_fields,
            )
            lines.extend([
                f"{observation_name} = analysis.query.inspect_relationship(",
                f"    key={json.dumps(relationship.relationship_id)},",
                f"    left={left_name},",
                f"    right={right_name},",
                f"    left_on={relationship.left_fields!r},",
                f"    right_on={relationship.right_fields!r},",
                ")",
            ])
            observation_names.append(observation_name)
        if not observation_names:
            raise ValueError("no relationship candidate is available for Probe")
        lines.extend([
            "result = analysis.complete_probe(",
            f"    message={json.dumps('已核验候选数据源关系')},",
            f"    observations=[{', '.join(observation_names)}],",
            ")",
        ])
        return "\n".join(lines)

    def _probe_relationship_candidates(
        self,
        *,
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
        selection: DatasourceSelectionContract,
    ) -> tuple[BusinessContextContract, list[dict[str, Any]]]:
        """Verify candidate ER edges without asking the model to author Probe code."""
        from agent.tools import execute_preparation_probe_program

        probe_code = self._compile_relationship_probe_program(
            candidate.relationships,
            schemas,
        )
        probe_result = execute_preparation_probe_program(
            code=probe_code,
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
        typed_observations = [
            item
            for item in (
                probe_result.observations.get("typed_observations") or []
                if isinstance(probe_result.observations, dict)
                else []
            )
            if isinstance(item, dict)
        ]
        observations_by_id = {
            str(item.get("observation_id") or ""): item
            for item in typed_observations
        }
        resolved_relationships: list[BusinessRelationshipContract] = []
        unresolved = list(candidate.unresolved_relationships)
        for relationship in candidate.relationships:
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
            resolved_relationships.append(relationship.model_copy(update={
                "joinability": (
                    "joinable"
                    if bool(facts.get("join_supported"))
                    else "not_joinable"
                ),
                "evidence_basis": "probe",
                "observation_id": observation_id,
            }))
        return (
            candidate.model_copy(update={
                "relationships": resolved_relationships,
                "unresolved_relationships": list(dict.fromkeys(unresolved)),
            }),
            typed_observations,
        )

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
            if relationship.evidence_basis != "probe":
                normalized.append(relationship)
                continue
            observation = observations_by_id.get(relationship.observation_id)
            if (
                observation is None
                or observation.get("status") != "verified"
                or not bool(observation.get("complete"))
            ):
                normalized.append(relationship)
                continue
            facts = observation.get("facts") or {}
            normalized.append(relationship.model_copy(update={
                "joinability": (
                    "joinable"
                    if bool(facts.get("join_supported"))
                    else "not_joinable"
                ),
            }))
        return candidate.model_copy(update={"relationships": normalized})

    @staticmethod
    def _normalize_unverified_schema_relationships(
        candidate: BusinessContextContract,
        schemas: list[dict[str, Any]],
    ) -> BusinessContextContract:
        """Keep semantic ER candidates while reserving evidence for verification."""
        declared = _declared_schema_relationships(schemas)
        normalized: list[BusinessRelationshipContract] = []
        for relationship in candidate.relationships:
            if (
                relationship.evidence_basis == "schema"
                and relationship.joinability in {"joinable", "alignable"}
                and not _schema_declares_relationship(
                    declared,
                    left_datasource_id=relationship.left_datasource_id,
                    right_datasource_id=relationship.right_datasource_id,
                    left_fields=relationship.left_fields,
                    right_fields=relationship.right_fields,
                )
            ):
                normalized.append(relationship.model_copy(update={
                    "joinability": "unknown",
                    "evidence_basis": "unresolved",
                    "observation_id": "",
                }))
            else:
                normalized.append(relationship)
        return candidate.model_copy(update={"relationships": normalized})

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
                    "conversation_context": self.conversation_context,
                },
                output_model=IntentContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "intent unresolved; carrying raw user goal to formal analysis: %s",
                exc,
            )
            intent = self._fallback_intent()
        return {"intent": intent}

    def select_datasources(
        self,
        state: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        self._start_stage("select_datasources")
        catalog = state["catalog"]
        intent = state["intent"]
        if len(catalog) == 1:
            selection = DatasourceSelectionContract(
                selected_datasource_ids=[catalog[0]["datasource_id"]],
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
                        *intent.business_questions,
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
                shortlisted = [
                    by_id[datasource_id]
                    for datasource_id in dict.fromkeys(
                        shortlist.shortlisted_datasource_ids
                    )
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
            selection = self._normalize_selection(selection, catalog)
            logger.info(
                "datasource selection resolved selected_count=%s selected_ids=%s",
                len(selection.selected_datasource_ids),
                selection.selected_datasource_ids,
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "selection unresolved; preserving candidate sources: %s",
                exc,
            )
            selection = self._fallback_selection(catalog)
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

    def business_context(
        self,
        state: AnalysisPreparationState,
    ) -> AnalysisPreparationState:
        self._start_stage("business_context")
        selection = state["selection"]
        if len(selection.selected_datasource_ids) == 1:
            contract = self._fallback_business_context(selection)
            return {
                "business_context": contract,
                "selected_snapshot": enrich_selected_datasource_schemas(
                    state["selected_snapshot"],
                    selection=selection,
                    business_context=contract,
                ),
            }
        payload = {
            "user_query": self.user_query,
            "intent_context": state["intent"].model_dump(),
            "datasource_selection": selection.model_dump(),
            "selected_datasources": state["selected_schemas"],
            "query_guidance": build_query_guidance(
                state.get("query_capabilities") or ()
            ),
        }
        try:
            candidate = _invoke_contract(
                stage="business_context",
                payload=payload,
                output_model=BusinessContextContract,
                runtime_config=self.runtime_config,
                cancelled=self.cancelled,
            )
            candidate = self._normalize_unverified_schema_relationships(
                candidate,
                state["selected_schemas"],
            )
            validate_business_context(
                candidate,
                state["selected_schemas"],
                selection=selection,
            )
            contract = candidate
            logger.info(
                "business context resolved relationships=%s unresolved=%s",
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
                contract.unresolved_relationships,
            )
        except AnalysisTaskCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "business context unresolved; carrying relationship uncertainty: %s",
                exc,
            )
            contract = self._fallback_business_context(selection)
        return {
            "business_context": contract,
            "probe_observations": (),
            "selected_snapshot": enrich_selected_datasource_schemas(
                state["selected_snapshot"],
                selection=selection,
                business_context=contract,
            ),
        }

    def compile(self):
        graph = StateGraph(AnalysisPreparationState)
        graph.add_node("intent_analysis", self.intent_analysis)
        graph.add_node("select_datasources", self.select_datasources)
        graph.add_node("business_context", self.business_context)
        graph.add_edge(START, "intent_analysis")
        graph.add_edge("intent_analysis", "select_datasources")
        graph.add_edge("select_datasources", "business_context")
        graph.add_edge("business_context", END)
        return graph.compile()

    def run(self) -> PreparationResult:
        state = self.compile().invoke({
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
