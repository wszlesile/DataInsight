from pathlib import Path
import sys

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
