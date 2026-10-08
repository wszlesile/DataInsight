"""Durable background runner for post-analysis Evidence compilation."""

import os
import socket
import time
from datetime import datetime, timedelta

from sqlalchemy import or_

from config.database import session_scope
from model import InsightNsArtifact, InsightNsExecution, InsightNsExecutionEvidence
from service.evidence_assembly_service import EvidenceAssemblyService
from utils import logger
from utils.datasource_utils import dump_json


class EvidenceTaskRunner:
    """Claims queued Evidence rows from the database, so process restarts are safe."""

    POLL_SECONDS = 2
    STALE_AFTER = timedelta(minutes=15)

    def __init__(self):
        self.worker_id = f'{socket.gethostname()}:{os.getpid()}'

    def run_forever(self) -> None:
        logger.info('Evidence 编译 Runner 已启动: worker_id=%s', self.worker_id)
        while True:
            try:
                if not self.run_once():
                    time.sleep(self.POLL_SECONDS)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.exception('Evidence 编译调度循环异常: %s', exc)
                time.sleep(self.POLL_SECONDS)

    def run_once(self) -> bool:
        now = datetime.now()
        stale_before = now - self.STALE_AFTER
        with session_scope(expire_on_commit=False) as session:
            candidate = session.query(InsightNsExecutionEvidence).filter(
                InsightNsExecutionEvidence.is_deleted == 0,
                or_(
                    InsightNsExecutionEvidence.compilation_status == 'queued',
                    (InsightNsExecutionEvidence.compilation_status == 'running')
                    & (InsightNsExecutionEvidence.updated_at < stale_before),
                ),
            ).order_by(InsightNsExecutionEvidence.created_at.asc()).first()
            if candidate is None:
                return False
            execution_id = int(candidate.execution_id or 0)
            claim = session.query(InsightNsExecutionEvidence).filter(
                InsightNsExecutionEvidence.id == candidate.id,
                InsightNsExecutionEvidence.compilation_status == candidate.compilation_status,
            )
            if candidate.compilation_status == 'running':
                claim = claim.filter(InsightNsExecutionEvidence.updated_at < stale_before)
            if claim.update(
                {InsightNsExecutionEvidence.compilation_status: 'running',
                 InsightNsExecutionEvidence.updated_at: now},
                synchronize_session=False,
            ) != 1:
                return False
            session.commit()

        try:
            with session_scope(expire_on_commit=False) as session:
                evidence = session.query(InsightNsExecutionEvidence).filter(
                    InsightNsExecutionEvidence.execution_id == execution_id,
                    InsightNsExecutionEvidence.is_deleted == 0,
                ).first()
                if evidence is None or evidence.compilation_status != 'running':
                    return True
                execution = session.query(InsightNsExecution).filter(
                    InsightNsExecution.id == execution_id,
                    InsightNsExecution.is_deleted == 0,
                ).first()
                if execution is None:
                    evidence.compilation_status = 'failed'
                    evidence.diagnostics_json = dump_json({'reason': 'execution_not_found'})
                    session.commit()
                    return True
                artifacts = session.query(InsightNsArtifact).filter(
                    InsightNsArtifact.execution_id == execution_id,
                    InsightNsArtifact.is_deleted == 0,
                ).order_by(InsightNsArtifact.sort_no.asc(), InsightNsArtifact.id.asc()).all()
                EvidenceAssemblyService(session).assemble_for_execution(
                    conversation_id=int(evidence.conversation_id),
                    turn_id=int(evidence.turn_id),
                    execution=execution,
                    artifacts=[artifact.to_dict() for artifact in artifacts],
                )
                if evidence.compilation_status == 'running':
                    evidence.compilation_status = 'failed'
                    evidence.diagnostics_json = dump_json({
                        'reason': 'assembler_did_not_finalize',
                    })
                    evidence.updated_at = datetime.now()
                session.commit()
            logger.info('Evidence 编译任务完成: execution_id=%s worker_id=%s', execution_id, self.worker_id)
        except Exception as exc:
            logger.exception('Evidence 编译任务持久化失败: execution_id=%s', execution_id)
            with session_scope(expire_on_commit=False) as session:
                evidence = session.query(InsightNsExecutionEvidence).filter(
                    InsightNsExecutionEvidence.execution_id == execution_id,
                    InsightNsExecutionEvidence.is_deleted == 0,
                ).first()
                if evidence is not None:
                    evidence.compilation_status = 'failed'
                    evidence.diagnostics_json = dump_json({
                        'reason': 'unexpected_worker_failure',
                        'error': str(exc)[:1200],
                    })
                    evidence.updated_at = datetime.now()
                    session.commit()
        return True
