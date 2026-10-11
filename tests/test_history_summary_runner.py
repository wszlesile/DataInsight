from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.database import Base
from model import InsightNsConversation, InsightNsMemory
from service.conversation_context_service import ConversationContextService
from service.history_summary_runner import refresh_history_summary


@pytest.mark.parametrize('change', ['none', 'before', 'during'])
def test_background_summary_owns_session_and_never_overwrites_newer_history(tmp_path, monkeypatch, change):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    revision = datetime(2026, 10, 11, 12)
    with factory() as session:
        session.add(InsightNsConversation(id=1, insight_namespace_id=1,
            summary_text='previous', updated_at=revision))
        session.commit()

    def update_history():
        with factory() as newer:
            conversation = newer.get(InsightNsConversation, 1)
            conversation.summary_text = 'newer history'
            conversation.updated_at = revision + timedelta(seconds=1)
            newer.commit()

    calls = []
    def build(service, conversation_id):
        calls.append(conversation_id)
        if change == 'during':
            update_history()
        return {'summary_text': 'exact new summary', 'compressed_turn_summaries': []}
    monkeypatch.setattr(ConversationContextService, '_build_rolling_summary_payload', build)
    if change == 'before':
        update_history()
    assert refresh_history_summary(1, revision, factory) == (change == 'none')
    with factory() as check:
        conversation = check.get(InsightNsConversation, 1)
        memory = check.query(InsightNsMemory).filter(InsightNsMemory.conversation_id == 1).first()
        if change == 'none':
            assert conversation.summary_text == 'exact new summary'
            assert conversation.updated_at == revision
            assert json.loads(memory.content_json)['summary_text'] == 'exact new summary'
        else:
            assert conversation.summary_text == 'newer history'
            assert memory is None
    assert calls == ([] if change == 'before' else [1])
    engine.dispose()


def test_summary_failure_is_optional_and_does_not_change_saved_state(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'failure.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    revision = datetime(2026, 10, 11, 12)
    with factory() as session:
        session.add(InsightNsConversation(id=1, insight_namespace_id=1,
            summary_text='previous', insight_result='committed report', updated_at=revision))
        session.commit()
    def fail(*args):
        raise RuntimeError('summary unavailable')
    monkeypatch.setattr(ConversationContextService, '_build_rolling_summary_payload', fail)
    assert refresh_history_summary(1, revision, factory) is False
    with factory() as session:
        assert session.get(InsightNsConversation, 1).insight_result == 'committed report'
        assert session.get(InsightNsConversation, 1).summary_text == 'previous'
    engine.dispose()


def test_background_schedule_retains_context_and_deduplicates_pending_revision(monkeypatch):
    from contextvars import ContextVar
    from types import SimpleNamespace
    import service.history_summary_runner as runner
    selected = ContextVar('test_selected_model', default='default')
    queued = []
    observed = []
    monkeypatch.setattr(runner, '_executor', SimpleNamespace(submit=lambda *args: queued.append(args)))
    monkeypatch.setattr(runner, '_pending', set())
    monkeypatch.setattr(runner, 'refresh_history_summary',
        lambda *args: observed.append(selected.get()))
    token = selected.set('selected for this analysis')
    revision = datetime(2026, 10, 11, 12)
    try:
        assert runner.enqueue_history_summary(1, revision)
        assert not runner.enqueue_history_summary(1, revision)
    finally:
        selected.reset(token)
    function, *arguments = queued[0]
    function(*arguments)
    assert observed == ['selected for this analysis']
    assert not runner._pending
