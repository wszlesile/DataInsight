from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
import copy
import hashlib
import re
import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import Config
from model import (
    InsightDatasource,
    InsightNsArtifact,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsMemory,
    InsightNsMessage,
    InsightNsRelDatasource,
    InsightNsRelKnowledge,
    InsightNsTurn,
    InsightNamespace,
)
from utils.context_compression import (
    build_llm_turn_summarizer,
    clip_text,
    summarize_turns_incrementally,
    history_compression_cache,
)
from utils.datasource_utils import (
    build_table_query_access,
    build_table_runtime_metadata,
    build_conversation_title,
    dump_json,
    extract_datasource_identifier,
    extract_datasource_schema,
    normalize_datasource_type,
    recommend_local_file_loader,
    safe_json_loads,
    to_int,
)
from utils.analysis_task_cancel import AnalysisTaskCancelled, CANCELLED_MESSAGE, is_analysis_task_cancelled
from utils import logger
from service.evidence_assembly_service import EvidenceAssemblyService

ROLE_TO_TYPE = {
    'system': 0,
    'summary': 0,
    'user': 1,
    'assistant': 2,
    'tool': 3,
}
AUTO_TITLE_PLACEHOLDERS = {'新建会话', '默认会话'}

TERMINAL_EXECUTION_STATUSES = ('success', 'failed')


def _now() -> datetime:
    return datetime.now()


def _attach_chart_artifact_refs(
    charts: list[dict[str, Any]],
    chart_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """回填流式结果里的图表产物 id，兼容直接从 charts[] 取收藏目标的前端。"""
    if not charts:
        return []

    artifact_ids = [
        int(artifact.get('id', 0) or 0)
        for artifact in (chart_artifacts or [])
        if int(artifact.get('id', 0) or 0) > 0
    ]

    merged_charts: list[dict[str, Any]] = []
    for index, chart in enumerate(charts):
        chart_payload = dict(chart) if isinstance(chart, dict) else {}
        artifact_id = artifact_ids[index] if index < len(artifact_ids) else 0
        if artifact_id > 0:
            chart_payload['id'] = artifact_id
        merged_charts.append(chart_payload)
    return merged_charts


@dataclass
class ConversationRunContext:
    """一轮分析启动后返回的内存态运行上下文。"""

    conversation: InsightNsConversation
    turn: InsightNsTurn
    active_datasource_snapshot: dict[str, Any]
    is_rerun: bool = False
    history_turn_limit: int | None = None
    analysis_task_id: int | None = None
    short_term_memory: dict[str, Any] = field(default_factory=dict)
    context_mode: str = ''
    output_contract: dict[str, Any] = field(
        default_factory=lambda: {"type": "standard"}
    )
    clarification_mode: str = "same_turn"
    evidence_enabled: bool = True


class ConversationContextService:
    """
    上下文工程主链路对应的运行时服务。

    主要职责：
    - 创建或恢复会话
    - 创建轮次与消息记录
    - 持久化最终回答、执行记录、派生产物与记忆
    - 为下一轮对话重建面向 Prompt 的记忆快照
    """

    def __init__(self, session: Session):
        self.session = session

    def start_run(
        self,
        username: str,
        namespace_id: Any,
        user_message: str,
        conversation_id: Any = None,
        allow_internal_namespace: bool = False,
    ) -> ConversationRunContext:
        """创建或恢复会话，并开启新一轮分析。"""
        requested_conversation_id = to_int(conversation_id, 0)
        conversation = self._get_conversation_for_run(
            conversation_id,
            username=username,
            allow_internal_namespace=allow_internal_namespace,
        )
        if requested_conversation_id > 0 and conversation is None:
            raise ValueError('当前会话不存在，请重新选择会话后再分析')
        if conversation is None:
            namespace = self._get_namespace_for_run(
                namespace_id,
                username=username,
                allow_internal_namespace=allow_internal_namespace,
            )
            if namespace is None:
                raise ValueError('当前洞察空间不存在，请重新选择空间后再分析')
            conversation = self._create_conversation(
                username=username,
                namespace_id=namespace.id,
                user_message=user_message,
            )

        # 默认会话数据源继承逻辑先保留方法但暂停启用。
        # 当前会话未显式绑定数据源时，应由 Agent 自然提示用户先关联数据源。
        # self._ensure_conversation_resource_bindings(conversation)

        # 第一步：生成当前会话维度上的最新数据源选择快照。
        next_turn_no = self._next_turn_no(conversation.id, conversation.last_turn_no or 0)
        active_snapshot = self._merge_datasource_snapshot(conversation=conversation)
        selected_ids = list(active_snapshot.get('selected_datasource_ids', []))

        # 第二步：把本轮使用的数据源快照固化到 turn 上，
        # 避免后续轮次覆盖这一轮的历史事实。
        turn_snapshot = list(active_snapshot.get('selected_datasource_snapshot') or [])

        conversation.active_datasource_snapshot = dump_json(active_snapshot)
        conversation.last_turn_no = next_turn_no
        conversation.last_message_at = _now()
        conversation.updated_at = _now()
        conversation.user_message = user_message
        conversation.status = 'active'

        turn = InsightNsTurn(
            conversation_id=conversation.id,
            turn_no=next_turn_no,
            user_query=user_message,
            selected_datasource_ids_json=dump_json(selected_ids),
            selected_datasource_snapshot_json=dump_json(turn_snapshot),
            final_answer='',
            status='running',
            error_message='',
            started_at=_now(),
        )
        self.session.add(turn)
        self.session.flush()

        # 第三步：把用户问题作为本轮第一条消息落库。
        self.add_message(
            conversation=conversation,
            turn=turn,
            role='user',
            message_kind='prompt',
            content=user_message,
            content_json={"user_query": user_message},
        )
        self.session.commit()
        return ConversationRunContext(
            conversation=conversation,
            turn=turn,
            active_datasource_snapshot=active_snapshot,
        )

    def start_rerun(
        self,
        username: str,
        conversation_id: Any,
        turn_id: Any,
    ) -> ConversationRunContext | None:
        """在同一轮次内重新执行分析，复用原始问题并刷新当前数据源选择。"""
        conversation = self._get_conversation_for_run(
            conversation_id,
            username=username,
            allow_internal_namespace=False,
        )
        if conversation is None:
            return None

        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return None

        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id_int,
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            return None

        active_snapshot = self._merge_datasource_snapshot(conversation=conversation)
        selected_ids = list(active_snapshot.get('selected_datasource_ids', []))
        selected_snapshot = list(active_snapshot.get('selected_datasource_snapshot') or [])

        self._reset_turn_runtime_state(conversation, turn)
        conversation.active_datasource_snapshot = dump_json(active_snapshot)
        conversation.last_message_at = _now()
        conversation.updated_at = _now()
        conversation.user_message = turn.user_query
        conversation.status = 'active'
        turn.selected_datasource_ids_json = dump_json(selected_ids)
        turn.selected_datasource_snapshot_json = dump_json(selected_snapshot)

        self.session.commit()
        return ConversationRunContext(
            conversation=conversation,
            turn=turn,
            active_datasource_snapshot=active_snapshot,
            is_rerun=True,
            history_turn_limit=max(turn.turn_no - 1, 0),
        )

    def add_message(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
        role: str,
        message_kind: str,
        content: str,
        content_json: dict[str, Any] | None = None,
        tool_name: str = '',
        tool_call_id: str = '',
        analysis_task_id: int | None = None,
    ) -> InsightNsMessage:
        """持久化一条用于多轮上下文重放的消息记录。"""
        message = InsightNsMessage(
            insight_namespace_id=conversation.insight_namespace_id,
            insight_conversation_id=conversation.id,
            turn_id=turn.id,
            turn_no=turn.turn_no,
            seq_no=self._next_seq_no(conversation.id, turn.turn_no),
            role=role,
            message_kind=message_kind,
            type=ROLE_TO_TYPE.get(role, 2),
            content=content or '',
            content_json=dump_json(content_json or {}),
            tool_name=tool_name or '',
            tool_call_id=tool_call_id or '',
            insight_result='',
            content_enabled=1,
            analysis_task_id=analysis_task_id,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def suspend_for_clarification(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        clarification: dict[str, Any],
        analysis_task_id: int | None = None,
    ) -> None:
        """Persist a clarification request without completing the current turn."""
        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return
        turn.status = "waiting_clarification"
        turn.error_message = ""
        turn.finished_at = None
        conversation.status = "active"
        conversation.last_message_at = _now()
        conversation.updated_at = _now()
        self.add_message(
            conversation=conversation,
            turn=turn,
            role="assistant",
            message_kind="clarification_request",
            content=str(clarification.get("question") or ""),
            content_json={"clarification": clarification},
            analysis_task_id=analysis_task_id,
        )

    def resume_from_clarification(
        self,
        *,
        conversation_id: int,
        turn_id: int,
        answer: dict[str, Any],
        analysis_task_id: int | None = None,
    ) -> None:
        """Append the answer to the same turn and make that turn runnable again."""
        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return
        turn.status = "running"
        turn.error_message = ""
        turn.finished_at = None
        conversation.status = "active"
        conversation.last_message_at = _now()
        conversation.updated_at = _now()
        self.add_message(
            conversation=conversation,
            turn=turn,
            role="user",
            message_kind="clarification_answer",
            content=str(answer.get("answer_text") or ""),
            content_json={"clarification_answer": answer},
            analysis_task_id=analysis_task_id,
        )

    def complete_run(
        self,
        conversation_id: int,
        turn_id: int,
        assistant_message: str,
        analysis_report: str,
        charts: list[dict[str, Any]] | None = None,
        tables: list[dict[str, Any]] | None = None,
        replace_existing_results: bool = False,
        analysis_task_id: int | None = None,
        evidence_enabled: bool = True,
    ) -> dict[str, Any]:
        """结束一次成功轮次，并刷新会话记忆。"""
        if analysis_task_id and is_analysis_task_cancelled(task_pk=int(analysis_task_id or 0)):
            raise AnalysisTaskCancelled(CANCELLED_MESSAGE)

        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return {}

        latest_execution = self.get_latest_execution(
            turn.id,
            started_at=turn.started_at if replace_existing_results else None,
            analysis_task_id=analysis_task_id,
            successful_only=True,
        )
        if replace_existing_results:
            self._archive_previous_turn_results(
                conversation=conversation,
                turn=turn,
                started_at=turn.started_at,
            )

        final_answer = (analysis_report or assistant_message or '').strip()
        turn.final_answer = final_answer
        if latest_execution is not None:
            turn.used_datasource_ids_json = getattr(latest_execution, 'used_datasource_ids_json', '') or '[]'
        else:
            turn.used_datasource_ids_json = '[]'
        turn.status = 'success'
        turn.error_message = ''
        turn.finished_at = _now()

        conversation.insight_result = final_answer
        conversation.last_message_at = _now()
        conversation.updated_at = _now()

        self._sync_first_turn_execution_title(
            conversation=conversation,
            turn=turn,
            latest_execution=latest_execution,
            replace_existing_results=replace_existing_results,
        )

        self.add_message(
            conversation=conversation,
            turn=turn,
            role='assistant',
            message_kind='final_answer',
            content=final_answer or assistant_message or '',
            content_json={
                "assistant_message": assistant_message or '',
                "analysis_report": analysis_report or '',
                "charts": charts or [],
                "tables": tables or [],
                "used_datasource_ids": safe_json_loads(turn.used_datasource_ids_json, []),
            },
            analysis_task_id=analysis_task_id,
        )

        artifacts: list[dict[str, Any]] = []
        for index, chart in enumerate(charts or [], start=1):
            artifacts.append(self._create_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=latest_execution.id if latest_execution else 0,
                artifact_type='chart',
                title=str(chart.get('title') or f"{conversation.title} 图表 {index}").strip(),
                summary_text=str(chart.get('description') or '').strip(),
                content=chart,
                metadata={
                    "turn_no": turn.turn_no,
                    "chart_type": chart.get('chart_type') or '',
                },
                analysis_task_id=analysis_task_id,
                sort_no=index,
            ))
        if analysis_report:
            artifacts.append(self._create_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=latest_execution.id if latest_execution else 0,
                artifact_type='report',
                title=f"{conversation.title} 报告",
                summary_text=analysis_report,
                content={"report_markdown": analysis_report},
                metadata={"turn_no": turn.turn_no},
                analysis_task_id=analysis_task_id,
                sort_no=0,
            ))
        for index, table in enumerate(tables or [], start=1):
            artifacts.append(self._create_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=latest_execution.id if latest_execution else 0,
                artifact_type='table',
                title=str(table.get('title') or f"{conversation.title} 表格 {index}").strip(),
                summary_text=str(table.get('description') or '').strip(),
                content=table,
                metadata={"turn_no": turn.turn_no},
                analysis_task_id=analysis_task_id,
                sort_no=100 + index,
            ))

        if analysis_task_id and is_analysis_task_cancelled(task_pk=int(analysis_task_id or 0)):
            raise AnalysisTaskCancelled(CANCELLED_MESSAGE)

        self._refresh_memories(conversation, refresh_summary=False)
        # Commit the successful analysis before queuing optional post-processing.
        self.session.commit()
        from service.history_summary_runner import enqueue_history_summary
        enqueue_history_summary(conversation.id, conversation.updated_at)
        if evidence_enabled:
            try:
                EvidenceAssemblyService(self.session).enqueue_for_execution(
                    conversation_id=conversation.id,
                    turn_id=turn.id,
                    execution=latest_execution,
                )
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                logger.warning(
                    "post-analysis evidence enqueue failed: execution_id=%s error=%s",
                    int(getattr(latest_execution, 'id', 0) or 0),
                    exc,
                    exc_info=True,
                )
        chart_artifacts = [artifact for artifact in artifacts if artifact.get('artifact_type') == 'chart']
        return {
            "conversation": conversation,
            "turn": turn,
            "artifacts": artifacts,
            "charts": _attach_chart_artifact_refs(charts or [], chart_artifacts),
            "tables": tables or [],
        }

    def _sync_first_turn_execution_title(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
        latest_execution: InsightNsExecution | None,
        replace_existing_results: bool = False,
    ) -> None:
        """空会话首轮成功后，同步会话标题，并在有执行记录时同步执行标题。"""
        if replace_existing_results:
            return
        if int(turn.turn_no or 0) != 1:
            return

        if str(conversation.title or '').strip() in AUTO_TITLE_PLACEHOLDERS:
            analysis_title = ''
            if latest_execution is not None and latest_execution.execution_status == 'success':
                analysis_title = str(latest_execution.title or '').strip()
            conversation.title = analysis_title or build_conversation_title(turn.user_query)

        if latest_execution is None or latest_execution.execution_status != 'success':
            return

        conversation_title = str(conversation.title or '').strip()[:260]
        if not conversation_title or latest_execution.title == conversation_title:
            return

        latest_execution.title = conversation_title
        latest_execution.updated_at = _now()

    def fail_run(
        self,
        conversation_id: int,
        turn_id: int,
        error_message: str,
        preserve_existing_results: bool = False,
    ) -> None:
        """结束一次失败轮次，同时保持整体上下文流程不变。"""
        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return

        if (
            turn.status == 'failed'
            and (turn.error_message or '').strip() == (error_message or '').strip()
        ):
            return

        if preserve_existing_results and self._turn_has_active_result(conversation, turn):
            self._archive_current_rerun_executions(conversation, turn)
            # Keep the last valid result available, but do not report the failed
            # rerun as a success. The task/turn state must describe the latest
            # run; otherwise clients mistake the preserved report for fresh data.
            turn.status = 'failed'
            turn.error_message = error_message
            turn.finished_at = _now()
            conversation.status = 'active'
            conversation.last_message_at = _now()
            conversation.updated_at = _now()
            self.session.commit()
            return

        turn.status = 'failed'
        turn.error_message = error_message
        turn.final_answer = error_message
        turn.finished_at = _now()
        conversation.status = 'active'
        conversation.insight_result = error_message
        conversation.last_message_at = _now()
        conversation.updated_at = _now()

        self.add_message(
            conversation=conversation,
            turn=turn,
            role='assistant',
            message_kind='error',
            content=error_message,
            content_json={"error_message": error_message},
        )
        self._refresh_memories(conversation)
        self.session.commit()

    def _turn_has_active_result(self, conversation: InsightNsConversation, turn: InsightNsTurn) -> bool:
        if self.session.query(InsightNsMessage.id).filter(
            InsightNsMessage.insight_conversation_id == conversation.id,
            InsightNsMessage.turn_id == turn.id,
            InsightNsMessage.role == 'assistant',
            InsightNsMessage.message_kind == 'final_answer',
            InsightNsMessage.is_deleted == 0,
            InsightNsMessage.content_enabled == 1,
        ).first() is not None:
            return True
        if self.session.query(InsightNsArtifact.id).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
            InsightNsArtifact.content_enabled == 1,
        ).first() is not None:
            return True

        final_answer = (turn.final_answer or '').strip()
        if not final_answer:
            return False
        return not self._looks_like_error_answer(final_answer)

    @staticmethod
    def _looks_like_error_answer(answer: str) -> bool:
        text = (answer or '').strip().lower()
        if not text:
            return False
        error_markers = (
            'operationalerror',
            'server closed the connection unexpectedly',
            'background on this error',
            'traceback',
            'exception',
            'error code:',
            'platform-llm-gateway',
            '网关超时',
            '暂无可用渠道',
            '请切换到其他可用模型后重试',
            '请重试',
            '未生成',
            '执行失败',
            '分析失败',
            '用户已停止',
            '已停止本轮分析',
            '停止本轮分析',
        )
        return any(marker in text for marker in error_markers)

    def _archive_current_rerun_executions(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
    ) -> None:
        started_at = turn.started_at
        if started_at is None:
            return
        self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.created_at >= started_at,
            InsightNsExecution.is_deleted == 0,
        ).update(
            {
                InsightNsExecution.content_enabled: 0,
                InsightNsExecution.updated_at: _now(),
            },
            synchronize_session=False,
        )

    def get_recent_prompt_messages(
        self,
        conversation_id: Any,
        limit_messages: int = 10,
        max_turn_no: int | None = None,
    ) -> list[InsightNsMessage]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        query = self.session.query(InsightNsMessage).join(
            InsightNsTurn,
            InsightNsTurn.id == InsightNsMessage.turn_id,
        ).filter(
            InsightNsMessage.insight_conversation_id == conversation_id_int,
            InsightNsMessage.role.in_(['user', 'assistant']),
            InsightNsMessage.message_kind.in_(['prompt', 'final_answer']),
            InsightNsMessage.is_deleted == 0,
            InsightNsMessage.content_enabled == 1,
            InsightNsTurn.is_deleted == 0,
        )
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)

        rows = query.order_by(
            InsightNsMessage.created_at.desc(),
            InsightNsMessage.id.desc(),
        ).limit(limit_messages * 3).all()

        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id_int)
        execution_turn_ids = self._get_execution_turn_ids(conversation_id_int)
        filtered_rows: list[InsightNsMessage] = []
        for row in rows:
            content = (row.content or '').strip()
            if row.role == 'assistant':
                if not content:
                    continue
                if '<tool_call>' in content:
                    continue
                # 分析轮次只有真正成功执行过，才把助手最终回答纳入历史重放。
                # 完全没有执行记录的轮次属于普通问答，保留其助手回复以维持 user/assistant 成对上下文。
                if row.turn_id not in successful_turn_ids and row.turn_id in execution_turn_ids:
                    continue
            filtered_rows.append(row)
            if len(filtered_rows) >= limit_messages:
                break

        return list(reversed(filtered_rows))

    def get_turn_clarification_answers(self, turn_id: Any) -> list[dict[str, Any]]:
        """Return persisted answers without collapsing complementary clarification items."""
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return []

        rows = self.session.query(InsightNsMessage).filter(
            InsightNsMessage.turn_id == turn_id_int,
            InsightNsMessage.message_kind.in_([
                'clarification_request',
                'clarification_answer',
            ]),
            InsightNsMessage.is_deleted == 0,
            InsightNsMessage.content_enabled == 1,
        ).order_by(
            InsightNsMessage.seq_no.asc(),
            InsightNsMessage.id.asc(),
        ).all()

        # One turn may have been rerun and clarified several times. Each rerun
        # owns an independent clarification branch through analysis_task_id;
        # replaying every historical branch mixes superseded answers into one
        # Runtime context. Restore the latest branch as a whole instead, while
        # retaining every complementary answer produced inside that branch.
        latest_answer_task_id = next(
            (
                row.analysis_task_id
                for row in reversed(rows)
                if row.message_kind == 'clarification_answer'
                and row.analysis_task_id is not None
            ),
            None,
        )
        if latest_answer_task_id is not None:
            rows = [
                row for row in rows
                if row.analysis_task_id == latest_answer_task_id
            ]

        request_by_id: dict[str, dict[str, Any]] = {}
        latest_by_item: dict[str, dict[str, Any]] = {}
        key_order: list[str] = []
        unkeyed_items: list[dict[str, Any]] = []
        for row in rows:
            payload = safe_json_loads(row.content_json, {})
            if row.message_kind == 'clarification_request':
                request = payload.get('clarification') if isinstance(payload, dict) else None
                if isinstance(request, dict):
                    clarification_id = str(request.get('clarification_id') or '').strip()
                    if clarification_id:
                        request_by_id[clarification_id] = request
                continue
            answer = (
                payload.get('clarification_answer')
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(answer, dict):
                continue
            answer_items = [
                item for item in answer.get('answers') or [] if isinstance(item, dict)
            ] or [answer]
            for item in answer_items:
                clarification_id = str(
                    item.get('clarification_id')
                    or answer.get('clarification_id')
                    or ''
                ).strip()
                request = request_by_id.get(clarification_id, {})
                answer_text = str(
                    item.get('resolved_value') or item.get('answer_text') or ''
                ).strip()
                if not answer_text:
                    continue
                semantic_scope = (
                    dict(item.get('semantic_scope') or {})
                    if isinstance(item.get('semantic_scope'), dict)
                    else dict(request.get('semantic_scope') or {})
                    if isinstance(request.get('semantic_scope'), dict)
                    else {}
                )
                subject = str(
                    semantic_scope.get('subject')
                    or semantic_scope.get('metric_name')
                    or ''
                ).strip()
                ambiguity_kind = str(
                    semantic_scope.get('ambiguity_kind')
                    or semantic_scope.get('clarification_kind')
                    or ''
                ).strip()
                item_id = str(item.get('clarification_item_id') or '').strip()
                question = str(
                    item.get('question') or request.get('question') or ''
                ).strip()
                normalized = {
                    **item,
                    'clarification_id': clarification_id,
                    'clarification_item_id': item_id,
                    'answer_text': str(item.get('answer_text') or answer_text).strip(),
                    'resolved_value': answer_text,
                    'semantic_scope': {
                        **semantic_scope,
                        **({'subject': subject} if subject else {}),
                        **({'ambiguity_kind': ambiguity_kind} if ambiguity_kind else {}),
                    },
                }
                if item_id:
                    key = f'item:{item_id}'
                elif clarification_id and question:
                    key = f'question:{clarification_id}:{question}'
                else:
                    unkeyed_items.append(normalized)
                    continue
                if key in latest_by_item:
                    key_order.remove(key)
                latest_by_item[key] = normalized
                key_order.append(key)

        return [latest_by_item[key] for key in key_order] + unkeyed_items

    def get_recent_artifacts(
        self,
        conversation_id: Any,
        limit_items: int = 3,
        max_turn_no: int | None = None,
    ) -> list[InsightNsArtifact]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        query = self.session.query(InsightNsArtifact).join(
            InsightNsTurn,
            InsightNsTurn.id == InsightNsArtifact.turn_id,
        ).filter(
            InsightNsArtifact.conversation_id == conversation_id_int,
            InsightNsArtifact.is_deleted == 0,
            InsightNsArtifact.content_enabled == 1,
            InsightNsTurn.is_deleted == 0,
        )
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)

        rows = query.order_by(
            InsightNsArtifact.created_at.desc(),
            InsightNsArtifact.id.desc(),
        ).limit(limit_items).all()
        return list(reversed(rows))

    def get_recent_executions(
        self,
        conversation_id: Any,
        limit_items: int = 3,
        max_turn_no: int | None = None,
        terminal_only: bool = False,
    ) -> list[InsightNsExecution]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        query = self.session.query(InsightNsExecution).join(
            InsightNsTurn,
            InsightNsTurn.id == InsightNsExecution.turn_id,
        ).filter(
            InsightNsExecution.conversation_id == conversation_id_int,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
            InsightNsTurn.is_deleted == 0,
        )
        if terminal_only:
            query = query.filter(InsightNsExecution.execution_status.in_(TERMINAL_EXECUTION_STATUSES))
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)

        rows = query.order_by(
            InsightNsExecution.created_at.desc(),
            InsightNsExecution.id.desc(),
        ).limit(limit_items).all()
        return list(reversed(rows))

    def get_latest_execution(
        self,
        turn_id: Any,
        started_at: datetime | None = None,
        analysis_task_id: int | None = None,
        successful_only: bool = False,
    ) -> InsightNsExecution | None:
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return None

        query = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id_int,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        )
        if started_at is not None:
            query = query.filter(InsightNsExecution.created_at >= started_at)
        if analysis_task_id:
            query = query.filter(InsightNsExecution.analysis_task_id == int(analysis_task_id))
        if successful_only:
            query = query.filter(InsightNsExecution.execution_status == 'success')

        return query.order_by(
            InsightNsExecution.created_at.desc(),
            InsightNsExecution.id.desc(),
        ).first()

    def get_latest_successful_analysis_turn(
        self,
        conversation_id: Any,
        max_turn_no: int | None = None,
    ) -> InsightNsTurn | None:
        """返回当前会话最近一条真正成功完成过分析执行的轮次。"""
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None

        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id_int)
        if not successful_turn_ids:
            return None

        query = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id_int,
            InsightNsTurn.id.in_(successful_turn_ids),
            InsightNsTurn.is_deleted == 0,
        )
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)

        return query.order_by(
            InsightNsTurn.turn_no.desc(),
            InsightNsTurn.id.desc(),
        ).first()

    def get_turn_executions(
        self,
        turn_id: Any,
        started_at: datetime | None = None,
        analysis_task_id: int | None = None,
    ) -> list[InsightNsExecution]:
        """返回某一轮内全部代码执行记录，按发生顺序排列。"""
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return []

        query = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id_int,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        )
        if started_at is not None:
            query = query.filter(InsightNsExecution.created_at >= started_at)
        if analysis_task_id:
            query = query.filter(InsightNsExecution.analysis_task_id == int(analysis_task_id))

        return query.order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()

    def get_memory(self, conversation_id: Any, memory_type: str) -> InsightNsMemory | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsMemory).filter(
            InsightNsMemory.conversation_id == conversation_id_int,
            InsightNsMemory.memory_type == memory_type,
            InsightNsMemory.is_deleted == 0,
        ).first()

    def set_memory(
        self,
        conversation_id: Any,
        memory_type: str,
        payload: dict[str, Any],
    ) -> None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0 or not memory_type.strip():
            raise ValueError("conversation memory requires a conversation and type")
        self._upsert_memory(conversation_id_int, memory_type.strip(), payload)

    def clear_memory(self, conversation_id: Any, memory_type: str) -> None:
        memory = self.get_memory(conversation_id, memory_type)
        if memory is None:
            return
        memory.content_json = '{}'
        memory.version = (memory.version or 0) + 1
        memory.updated_at = _now()
        self.session.flush()

    def build_runtime_summary_text(self, conversation_id: Any, max_turn_no: int | None = None) -> str:
        """实时根据当前有效轮次重建摘要，避免旧记忆污染当前 Prompt。"""
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return ''
        return self._build_rolling_summary_payload(
            conversation_id_int,
            max_turn_no=max_turn_no,
        ).get("summary_text", "")

    def get_context_compression_metadata(
        self,
        conversation_id: Any,
        max_turn_no: int | None = None,
    ) -> dict[str, Any]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return {}
        payload = self._build_rolling_summary_payload(
            conversation_id_int,
            max_turn_no=max_turn_no,
            metadata_only=True,
        )
        return {
            "enabled": Config.CONTEXT_COMPRESSION_ENABLED,
            "trigger_turns": Config.CONTEXT_COMPRESSION_TRIGGER_TURNS,
            "keep_recent_turns": Config.CONTEXT_COMPRESSION_KEEP_RECENT_TURNS,
            "covered_until_turn_no": payload.get("covered_until_turn_no", 0),
            "compressed_turn_count": len(payload.get("compressed_turn_summaries", []) or []),
            "summary_updated_at": payload.get("updated_at"),
            "state": payload.get("compression_state", "ready"),
        }

    def build_runtime_analysis_state(
        self,
        conversation_id: Any,
        max_turn_no: int | None = None,
        active_snapshot_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """实时根据数据库状态重建当前分析状态，而不是直接复用旧记忆快照。"""
        conversation_id_int = to_int(conversation_id, 0)
        conversation = self._get_conversation_by_id(conversation_id_int)
        if conversation is None:
            return {}

        return {
            "active_datasource_snapshot": active_snapshot_override or safe_json_loads(conversation.active_datasource_snapshot, {}),
            "recent_turn_datasource_usage": self._build_recent_turn_datasource_usage(
                conversation_id_int,
                limit_turns=3,
                max_turn_no=max_turn_no,
            ),
            "recent_execution_summaries": self._build_recent_execution_summaries(
                conversation_id_int,
                limit_items=3,
                max_turn_no=max_turn_no,
            ),
            "latest_execution": self._build_latest_execution_summary(
                conversation_id_int,
                max_turn_no=max_turn_no,
            ),
            "latest_artifacts": [
                self._build_artifact_summary_item(artifact)
                for artifact in self.get_recent_artifacts(
                    conversation_id_int,
                    limit_items=3,
                    max_turn_no=max_turn_no,
                )
            ],
            "context_compression": self.get_context_compression_metadata(
                conversation_id_int,
                max_turn_no=max_turn_no,
            ),
            "last_turn_no": max_turn_no if max_turn_no is not None else conversation.last_turn_no,
        }

    def get_active_datasource_snapshot(self, conversation_id: Any) -> dict[str, Any]:
        conversation = self._get_conversation_by_id(to_int(conversation_id, 0))
        if conversation is None:
            return {}
        return safe_json_loads(conversation.active_datasource_snapshot, {})

    def _create_conversation(self, username: str, namespace_id: Any, user_message: str) -> InsightNsConversation:
        conversation = InsightNsConversation(
            insight_namespace_id=to_int(namespace_id, 0),
            title=build_conversation_title(user_message),
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=0,
            last_message_at=_now(),
            user_message='',
            insight_result='',
        )
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _get_conversation_by_id(self, conversation_id: int) -> InsightNsConversation | None:
        if conversation_id <= 0:
            return None
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _get_turn_by_id(self, turn_id: int) -> InsightNsTurn | None:
        if turn_id <= 0:
            return None
        return self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id,
            InsightNsTurn.is_deleted == 0,
        ).first()

    def _reset_turn_runtime_state(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
    ) -> None:
        """重跑开始时只标记运行态，旧结果等新结果成功后再替换。"""
        now = _now()
        turn.status = 'running'
        turn.error_message = ''
        turn.started_at = now
        turn.finished_at = None

    def _archive_previous_turn_results(
        self,
        conversation: InsightNsConversation,
        turn: InsightNsTurn,
        started_at: datetime | None,
    ) -> None:
        """刷新成功后归档旧结果，避免刷新失败时把用户已看到的结果清空。"""
        now = _now()
        self.session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == conversation.id,
            InsightNsMessage.turn_id == turn.id,
            InsightNsMessage.role == 'assistant',
            InsightNsMessage.is_deleted == 0,
            InsightNsMessage.content_enabled == 1,
        ).update(
            {InsightNsMessage.content_enabled: 0},
            synchronize_session=False,
        )
        self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
            InsightNsArtifact.content_enabled == 1,
        ).update(
            {InsightNsArtifact.content_enabled: 0},
            synchronize_session=False,
        )
        execution_query = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        )
        if started_at is not None:
            execution_query = execution_query.filter(InsightNsExecution.created_at < started_at)
        execution_query.update(
            {
                InsightNsExecution.content_enabled: 0,
                InsightNsExecution.updated_at: now,
            },
            synchronize_session=False,
        )

    def _next_seq_no(self, conversation_id: int, turn_no: int) -> int:
        last_seq = self.session.query(func.max(InsightNsMessage.seq_no)).filter(
            InsightNsMessage.insight_conversation_id == conversation_id,
            InsightNsMessage.turn_no == turn_no,
        ).scalar()
        return int(last_seq or 0) + 1

    def _next_turn_no(self, conversation_id: int, current_turn_no: int) -> int:
        max_turn_no = self.session.query(func.max(InsightNsTurn.turn_no)).filter(
            InsightNsTurn.conversation_id == conversation_id,
        ).scalar()
        return max(int(current_turn_no or 0), int(max_turn_no or 0)) + 1

    def _get_conversation_for_run(
        self,
        conversation_id: Any,
        *,
        username: str,
        allow_internal_namespace: bool,
    ) -> InsightNsConversation | None:
        """按调用用户和调用通道恢复会话，隐藏工作流上下文不进入交互链路。"""
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        query = self.session.query(InsightNsConversation).join(
            InsightNamespace,
            InsightNamespace.id == InsightNsConversation.insight_namespace_id,
        ).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.is_deleted == 0,
            InsightNamespace.username == (username or 'anonymous'),
            InsightNamespace.is_deleted == 0,
        )
        if not allow_internal_namespace:
            query = query.filter(InsightNamespace.namespace_kind == 'interactive')
        return query.first()

    def _get_namespace_for_run(
        self,
        namespace_id: Any,
        *,
        username: str,
        allow_internal_namespace: bool,
    ) -> InsightNamespace | None:
        namespace_id_int = to_int(namespace_id, 0)
        if namespace_id_int <= 0:
            return None
        query = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == namespace_id_int,
            InsightNamespace.username == (username or 'anonymous'),
            InsightNamespace.is_deleted == 0,
        )
        if not allow_internal_namespace:
            query = query.filter(InsightNamespace.namespace_kind == 'interactive')
        return query.first()

    def _get_conversation_namespace_id_subquery(self, conversation_id: int):
        return self.session.query(InsightNsConversation.insight_namespace_id).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).scalar_subquery()

    def _merge_datasource_snapshot(
        self,
        conversation: InsightNsConversation,
    ) -> dict[str, Any]:
        """
        刷新会话级数据源快照。

        当前数据源范围以会话级数据源关系表为准，
        不再接收前端直接传入的 selected_datasource_ids。
        """
        snapshot = safe_json_loads(conversation.active_datasource_snapshot, {})
        selected_datasource_ids = self._load_bound_datasource_ids(conversation.id)
        selected_datasource_snapshot = self._build_turn_datasource_snapshot(
            conversation_id=conversation.id,
            datasource_ids=selected_datasource_ids,
        )

        snapshot["namespace_id"] = conversation.insight_namespace_id
        snapshot["conversation_id"] = conversation.id
        snapshot["selected_datasource_ids"] = selected_datasource_ids
        snapshot["selected_datasource_snapshot"] = selected_datasource_snapshot
        snapshot["updated_at"] = _now().isoformat()
        return snapshot

    def _load_bound_datasource_ids(self, conversation_id: int) -> list[int]:
        """按会话级关系表加载当前轮实际可用的数据源 ID 列表。"""
        if conversation_id <= 0:
            return []

        rows = self.session.query(InsightNsRelDatasource.datasource_id).join(
            InsightDatasource,
            InsightDatasource.id == InsightNsRelDatasource.datasource_id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightNsRelDatasource.sort_no.asc(),
            InsightNsRelDatasource.id.asc(),
        ).all()

        datasource_ids: list[int] = []
        seen: set[int] = set()
        for row in rows:
            datasource_id = to_int(row[0], 0)
            if datasource_id <= 0 or datasource_id in seen:
                continue
            datasource_ids.append(datasource_id)
            seen.add(datasource_id)
        return datasource_ids

    def _filter_valid_datasource_ids(self, conversation_id: int, datasource_ids: list[int]) -> list[int]:
        """把所选数据源 ID 过滤为当前会话下仍然有效的绑定数据源。"""
        normalized_ids = [
            datasource_id
            for datasource_id in (to_int(item, 0) for item in datasource_ids or [])
            if datasource_id > 0
        ]
        if not normalized_ids or conversation_id <= 0:
            return []

        rows = self.session.query(InsightNsRelDatasource.datasource_id).join(
            InsightDatasource,
            InsightDatasource.id == InsightNsRelDatasource.datasource_id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id,
            InsightNsRelDatasource.datasource_id.in_(normalized_ids),
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).all()
        valid_ids = {int(row[0]) for row in rows}

        deduplicated: list[int] = []
        seen: set[int] = set()
        for datasource_id in normalized_ids:
            if datasource_id in valid_ids and datasource_id not in seen:
                deduplicated.append(datasource_id)
                seen.add(datasource_id)
        return deduplicated

    def _build_turn_datasource_snapshot(
        self,
        conversation_id: Any,
        datasource_ids: list[int],
    ) -> list[dict[str, Any]]:
        """把所选数据源 ID 解析成不可变的轮次级快照。"""
        conversation_id_int = to_int(conversation_id, 0)
        normalized_ids = self._filter_valid_datasource_ids(conversation_id_int, datasource_ids)
        if not normalized_ids:
            return []

        rows = self.session.query(InsightDatasource, InsightNsRelDatasource.bind_source).join(
            InsightNsRelDatasource,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id_int,
            InsightDatasource.id.in_(normalized_ids),
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).all()

        payload_map = {
            item["datasource_id"]: item
            for datasource, bind_source in rows
            for item in [{
                **self._build_datasource_snapshot_item(datasource),
                "source_role": (
                    "user_selected" if bind_source in {"user_selected", "import_selected"}
                    else "conversation_candidate"
                ),
            }]
        }
        return [payload_map[datasource_id] for datasource_id in normalized_ids if datasource_id in payload_map]

    def build_datasource_snapshot_by_ids(
        self,
        *,
        namespace_id: int,
        datasource_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Build a turn-scoped snapshot for namespace or shared UNS datasources."""
        normalized_ids = list(dict.fromkeys(
            datasource_id
            for datasource_id in (to_int(item, 0) for item in datasource_ids or [])
            if datasource_id > 0
        ))
        if not normalized_ids:
            return []
        rows = self.session.query(InsightDatasource).filter(
            InsightDatasource.id.in_(normalized_ids),
            InsightDatasource.insight_namespace_id.in_([0, int(namespace_id)]),
            InsightDatasource.is_deleted == 0,
        ).all()
        payload_map = {
            item['datasource_id']: item
            for item in (self._build_datasource_snapshot_item(row) for row in rows)
        }
        return [
            payload_map[datasource_id]
            for datasource_id in normalized_ids
            if datasource_id in payload_map
        ]

    def hydrate_datasource_snapshot_query_access(
        self,
        datasource_snapshot: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Add access contracts to legacy snapshots without re-importing UNS."""
        rows = [
            copy.deepcopy(item)
            for item in datasource_snapshot or []
            if isinstance(item, dict)
        ]
        missing_ids = {
            to_int(item.get('datasource_id') or item.get('id'), 0)
            for item in rows
            if item.get('datasource_type') == 'table'
            and not isinstance(item.get('query_access'), dict)
        }
        missing_ids.discard(0)
        if not missing_ids:
            return rows
        datasources = self.session.query(InsightDatasource).filter(
            InsightDatasource.id.in_(missing_ids),
            InsightDatasource.is_deleted == 0,
        ).all()
        access_by_id = {
            int(datasource.id): build_table_query_access(
                safe_json_loads(datasource.datasource_config_json, {})
            )
            for datasource in datasources
        }
        for item in rows:
            access = access_by_id.get(
                to_int(item.get('datasource_id') or item.get('id'), 0)
            )
            if access:
                item['query_access'] = access
        return rows

    def merge_turn_datasource_snapshot(
        self,
        *,
        turn_id: int,
        datasource_snapshot: list[dict[str, Any]],
    ) -> None:
        """Persist newly materialized sources in the immutable current-turn scope."""
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == int(turn_id),
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            raise ValueError('analysis turn does not exist')
        rows = [
            dict(item)
            for item in safe_json_loads(turn.selected_datasource_snapshot_json, [])
            if isinstance(item, dict)
        ]
        rows_by_id = {
            to_int(item.get('datasource_id') or item.get('id'), 0): item
            for item in rows
            if to_int(item.get('datasource_id') or item.get('id'), 0) > 0
        }
        ordered_ids = list(rows_by_id)
        for item in datasource_snapshot:
            if not isinstance(item, dict):
                continue
            datasource_id = to_int(
                item.get('datasource_id') or item.get('id'), 0
            )
            if datasource_id <= 0:
                continue
            if datasource_id not in rows_by_id:
                ordered_ids.append(datasource_id)
            rows_by_id[datasource_id] = dict(item)
        turn.selected_datasource_ids_json = dump_json(ordered_ids)
        turn.selected_datasource_snapshot_json = dump_json([
            rows_by_id[datasource_id] for datasource_id in ordered_ids
        ])
        self.session.commit()

    def _ensure_conversation_resource_bindings(self, conversation: InsightNsConversation) -> None:
        """
        根据当前空间下的默认关系，回填会话级资源关系。

        这样既兼容当前“空间与会话近似 1:1”的业务现实，
        也为后续“空间 1:N 会话”的结构升级预留好了落点。
        """
        has_datasource_rows = self.session.query(InsightNsRelDatasource.id).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation.id,
            InsightNsRelDatasource.is_deleted == 0,
        ).first()
        if has_datasource_rows is None:
            namespace_rows = self.session.query(InsightNsRelDatasource).filter(
                InsightNsRelDatasource.insight_conversation_id == 0,
                InsightNsRelDatasource.is_deleted == 0,
            ).order_by(
                InsightNsRelDatasource.sort_no.asc(),
                InsightNsRelDatasource.id.asc(),
            ).limit(10)
            for row in namespace_rows:
                self.session.add(InsightNsRelDatasource(
                    insight_namespace_id=conversation.insight_namespace_id,
                    insight_conversation_id=conversation.id,
                    datasource_id=row.datasource_id,
                    is_active=row.is_active,
                    sort_no=row.sort_no,
                    bind_source=getattr(row, 'bind_source', 'system_default') or 'system_default',
                    is_deleted=0,
                ))

        has_knowledge_rows = self.session.query(InsightNsRelKnowledge.id).filter(
            InsightNsRelKnowledge.insight_conversation_id == conversation.id,
            InsightNsRelKnowledge.is_deleted == 0,
        ).first()
        if has_knowledge_rows is None:
            namespace_rows = self.session.query(InsightNsRelKnowledge).filter(
                InsightNsRelKnowledge.insight_namespace_id == conversation.insight_namespace_id,
                InsightNsRelKnowledge.insight_conversation_id == 0,
                InsightNsRelKnowledge.is_deleted == 0,
            ).order_by(InsightNsRelKnowledge.id.asc()).all()
            for row in namespace_rows:
                self.session.add(InsightNsRelKnowledge(
                    insight_namespace_id=conversation.insight_namespace_id,
                    insight_conversation_id=conversation.id,
                    knowledge_id=row.knowledge_id,
                    is_deleted=0,
                ))
        self.session.flush()

    def _build_datasource_snapshot_item(self, datasource: InsightDatasource) -> dict[str, Any]:
        """构造一条写入会话和轮次快照中的数据源摘要。"""
        config_json = safe_json_loads(datasource.datasource_config_json, {})
        datasource_type = normalize_datasource_type(datasource.datasource_type)
        uns_node_id = str(datasource.uns_node_id or '').strip()
        payload = {
            "datasource_id": datasource.id,
            "datasource_type": datasource_type,
            "datasource_name": datasource.datasource_name,
            "datasource_identifier": extract_datasource_identifier(datasource, config_json),
            "metadata_schema": extract_datasource_schema(datasource, config_json),
            # Selection routing needs source provenance, but runtime analysis does
            # not need the physical UNS identifier in its prompt context.
            "selection_origin": "uns" if uns_node_id else "external",
            "uns_node_id": uns_node_id,
            "uns_path": str(config_json.get("uns_path") or ""),
        }
        if datasource_type == "local_file":
            payload["recommended_loader"] = recommend_local_file_loader(
                config_json,
                Config.LOCAL_FILE_LOW_MEMORY_THRESHOLD_BYTES,
            )
        if datasource_type == "table":
            if str(config_json.get("uns_data_type") or "") in {"2", "7"}:
                # UNS definitions enumerate business fields, not all physical
                # platform columns returned by the query endpoint.
                payload["schema_scope"] = "declared_business_fields"
            payload.update(build_table_runtime_metadata(config_json))
            payload["query_access"] = build_table_query_access(config_json)
        sheet_name = str(config_json.get("sheet_name") or "").strip()
        if sheet_name:
            payload["sheet_name"] = sheet_name
        return payload

    def _create_artifact(
        self,
        conversation_id: int,
        turn_id: int,
        execution_id: int,
        artifact_type: str,
        title: str,
        summary_text: str,
        content: dict[str, Any],
        metadata: dict[str, Any],
        analysis_task_id: int | None = None,
        sort_no: int = 0,
    ) -> dict[str, Any]:
        artifact = InsightNsArtifact(
            conversation_id=conversation_id,
            turn_id=turn_id,
            execution_id=execution_id,
            artifact_type=artifact_type,
            title=title,
            summary_text=summary_text or '',
            content_json=dump_json(content or {}),
            metadata_json=dump_json(metadata),
            sort_no=sort_no,
            content_enabled=1,
            analysis_task_id=analysis_task_id,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact.to_dict()

    def _refresh_memories(self, conversation: InsightNsConversation, *, refresh_summary: bool = True) -> None:
        """刷新下一轮 Prompt 组装要使用的压缩记忆。"""
        if refresh_summary:
            rolling_summary_payload = self._build_rolling_summary_payload(conversation.id)
            conversation.summary_text = str(rolling_summary_payload.get("summary_text") or "")
            self._upsert_memory(conversation.id, 'rolling_summary', rolling_summary_payload)

        # `analysis_state` 保存下一轮继续分析时要读取的结构化状态。
        self._upsert_memory(
            conversation.id,
            'analysis_state',
            self.build_runtime_analysis_state(conversation.id),
        )

    def _build_rolling_summary_payload(
        self,
        conversation_id: int,
        max_turn_no: int | None = None,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        query = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        )
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)

        turns = query.order_by(InsightNsTurn.turn_no.asc()).all()
        if not turns:
            return {
                "summary_text": "",
                "compressed_turn_summaries": [],
                "covered_until_turn_no": 0,
                "updated_at": _now().isoformat(),
            }

        if not Config.CONTEXT_COMPRESSION_ENABLED:
            summary_text = self._build_summary_text(
                conversation_id,
                max_turn_no=max_turn_no,
                limit_turns=6,
            )
            return {
                "summary_text": summary_text,
                "compressed_summary_text": summary_text,
                "compressed_turn_summaries": [],
                "covered_until_turn_no": 0,
                "updated_at": _now().isoformat(),
            }

        trigger_turns = max(Config.CONTEXT_COMPRESSION_TRIGGER_TURNS, 1)
        keep_recent_turns = max(Config.CONTEXT_COMPRESSION_KEEP_RECENT_TURNS, 1)
        if len(turns) < trigger_turns:
            summary_text = self._build_summary_text(
                conversation_id,
                max_turn_no=max_turn_no,
                limit_turns=max(trigger_turns, 6),
            )
            return {
                "summary_text": summary_text,
                "compressed_summary_text": summary_text,
                "compressed_turn_summaries": [],
                "covered_until_turn_no": 0,
                "updated_at": _now().isoformat(),
            }

        compressed_turns = turns[:-keep_recent_turns] if len(turns) > keep_recent_turns else []
        compressed_payloads = self._build_turn_summary_payloads(conversation_id, compressed_turns)
        compression_config = {
            'version': 1,
            'max_chars': Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS,
            'use_llm': bool(Config.CONTEXT_COMPRESSION_USE_LLM),
        }
        memory = self.get_memory(conversation_id, 'rolling_summary')
        previous = safe_json_loads(memory.content_json, {}) if memory is not None else {}
        # Reuse only an exact producer input. A refreshed historical answer,
        # changed source scope, or a different rerun history boundary must not
        # inherit a summary of another set of turns.
        reusable = (
            isinstance(previous, dict)
            and previous.get('compression_config') == compression_config
            and previous.get('compression_reusable') is True
            and previous.get('compressed_turn_summaries') == compressed_payloads
            and isinstance(previous.get('compressed_summary_text'), str)
        )
        compression_key = (
            Config.DB_HOST, Config.DB_PORT, Config.DB_NAME, conversation_id,
            hashlib.sha256(dump_json({'config': compression_config, 'turns': compressed_payloads}).encode('utf-8')).hexdigest(),
        )
        # Prompt readers may ask for a bounded historical prefix through
        # separate sessions. They must not overwrite the conversation's full
        # durable memory, but identical prefixes still have one producer input.
        cached_summary = history_compression_cache.get(compression_key)
        if metadata_only and not reusable and cached_summary is None:
            return {'covered_until_turn_no': 0, 'compressed_turn_summaries': [],
                    'updated_at': None, 'compression_state': 'pending'}
        compression_started = time.monotonic()
        compression_reusable = reusable or cached_summary is not None
        if reusable:
            compressed_summary_text = previous['compressed_summary_text']
        elif cached_summary is not None:
            compressed_summary_text = cached_summary
        else:
            summarizer = build_llm_turn_summarizer() if compressed_payloads else None
            compression_reusable = not compressed_payloads or not compression_config['use_llm']
            def summarize_with_receipt(text):
                nonlocal compression_reusable
                candidate = summarizer(text)
                compression_reusable = isinstance(candidate, str) and bool(candidate.strip())
                return candidate
            compressed_summary_text = summarize_turns_incrementally(
                turn_payloads=compressed_payloads,
                max_chars=Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS,
                summarizer=summarize_with_receipt if summarizer is not None else None,
            )
        if compression_reusable:
            history_compression_cache.put(compression_key, compressed_summary_text)
        logger.info(
            'conversation history compression: conversation_id=%s compressed_turns=%s '
            'reused=%s elapsed_seconds=%.3f',
            conversation_id, len(compressed_payloads), reusable or cached_summary is not None,
            time.monotonic() - compression_started,
        )
        recent_text = self._build_summary_text(
            conversation_id,
            max_turn_no=max_turn_no,
            start_turn_no=(compressed_turns[-1].turn_no + 1) if compressed_turns else None,
            limit_turns=keep_recent_turns,
        )
        summary_text = "\n".join(
            item for item in [compressed_summary_text.strip(), recent_text.strip()] if item
        )
        return {
            "summary_text": clip_text(summary_text, Config.CONTEXT_COMPRESSION_MAX_SUMMARY_CHARS),
            "compressed_summary_text": compressed_summary_text,
            "compressed_turn_summaries": compressed_payloads,
            "compression_config": compression_config,
            "compression_reusable": compression_reusable,
            "covered_until_turn_no": compressed_turns[-1].turn_no if compressed_turns else 0,
            "updated_at": _now().isoformat(),
        }

    def _build_turn_summary_payloads(
        self,
        conversation_id: int,
        turns: list[InsightNsTurn],
    ) -> list[dict[str, Any]]:
        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id)
        payloads: list[dict[str, Any]] = []
        for turn in turns:
            question = clip_text((turn.user_query or '').strip().replace('\n', ' '), 120)
            answer = clip_text((turn.final_answer or turn.error_message or '').strip().replace('\n', ' '), 200)
            payloads.append({
                "turn_id": turn.id,
                "turn_no": turn.turn_no,
                "used_datasource_ids": safe_json_loads(getattr(turn, 'used_datasource_ids_json', ''), []),
                "question": question,
                "answer": answer if turn.id in successful_turn_ids and '<tool_call>' not in answer else '',
            })
        return payloads

    def _build_summary_text(
        self,
        conversation_id: int,
        max_turn_no: int | None = None,
        start_turn_no: int | None = None,
        limit_turns: int = 6,
    ) -> str:
        """根据最近几轮构建紧凑的自然语言摘要。"""
        query = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        )
        if start_turn_no is not None and start_turn_no > 0:
            query = query.filter(InsightNsTurn.turn_no >= start_turn_no)
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)
        turns = query.order_by(InsightNsTurn.turn_no.desc()).limit(limit_turns).all()
        turns.reverse()
        if not turns:
            return ''

        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id)
        parts: list[str] = []
        for turn in turns:
            question = clip_text((turn.user_query or '').strip().replace('\n', ' '), 120)
            answer = clip_text((turn.final_answer or turn.error_message or '').strip().replace('\n', ' '), 200)
            if turn.id in successful_turn_ids and answer and '<tool_call>' not in answer:
                parts.append(f"第{turn.turn_no}轮 用户: {question}；系统结论: {answer}")
            else:
                parts.append(f"第{turn.turn_no}轮 用户: {question}")
        return '\n'.join(parts)

    def _build_recent_turn_datasource_usage(
        self,
        conversation_id: int,
        limit_turns: int = 3,
        max_turn_no: int | None = None,
    ) -> list[dict[str, Any]]:
        """收集最近几轮使用过的数据源 ID。"""
        query = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        )
        if max_turn_no is not None and max_turn_no >= 0:
            query = query.filter(InsightNsTurn.turn_no <= max_turn_no)
        turns = query.order_by(InsightNsTurn.turn_no.desc()).limit(limit_turns).all()
        turns.reverse()
        return [
            {
                "turn_id": turn.id,
                "turn_no": turn.turn_no,
                "selected_datasource_ids": safe_json_loads(turn.selected_datasource_ids_json, []),
                "used_datasource_ids": safe_json_loads(getattr(turn, 'used_datasource_ids_json', ''), []),
            }
            for turn in turns
        ]

    def _build_recent_execution_summaries(
        self,
        conversation_id: int,
        limit_items: int = 3,
        max_turn_no: int | None = None,
    ) -> list[dict[str, Any]]:
        """收集最近的执行摘要，供结构化记忆使用。"""
        executions = self.get_recent_executions(
            conversation_id,
            limit_items=limit_items,
            max_turn_no=max_turn_no,
            terminal_only=True,
        )
        return [self._build_execution_summary_item(execution) for execution in executions]

    def _build_latest_execution_summary(self, conversation_id: int, max_turn_no: int | None = None) -> dict[str, Any]:
        """返回最近一次执行摘要，并附带完整代码以支撑后续追问。"""
        executions = self.get_recent_executions(
            conversation_id,
            limit_items=1,
            max_turn_no=max_turn_no,
            terminal_only=True,
        )
        if not executions:
            return {}
        return self._build_execution_summary_item(executions[-1], include_code=True)

    def _build_execution_summary_item(
        self,
        execution: InsightNsExecution,
        include_code: bool = False,
    ) -> dict[str, Any]:
        """把一条执行记录转换成适合写入记忆的摘要结构。"""
        result_payload = safe_json_loads(execution.result_payload_json, {})
        chart_count = len(result_payload.get('charts') or []) if isinstance(result_payload, dict) else 0
        table_count = len(result_payload.get('tables') or []) if isinstance(result_payload, dict) else 0
        payload = {
            "execution_id": execution.id,
            "turn_id": execution.turn_id,
            "tool_call_id": execution.tool_call_id,
            "title": execution.title,
            "description": execution.description,
            "execution_status": execution.execution_status,
            "used_datasource_ids": safe_json_loads(getattr(execution, 'used_datasource_ids_json', ''), []),
            "analysis_report_preview": clip_text(
                execution.analysis_report or '',
                Config.CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS,
            ),
            "chart_count": chart_count,
            "table_count": table_count,
            "error_message": execution.error_message,
            "execution_seconds": execution.execution_seconds,
            "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        }
        if include_code:
            payload["generated_code"] = clip_text(
                execution.generated_code or '',
                Config.CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS,
            )
        else:
            payload["generated_code_preview"] = clip_text(
                execution.generated_code or '',
                Config.CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS,
            )
        return payload

    def _build_artifact_summary_item(self, artifact: InsightNsArtifact) -> dict[str, Any]:
        """把产物压缩成适合会话记忆的轻量摘要。"""
        content = safe_json_loads(artifact.content_json, {})
        summary = {
            "id": artifact.id,
            "turn_id": artifact.turn_id,
            "execution_id": artifact.execution_id,
            "artifact_type": artifact.artifact_type,
            "title": artifact.title,
            "summary_text": clip_text(
                artifact.summary_text or '',
                Config.CONTEXT_COMPRESSION_ARTIFACT_SUMMARY_CHARS,
            ),
            "sort_no": artifact.sort_no,
            "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        }

        if artifact.artifact_type == 'chart':
            chart_spec = content.get('chart_spec', {}) if isinstance(content, dict) else {}
            summary["chart_type"] = content.get('chart_type', '')
            summary["chart_series_count"] = len(chart_spec.get('series', [])) if isinstance(chart_spec, dict) else 0
        elif artifact.artifact_type == 'table':
            summary["row_count"] = len(content.get('rows', [])) if isinstance(content, dict) else 0
            summary["column_count"] = len(content.get('columns', [])) if isinstance(content, dict) else 0

        return summary

    def _upsert_memory(self, conversation_id: int, memory_type: str, payload: dict[str, Any]) -> None:
        """插入或更新一条记忆记录，并同步提升版本号。"""
        memory = self.session.query(InsightNsMemory).filter(
            InsightNsMemory.conversation_id == conversation_id,
            InsightNsMemory.memory_type == memory_type,
            InsightNsMemory.is_deleted == 0,
        ).first()

        if memory is None:
            memory = InsightNsMemory(
                conversation_id=conversation_id,
                memory_type=memory_type,
                content_json=dump_json(payload),
                version=1,
            )
            self.session.add(memory)
        else:
            memory.content_json = dump_json(payload)
            memory.version = (memory.version or 0) + 1
            memory.updated_at = _now()

        self.session.flush()

    def _get_successful_execution_turn_ids(self, conversation_id: int) -> set[int]:
        """返回当前会话中真正完成过分析执行的轮次集合。"""
        rows = self.session.query(InsightNsExecution.turn_id).filter(
            InsightNsExecution.conversation_id == conversation_id,
            InsightNsExecution.execution_status == 'success',
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        ).all()
        return {to_int(row[0], 0) for row in rows if to_int(row[0], 0) > 0}

    def _get_execution_turn_ids(self, conversation_id: int) -> set[int]:
        """返回当前会话中曾进入过分析执行流程的轮次集合。"""
        rows = self.session.query(InsightNsExecution.turn_id).filter(
            InsightNsExecution.conversation_id == conversation_id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        ).all()
        return {to_int(row[0], 0) for row in rows if to_int(row[0], 0) > 0}
