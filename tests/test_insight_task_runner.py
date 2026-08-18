import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from insight_task.runner import InsightTaskRunner  # noqa: E402
from insight_task.dispatcher import InsightTaskDispatchError  # noqa: E402


def test_local_runner_executes_in_process_with_local_dual_token_context():
    runner = InsightTaskRunner()
    session = MagicMock()
    service = MagicMock()
    service.execute_run.return_value = {'run_id': 'run-1', 'status': 'success'}
    connection = SimpleNamespace(
        host='fedquery',
        port='5432',
        user='insight',
        password='secret',
        lake_rds_database_name='lake',
        ts_query_source='vxbase',
    )

    with (
        patch('insight_task.runner.Config.PROFILE', 'local'),
        patch('insight_task.runner.Config.LOCAL_ORIGIN_TOKEN', 'Bearer platform-ticket'),
        patch('insight_task.runner.Config.LOCAL_GATEWAY_JWT', 'Bearer fedquery-jwt'),
        patch('insight_task.runner.supos_kernel_api.get_database_conn_info', return_value=connection),
        patch('insight_task.runner.SessionLocal', return_value=session),
        patch('insight_task.runner.InsightTaskService', return_value=service),
        patch.object(runner.dispatcher, 'dispatch') as dispatch,
    ):
        assert runner._dispatch_one('run-1') is True

    dispatch.assert_not_called()
    service.execute_run.assert_called_once()
    call = service.execute_run.call_args.kwargs
    assert call['run_id'] == 'run-1'
    assert call['execution_principal'] == 'local-task-runner'
    assert call['runtime_user_context']['platform_authorization'] == 'Bearer platform-ticket'
    assert call['runtime_user_context']['fedquery_authorization'] == 'Bearer fedquery-jwt'
    assert call['runtime_user_context']['database_conn_info']['host'] == 'fedquery'
    session.close.assert_called_once()


def test_production_runner_keeps_gateway_dispatch():
    runner = InsightTaskRunner()
    with (
        patch('insight_task.runner.Config.PROFILE', 'prod'),
        patch.object(runner.dispatcher, 'dispatch', return_value={'status': 'success'}) as dispatch,
    ):
        assert runner._dispatch_one('run-2') is True

    dispatch.assert_called_once_with('run-2')


def test_production_runner_records_dispatch_failure_without_exiting():
    runner = InsightTaskRunner()
    failed_session = MagicMock()
    failed_service = MagicMock()
    dispatch_error = InsightTaskDispatchError('gateway unavailable', retryable=True)

    with (
        patch('insight_task.runner.Config.PROFILE', 'prod'),
        patch.object(runner.dispatcher, 'dispatch', side_effect=dispatch_error),
        patch('insight_task.runner.SessionLocal', return_value=failed_session),
        patch('insight_task.runner.InsightTaskService', return_value=failed_service),
    ):
        assert runner._dispatch_one('run-3') is False

    failed_service.record_dispatch_failure.assert_called_once_with(
        run_id='run-3',
        error_message='gateway unavailable',
        retryable=True,
    )
    failed_session.close.assert_called_once()
