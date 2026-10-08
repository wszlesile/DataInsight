from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.database import Base  # noqa: E402
from model import (  # noqa: E402
    InsightNamespace,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsExecutionEvidence,
    InsightNsTurn,
)
from service import evidence_task_runner  # noqa: E402
from service.evidence_task_runner import EvidenceTaskRunner  # noqa: E402


def test_runner_claims_queued_evidence_and_invokes_existing_assembler(monkeypatch):
    candidate = MagicMock(
        id=4,
        execution_id=91,
        compilation_status="queued",
        conversation_id=1,
        turn_id=2,
    )
    evidence = MagicMock(
        execution_id=91,
        compilation_status="running",
        conversation_id=1,
        turn_id=2,
    )
    execution = MagicMock(id=91)
    artifact_query = MagicMock()
    artifact_query.filter.return_value.order_by.return_value.all.return_value = []

    candidate_query = MagicMock()
    candidate_query.filter.return_value.order_by.return_value.first.return_value = candidate
    claim_query = MagicMock()
    claim_query.filter.return_value.update.return_value = 1
    evidence_query = MagicMock()
    evidence_query.filter.return_value.first.return_value = evidence
    execution_query = MagicMock()
    execution_query.filter.return_value.first.return_value = execution
    session = MagicMock()
    session.query.side_effect = [
        candidate_query,
        claim_query,
        evidence_query,
        execution_query,
        artifact_query,
    ]

    @contextmanager
    def session_scope(**_kwargs):
        yield session

    monkeypatch.setattr(evidence_task_runner, "session_scope", session_scope)
    assemble = MagicMock()
    monkeypatch.setattr(
        evidence_task_runner.EvidenceAssemblyService,
        "assemble_for_execution",
        assemble,
    )

    assert EvidenceTaskRunner().run_once() is True
    assert claim_query.filter.return_value.update.call_count == 1
    assemble.assert_called_once_with(
        conversation_id=1,
        turn_id=2,
        execution=execution,
        artifacts=[],
    )
    assert session.commit.call_count == 2


def test_runner_persists_terminal_state_with_database_claim(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    session.add(InsightNamespace(id=1, username="alice", name="test", namespace_kind="interactive"))
    session.add(InsightNsConversation(
        id=2,
        insight_namespace_id=1,
        title="test",
        status="active",
        summary_text="",
        active_datasource_snapshot="{}",
        last_turn_no=1,
        user_message="query",
        insight_result="",
    ))
    session.add(InsightNsTurn(
        id=3,
        conversation_id=2,
        turn_no=1,
        user_query="query",
        selected_datasource_ids_json="[]",
        selected_datasource_snapshot_json="[]",
        final_answer="report",
        status="success",
        error_message="",
    ))
    session.add(InsightNsExecution(
        id=4,
        conversation_id=2,
        turn_id=3,
        generated_code="result = {}",
        execution_status="success",
        result_payload_json="{}",
        content_enabled=1,
    ))
    session.add(InsightNsExecutionEvidence(
        conversation_id=2,
        turn_id=3,
        execution_id=4,
        compilation_status="queued",
        diagnostics_json="{}",
        query_snapshots_json="{}",
        evidence_map_json="{}",
        is_deleted=0,
    ))
    session.commit()
    session.close()

    @contextmanager
    def session_scope(*, expire_on_commit=None):
        worker_session = session_factory()
        try:
            yield worker_session
        except Exception:
            worker_session.rollback()
            raise
        finally:
            worker_session.close()

    monkeypatch.setattr(evidence_task_runner, "session_scope", session_scope)
    monkeypatch.setattr(
        "service.evidence_assembly_service.PostAnalysisEvidenceCompiler.compile",
        lambda _self, _snapshot: {
            "status": "complete",
            "diagnostics": {},
            "evidence_definitions": [],
            "chart_trace_refs": [],
            "table_trace_refs": [],
            "report_trace_refs": [],
            "query_snapshots": {},
        },
    )

    assert EvidenceTaskRunner().run_once() is True

    check = session_factory()
    row = check.query(InsightNsExecutionEvidence).filter_by(execution_id=4).one()
    assert row.compilation_status == "complete"
    assert row.evidence_map_json == "{}"
    check.close()
    engine.dispose()
