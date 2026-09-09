from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.database import Base
from service.analysis_task_service import AnalysisTaskService
import controller.agent_controller as controller


@pytest.fixture
def waiting():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = AnalysisTaskService(session)
    task = service.create_task(username='tester', namespace_id=1, conversation_id=2,
        turn_id=3, task_type='new_analysis', request_payload={'user_message': 'Analyze'})
    service.mark_running(task.task_id)
    service.mark_waiting_clarification(task.task_id, clarification={
        'clarification_id': 'c1', 'items': [{
            'item_id': 'period', 'question': 'Period?', 'allow_free_text': True,
            'options': [{'id': 'month', 'label': 'This month', 'value': 'current_month'}],
        }],
    })
    session.commit()
    yield session, service, task
    session.close()
    engine.dispose()


@pytest.mark.parametrize('answers', [
    [], {}, [None],
    [{'item_id': 'wrong', 'answer_text': 'July'}],
    [{'item_id': 'period', 'answer_text': 'July'}, {'item_id': 'period', 'answer_text': 'June'}],
    [{'item_id': 'period', 'option_id': 'month', 'answer_text': 'Last year'}],
])
def test_invalid_answers_do_not_mutate_waiting_task(waiting, answers):
    session, service, task = waiting
    original = task.request_json
    result, _ = service.resume_from_clarification(task.task_id, clarification_id='c1', answer_text='', answers=answers)
    assert result is None
    assert task.status == 'waiting_clarification'
    assert task.request_json == original


def test_resumed_old_task_uses_new_queue_entry_time(waiting):
    session, service, task = waiting
    original_created = datetime.now() - timedelta(days=1)
    task.created_at = original_created
    task.updated_at = original_created
    result, answer = service.resume_from_clarification(task.task_id, clarification_id='c1', answer_text='July')
    assert result is task
    assert answer['resolved_value'] == 'July'
    assert not service._mark_stale_if_needed(task)
    assert task.status == 'queued'
    assert task.created_at == original_created


def _client(monkeypatch, session):
    monkeypatch.setattr(controller, '_get_username', lambda: 'tester')
    monkeypatch.setattr(controller, 'SessionLocal', lambda: session)
    app = Flask(__name__)
    app.register_blueprint(controller.create_agent_controller())
    return app.test_client()


def test_dispatch_failure_does_not_leave_queued_task(waiting, monkeypatch):
    session, service, task = waiting
    context = Mock()
    monkeypatch.setattr(controller, 'ConversationContextService', lambda _: context)
    monkeypatch.setattr(controller, '_build_agent_request', lambda _: SimpleNamespace())
    monkeypatch.setattr(controller, 'get_redis_client', Mock(side_effect=RuntimeError('offline')))
    runner = Mock()
    runner.submit.side_effect = RuntimeError('executor stopped')
    monkeypatch.setattr(controller, 'get_analysis_task_runner', lambda: runner)
    task_id = task.task_id
    response = _client(monkeypatch, session).post(f'/api/agent/tasks/{task_id}/clarification',
        json={'clarification_id': 'c1', 'answer_text': 'July'})
    assert response.status_code == 500
    stored = service.get_task(task_id, username='tester')
    assert stored.status == 'failed'
    context.fail_run.assert_called_once()


def test_conflicting_answer_returns_409_and_can_be_corrected(waiting, monkeypatch):
    session, service, task = waiting
    task_id = task.task_id
    response = _client(monkeypatch, session).post(f'/api/agent/tasks/{task_id}/clarification',
        json={'clarification_id': 'c1', 'option_id': 'month', 'answer_text': 'Last year'})
    assert response.status_code == 409
    assert service.get_task(task_id).status == 'waiting_clarification'


@pytest.mark.parametrize('status', ['cancelled', 'running', 'success'])
def test_dispatch_failure_cannot_overwrite_other_states(waiting, status):
    session, service, task = waiting
    task.status = status
    session.flush()
    assert not service.mark_dispatch_failed(task.task_id, 'dispatch failed')
    assert task.status == status


@pytest.mark.parametrize('mode', ['same_turn', 'disabled', None])
def test_rerun_delivers_clarification_mode_to_request_and_task(monkeypatch, mode):
    import controller.insight_ns_conversation_controller as rerun
    session = Mock()
    task_service = Mock()
    task_service.count_active_tasks.return_value = 0
    task_service.has_active_conversation_task.return_value = False
    task = SimpleNamespace(task_id='t1', id=4)
    task_service.create_task.return_value = task
    task_service.serialize_task.return_value = {'task_id': 't1'}
    runtime = SimpleNamespace(conversation=SimpleNamespace(id=2, insight_namespace_id=1),
        turn=SimpleNamespace(id=3, user_query='Analyze'))
    context = Mock()
    context.start_rerun.return_value = runtime
    runner = Mock()
    monkeypatch.setattr(rerun, 'SessionLocal', lambda: session)
    monkeypatch.setattr(rerun, 'AnalysisTaskService', lambda _: task_service)
    monkeypatch.setattr(rerun, 'ConversationContextService', lambda _: context)
    monkeypatch.setattr(rerun, 'get_redis_client', Mock())
    monkeypatch.setattr(rerun, 'AnalysisStreamQueue', Mock())
    monkeypatch.setattr(rerun, 'get_analysis_task_runner', lambda: runner)
    monkeypatch.setattr(rerun.InsightNsConversationController, '_get_username', lambda _: 'tester')
    monkeypatch.setattr(rerun.InsightNsConversationController, '_build_request_runtime_context', lambda _: ('', '', '', {}))
    app = Flask(__name__)
    app.register_blueprint(rerun.create_insight_ns_conversation_controller())
    response = app.test_client().post('/api/insight/conversations/2/turns/3/rerun/task',
        **({'json': {'clarification_mode': mode}} if mode is not None else {}))
    assert response.status_code == 200
    assert response.get_json()['success']
    expected_mode = mode or 'same_turn'
    assert task_service.create_task.call_args.kwargs['request_payload']['clarification_mode'] == expected_mode
    assert runner.submit.call_args.kwargs['agent_request'].clarification_mode == expected_mode
