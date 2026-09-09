"""Pure control state for the future explicit Runtime Driver.

This module intentionally has no LangChain, database, or tool dependency. It
exists so lifecycle invariants can be proven before the current ReAct path is
replaced. The message transcript remains an audit source, never a phase source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class RuntimePhase(StrEnum):
    MATERIALIZE = "materialize"
    PROBE = "probe"
    IMPLEMENT = "implement"
    REVIEW = "review"
    REPAIR = "repair"
    WAITING_CLARIFICATION = "waiting_clarification"
    COMPLETED = "completed"


ACTIVE_PHASES = frozenset({
    RuntimePhase.MATERIALIZE,
    RuntimePhase.PROBE,
    RuntimePhase.IMPLEMENT,
    RuntimePhase.REVIEW,
    RuntimePhase.REPAIR,
})


@dataclass(frozen=True)
class RuntimeWorkspace:
    """Versioned control-plane state for one unified analysis runtime."""

    workspace_id: str
    revision: int
    phase: RuntimePhase
    goal_revision: int = 1
    material_revision: int = 0
    probe_revision: int = 0
    program_revision: int = 0
    diagnostic_revision: int = 0
    material_manifest: dict[str, Any] = field(default_factory=dict)
    schema_requirements: dict[str, tuple[str, ...]] = field(default_factory=dict)
    probe_ledgers: tuple[dict[str, Any], ...] = ()
    latest_program: dict[str, Any] = field(default_factory=dict)
    latest_diagnostic: dict[str, Any] = field(default_factory=dict)
    reason: str = "initial_materialization"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_workspace(*, workspace_id: str | None = None) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        workspace_id=workspace_id or uuid4().hex,
        revision=1,
        phase=RuntimePhase.MATERIALIZE,
    )


def _next(workspace: RuntimeWorkspace, **changes: Any) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        **{
            **workspace.to_dict(),
            **changes,
            "revision": workspace.revision + 1,
        }
    )


def record_schema_requirements(
    workspace: RuntimeWorkspace,
    requirements: dict[str, list[str] | tuple[str, ...]],
) -> RuntimeWorkspace:
    """Record exact schema fields without changing lifecycle phase."""
    if workspace.phase is not RuntimePhase.MATERIALIZE:
        raise ValueError("Schema requirements may only be expanded during materialization.")
    normalized = {
        str(datasource_id): tuple(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
        for datasource_id, fields in requirements.items()
        if fields
    }
    merged = dict(workspace.schema_requirements)
    for datasource_id, fields in normalized.items():
        merged[datasource_id] = tuple(dict.fromkeys([*merged.get(datasource_id, ()), *fields]))
    return _next(workspace, schema_requirements=merged, reason="schema_requirements_expanded")


def record_material_manifest(
    workspace: RuntimeWorkspace,
    manifest: dict[str, Any],
) -> RuntimeWorkspace:
    """Commit a validated material manifest and select the next local phase."""
    if workspace.phase is not RuntimePhase.MATERIALIZE:
        raise ValueError("Material manifest may only be committed from materialization.")
    units = manifest.get("material_units") if isinstance(manifest, dict) else None
    if not isinstance(units, list) or not units:
        raise ValueError("A material manifest requires at least one material unit.")
    next_phase = RuntimePhase.PROBE if bool(manifest.get("probe_required")) else RuntimePhase.IMPLEMENT
    return _next(
        workspace,
        phase=next_phase,
        material_revision=workspace.material_revision + 1,
        material_manifest=dict(manifest),
        latest_diagnostic={},
        reason="material_manifest_requires_probe" if next_phase is RuntimePhase.PROBE else "material_manifest_ready",
    )


def record_probe_result(
    workspace: RuntimeWorkspace,
    result: dict[str, Any],
) -> RuntimeWorkspace:
    """Advance only from actual structured Probe facts, never from prose."""
    if workspace.phase is not RuntimePhase.PROBE:
        raise ValueError("Probe result is only valid while Runtime is probing.")
    diagnostics = result.get("diagnostics") if isinstance(result, dict) else None
    ledger = diagnostics.get("probe_fact_ledger") if isinstance(diagnostics, dict) else None
    if result.get("status") == "observation" and isinstance(ledger, dict):
        return _next(
            workspace,
            phase=RuntimePhase.IMPLEMENT,
            probe_revision=workspace.probe_revision + 1,
            probe_ledgers=(*workspace.probe_ledgers, dict(ledger)),
            latest_diagnostic={},
            reason="probe_observation_captured",
        )
    return _next(
        workspace,
        probe_revision=workspace.probe_revision + 1,
        latest_diagnostic=dict(result or {}),
        diagnostic_revision=workspace.diagnostic_revision + 1,
        reason="probe_requires_repair",
    )


def record_program_result(
    workspace: RuntimeWorkspace,
    result: dict[str, Any],
    *,
    program: dict[str, Any],
) -> RuntimeWorkspace:
    """Move a formal program to completion, repair, or an explicitly required Probe."""
    if workspace.phase not in {RuntimePhase.IMPLEMENT, RuntimePhase.REPAIR}:
        raise ValueError("Formal program is only valid during implementation or repair.")
    if result.get("status") == "success":
        return _next(
            workspace,
            phase=RuntimePhase.REVIEW,
            program_revision=workspace.program_revision + 1,
            latest_program=dict(program),
            latest_diagnostic={},
            reason="formal_program_candidate_ready",
        )
    diagnostics = result.get("diagnostics") if isinstance(result, dict) else None
    requires_probe = isinstance(diagnostics, dict) and diagnostics.get("violated_contract") == "formal_tool_received_probe_program"
    return _next(
        workspace,
        phase=RuntimePhase.PROBE if requires_probe else RuntimePhase.REPAIR,
        program_revision=workspace.program_revision + 1,
        diagnostic_revision=workspace.diagnostic_revision + 1,
        latest_program=dict(program),
        latest_diagnostic=dict(result or {}),
        reason="formal_program_requires_probe" if requires_probe else "formal_program_requires_repair",
    )


def accept_program_result(workspace: RuntimeWorkspace) -> RuntimeWorkspace:
    if workspace.phase is not RuntimePhase.REVIEW:
        raise ValueError("Only a reviewed candidate may be accepted.")
    return _next(
        workspace,
        phase=RuntimePhase.COMPLETED,
        latest_diagnostic={},
        reason="candidate_result_accepted",
    )


def request_program_revision(
    workspace: RuntimeWorkspace,
    diagnostic: dict[str, Any],
) -> RuntimeWorkspace:
    if workspace.phase is not RuntimePhase.REVIEW:
        raise ValueError("Only a candidate under review may request revision.")
    return _next(
        workspace,
        phase=RuntimePhase.REPAIR,
        diagnostic_revision=workspace.diagnostic_revision + 1,
        latest_diagnostic=dict(diagnostic or {}),
        reason="candidate_result_requires_revision",
    )


def request_clarification(workspace: RuntimeWorkspace) -> RuntimeWorkspace:
    if workspace.phase not in ACTIVE_PHASES:
        raise ValueError("Clarification may only interrupt active Runtime work.")
    return _next(
        workspace,
        phase=RuntimePhase.WAITING_CLARIFICATION,
        reason="user_clarification_required",
    )


def resume_after_clarification(workspace: RuntimeWorkspace) -> RuntimeWorkspace:
    if workspace.phase is not RuntimePhase.WAITING_CLARIFICATION:
        raise ValueError("Only a waiting Runtime may resume from clarification.")
    return _next(
        workspace,
        phase=RuntimePhase.MATERIALIZE,
        goal_revision=workspace.goal_revision + 1,
        material_manifest={},
        schema_requirements={},
        latest_diagnostic={},
        reason="clarification_answered",
    )
