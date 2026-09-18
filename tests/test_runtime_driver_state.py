from dataclasses import replace

import pytest

from agent.runtime_driver_state import (
    RuntimePhase,
    accept_program_result,
    new_runtime_work_packet,
    new_workspace,
    record_material_manifest,
    record_probe_result,
    record_program_result,
    record_schema_requirements,
    request_clarification,
    request_program_revision,
    reduce_runtime_tool_result,
    resume_after_clarification,
    resume_runtime_work_packet,
    runtime_work_packet_from_dict,
)


def manifest(*, probe_required=False):
    return {
        "focus": "Assess product profitability.",
        "material_units": [{"datasource_id": 12, "field_names": ["amount"]}],
        "probe_required": probe_required,
    }


def test_materialization_is_the_only_phase_that_can_expand_schema_requirements():
    workspace = record_schema_requirements(new_workspace(workspace_id="w"), {"12": ["amount"]})
    assert workspace.phase is RuntimePhase.MATERIALIZE
    assert workspace.schema_requirements == {"12": ("amount",)}

    implemented = record_material_manifest(workspace, manifest())
    with pytest.raises(ValueError, match="materialization"):
        record_schema_requirements(implemented, {"12": ["status"]})


def test_material_manifest_locally_selects_implementation_or_probe():
    assert record_material_manifest(new_workspace(), manifest()).phase is RuntimePhase.IMPLEMENT
    assert record_material_manifest(new_workspace(), manifest(probe_required=True)).phase is RuntimePhase.PROBE


def test_probe_only_advances_on_structured_fact_ledger():
    probing = record_material_manifest(new_workspace(), manifest(probe_required=True))
    unresolved = record_probe_result(probing, {"status": "failed", "error_type": "query_error"})
    assert unresolved.phase is RuntimePhase.PROBE
    assert unresolved.latest_diagnostic["error_type"] == "query_error"

    observed = record_probe_result(unresolved, {
        "status": "observation",
        "diagnostics": {"probe_fact_ledger": {"confirmed_facts": [{"fact_id": "f1"}]}},
    })
    assert observed.phase is RuntimePhase.IMPLEMENT
    assert observed.probe_ledgers[-1]["confirmed_facts"][0]["fact_id"] == "f1"


def test_formal_failure_keeps_only_latest_program_and_diagnostic_for_repair():
    implementing = record_material_manifest(new_workspace(), manifest())
    failed = record_program_result(
        implementing,
        {"status": "failed", "error_type": "contract_error", "diagnostics": {"field": "amount"}},
        program={"code": "first"},
    )
    assert failed.phase is RuntimePhase.REPAIR
    assert failed.latest_program == {"code": "first"}
    assert failed.latest_diagnostic["error_type"] == "contract_error"

    completed = record_program_result(failed, {"status": "success"}, program={"code": "fixed"})
    assert completed.phase is RuntimePhase.COMPLETED
    assert completed.latest_program == {"code": "fixed"}
    assert completed.latest_diagnostic == {}


def test_clarification_resets_materialization_without_discarding_probe_audit():
    probing = record_material_manifest(new_workspace(), manifest(probe_required=True))
    observed = record_probe_result(probing, {
        "status": "observation",
        "diagnostics": {"probe_fact_ledger": {"confirmed_facts": [{"fact_id": "f1"}]}},
    })
    waiting = request_clarification(observed)
    resumed = resume_after_clarification(waiting)
    assert resumed.phase is RuntimePhase.MATERIALIZE
    assert resumed.goal_revision == 2
    assert resumed.material_manifest == {}
    assert resumed.probe_ledgers == observed.probe_ledgers


def test_runtime_work_packet_owns_the_complete_action_lifecycle():
    packet = new_runtime_work_packet(workspace_id="conversation:turn:task", focus="Analyze")
    assert packet.phase is RuntimePhase.UNDERSTAND

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="select_runtime_work",
        tool_call_id="select",
        arguments={"work": "plan"},
        result={"status": "success", "work": "plan"},
    )
    assert packet.phase is RuntimePhase.PLAN

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="plan_runtime_materials",
        tool_call_id="plan",
        arguments={},
        result={
            "status": "success",
            "focus": "Analyze",
            "material_units": [{"datasource_id": 12, "field_names": ["amount"]}],
            "probe_required": False,
        },
    )
    assert packet.phase is RuntimePhase.IMPLEMENT
    assert packet.material_revision == 1

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="submit_analysis_program",
        tool_call_id="program-1",
        arguments={},
        result={"status": "failed", "error_type": "contract_error", "execution_id": 31},
    )
    assert packet.phase is RuntimePhase.REPAIR
    assert packet.active_execution_id == 31

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="request_runtime_probe",
        tool_call_id="probe-request",
        arguments={"focus": "Confirm stored enum"},
        result={"status": "success", "focus": "Confirm stored enum"},
    )
    assert packet.phase is RuntimePhase.PROBE
    assert packet.resume_phase is RuntimePhase.REPAIR
    assert packet.focus == "Confirm stored enum"
    assert packet.resume_focus == "Analyze"

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="probe_analysis_data",
        tool_call_id="probe-1",
        arguments={},
        result={
            "status": "observation",
            "diagnostics": {"probe_fact_ledger": {"confirmed_facts": []}},
        },
    )
    assert packet.phase is RuntimePhase.REPAIR
    assert packet.resume_phase is None
    assert packet.focus == "Analyze"
    assert packet.resume_focus == ""


def test_runtime_work_packet_round_trip_and_clarification_resume_are_lossless():
    packet = new_runtime_work_packet(workspace_id="w", focus="Analyze")
    waiting = reduce_runtime_tool_result(
        packet,
        tool_name="request_analysis_clarification",
        tool_call_id="clarify",
        arguments={},
        result={"status": "clarification_required"},
    )
    restored = runtime_work_packet_from_dict(waiting.to_dict())
    assert restored == waiting
    assert restored.phase is RuntimePhase.WAITING_CLARIFICATION

    resumed = resume_runtime_work_packet(restored)
    assert resumed.phase is RuntimePhase.PLAN
    assert resumed.goal_revision == waiting.goal_revision + 1


def test_material_and_probe_scope_return_with_the_parent_focus():
    packet = new_runtime_work_packet(workspace_id="w", focus="Analyze all products")
    packet = reduce_runtime_tool_result(
        packet,
        tool_name="plan_runtime_materials",
        tool_call_id="plan",
        arguments={},
        result={
            "status": "success",
            "focus": "Analyze all products",
            "probe_required": True,
            "probe_focus": "Confirm status values",
            "material_units": [{
                "datasource_id": 12,
                "field_names": ["product", "status", "amount"],
            }],
        },
    )
    assert packet.phase is RuntimePhase.PROBE
    assert packet.focus == "Confirm status values"
    assert packet.resume_focus == "Analyze all products"
    assert packet.active_datasource_ids == (12,)

    packet = reduce_runtime_tool_result(
        packet,
        tool_name="probe_analysis_data",
        tool_call_id="probe",
        arguments={},
        result={
            "status": "observation",
            "diagnostics": {"probe_fact_ledger": {}},
        },
    )
    assert packet.phase is RuntimePhase.IMPLEMENT
    assert packet.focus == "Analyze all products"
    assert packet.active_fields_by_datasource == {
        "12": ("product", "status", "amount"),
    }
    assert packet.probe_ledgers == ({},)


def test_all_unavailable_probe_paths_block_runtime_instead_of_entering_implementation():
    packet = reduce_runtime_tool_result(
        new_runtime_work_packet(workspace_id="w", focus="Analyze"),
        tool_name="plan_runtime_materials",
        tool_call_id="plan",
        arguments={},
        result={
            "status": "success",
            "focus": "Analyze",
            "probe_required": True,
            "material_units": [{"datasource_id": 12, "field_names": ["amount"]}],
        },
    )

    blocked = reduce_runtime_tool_result(
        packet,
        tool_name="probe_analysis_data",
        tool_call_id="probe",
        arguments={},
        result={
            "status": "observation",
            "diagnostics": {
                "_terminal_probe_failure": {
                    "kind": "all_required_query_paths_unavailable",
                    "query_count": 1,
                },
            },
        },
    )

    assert blocked.phase is RuntimePhase.BLOCKED
    assert blocked.reason == "probe_paths_unavailable"
    assert blocked.latest_diagnostic["error_type"] == "data_source_unavailable"


def test_formal_probe_program_only_enters_initial_probe_once():
    packet = reduce_runtime_tool_result(
        new_runtime_work_packet(workspace_id="w", focus="Analyze"),
        tool_name="plan_runtime_materials",
        tool_call_id="plan",
        arguments={},
        result={
            "status": "success",
            "focus": "Analyze",
            "material_units": [{"datasource_id": 12, "field_names": ["status"]}],
            "probe_required": False,
        },
    )
    first = reduce_runtime_tool_result(
        packet,
        tool_name="submit_analysis_program",
        tool_call_id="formal-probe-1",
        arguments={},
        result={
            "status": "failed",
            "diagnostics": {"violated_contract": "formal_tool_received_probe_program"},
        },
    )
    assert first.phase is RuntimePhase.PROBE

    observed = reduce_runtime_tool_result(
        first,
        tool_name="probe_analysis_data",
        tool_call_id="probe",
        arguments={},
        result={
            "status": "observation",
            "diagnostics": {"probe_fact_ledger": {"confirmed_facts": [{"field": "status"}]}},
        },
    )
    assert observed.phase is RuntimePhase.IMPLEMENT
    assert observed.probe_ledgers

    repeated = reduce_runtime_tool_result(
        observed,
        tool_name="submit_analysis_program",
        tool_call_id="formal-probe-2",
        arguments={},
        result={
            "status": "failed",
            "diagnostics": {"violated_contract": "formal_tool_received_probe_program"},
        },
    )
    assert repeated.phase is RuntimePhase.IMPLEMENT
    assert repeated.reason == "formal_program_must_reuse_probe_facts"


def test_stale_probe_tool_call_does_not_reopen_probe_repair():
    packet = replace(
        new_runtime_work_packet(workspace_id="w", focus="Build the report"),
        phase=RuntimePhase.IMPLEMENT,
        probe_revision=1,
        probe_ledgers=({"confirmed_facts": [{"field": "status"}]},),
    )

    next_packet = reduce_runtime_tool_result(
        packet,
        tool_name="probe_analysis_data",
        tool_call_id="stale-probe",
        arguments={"code": "probe_with_cosmetic_rewrite()"},
        result={
            "status": "failed",
            "error_type": "probe_not_authorized_for_current_phase",
            "diagnostics": {
                "violated_contract": "probe_not_authorized_for_current_phase",
            },
        },
    )

    assert next_packet.phase is RuntimePhase.IMPLEMENT
    assert next_packet.reason == "stale_probe_call_rejected"
    assert next_packet.probe_ledgers == packet.probe_ledgers


def test_review_patch_authorization_survives_rejected_full_program_until_patch_executes():
    packet = reduce_runtime_tool_result(
        new_runtime_work_packet(workspace_id="w", focus="Analyze"),
        tool_name="request_analysis_revision",
        tool_call_id="review",
        arguments={},
        result={
            "status": "revision_required",
            "revision_work_packet": {
                "patch_mode_allowed": True,
                "base_execution_id": 31,
                "allowed_ranges": [{"start_line": 4, "end_line": 4}],
            },
        },
    )
    assert packet.phase is RuntimePhase.REPAIR
    assert packet.pending_review_patch["base_execution_id"] == 31

    rejected = reduce_runtime_tool_result(
        packet,
        tool_name="submit_analysis_program",
        tool_call_id="full-rewrite",
        arguments={"code": "full program"},
        result={
            "status": "failed",
            "diagnostics": {"violated_contract": "review_patch_required"},
        },
    )
    assert rejected.phase is RuntimePhase.REPAIR
    assert rejected.pending_review_patch["base_execution_id"] == 31

    repaired = reduce_runtime_tool_result(
        rejected,
        tool_name="submit_analysis_program",
        tool_call_id="patch",
        arguments={"base_execution_id": 31, "patches": [{"start_line": 4, "end_line": 4}]},
        result={"status": "success", "execution_id": 32},
    )
    assert repaired.phase is RuntimePhase.COMPLETED
    assert repaired.pending_review_patch == {}
