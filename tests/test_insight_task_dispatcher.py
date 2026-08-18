import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from insight_task.dispatcher import InsightTaskDispatcher  # noqa: E402


def test_dispatch_preserves_app_key_as_gateway_origin_token():
    response = MagicMock()
    response.read.return_value = json.dumps({
        'success': True,
        'data': {'status': 'queued'},
    }).encode('utf-8')
    response.__enter__.return_value = response
    response.__exit__.return_value = False

    with (
        patch('insight_task.dispatcher.Config.SUPOS_DATAINSIGHT_SERVER_APPKEY', 'service-app-key'),
        patch('insight_task.dispatcher.Config.SUPOS_GATEWAY', 'http://gateway:8000'),
        patch(
            'insight_task.dispatcher.Config.INSIGHT_TASK_INTERNAL_EXECUTE_PATH',
            '/api/insight/tasks/internal/runs/{run_id}/execute',
        ),
        patch('insight_task.dispatcher.urlopen', return_value=response) as urlopen,
    ):
        result = InsightTaskDispatcher().dispatch('run-1')

    request = urlopen.call_args.args[0]
    assert request.get_header('Authorization') == 'Bearer service-app-key'
    assert request.get_header('Origin-token') == 'Bearer service-app-key'
    assert result == {'status': 'queued'}
