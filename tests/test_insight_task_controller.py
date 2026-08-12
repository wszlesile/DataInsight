import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from controller.insight_task_controller import create_insight_task_controller  # noqa: E402


def test_manual_run_uses_authenticated_user_context_without_runner_queue():
    app = Flask(__name__)
    context = SimpleNamespace(
        username='alice',
        platform_authorization='Bearer user-ticket',
        fedquery_authorization='Bearer user-jwt',
        database_conn_info=SimpleNamespace(
            host='fedquery',
            port='15432',
            user='fedquery',
            password='secret',
            lake_rds_database_name='fqe_uns',
            ts_query_source='vxbase',
        ),
    )

    @app.before_request
    def bind_user_context():
        g.user_context = context

    app.register_blueprint(create_insight_task_controller())
    session = MagicMock()
    service = MagicMock()
    service.execute_manual_run.return_value = {
        'run_id': 'run-1',
        'trigger_type': 'manual',
        'status': 'success',
        'execution_principal': 'alice',
    }

    with (
        patch('controller.insight_task_controller.SessionLocal', return_value=session),
        patch('controller.insight_task_controller.InsightTaskService', return_value=service),
    ):
        response = app.test_client().post(
            '/api/insight/tasks/task-1/runs',
            json={'analysis_parameters': {'month': '2026-08'}},
        )

    assert response.status_code == 200
    assert response.get_json()['data']['status'] == 'success'
    service.execute_manual_run.assert_called_once()
    call = service.execute_manual_run.call_args.kwargs
    assert call['username'] == 'alice'
    assert call['task_id'] == 'task-1'
    assert call['parameters'] == {'month': '2026-08'}
    assert call['runtime_user_context']['platform_authorization'] == 'Bearer user-ticket'
    assert call['runtime_user_context']['fedquery_authorization'] == 'Bearer user-jwt'
    assert call['runtime_user_context']['database_conn_info']['host'] == 'fedquery'
    session.close.assert_called_once()
