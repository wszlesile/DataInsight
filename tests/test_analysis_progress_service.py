import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.database import Base
from model import (
    InsightAnalysisTask,
    InsightNamespace,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsExecutionEvidence,
    InsightNsTurn,
)
from service.analysis_progress_service import load_progress, save_progress
from service.insight_ns_conversation_service import InsightNsConversationService


def test_progress_history_returns_only_latest_task_version():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(InsightNamespace(id=7, username='alice', name='test', namespace_kind='interactive'))
    session.add(InsightNsConversation(
        id=19, insight_namespace_id=7, title='test', status='active', summary_text='',
        active_datasource_snapshot='{}', last_turn_no=1, user_message='hello', insight_result='',
    ))
    session.add(InsightNsTurn(
        id=39, conversation_id=19, turn_no=1, user_query='hello',
        selected_datasource_ids_json='[]', selected_datasource_snapshot_json='[]',
        final_answer='', status='running', error_message='',
    ))
    tasks = []
    for identity in ('old', 'new'):
        task = InsightAnalysisTask(
            task_id=identity, username='alice', namespace_id=7, conversation_id=19,
            turn_id=39, task_type='rerun', status='running', request_json='{}',
        )
        session.add(task)
        session.flush()
        tasks.append(task)
        save_progress(session, {
            'type': 'status', 'stage': 'work_progress', 'event_id': identity,
            'turn_id': 39, 'conversation_id': 19, 'message': identity,
            'progress': {'activity_id': f'tool:{identity}', 'title': identity, 'state': 'running'},
        }, task.id)
        session.flush()
    session.commit()

    events = load_progress(session, 19, 39)
    assert [event['event_id'] for event in events] == ['new']
    service = InsightNsConversationService(session)
    assert [event['event_id'] for event in service.get_conversation_history('alice', 19)['history'][0]['progress']] == ['new']
    assert [event['event_id'] for event in service.get_turn_detail('alice', 19, 39)['progress']] == ['new']
    execution = InsightNsExecution(
        id=47,
        conversation_id=19,
        turn_id=39,
        execution_status='success',
        content_enabled=1,
    )
    session.add(execution)
    session.add(InsightNsExecutionEvidence(
        conversation_id=19,
        turn_id=39,
        execution_id=47,
        compilation_status='queued',
        diagnostics_json='{}',
        query_snapshots_json='{}',
        evidence_map_json='{}',
        is_deleted=0,
    ))
    session.commit()
    assert service.get_evidence_compilation_status('alice', 19, 39)['status'] == 'queued'
    rows = session.execute(Base.metadata.tables['insight_ns_message'].select()).mappings().all()
    assert {json.loads(row['content_json'])['event_id'] for row in rows} == {'old', 'new'}
    session.close()
