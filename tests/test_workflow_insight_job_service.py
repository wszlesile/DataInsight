from pathlib import Path
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import Base
from model import InsightNsExecution, InsightNsTurn, InsightWorkflowJob
from service.workflow_insight_job_service import WorkflowInsightJobService


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _job(session, *, output_contract):
    turn = InsightNsTurn(
        conversation_id=7,
        turn_no=1,
        user_query="分析风险",
        selected_datasource_ids_json="[11]",
        selected_datasource_snapshot_json="[]",
        used_datasource_ids_json="[11]",
        final_answer="",
        status="success",
        error_message="",
        is_deleted=0,
    )
    session.add(turn)
    session.flush()
    job = InsightWorkflowJob(
        job_id="job-1",
        username="alice",
        namespace_id=3,
        conversation_id=7,
        turn_id=turn.id,
        analysis_task_id=5,
        analysis_task_public_id="task-1",
        analysis_question="分析风险",
        uns_node_ids_json='["uns-1"]',
        output_contract_json=json.dumps(output_contract),
        analysis_preferences="",
        status="success",
        idempotency_key="",
        request_id="request-1",
        request_fingerprint="fingerprint",
        result_json="{}",
        error_message="",
    )
    session.add(job)
    session.flush()
    return job, turn


def test_json_workflow_result_returns_original_json_payload():
    session = _session()
    try:
        contract = {
            "type": "json",
            "schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
        }
        job, turn = _job(session, output_contract=contract)
        session.add(InsightNsExecution(
            conversation_id=7,
            turn_id=turn.id,
            execution_status="success",
            analysis_report='{"count": 2}',
            result_payload_json=json.dumps({
                "analysis_report": '{"count": 2}',
                "charts": [],
                "tables": [],
                "output_format": "json",
                "output_payload": {"count": 2},
            }),
            used_datasource_ids_json="[11]",
            is_deleted=0,
            content_enabled=1,
        ))
        session.flush()

        result = WorkflowInsightJobService(session)._build_result(job)

        assert result["data"] == {"count": 2}
        assert "used_datasource_ids" not in result
        assert "execution_id" not in result
    finally:
        session.close()


def test_text_workflow_result_returns_content_not_artifacts():
    session = _session()
    try:
        job, turn = _job(session, output_contract={"type": "text"})
        session.add(InsightNsExecution(
            conversation_id=7,
            turn_id=turn.id,
            execution_status="success",
            analysis_report="风险集中在甲产品。",
            result_payload_json=json.dumps({
                "analysis_report": "风险集中在甲产品。",
                "charts": [],
                "tables": [],
                "output_format": "text",
                "output_payload": "风险集中在甲产品。",
            }, ensure_ascii=False),
            used_datasource_ids_json="[11]",
            is_deleted=0,
            content_enabled=1,
        ))
        session.flush()

        result = WorkflowInsightJobService(session)._build_result(job)

        assert result["content"] == "风险集中在甲产品。"
        assert "charts" not in result
    finally:
        session.close()


def test_workflow_request_rejects_non_array_uns_scope_before_materialization():
    session = _session()
    try:
        with pytest.raises(ValueError, match="uns_node_ids 必须是数组"):
            WorkflowInsightJobService(session).submit(
                username="alice",
                analysis_question="分析风险",
                uns_node_ids="uns-1",
                output_format={"type": "text"},
                analysis_preferences="",
                platform_authorization="Bearer token",
                fedquery_authorization="Bearer jwt",
                selected_llm_model_id="",
                database_conn_info={},
            )
    finally:
        session.close()
