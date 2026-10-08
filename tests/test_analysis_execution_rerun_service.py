import sys
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import Base  # noqa: E402
from model import (  # noqa: E402
    InsightNsArtifact,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsExecutionEvidence,
    InsightNsTurn,
)
from service.analysis_execution_rerun_service import AnalysisExecutionRerunService  # noqa: E402
from utils.datasource_utils import dump_json  # noqa: E402


def test_local_rerun_does_not_compile_or_persist_evidence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        conversation = InsightNsConversation(insight_namespace_id=6, title="Annual contracts")
        session.add(conversation)
        session.flush()
        turn = InsightNsTurn(
            conversation_id=conversation.id,
            turn_no=1,
            user_query="Analyze annual contracts",
            status="success",
        )
        session.add(turn)
        session.flush()
        original = InsightNsExecution(
            conversation_id=conversation.id,
            turn_id=turn.id,
            generated_code="result = save_analysis_result(analysis_report='ok')",
            execution_status="success",
            analysis_report="Original report",
            result_payload_json=dump_json({'semantic_execution_coverage': {'results': [{
                'question_id': 'q1', 'result_id': 'contracts', 'execution_token': 'old'}]}}),
            analysis_parameters_schema_json=dump_json({
                "parameters": [],
                "defaults": {"limit": 5},
            }),
            analysis_parameters_json=dump_json({"limit": 5}),
        )
        session.add(original)
        session.commit()

        def run_locally(**kwargs):
            assert kwargs['runtime_user_context']['semantic_delivery_requirements'] == [
                {'question_id': 'q1', 'result_id': 'contracts'}]
            execution = InsightNsExecution(
                conversation_id=conversation.id,
                turn_id=turn.id,
                generated_code=kwargs["code"],
                execution_status="success",
                analysis_report="Rerun report: 8 contracts",
                result_payload_json=dump_json({"query_snapshots": {"contracts": {}}}),
                analysis_parameters_schema_json=dump_json({
                    "parameters": [],
                    "defaults": {"limit": 5},
                }),
                analysis_parameters_json=dump_json(kwargs["analysis_parameters"]),
                parent_execution_id=original.id,
                execution_source="manual_param_rerun",
                analysis_task_id=kwargs["analysis_task_id"],
            )
            session.add(execution)
            session.flush()
            return {
                "execution_id": execution.id,
                "status": "success",
                "analysis_report": execution.analysis_report,
                "charts": [{"title": "Contracts", "chart_type": "bar"}],
                "tables": [{"title": "Contract details", "columns": [], "rows": []}],
            }

        with (
            patch(
                "service.analysis_execution_rerun_service.run_generated_code_locally",
                side_effect=run_locally,
            ),
            patch(
                "service.post_analysis_evidence_service.PostAnalysisEvidenceCompiler.compile",
                side_effect=AssertionError("local rerun must not compile Evidence"),
            ),
        ):
            result = AnalysisExecutionRerunService(session).rerun_execution(
                username="tester",
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=original.id,
                analysis_parameters={"limit": 8},
            )

        assert result["status"] == "success"
        assert session.query(InsightNsArtifact).count() == 3
        assert session.query(InsightNsExecutionEvidence).count() == 0
    finally:
        session.close()
