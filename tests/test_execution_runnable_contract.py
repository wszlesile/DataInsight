import sys
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from model import InsightNsExecution  # noqa: E402
from service.insight_ns_conversation_service import InsightNsConversationService  # noqa: E402


def test_execution_dict_exposes_runnable_capability_from_persisted_code():
    execution = InsightNsExecution(
        id=1,
        execution_status='success',
        generated_code="result = save_analysis_result(analysis_report='ok')",
    )

    assert execution.to_dict()['is_runnable'] is True


def test_history_summary_keeps_runnable_capability_without_shipping_code():
    service = InsightNsConversationService(MagicMock())
    summary = service._build_execution_summary({
        'id': 1,
        'execution_status': 'success',
        'generated_code': "result = save_analysis_result(analysis_report='ok')",
        'is_runnable': True,
    })

    assert summary['is_runnable'] is True
    assert 'generated_code' not in summary


def test_failed_or_code_less_execution_is_not_runnable():
    service = InsightNsConversationService(MagicMock())

    failed = service._build_execution_summary({
        'id': 1,
        'execution_status': 'failed',
        'generated_code': 'print(1)',
    })
    code_less = service._build_execution_summary({
        'id': 2,
        'execution_status': 'success',
        'generated_code': '',
    })

    assert failed['is_runnable'] is False
    assert code_less['is_runnable'] is False
