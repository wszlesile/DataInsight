from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal


OutputFormatType = Literal["standard", "text", "json"]
ClarificationMode = Literal["same_turn", "disabled"]

MAX_OUTPUT_SCHEMA_CHARS = 30000
MAX_OUTPUT_SCHEMA_DEPTH = 10
MAX_OUTPUT_SCHEMA_PROPERTIES = 200
SUPPORTED_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}
SUPPORTED_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "pattern",
}


@dataclass(frozen=True)
class OutputContract:
    type: OutputFormatType = "standard"
    schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.type == "json":
            payload["schema"] = self.schema
        return payload


@dataclass(frozen=True)
class InvocationPolicy:
    channel: Literal["interactive", "workflow"] = "interactive"
    output: OutputContract = field(default_factory=OutputContract)
    clarification_mode: ClarificationMode = "same_turn"
    evidence_enabled: bool = True

    @classmethod
    def interactive(
        cls,
        *,
        clarification_mode: str = "same_turn",
        output_format: Any = None,
    ) -> "InvocationPolicy":
        return cls(
            channel="interactive",
            output=normalize_output_contract(output_format),
            clarification_mode=normalize_clarification_mode(clarification_mode),
            evidence_enabled=True,
        )

    @classmethod
    def workflow(cls, *, output_format: Any) -> "InvocationPolicy":
        return cls(
            channel="workflow",
            output=normalize_output_contract(output_format),
            clarification_mode="disabled",
            evidence_enabled=False,
        )


def normalize_clarification_mode(value: Any) -> ClarificationMode:
    normalized = str(value or "same_turn").strip().lower()
    if normalized not in {"same_turn", "disabled"}:
        raise ValueError("clarification_mode 仅支持 same_turn 或 disabled")
    return normalized  # type: ignore[return-value]


def normalize_output_contract(value: Any) -> OutputContract:
    if value in (None, ""):
        return OutputContract()
    if isinstance(value, str):
        payload = {"type": value}
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise ValueError("output_format 必须是对象")

    output_type = str(payload.get("type") or "").strip().lower()
    if output_type not in {"standard", "text", "json"}:
        raise ValueError("output_format.type 仅支持 standard、text 或 json")
    if output_type != "json":
        if payload.get("schema") not in (None, {}):
            raise ValueError("只有 json 输出格式可以声明 schema")
        return OutputContract(type=output_type)  # type: ignore[arg-type]

    schema = payload.get("schema")
    if not isinstance(schema, dict) or not schema:
        raise ValueError("json 输出格式必须提供非空 JSON Schema")
    if schema.get("type") != "object":
        raise ValueError("json 输出格式的根 Schema 必须是 object")
    validate_output_schema(schema)
    return OutputContract(type="json", schema=schema)


def validate_output_schema(schema: dict[str, Any]) -> None:
    serialized = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > MAX_OUTPUT_SCHEMA_CHARS:
        raise ValueError(f"JSON Schema 不能超过 {MAX_OUTPUT_SCHEMA_CHARS} 字符")
    property_count = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal property_count
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise ValueError(f"JSON Schema 嵌套超过 {MAX_OUTPUT_SCHEMA_DEPTH} 层: {path}")
        if not isinstance(node, dict):
            raise ValueError(f"JSON Schema 节点必须是对象: {path}")
        unsupported = sorted(set(node) - SUPPORTED_SCHEMA_KEYS)
        if unsupported:
            raise ValueError(f"JSON Schema 包含不支持的关键字 {unsupported}: {path}")
        schema_type = node.get("type")
        allowed_types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not allowed_types or any(item not in SUPPORTED_SCHEMA_TYPES for item in allowed_types):
            raise ValueError(f"JSON Schema 必须声明受支持的 type: {path}")
        if "object" in allowed_types:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ValueError(f"object Schema 必须声明 properties: {path}")
            property_count += len(properties)
            if property_count > MAX_OUTPUT_SCHEMA_PROPERTIES:
                raise ValueError(
                    f"JSON Schema 属性总数不能超过 {MAX_OUTPUT_SCHEMA_PROPERTIES}"
                )
            required = node.get("required", [])
            if not isinstance(required, list) or any(
                not isinstance(item, str) or item not in properties for item in required
            ):
                raise ValueError(f"required 必须引用当前 properties 中的字段: {path}")
            additional = node.get("additionalProperties", True)
            if not isinstance(additional, bool):
                raise ValueError(f"additionalProperties 仅支持布尔值: {path}")
            for name, child in properties.items():
                walk(child, f"{path}.{name}", depth + 1)
        if "array" in allowed_types:
            items = node.get("items")
            if not isinstance(items, dict):
                raise ValueError(f"array Schema 必须声明单一 items Schema: {path}")
            walk(items, f"{path}[]", depth + 1)
        if "pattern" in node:
            try:
                re.compile(str(node["pattern"]))
            except re.error as exc:
                raise ValueError(f"JSON Schema pattern 非法: {path}: {exc}") from exc

    walk(schema, "$", 1)


def validate_json_output(value: Any, schema: dict[str, Any]) -> None:
    errors: list[str] = []

    def fail(path: str, message: str) -> None:
        if len(errors) < 20:
            errors.append(f"{path}: {message}")

    def matches_type(item: Any, schema_type: str) -> bool:
        if schema_type == "null":
            return item is None
        if schema_type == "boolean":
            return isinstance(item, bool)
        if schema_type == "integer":
            return isinstance(item, int) and not isinstance(item, bool)
        if schema_type == "number":
            return isinstance(item, (int, float)) and not isinstance(item, bool)
        if schema_type == "string":
            return isinstance(item, str)
        if schema_type == "array":
            return isinstance(item, list)
        if schema_type == "object":
            return isinstance(item, dict)
        return False

    def walk(item: Any, node: dict[str, Any], path: str) -> None:
        raw_types = node.get("type")
        allowed_types = raw_types if isinstance(raw_types, list) else [raw_types]
        if not any(matches_type(item, schema_type) for schema_type in allowed_types):
            fail(path, f"类型应为 {allowed_types}，实际为 {type(item).__name__}")
            return
        if "const" in node and item != node["const"]:
            fail(path, f"值必须等于 {node['const']!r}")
        if "enum" in node and item not in node["enum"]:
            fail(path, f"值不在允许枚举 {node['enum']!r} 中")
        if isinstance(item, dict):
            properties = node.get("properties") or {}
            for name in node.get("required") or []:
                if name not in item:
                    fail(path, f"缺少必填字段 {name}")
            if node.get("additionalProperties") is False:
                for name in item:
                    if name not in properties:
                        fail(path, f"包含未声明字段 {name}")
            for name, child in properties.items():
                if name in item:
                    walk(item[name], child, f"{path}.{name}")
        elif isinstance(item, list):
            if "minItems" in node and len(item) < int(node["minItems"]):
                fail(path, f"数组长度不能小于 {node['minItems']}")
            if "maxItems" in node and len(item) > int(node["maxItems"]):
                fail(path, f"数组长度不能大于 {node['maxItems']}")
            child = node.get("items") or {}
            for index, child_value in enumerate(item):
                walk(child_value, child, f"{path}[{index}]")
        elif isinstance(item, str):
            if "minLength" in node and len(item) < int(node["minLength"]):
                fail(path, f"字符串长度不能小于 {node['minLength']}")
            if "maxLength" in node and len(item) > int(node["maxLength"]):
                fail(path, f"字符串长度不能大于 {node['maxLength']}")
            if "pattern" in node and re.search(str(node["pattern"]), item) is None:
                fail(path, f"字符串不符合 pattern {node['pattern']!r}")
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            if "minimum" in node and item < node["minimum"]:
                fail(path, f"数值不能小于 {node['minimum']}")
            if "maximum" in node and item > node["maximum"]:
                fail(path, f"数值不能大于 {node['maximum']}")
            if "exclusiveMinimum" in node and item <= node["exclusiveMinimum"]:
                fail(path, f"数值必须大于 {node['exclusiveMinimum']}")
            if "exclusiveMaximum" in node and item >= node["exclusiveMaximum"]:
                fail(path, f"数值必须小于 {node['exclusiveMaximum']}")

    walk(value, schema, "$")
    if errors:
        raise ValueError("JSON 输出不符合调用契约：" + "；".join(errors))
