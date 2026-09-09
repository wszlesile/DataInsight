"""Durable, task-scoped display events excluded from conversational memory."""

import json
from sqlalchemy import func
from model import InsightNsMessage, InsightNsTurn, InsightAnalysisTask


def save_progress(session, event, analysis_task_id):
    if event.get('stage') != 'work_progress' or not isinstance(event.get('progress'), dict):
        return
    turn = session.get(InsightNsTurn, event.get('turn_id'))
    if turn is None:
        return
    task = session.get(InsightAnalysisTask, analysis_task_id) if analysis_task_id else None
    # Only the public event schema is persisted; no code, samples or raw payload.
    public = {key: event[key] for key in (
        'type', 'stage', 'event_id', 'occurred_at', 'level', 'message', 'progress',
        'task_id', 'turn_id', 'conversation_id') if key in event}
    seq = session.query(func.max(InsightNsMessage.seq_no)).filter(
        InsightNsMessage.turn_id == turn.id).scalar() or 0
    session.add(InsightNsMessage(
        insight_namespace_id=task.namespace_id if task else 0,
        insight_conversation_id=turn.conversation_id, turn_id=turn.id, turn_no=turn.turn_no,
        seq_no=seq + 1, role='system', type=2, message_kind='analysis_progress',
        content=public.get('message', ''), content_json=json.dumps(public, ensure_ascii=False),
        content_enabled=1, analysis_task_id=analysis_task_id,
    ))


def load_progress(session, conversation_id, turn_id=None, analysis_task_id=None):
    query = session.query(InsightNsMessage).filter(
        InsightNsMessage.insight_conversation_id == conversation_id,
        InsightNsMessage.message_kind == 'analysis_progress', InsightNsMessage.is_deleted == 0)
    if turn_id is not None:
        query = query.filter(InsightNsMessage.turn_id == turn_id)
    if analysis_task_id is not None:
        query = query.filter(InsightNsMessage.analysis_task_id == analysis_task_id)
    else:
        latest = session.query(InsightAnalysisTask.turn_id, func.max(InsightAnalysisTask.id)).filter(
            InsightAnalysisTask.conversation_id == conversation_id).group_by(InsightAnalysisTask.turn_id).all()
        ids = [item[1] for item in latest]
        if not ids:
            return []
        query = query.filter(InsightNsMessage.analysis_task_id.in_(ids))
    result = []
    for row in query.order_by(InsightNsMessage.id.asc()).all():
        try:
            value = json.loads(row.content_json)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def group_latest_progress(session, conversation_id):
    grouped = {}
    for event in load_progress(session, conversation_id):
        grouped.setdefault(int(event.get('turn_id') or 0), []).append(event)
    return grouped
