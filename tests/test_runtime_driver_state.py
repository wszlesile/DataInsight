import pytest

from agent.runtime_driver_state import (
    RuntimePhase,
    accept_program_result,
    new_workspace,
    record_material_manifest,
    record_probe_result,
    record_program_result,
    record_schema_requirements,
    request_clarification,
    request_program_revision,
    resume_after_clarification,
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

    candidate = record_program_result(failed, {"status": "success"}, program={"code": "fixed"})
    assert candidate.phase is RuntimePhase.REVIEW
    assert candidate.latest_program == {"code": "fixed"}
    assert candidate.latest_diagnostic == {}

    revision = request_program_revision(candidate, {"issues": [{"issue_type": "calculation"}]})
    assert revision.phase is RuntimePhase.REPAIR
    assert revision.latest_program == {"code": "fixed"}

    reviewed = record_program_result(revision, {"status": "success"}, program={"code": "fixed again"})
    completed = accept_program_result(reviewed)
    assert completed.phase is RuntimePhase.COMPLETED


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
