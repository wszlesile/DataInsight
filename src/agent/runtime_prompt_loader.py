from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from agent.runtime_contract_registry import (
    load_runtime_contract_pack,
    load_runtime_contract_text,
    load_runtime_initial_contract_text,
    render_runtime_contract_manifest,
    required_runtime_contract_ids,
)
from utils.token_budget import estimate_text_tokens


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "runtime" / "sys_prompt.md"


@lru_cache(maxsize=16)
def load_runtime_semantic_prompt(
    query_capabilities: tuple[str, ...] = (),
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> str:
    """Load the semantic kernel and the manifest without mechanical contracts."""
    semantic_kernel = PROMPT_PATH.read_text(encoding="utf-8").strip()
    manifest = render_runtime_contract_manifest(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    return f"{semantic_kernel}\n\n{manifest}".strip()


@lru_cache(maxsize=16)
def load_runtime_contract_prompt(
    query_capabilities: tuple[str, ...] = (),
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> str:
    """Load the mechanical contracts selected for the current datasource union."""
    required = required_runtime_contract_ids(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    contract_text = load_runtime_contract_text(required)
    return f"<runtime-contracts>\n{contract_text}\n</runtime-contracts>".strip()


@lru_cache(maxsize=16)
def load_runtime_initial_contract_prompt(
    query_capabilities: tuple[str, ...] = (),
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> str:
    """Load the bounded first-turn view; full packs stay available locally."""
    required = required_runtime_contract_ids(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    contract_text = load_runtime_initial_contract_text(required)
    return f"<runtime-contract-summaries>\n{contract_text}\n</runtime-contract-summaries>".strip()


@lru_cache(maxsize=16)
def load_runtime_prompt(
    query_capabilities: tuple[str, ...] = (),
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> str:
    """Compile the complete prompt for diagnostics and compatibility callers."""
    return (
        f"{load_runtime_semantic_prompt(query_capabilities, context_mode, output_format, clarification_mode)}\n\n"
        f"{load_runtime_contract_prompt(query_capabilities, context_mode, output_format, clarification_mode)}"
    ).strip()


@lru_cache(maxsize=16)
def describe_runtime_prompt(
    query_capabilities: tuple[str, ...] = (),
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> dict[str, object]:
    """Return size-only diagnostics for the compiled Runtime prompt."""
    semantic_kernel = PROMPT_PATH.read_text(encoding="utf-8").strip()
    required = required_runtime_contract_ids(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    packs = []
    for contract_id in required:
        text = load_runtime_contract_pack(contract_id)
        packs.append({
            "contract_id": contract_id,
            "chars": len(text),
            "estimated_tokens": estimate_text_tokens(text),
        })
    compiled = load_runtime_prompt(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    return {
        "context_mode": context_mode,
        "query_capabilities": list(query_capabilities),
        "semantic_kernel": {
            "chars": len(semantic_kernel),
            "estimated_tokens": estimate_text_tokens(semantic_kernel),
        },
        "contract_packs": packs,
        "initial_contracts": {
            "chars": len(load_runtime_initial_contract_prompt(
                query_capabilities, context_mode, output_format, clarification_mode,
            )),
            "estimated_tokens": estimate_text_tokens(load_runtime_initial_contract_prompt(
                query_capabilities, context_mode, output_format, clarification_mode,
            )),
        },
        "compiled": {
            "chars": len(compiled),
            "estimated_tokens": estimate_text_tokens(compiled),
        },
    }
