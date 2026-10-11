"""Warm exact history memory after results commit, with independent DB ownership.

Prompt readers still rebuild and compare producer inputs. A skipped or interrupted
warmup therefore never makes an old summary authoritative for a new history.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock

from utils import logger

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='history-summary')
_pending = set()
_lock = Lock()


def refresh_history_summary(conversation_id, expected_revision, session_factory=None):
    from config.database import SessionLocal
    from model import InsightNsConversation
    from service.conversation_context_service import ConversationContextService
    session = (session_factory or SessionLocal)()
    try:
        conversation = session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0).first()
        if conversation is None or conversation.updated_at != expected_revision:
            return False
        payload = ConversationContextService(session)._build_rolling_summary_payload(conversation_id)
        # Release the read transaction before acquiring the current revision.
        session.rollback()
        conversation = session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0).populate_existing().with_for_update().first()
        if conversation is None or conversation.updated_at != expected_revision:
            return False
        service = ConversationContextService(session)
        session.query(InsightNsConversation).filter(InsightNsConversation.id == conversation_id).update(
            {'summary_text': str(payload.get('summary_text') or ''), 'updated_at': expected_revision},
            synchronize_session=False)
        service._upsert_memory(conversation_id, 'rolling_summary', payload)
        session.commit()
        logger.info('post-analysis history summary refreshed: conversation_id=%s', conversation_id)
        return True
    except Exception:
        session.rollback()
        logger.warning('post-analysis history summary refresh failed: conversation_id=%s',
                       conversation_id, exc_info=True)
        return False
    finally:
        session.close()


def enqueue_history_summary(conversation_id, expected_revision):
    # Capture the selected model and authorization context without sharing a
    # request's SQLAlchemy session with the worker.
    from config import Config
    key = (Config.DB_HOST, Config.DB_PORT, Config.DB_NAME, conversation_id, expected_revision)
    with _lock:
        if key in _pending or len(_pending) >= 128:
            return False
        _pending.add(key)
    def run():
        try:
            refresh_history_summary(conversation_id, expected_revision)
        finally:
            with _lock:
                _pending.discard(key)
    try:
        _executor.submit(copy_context().run, run)
        return True
    except Exception:
        with _lock:
            _pending.discard(key)
        logger.warning('history summary scheduling failed: conversation_id=%s', conversation_id,
                       exc_info=True)
        return False
