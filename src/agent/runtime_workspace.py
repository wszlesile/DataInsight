"""Local, bounded readers for the Runtime's datasource workspace."""

import copy
import json
from typing import Any

from langchain.tools import tool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field, field_validator

from agent.context_engineering_runtime import CustomContext


class RuntimeDatasourceContextInput(BaseModel):
    """Request exact Schema definitions after inspecting the Runtime index."""

    datasource_ids: list[int] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Selected datasource ids whose exact Schema details are needed; "
            "request at most eight sources per local read and use field_names_by_datasource "
            "to keep multi-source details focused."
        ),
    )
    field_names_by_datasource: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Optional exact field names keyed by datasource id. Omit a datasource "
            "entry only when its complete Schema is genuinely needed."
        ),
    )

    @field_validator('datasource_ids', mode='before')
    @classmethod
    def _decode_datasource_ids(cls, value):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return value
            if isinstance(decoded, list):
                return decoded
        return value

    @field_validator('field_names_by_datasource', mode='before')
    @classmethod
    def _decode_field_names(cls, value):
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return value
            if isinstance(decoded, dict):
                return decoded
        return value

    include_relationships: bool = Field(
        default=False,
        description="Include these sources' Schema relationships and verified join-safety details.",
    )


def _selected_datasources(context: CustomContext) -> dict[int, dict[str, Any]]:
    snapshot = getattr(context, "execution_datasource_snapshot", {}) or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    rows = snapshot.get("selected_datasource_snapshot") or []
    selected: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            datasource_id = int(row.get("datasource_id") or row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id > 0:
            selected[datasource_id] = row
    active = getattr(context, "active_datasource_snapshot", {}) or {}
    for row in active.get("selected_datasource_snapshot") or []:
        datasource_id = int(row.get("datasource_id") or row.get("id") or 0)
        if datasource_id not in selected:
            continue
        properties = (row.get("metadata_schema") or {}).get("properties") or {}
        for field, definition in properties.items():
            if not isinstance(definition, dict) or "observed_value_profile" not in definition:
                continue
            source = copy.deepcopy(selected[datasource_id])
            original = (source.get("metadata_schema") or {}).get("properties") or {}
            if isinstance(original.get(field), dict):
                original[field]["observed_value_profile"] = copy.deepcopy(
                    definition["observed_value_profile"]
                )
                selected[datasource_id] = source
    return selected


def _requested_fields(
    field_names_by_datasource: dict[str, list[str]], datasource_id: int,
) -> list[str] | None:
    raw = field_names_by_datasource.get(str(datasource_id))
    if raw is None:
        raw = field_names_by_datasource.get(datasource_id)  # type: ignore[arg-type]
    if not isinstance(raw, list):
        return None
    return list(dict.fromkeys(
        str(value).strip() for value in raw if str(value).strip()
    ))


def _source_detail(
    source: dict[str, Any],
    *,
    requested_fields: list[str] | None,
    include_relationships: bool,
) -> dict[str, Any]:
    schema = source.get("metadata_schema")
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    selected_fields = list(properties) if requested_fields is None else requested_fields
    available = set(properties)
    source_payload = {
        key: copy.deepcopy(value)
        for key, value in source.items()
        if key not in {"metadata_schema", "field_capabilities"}
    }
    detail_schema: dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in schema.items()
        if key in {"title", "description", "type", "required", "semantic_enrichment_status"}
    }
    detail_schema["properties"] = {
        field_name: copy.deepcopy(properties[field_name])
        for field_name in selected_fields
        if field_name in available
    }
    if include_relationships and isinstance(schema.get("relationships"), list):
        detail_schema["relationships"] = copy.deepcopy(schema["relationships"])
    for key in ("$defs", "definitions"):
        if key in schema:
            detail_schema[key] = copy.deepcopy(schema[key])
    source_payload["metadata_schema"] = detail_schema
    missing = [field_name for field_name in selected_fields if field_name not in available]
    if missing:
        source_payload["unavailable_field_names"] = missing
    return source_payload


@tool(args_schema=RuntimeDatasourceContextInput)
def read_runtime_datasource_context(
    runtime: ToolRuntime[CustomContext],
    datasource_ids: list[int] | str,
    field_names_by_datasource: dict[str, list[str]] | str | None = None,
    include_relationships: bool = False,
) -> dict[str, Any]:
    """Read exact selected datasource Schema details on demand without querying data.

    The initial Runtime workspace contains every selected datasource and every
    field name. Use this local reader only when a planned query or calculation
    needs exact field descriptions, enum/format details, or relationship
    metadata that were not included in the compact index. It never expands the
    selected datasource scope and never executes a query.
    """
    if isinstance(datasource_ids, str):
        try:
            decoded_ids = json.loads(datasource_ids)
        except (TypeError, ValueError):
            decoded_ids = []
        datasource_ids = decoded_ids if isinstance(decoded_ids, list) else []
    if isinstance(field_names_by_datasource, str):
        try:
            decoded_fields = json.loads(field_names_by_datasource)
        except (TypeError, ValueError):
            decoded_fields = {}
        field_names_by_datasource = decoded_fields if isinstance(decoded_fields, dict) else {}
    sources = _selected_datasources(runtime.context)
    requested_ids = list(dict.fromkeys(int(item) for item in datasource_ids))
    invalid_ids = [item for item in requested_ids if item not in sources]
    fields_map = field_names_by_datasource or {}
    details = [
        _source_detail(
            sources[datasource_id],
            requested_fields=_requested_fields(fields_map, datasource_id),
            include_relationships=include_relationships,
        )
        for datasource_id in requested_ids
        if datasource_id in sources
    ]
    result = {
        "tool": "read_runtime_datasource_context",
        "status": "partial" if invalid_ids else "success",
        "datasources": details,
        "unavailable_datasource_ids": invalid_ids,
        "selected_datasource_ids": sorted(sources),
    }
    return result
