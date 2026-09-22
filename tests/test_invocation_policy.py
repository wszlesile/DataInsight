from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.invocation_policy import (
    InvocationPolicy,
    normalize_output_contract,
    validate_json_output,
)


JSON_CONTRACT = {
    "type": "json",
    "schema": {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "对象数量"},
            "summary": {"type": "string", "description": "洞察结论"},
        },
        "required": ["count", "summary"],
        "additionalProperties": False,
    },
}


def test_workflow_policy_disables_clarification_and_evidence():
    policy = InvocationPolicy.workflow(output_format=JSON_CONTRACT)

    assert policy.output.type == "json"
    assert policy.clarification_mode == "disabled"
    assert policy.evidence_enabled is False


def test_json_output_is_validated_locally():
    contract = normalize_output_contract(JSON_CONTRACT)
    validate_json_output({"count": 3, "summary": "风险集中"}, contract.schema)

    with pytest.raises(ValueError, match="缺少必填字段"):
        validate_json_output({"count": 3}, contract.schema)
    with pytest.raises(ValueError, match="包含未声明字段"):
        validate_json_output(
            {"count": 3, "summary": "风险集中", "extra": True},
            contract.schema,
        )


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_json_output_rejects_nonfinite_values(value):
    with pytest.raises(ValueError, match='NaN'):
        validate_json_output({'nested': [value]}, {
            'type': 'object', 'properties': {'nested': {'type': 'array', 'items': {'type': 'number'}}},
        })


def test_json_output_contract_requires_schema():
    with pytest.raises(ValueError, match="必须提供非空 JSON Schema"):
        normalize_output_contract({"type": "json"})


def test_json_output_contract_requires_object_root():
    with pytest.raises(ValueError, match="根 Schema 必须是 object"):
        normalize_output_contract({
            "type": "json",
            "schema": {
                "type": "array",
                "items": {"type": "integer"},
            },
        })


def test_json_output_contract_rejects_reversed_numeric_bounds():
    with pytest.raises(
        ValueError,
        match=r"\$\.total_batch_count: minimum \(100000\) 不能大于 maximum \(10\)",
    ):
        normalize_output_contract({
            "type": "json",
            "schema": {
                "type": "object",
                "properties": {
                    "total_batch_count": {
                        "type": "integer",
                        "minimum": 100000,
                        "maximum": 10,
                    },
                },
                "required": ["total_batch_count"],
                "additionalProperties": False,
            },
        })


@pytest.mark.parametrize(
    "property_schema, expected_message",
    [
        (
            {"type": "number", "exclusiveMinimum": 10, "maximum": 10},
            "数值范围不可满足",
        ),
        (
            {"type": "array", "items": {"type": "integer"}, "minItems": 5, "maxItems": 2},
            "minItems (5) 不能大于 maxItems (2)",
        ),
        (
            {"type": "string", "minLength": 8, "maxLength": 3},
            "minLength (8) 不能大于 maxLength (3)",
        ),
    ],
)
def test_json_output_contract_rejects_unsatisfiable_ranges(property_schema, expected_message):
    with pytest.raises(ValueError) as exc_info:
        normalize_output_contract({
            "type": "json",
            "schema": {
                "type": "object",
                "properties": {"value": property_schema},
            },
        })
    assert expected_message in str(exc_info.value)
