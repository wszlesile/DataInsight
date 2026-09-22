from pathlib import Path
import sys
from types import SimpleNamespace

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import controller.workflow_insight_controller as workflow_controller


def _client():
    app = Flask(__name__)
    app.register_blueprint(workflow_controller.create_workflow_insight_controller())
    return app.test_client()


def test_workflow_submit_returns_real_401_without_user(monkeypatch):
    monkeypatch.setattr(workflow_controller, "get_current_user_context", lambda: None)

    response = _client().post(
        "/api/insight/workflow/jobs",
        json={
            "analysis_question": "分析风险",
            "uns_node_ids": ["1"],
            "output_format": {"type": "text"},
        },
    )

    assert response.status_code == 401
    assert response.get_json()["code"] == 401


def test_workflow_submit_rejects_non_object_body_with_real_400():
    response = _client().post(
        "/api/insight/workflow/jobs",
        json=["not", "an", "object"],
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == 400


def test_workflow_submit_rejects_inconsistent_json_schema_with_real_400(monkeypatch):
    class FakeSession:
        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        workflow_controller,
        "get_current_user_context",
        lambda: SimpleNamespace(
            username="alice",
            database_conn_info=None,
            platform_authorization="Bearer platform",
            fedquery_authorization="Bearer fedquery",
            selected_llm_model_id="",
        ),
    )
    monkeypatch.setattr(workflow_controller, "SessionLocal", FakeSession)

    response = _client().post(
        "/api/insight/workflow/jobs",
        json={
            "analysis_question": "统计生产批次总数",
            "uns_node_ids": ["2066811253765967872"],
            "output_format": {
                "type": "json",
                "schema": {
                    "type": "object",
                    "properties": {
                        "total_batch_count": {
                            "type": "integer",
                            "minimum": 100000,
                            "maximum": 10,
                        },
                    },
                    "required": ["total_batch_count"],
                    "additionalProperties": False,
                },
            },
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["code"] == 400
    assert "minimum (100000) 不能大于 maximum (10)" in payload["message"]
