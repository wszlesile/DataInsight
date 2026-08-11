import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from utils import logger
from utils.llm_model_factory import create_data_insight_model, create_runtime_config


_TECHNICAL_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_LABEL_SEPARATORS = re.compile(r"[;\uff1b\u3002\n]")
_LABEL_RESOLVER_PROMPT = """You normalize table presentation metadata for a data insight report.

Return one JSON object with this exact shape:
{"labels":[{"table_index":0,"column_index":0,"key":"field_key","label":"concise business label"}]}

Rules:
- Resolve every supplied column exactly once.
- Keep table_index, column_index, and key unchanged.
- Produce only a concise user-facing column label in the report language.
- Use the table title, description, field meaning, and sample values for meaning.
- Never expose a snake_case/camelCase implementation identifier as the label.
- Do not change data, calculations, units, precision, or field keys.
- Return JSON only.
"""


@dataclass(frozen=True)
class TableLabelNormalization:
    tables: list[dict[str, Any]]
    local_count: int
    model_count: int
    unresolved: list[dict[str, Any]]


def _is_technical_label(key: str, label: str) -> bool:
    return bool(key and label == key and _TECHNICAL_IDENTIFIER.fullmatch(key))


def _schema_label(definition: dict[str, Any]) -> str:
    for key in (
        "displayName",
        "display_name",
        "businessName",
        "business_name",
        "title",
        "label",
    ):
        value = str(definition.get(key) or "").strip()
        if value and not _TECHNICAL_IDENTIFIER.fullmatch(value):
            return value
    description = str(
        definition.get("description") or definition.get("remark") or ""
    ).strip()
    if not description:
        return ""
    candidate = _LABEL_SEPARATORS.split(description, maxsplit=1)[0].strip()
    if not candidate or len(candidate) > 32 or _TECHNICAL_IDENTIFIER.fullmatch(candidate):
        return ""
    return candidate


def _schema_label_catalog(
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    for datasource in selected_datasource_snapshot or []:
        metadata_schema = datasource.get("metadata_schema")
        if isinstance(metadata_schema, str):
            try:
                metadata_schema = json.loads(metadata_schema)
            except (TypeError, ValueError):
                metadata_schema = {}
        if not isinstance(metadata_schema, dict):
            continue
        properties = metadata_schema.get("properties")
        if not isinstance(properties, dict):
            continue
        for field, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            label = _schema_label(definition)
            if label:
                candidates.setdefault(str(field), []).append(label)

    catalog: dict[str, str] = {}
    for field, labels in candidates.items():
        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        catalog[field] = sorted(
            counts,
            key=lambda label: (-counts[label], len(label), label),
        )[0]
    return catalog


def _extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.I)
        normalized = re.sub(r"\s*```$", "", normalized)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(normalized[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("table label resolver must return a JSON object")
    return payload


def _invoke_label_resolver(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime_config = create_runtime_config()
    model = create_data_insight_model(runtime_config)
    if runtime_config.adapter == "openai":
        model = model.bind(response_format={"type": "json_object"})
    response = model.invoke(
        [
            SystemMessage(_LABEL_RESOLVER_PROMPT),
            HumanMessage(json.dumps({"columns": items}, ensure_ascii=False)),
        ]
    )
    content = getattr(response, "content", response)
    if isinstance(content, list):
        text = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    else:
        text = str(content or "")
    labels = _extract_json_object(text).get("labels")
    return labels if isinstance(labels, list) else []


def normalize_table_column_labels(
    tables: list[dict[str, Any]] | None,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> TableLabelNormalization:
    """Normalize only table labels after successful artifact materialization."""
    normalized = deepcopy(tables or [])
    catalog = _schema_label_catalog(selected_datasource_snapshot)
    unresolved: list[dict[str, Any]] = []
    local_count = 0

    for table_index, table in enumerate(normalized):
        if not isinstance(table, dict):
            continue
        rows = [row for row in table.get("rows") or [] if isinstance(row, dict)]
        for column_index, column in enumerate(table.get("columns") or []):
            if not isinstance(column, dict):
                continue
            key = str(column.get("key") or "").strip()
            label = str(column.get("label") or "").strip()
            if not _is_technical_label(key, label):
                continue
            schema_label = catalog.get(key, "")
            if schema_label:
                column["label"] = schema_label
                local_count += 1
                continue
            unresolved.append(
                {
                    "table_index": table_index,
                    "column_index": column_index,
                    "key": key,
                    "table_title": str(table.get("title") or ""),
                    "table_description": str(table.get("description") or ""),
                    "sample_values": [row.get(key) for row in rows[:3]],
                }
            )

    model_count = 0
    if unresolved:
        try:
            resolved = _invoke_label_resolver(unresolved)
            expected = {
                (item["table_index"], item["column_index"], item["key"])
                for item in unresolved
            }
            for item in resolved:
                if not isinstance(item, dict):
                    continue
                identity = (
                    item.get("table_index"),
                    item.get("column_index"),
                    str(item.get("key") or ""),
                )
                label = str(item.get("label") or "").strip()
                if identity not in expected or not label or _is_technical_label(identity[2], label):
                    continue
                table_index, column_index, _ = identity
                normalized[table_index]["columns"][column_index]["label"] = label
                model_count += 1
        except Exception as exc:
            logger.warning("table label resolver failed: %s", exc)

    remaining: list[dict[str, Any]] = []
    for item in unresolved:
        table_index = item["table_index"]
        column_index = item["column_index"]
        column = normalized[table_index]["columns"][column_index]
        if _is_technical_label(
            str(column.get("key") or "").strip(),
            str(column.get("label") or "").strip(),
        ):
            remaining.append(item)

    return TableLabelNormalization(
        tables=normalized,
        local_count=local_count,
        model_count=model_count,
        unresolved=remaining,
    )
