import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from agent.tools import (  # noqa: E402
    CURRENT_QUERY_SNAPSHOTS,
    StructuredResult,
    _validate_evidence_contract,
    build_report_result,
    build_table_result,
    save_analysis_result,
    validate_query_constraints,
)
from model import InsightNsArtifact, InsightNsExecution, InsightNsExecutionEvidence, InsightNsTurn  # noqa: E402
from config.database import Base  # noqa: E402
from service.evidence_assembly_service import EvidenceAssemblyService  # noqa: E402


def test_build_report_result_cleans_trace_tags_and_generates_refs():
    result = build_report_result("完工率 [[trace:evidence_key=completion_rate]]76.3%[[/trace]]。")

    assert result["analysis_report"] == "完工率 76.3%。"
    ref = result["report_trace_refs"][0]
    assert result["analysis_report"][ref["start"]:ref["end"]] == "76.3%"
    assert ref["evidence_key"] == "completion_rate"


def test_build_table_result_splits_display_table_and_trace_metadata():
    result = build_table_result(
        title="区域完工率",
        columns=[
            {"key": "region", "label": "区域", "role": "dimension"},
            {"key": "completion_rate", "label": "完工率", "role": "metric", "value_type": "percentage", "unit": "%"},
        ],
        rows=[{"region": "华东", "completion_rate": 76.3}],
    )

    assert result["table"]["columns"] == [
        {"key": "region", "label": "区域"},
        {"key": "completion_rate", "label": "完工率"},
    ]
    assert result["trace_metadata"]["column_roles"]["completion_rate"] == "metric"
    assert result["trace_metadata"]["column_units"]["completion_rate"] == "%"


def test_save_analysis_result_injects_query_snapshots():
    CURRENT_QUERY_SNAPSHOTS.set({})
    retry = validate_query_constraints(
        query_key="q_completion",
        query_constraints={"time_range": {}, "target_filters": [], "aggregation": {"grain": "month"}},
        sql="select month, sum(v) from t group by month",
        aggregation_hints=["按月份聚合"],
    )

    assert retry is None
    result = save_analysis_result(
        analysis_report="## 分析报告\n\n- 完工率 76.3%。",
        charts=[
            {
                "title": "完工率",
                "description": "按月统计",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [{"month": "2026-01", "completion_rate": 76.3}]},
                    "encoding": {"category_field": "month", "value_field": "completion_rate"},
                },
            }
        ],
        evidence_definitions=[
            {
                "evidence_key": "completion_rate",
                "metric_name": "完工率",
                "value_type": "percentage",
                "unit": "%",
                "query_refs": ["q_completion"],
                "derivation": {"mode": "query_output"},
                "calculation": {"metric_fields": ["completion_rate"]},
            }
        ],
        chart_trace_refs=[
            {"evidence_key": "completion_rate", "chart_index": 0, "value_field": "completion_rate"}
        ],
    )

    assert "q_completion" in result.query_snapshots


def _base_evidence_result(**overrides):
    analysis_report = "## 分析报告\n\n- 完工率 76.3%。"
    report_value_start = analysis_report.index("76.3%")
    payload = {
        "analysis_report": analysis_report,
        "charts": [
            {
                "title": "完工率",
                "description": "按月统计",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {
                        "columns": ["month", "completion_rate"],
                        "rows": [{"month": "2026-01", "completion_rate": 76.3}],
                    },
                    "encoding": {"category_field": "month", "value_field": "completion_rate"},
                },
            }
        ],
        "tables": [],
        "evidence_definitions": [
            {
                "evidence_key": "completion_rate",
                "metric_name": "完工率",
                "value_type": "percentage",
                "unit": "%",
                "query_refs": ["q_completion"],
                "derivation": {"mode": "query_output"},
                "calculation": {"metric_fields": ["completion_rate"]},
            }
        ],
        "chart_trace_refs": [
            {"evidence_key": "completion_rate", "chart_index": 0, "value_field": "completion_rate"}
        ],
        "report_trace_refs": [
            {
                "evidence_key": "completion_rate",
                "start": report_value_start,
                "end": report_value_start + len("76.3%"),
                "text": "76.3%",
            }
        ],
        "query_snapshots": {"q_completion": {"query_key": "q_completion"}},
    }
    payload.update(overrides)
    return StructuredResult(**payload)


def test_chart_expected_refs_require_trace_ref():
    result = _base_evidence_result(chart_trace_refs=[])

    retry = _validate_evidence_contract(result)

    assert retry is not None
    assert retry.retry_type == "invalid_chart_trace_ref"


def test_missing_evidence_payload_retries_when_result_has_artifacts():
    result = StructuredResult(
        analysis_report="## 分析报告\n\n- 完工率 76.3%。",
        charts=[
            {
                "title": "完工率",
                "chart_document": {
                    "chart_kind": "bar",
                    "dataset": {"rows": [{"month": "2026-01", "completion_rate": 76.3}]},
                    "encoding": {"category_field": "month", "value_field": "completion_rate"},
                },
            }
        ],
        tables=[],
    )

    retry = _validate_evidence_contract(result)

    assert retry is not None
    assert retry.retry_type == "missing_evidence_contract"


def test_numeric_report_requires_report_trace_refs():
    result = _base_evidence_result(report_trace_refs=[])

    retry = _validate_evidence_contract(result)

    assert retry is not None
    assert retry.retry_type == "invalid_report_trace_ref"


def test_table_metric_columns_require_trace_ref():
    result = _base_evidence_result(
        charts=[],
        tables=[
            {
                "title": "表格",
                "description": "",
                "columns": [{"key": "completion_rate", "label": "完工率"}],
                "rows": [{"completion_rate": 76.3}],
            }
        ],
        chart_trace_refs=[],
        table_trace_refs=[],
        table_trace_metadata={"column_roles": {"completion_rate": "metric"}},
    )

    retry = _validate_evidence_contract(result)

    assert retry is not None
    assert retry.retry_type == "invalid_table_trace_ref"


def test_intermediate_outputs_reject_large_detail_payload():
    evidence = dict(_base_evidence_result().evidence_definitions[0])
    evidence["intermediate_outputs"] = [{"rows": [{"a": i} for i in range(30)]}]
    result = _base_evidence_result(evidence_definitions=[evidence])

    retry = _validate_evidence_contract(result)

    assert retry is not None
    assert retry.retry_type == "invalid_evidence_definition"


def test_limit_requires_quality_note_disclosure():
    result = _base_evidence_result()

    retry = _validate_evidence_contract(result, generated_code="df = df.head(10)")

    assert retry is not None
    assert retry.retry_type == "invalid_evidence_definition"


def test_evidence_assembly_writes_bundle_and_artifact_refs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db_session = Session()
    turn = InsightNsTurn(
        conversation_id=1,
        turn_no=1,
        user_query="分析完工率",
        selected_datasource_snapshot_json='[{"datasource_id": 12, "datasource_name": "工单表", "datasource_type": "table", "datasource_identifier": "work_order"}]',
    )
    db_session.add(turn)
    db_session.flush()
    execution = InsightNsExecution(
        conversation_id=1,
        turn_id=turn.id,
        tool_call_id="tool",
        generated_code="",
        used_datasource_ids_json="[12]",
        execution_status="success",
        result_payload_json='{"evidence_definitions":[{"evidence_key":"completion_rate","metric_name":"完工率","value_type":"percentage","unit":"%","query_refs":["q1"],"derivation":{"mode":"query_output"},"calculation":{"metric_fields":["completion_rate"]}}],"chart_trace_refs":[{"evidence_key":"completion_rate","chart_index":0,"value_field":"completion_rate"}],"query_snapshots":{"q1":{"implementation":{"sql_context":{"fragments":[{"datasource_identifier":"work_order"}]}}}}}',
    )
    db_session.add(execution)
    db_session.flush()
    artifact = InsightNsArtifact(
        conversation_id=1,
        turn_id=turn.id,
        execution_id=execution.id,
        artifact_type="chart",
        title="图表",
        content_json="{}",
        metadata_json="{}",
        sort_no=1,
    )
    db_session.add(artifact)
    db_session.flush()

    EvidenceAssemblyService(db_session).assemble_for_execution(
        conversation_id=1,
        turn_id=turn.id,
        execution=execution,
        artifacts=[artifact.to_dict()],
    )
    db_session.flush()

    db_session.refresh(artifact)
    metadata = json.loads(artifact.metadata_json)
    assert metadata["evidence_refs"]["chart_trace_refs"][0]["evidence_id"].startswith(f"ev_{execution.id}_")
    bundle = db_session.query(InsightNsExecutionEvidence).filter(
        InsightNsExecutionEvidence.execution_id == execution.id
    ).first()
    evidence_map = json.loads(bundle.evidence_map_json)
    source_refs = next(iter(evidence_map.values()))["source_refs"]
    assert "metadata_schema" not in source_refs[0]
    assert source_refs[0]["field_names"] == ["completion_rate"]
