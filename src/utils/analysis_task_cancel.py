from __future__ import annotations


CANCELLED_MESSAGE = '用户已停止本轮分析。'


class AnalysisTaskCancelled(Exception):
    """Raised when an analysis task has been cancelled by the user."""


def is_analysis_task_cancelled(*, task_id: str | None = None, task_pk: int | None = None) -> bool:
    if not task_id and not task_pk:
        return False

    from config.database import session_scope
    from model import InsightAnalysisTask

    with session_scope(expire_on_commit=False) as session:
        query = session.query(InsightAnalysisTask.status)
        if task_pk:
            query = query.filter(InsightAnalysisTask.id == int(task_pk or 0))
        else:
            query = query.filter(InsightAnalysisTask.task_id == str(task_id or ''))
        row = query.first()
        return bool(row and row[0] == 'cancelled')
