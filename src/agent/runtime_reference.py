import hashlib

from langchain.tools import tool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from agent.context_engineering_runtime import CustomContext
from agent.runtime_contract_registry import (
    load_runtime_contract_reference,
    runtime_contract_pack_ids,
)


class RuntimeReferenceInput(BaseModel):
    contract_id: str = Field(
        min_length=1,
        description=(
            "One contract ID from runtime-contract-manifest for the current concrete code gap. "
            "Read one reference, use it, then request another only if the next work still lacks a specific detail."
        ),
    )


@tool(args_schema=RuntimeReferenceInput)
def read_runtime_reference(
    runtime: ToolRuntime[CustomContext], contract_id: str,
) -> dict:
    """Read one complete Python API usage reference when the current work needs it.

    Core rules are already supplied. This optional, local reference lookup does not
    query data or execute code. Examples are not business facts or report templates;
    the current Schema and output contract still determine the actual analysis.
    """
    context = runtime.context
    output_type = str((getattr(context, "output_contract", None) or {}).get("type") or "standard")
    result_pack = {"standard": "result.artifacts", "text": "result.text", "json": "result.json"}[output_type]
    interaction_pack = (
        "interaction.clarification"
        if getattr(context, "clarification_mode", "same_turn") == "same_turn"
        else "interaction.no_clarification"
    )
    available = [
        item for item in runtime_contract_pack_ids()
        if (not item.startswith("result.") or item == result_pack)
        and (not item.startswith("interaction.") or item == interaction_pack)
    ]
    requested = [str(contract_id or '').strip()]
    requested = [item for item in requested if item]
    references = []
    for contract_id in requested:
        if contract_id not in available:
            continue
        content = load_runtime_contract_reference(contract_id, output_format=output_type)
        references.append({
            "contract_id": contract_id,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        })
    unavailable = [item for item in requested if item not in available]
    return {
        "tool": "read_runtime_reference",
        "status": "partial" if references and unavailable else "success" if references else "unavailable",
        "references": references,
        "unavailable_contract_ids": unavailable,
        "available_contract_ids": available,
    }
