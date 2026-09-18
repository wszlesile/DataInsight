"""Pure control state for the unified analysis Runtime.

This module intentionally has no LangChain, database, or tool dependency. It
owns lifecycle transitions while the message transcript remains an audit
source, never a phase source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class RuntimePhase(StrEnum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    MATERIALIZE = "materialize"
    PROBE = "probe"
    PROBE_REPAIR = "probe_repair"
    IMPLEMENT = "implement"
    REVIEW = "review"
    REPAIR = "repair"
    WAITING_CLARIFICATION = "waiting_clarification"
    COMPLETED = "completed"
    BLOCKED = "blocked"


ACTIVE_PHASES = frozenset({
    RuntimePhase.UNDERSTAND,
    RuntimePhase.PLAN,
    RuntimePhase.MATERIALIZE,
    RuntimePhase.PROBE,
    RuntimePhase.PROBE_REPAIR,
    RuntimePhase.IMPLEMENT,
    RuntimePhase.REVIEW,
    RuntimePhase.REPAIR,
})


@dataclass(frozen=True)
class RuntimeWorkPacket:
    """Small, versioned control packet for one unified analysis Runtime.

    The packet stores control state and references only. Exact Schema, Probe
    observations, Python source and artifacts remain in their producer-owned
    stores and are projected into a model request for the active phase.
    """

    workspace_id: str
    revision: int
    phase: RuntimePhase
    focus: str = ""
    resume_phase: RuntimePhase | None = None
    resume_focus: str = ""
    goal_revision: int = 1
    material_revision: int = 0
    probe_revision: int = 0
    program_revision: int = 0
    diagnostic_revision: int = 0
    material_manifest: dict[str, Any] = field(default_factory=dict)
    schema_requirements: dict[str, tuple[str, ...]] = field(default_factory=dict)
    active_datasource_ids: tuple[int, ...] = ()
    active_fields_by_datasource: dict[str, tuple[str, ...]] = field(default_factory=dict)
    resume_datasource_ids: tuple[int, ...] = ()
    resume_fields_by_datasource: dict[str, tuple[str, ...]] = field(default_factory=dict)
    probe_ledgers: tuple[dict[str, Any], ...] = ()
    # A Review can authorize a source-bounded presentation repair. This is
    # control-plane state: until a new execution exists, formal submission
    # must preserve the reviewed candidate and use only these patch ranges.
    pending_review_patch: dict[str, Any] = field(default_factory=dict)
    latest_program: dict[str, Any] = field(default_factory=dict)
    latest_diagnostic: dict[str, Any] = field(default_factory=dict)
    active_program_call_id: str = ""
    active_execution_id: int = 0
    latest_result_call_id: str = ""
    reason: str = "initial_materialization"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Compatibility name for the pure reducer tests and callers introduced before
# the packet became the active Runtime control plane.
RuntimeWorkspace = RuntimeWorkPacket


RUNTIME_WORK_PACKET_MEMORY_KEY = "runtime_work_packet"
RUNTIME_WORK_PACKET_PAYLOAD_KEY = "_runtime_work_packet"


def new_runtime_work_packet(
    *,
    workspace_id: str | None = None,
    focus: str = "",
) -> RuntimeWorkPacket:
    return RuntimeWorkPacket(
        workspace_id=workspace_id or uuid4().hex,
        revision=1,
        phase=RuntimePhase.UNDERSTAND,
        focus=str(focus or "").strip(),
        reason="analysis_received",
    )


def new_workspace(*, workspace_id: str | None = None) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        workspace_id=workspace_id or uuid4().hex,
        revision=1,
        phase=RuntimePhase.MATERIALIZE,
    )


def _next(workspace: RuntimeWorkspace, **changes: Any) -> RuntimeWorkspace:
    return RuntimeWorkPacket(
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


def runtime_work_packet_from_dict(value: dict[str, Any] | None) -> RuntimeWorkPacket | None:
    if not isinstance(value, dict):
        return None
    try:
        payload = dict(value)
        payload["phase"] = RuntimePhase(payload["phase"])
        resume_phase = payload.get("resume_phase")
        payload["resume_phase"] = RuntimePhase(resume_phase) if resume_phase else None
        payload["probe_ledgers"] = tuple(payload.get("probe_ledgers") or ())
        payload["active_datasource_ids"] = tuple(payload.get("active_datasource_ids") or ())
        payload["resume_datasource_ids"] = tuple(payload.get("resume_datasource_ids") or ())
        payload["schema_requirements"] = {
            str(key): tuple(fields or ())
            for key, fields in (payload.get("schema_requirements") or {}).items()
        }
        payload["active_fields_by_datasource"] = {
            str(key): tuple(fields or ())
            for key, fields in (payload.get("active_fields_by_datasource") or {}).items()
        }
        payload["resume_fields_by_datasource"] = {
            str(key): tuple(fields or ())
            for key, fields in (payload.get("resume_fields_by_datasource") or {}).items()
        }
        return RuntimeWorkPacket(**payload)
    except (KeyError, TypeError, ValueError):
        return None


def runtime_phase_work(phase: RuntimePhase) -> str:
    if phase is RuntimePhase.MATERIALIZE:
        return "plan"
    return phase.value


def runtime_phase_from_work(work: str) -> RuntimePhase:
    return {
        "understand": RuntimePhase.UNDERSTAND,
        "plan": RuntimePhase.PLAN,
        "probe": RuntimePhase.PROBE,
        "probe_repair": RuntimePhase.PROBE_REPAIR,
        "implement": RuntimePhase.IMPLEMENT,
        "repair": RuntimePhase.REPAIR,
        "review": RuntimePhase.REVIEW,
        "completed": RuntimePhase.COMPLETED,
    }.get(str(work or "").strip(), RuntimePhase.UNDERSTAND)


def reduce_runtime_tool_result(
    packet: RuntimeWorkPacket,
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None,
    analysis_complete: bool = False,
) -> RuntimeWorkPacket:
    """Advance the packet from one actual tool result.

    The model chooses an allowed action; this reducer owns the resulting state
    transition. Prose and projected history never advance the lifecycle.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or "").strip()
    focus = str(result.get("focus") or arguments.get("focus") or packet.focus).strip()
    common = {
        "latest_result_call_id": str(tool_call_id or ""),
    }
    if analysis_complete:
        return _next(
            packet,
            phase=RuntimePhase.COMPLETED,
            focus=focus,
            resume_phase=None,
            resume_focus="",
            resume_datasource_ids=(),
            resume_fields_by_datasource={},
            latest_diagnostic={},
            reason="analysis_complete",
            **common,
        )
    if tool_name == "select_runtime_work" and status == "success":
        selected = str(result.get("work") or arguments.get("work") or "").strip()
        phase = {
            "plan": RuntimePhase.PLAN,
            "probe": RuntimePhase.PROBE,
            "implement": RuntimePhase.IMPLEMENT,
        }.get(selected)
        if phase is not None:
            return _next(packet, phase=phase, focus=focus, reason=f"work_selected:{selected}", **common)
    if tool_name == "plan_runtime_materials" and status == "success":
        manifest = {
            key: result.get(key)
            for key in ("focus", "material_units", "probe_required", "probe_focus")
        }
        probe_required = bool(result.get("probe_required"))
        analysis_focus = str(result.get("focus") or packet.focus).strip()
        probe_focus = str(result.get("probe_focus") or analysis_focus).strip()
        datasource_ids, fields = _material_scope_from_units(result.get("material_units"))
        return _next(
            packet,
            phase=RuntimePhase.PROBE if probe_required else RuntimePhase.IMPLEMENT,
            focus=probe_focus if probe_required else analysis_focus,
            resume_phase=RuntimePhase.IMPLEMENT if probe_required else None,
            resume_focus=analysis_focus if probe_required else "",
            material_revision=packet.material_revision + 1,
            material_manifest=manifest,
            schema_requirements=fields,
            active_datasource_ids=datasource_ids,
            active_fields_by_datasource=fields,
            resume_datasource_ids=datasource_ids if probe_required else (),
            resume_fields_by_datasource=fields if probe_required else {},
            latest_diagnostic={},
            reason="material_plan_requires_probe" if probe_required else "material_plan_ready",
            **common,
        )
    if tool_name == "request_runtime_probe" and status == "success":
        resume_phase = (
            packet.phase
            if packet.phase in {RuntimePhase.IMPLEMENT, RuntimePhase.REPAIR}
            else RuntimePhase.IMPLEMENT
        )
        requested_ids, requested_fields = _requested_material_scope(
            arguments,
            fallback_ids=packet.active_datasource_ids,
            fallback_fields=packet.active_fields_by_datasource,
        )
        return _next(
            packet,
            phase=RuntimePhase.PROBE,
            focus=focus,
            resume_phase=resume_phase,
            resume_focus=packet.focus,
            active_datasource_ids=requested_ids,
            active_fields_by_datasource=requested_fields,
            resume_datasource_ids=packet.active_datasource_ids,
            resume_fields_by_datasource=packet.active_fields_by_datasource,
            reason="technical_unknown_requires_probe",
            **common,
        )
    if tool_name == "probe_analysis_data":
        diagnostics = result.get("diagnostics")
        violation = (
            str(diagnostics.get("violated_contract") or "")
            if isinstance(diagnostics, dict) else ""
        )
        if violation == "probe_not_authorized_for_current_phase":
            # A provider may emit a tool from a stale schema after Probe has
            # already resumed its caller. The rejected call performed no
            # analytical work and must not reopen Probe repair.
            return _next(
                packet,
                phase=(
                    packet.phase
                    if packet.phase in {RuntimePhase.IMPLEMENT, RuntimePhase.REPAIR}
                    else RuntimePhase.IMPLEMENT
                ),
                latest_diagnostic={
                    "error_type": result.get("error_type"),
                    "violated_contract": violation,
                    "call_id": str(tool_call_id or ""),
                },
                reason="stale_probe_call_rejected",
                **common,
            )
        terminal_failure = (
            diagnostics.get("_terminal_probe_failure")
            if isinstance(diagnostics, dict) else None
        )
        if isinstance(terminal_failure, dict):
            # Every executed Probe path reached the data boundary and proved
            # unavailable. There is no analytical fact for the model to repair.
            return _next(
                packet,
                phase=RuntimePhase.BLOCKED,
                resume_phase=None,
                resume_focus="",
                resume_datasource_ids=(),
                resume_fields_by_datasource={},
                probe_revision=packet.probe_revision + 1,
                diagnostic_revision=packet.diagnostic_revision + 1,
                latest_diagnostic={
                    "error_type": "data_source_unavailable",
                    "probe_failure": dict(terminal_failure),
                    "call_id": str(tool_call_id or ""),
                },
                reason="probe_paths_unavailable",
                **common,
            )
        ledger = diagnostics.get("probe_fact_ledger") if isinstance(diagnostics, dict) else None
        if status in {"success", "observation"} and isinstance(ledger, dict):
            return _next(
                packet,
                phase=packet.resume_phase or RuntimePhase.IMPLEMENT,
                focus=packet.resume_focus or packet.focus,
                resume_phase=None,
                resume_focus="",
                active_datasource_ids=packet.resume_datasource_ids or packet.active_datasource_ids,
                active_fields_by_datasource=(
                    packet.resume_fields_by_datasource or packet.active_fields_by_datasource
                ),
                resume_datasource_ids=(),
                resume_fields_by_datasource={},
                probe_revision=packet.probe_revision + 1,
                probe_ledgers=(*packet.probe_ledgers, dict(ledger)),
                latest_diagnostic={},
                reason="probe_observation_captured",
                **common,
            )
        return _next(
            packet,
            phase=RuntimePhase.PROBE_REPAIR,
            focus=focus,
            probe_revision=packet.probe_revision + 1,
            diagnostic_revision=packet.diagnostic_revision + 1,
            latest_diagnostic={
                "error_type": result.get("error_type") or result.get("retry_type"),
                "call_id": str(tool_call_id or ""),
            },
            reason="probe_requires_repair",
            **common,
        )
    if tool_name in {"submit_analysis_program", "execute_python"}:
        execution_id = _positive_int(result.get("execution_id"))
        if status == "success":
            return _next(
                packet,
                # A successful structured execution has already passed the
                # runtime's deterministic result contract. Do not turn every
                # success into a speculative model review: that made the
                # normal one-pass path rewrite valid programs and obscured
                # the actual execution/Probe repair loop.
                phase=RuntimePhase.COMPLETED,
                focus=focus,
                resume_phase=None,
                program_revision=packet.program_revision + 1,
                active_program_call_id=str(tool_call_id or ""),
                active_execution_id=execution_id,
                pending_review_patch={},
                latest_diagnostic={},
                reason="formal_result_accepted",
                **common,
            )
        diagnostics = result.get("diagnostics")
        violation = (
            str(diagnostics.get("violated_contract") or "")
            if isinstance(diagnostics, dict) else ""
        )
        requires_probe = violation == "formal_tool_received_probe_program"
        # A formal tool receiving Probe source is a mode error, not itself
        # proof that another Probe is needed. Before any successful Probe, it
        # is safe to enter the initial observation pass. Once an observation
        # is already in the work packet, returning to Probe would let a model
        # repeat the same inspection with cosmetically different code. Keep
        # the next turn in formal implementation so it must consume the fact
        # ledger and submit an end-to-end analysis program instead.
        probe_already_available = bool(packet.probe_ledgers or packet.probe_revision > 0)
        enter_probe = requires_probe and not probe_already_available
        resume_phase = (
            packet.phase
            if packet.phase in {RuntimePhase.IMPLEMENT, RuntimePhase.REPAIR}
            else RuntimePhase.IMPLEMENT
        )
        return _next(
            packet,
            phase=(
                RuntimePhase.PROBE if enter_probe
                else RuntimePhase.IMPLEMENT if requires_probe
                else RuntimePhase.REPAIR
            ),
            focus=focus,
            resume_phase=resume_phase if enter_probe else None,
            resume_focus=packet.focus if enter_probe else "",
            program_revision=packet.program_revision + 1,
            diagnostic_revision=packet.diagnostic_revision + 1,
            active_program_call_id=str(tool_call_id or ""),
            active_execution_id=execution_id,
            pending_review_patch=(
                {} if execution_id > 0 else packet.pending_review_patch
            ),
            latest_diagnostic={
                "error_type": result.get("error_type") or result.get("retry_type"),
                "violated_contract": violation,
                "call_id": str(tool_call_id or ""),
            },
            reason=(
                "formal_program_requires_probe" if enter_probe
                else "formal_program_must_reuse_probe_facts" if requires_probe
                else "formal_program_requires_repair"
            ),
            **common,
        )
    if tool_name == "request_analysis_revision" and status == "revision_required":
        revision_work_packet = result.get("revision_work_packet")
        bounded_review_patch = (
            dict(revision_work_packet)
            if isinstance(revision_work_packet, dict)
            and bool(revision_work_packet.get("patch_mode_allowed"))
            else {}
        )
        return _next(
            packet,
            phase=RuntimePhase.REPAIR,
            focus=focus,
            diagnostic_revision=packet.diagnostic_revision + 1,
            pending_review_patch=bounded_review_patch,
            latest_diagnostic={
                "error_type": "review_revision",
                "call_id": str(tool_call_id or ""),
                "revision_work_packet": dict(revision_work_packet or {}),
            },
            reason="candidate_requires_revision",
            **common,
        )
    if tool_name == "accept_analysis_result" and status == "success":
        return _next(
            packet,
            phase=RuntimePhase.COMPLETED,
            focus=focus,
            resume_phase=None,
            resume_focus="",
            resume_datasource_ids=(),
            resume_fields_by_datasource={},
            latest_diagnostic={},
            reason="candidate_accepted",
            **common,
        )
    if tool_name == "request_analysis_clarification" and status == "clarification_required":
        return _next(
            packet,
            phase=RuntimePhase.WAITING_CLARIFICATION,
            focus=focus,
            resume_phase=RuntimePhase.PLAN,
            resume_focus=packet.focus,
            reason="user_clarification_required",
            **common,
        )
    return packet


def resume_runtime_work_packet(
    packet: RuntimeWorkPacket,
    *,
    focus: str = "",
) -> RuntimeWorkPacket:
    if packet.phase is not RuntimePhase.WAITING_CLARIFICATION:
        return packet
    return _next(
        packet,
        phase=packet.resume_phase or RuntimePhase.PLAN,
        resume_phase=None,
        resume_focus="",
        goal_revision=packet.goal_revision + 1,
        focus=str(focus or packet.focus).strip(),
        latest_diagnostic={},
        reason="clarification_answered",
    )


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _material_scope_from_units(
    units: Any,
) -> tuple[tuple[int, ...], dict[str, tuple[str, ...]]]:
    datasource_ids: list[int] = []
    fields: dict[str, tuple[str, ...]] = {}
    for item in units or ():
        if not isinstance(item, dict):
            continue
        datasource_id = _positive_int(item.get("datasource_id"))
        if datasource_id <= 0:
            continue
        if datasource_id not in datasource_ids:
            datasource_ids.append(datasource_id)
        fields[str(datasource_id)] = tuple(dict.fromkeys(
            str(value).strip()
            for value in item.get("field_names") or ()
            if str(value).strip()
        ))
    return tuple(datasource_ids), fields


def _requested_material_scope(
    arguments: dict[str, Any],
    *,
    fallback_ids: tuple[int, ...],
    fallback_fields: dict[str, tuple[str, ...]],
) -> tuple[tuple[int, ...], dict[str, tuple[str, ...]]]:
    requested_ids = tuple(dict.fromkeys(
        parsed
        for value in arguments.get("datasource_ids") or ()
        for parsed in (_positive_int(value),)
        if parsed > 0
    ))
    requested_fields = {
        str(key): tuple(dict.fromkeys(
            str(value).strip() for value in values or () if str(value).strip()
        ))
        for key, values in (arguments.get("field_names_by_datasource") or {}).items()
        if isinstance(values, (list, tuple))
    }
    return (
        requested_ids or fallback_ids,
        requested_fields or dict(fallback_fields),
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
            phase=RuntimePhase.COMPLETED,
            program_revision=workspace.program_revision + 1,
            latest_program=dict(program),
            latest_diagnostic={},
            reason="formal_program_accepted",
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
