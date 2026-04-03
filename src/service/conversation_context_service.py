from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

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
)
from utils.datasource_utils import (
    build_conversation_title,
    dump_json,
    extract_datasource_identifier,
    extract_datasource_schema,
    normalize_datasource_type,
    safe_json_loads,
    to_int,
)

ROLE_TO_TYPE = {
    'system': 0,
    'summary': 0,
    'user': 1,
    'assistant': 2,
    'tool': 3,
}


def _now() -> datetime:
    return datetime.now()


@dataclass
class ConversationRunContext:
    """一轮分析启动后返回的内存态运行上下文。"""

    conversation: InsightNsConversation
    turn: InsightNsTurn
    active_datasource_snapshot: dict[str, Any]


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
    ) -> ConversationRunContext:
        """创建或恢复会话，并开启新一轮分析。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            conversation = self._create_conversation(
                username=username,
                namespace_id=namespace_id,
                user_message=user_message,
            )

        self._ensure_conversation_resource_bindings(conversation)

        # 第一步：生成当前会话维度上的最新数据源选择快照。
        next_turn_no = self._next_turn_no(conversation.id, conversation.last_turn_no or 0)
        active_snapshot = self._merge_datasource_snapshot(conversation=conversation)
        selected_ids = list(active_snapshot.get('selected_datasource_ids', []))

        # 第二步：把本轮使用的数据源快照固化到 turn 上，
        # 避免后续轮次覆盖这一轮的历史事实。
        turn_snapshot = self._build_turn_datasource_snapshot(
            conversation_id=conversation.id,
            datasource_ids=selected_ids,
        )

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
    ) -> InsightNsMessage:
        """持久化一条用于多轮上下文重放的消息记录。"""
        message = InsightNsMessage(
            username=conversation.username,
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
        )
        self.session.add(message)
        self.session.flush()
        return message

    def complete_run(
        self,
        conversation_id: int,
        turn_id: int,
        assistant_message: str,
        analysis_report: str,
        file_id: str,
    ) -> dict[str, Any]:
        """结束一次成功轮次，并刷新会话记忆。"""
        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return {}

        final_answer = (analysis_report or assistant_message or '').strip()
        turn.final_answer = final_answer
        turn.status = 'success'
        turn.error_message = ''
        turn.finished_at = _now()

        conversation.insight_result = final_answer
        conversation.last_message_at = _now()
        conversation.updated_at = _now()

        latest_execution = self.get_latest_execution(turn.id)
        self.add_message(
            conversation=conversation,
            turn=turn,
            role='assistant',
            message_kind='final_answer',
            content=final_answer or assistant_message or '',
            content_json={
                "assistant_message": assistant_message or '',
                "analysis_report": analysis_report or '',
                "file_id": file_id or '',
            },
        )

        artifacts: list[dict[str, Any]] = []
        if file_id:
            # 图表和报告是执行记录的派生产物，
            # 这里会反向关联到最近一次执行记录，而不是把它们视为主产物。
            artifacts.append(self._create_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=latest_execution.id if latest_execution else 0,
                artifact_type='chart',
                title=f"{conversation.title} 图表",
                file_id=file_id,
                summary_text=analysis_report or assistant_message,
                metadata={"turn_no": turn.turn_no},
            ))
        if analysis_report:
            artifacts.append(self._create_artifact(
                conversation_id=conversation.id,
                turn_id=turn.id,
                execution_id=latest_execution.id if latest_execution else 0,
                artifact_type='report',
                title=f"{conversation.title} 报告",
                file_id='',
                summary_text=analysis_report,
                metadata={"turn_no": turn.turn_no},
            ))

        self._refresh_memories(conversation)
        self.session.commit()
        return {
            "conversation": conversation,
            "turn": turn,
            "artifacts": artifacts,
        }

    def fail_run(self, conversation_id: int, turn_id: int, error_message: str) -> None:
        """结束一次失败轮次，同时保持整体上下文流程不变。"""
        conversation = self._get_conversation_by_id(conversation_id)
        turn = self._get_turn_by_id(turn_id)
        if not conversation or not turn:
            return

        turn.status = 'failed'
        turn.error_message = error_message
        turn.finished_at = _now()
        conversation.status = 'active'
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

    def get_recent_prompt_messages(self, conversation_id: Any, limit_messages: int = 10) -> list[InsightNsMessage]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        rows = self.session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == conversation_id_int,
            InsightNsMessage.role.in_(['user', 'assistant']),
            InsightNsMessage.message_kind.in_(['prompt', 'final_answer']),
            InsightNsMessage.is_deleted == 0,
        ).order_by(
            InsightNsMessage.created_at.desc(),
            InsightNsMessage.id.desc(),
        ).limit(limit_messages * 3).all()

        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id_int)
        filtered_rows: list[InsightNsMessage] = []
        for row in rows:
            content = (row.content or '').strip()
            if row.role == 'assistant':
                # 只有真正成功执行过分析任务的轮次，才把助手最终回答纳入历史重放。
                # 这样可以避免“未真正执行、仅靠记忆复述”的回答继续污染后续上下文。
                if row.turn_id not in successful_turn_ids:
                    continue
                if '<tool_call>' in content:
                    continue
            filtered_rows.append(row)
            if len(filtered_rows) >= limit_messages:
                break

        return list(reversed(filtered_rows))

    def get_recent_artifacts(self, conversation_id: Any, limit_items: int = 3) -> list[InsightNsArtifact]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        rows = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation_id_int,
            InsightNsArtifact.is_deleted == 0,
        ).order_by(
            InsightNsArtifact.created_at.desc(),
            InsightNsArtifact.id.desc(),
        ).limit(limit_items).all()
        return list(reversed(rows))

    def get_recent_executions(self, conversation_id: Any, limit_items: int = 3) -> list[InsightNsExecution]:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return []

        rows = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation_id_int,
            InsightNsExecution.is_deleted == 0,
        ).order_by(
            InsightNsExecution.created_at.desc(),
            InsightNsExecution.id.desc(),
        ).limit(limit_items).all()
        return list(reversed(rows))

    def get_latest_execution(self, turn_id: Any) -> InsightNsExecution | None:
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return None

        return self.session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id_int,
            InsightNsExecution.is_deleted == 0,
        ).order_by(
            InsightNsExecution.created_at.desc(),
            InsightNsExecution.id.desc(),
        ).first()

    def get_memory(self, conversation_id: Any, memory_type: str) -> InsightNsMemory | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsMemory).filter(
            InsightNsMemory.conversation_id == conversation_id_int,
            InsightNsMemory.memory_type == memory_type,
            InsightNsMemory.is_deleted == 0,
        ).first()

    def build_runtime_summary_text(self, conversation_id: Any) -> str:
        """实时根据当前有效轮次重建摘要，避免旧记忆污染当前 Prompt。"""
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return ''
        return self._build_summary_text(conversation_id_int)

    def build_runtime_analysis_state(self, conversation_id: Any) -> dict[str, Any]:
        """实时根据数据库状态重建当前分析状态，而不是直接复用旧记忆快照。"""
        conversation_id_int = to_int(conversation_id, 0)
        conversation = self._get_conversation_by_id(conversation_id_int)
        if conversation is None:
            return {}

        return {
            "active_datasource_snapshot": safe_json_loads(conversation.active_datasource_snapshot, {}),
            "recent_turn_datasource_usage": self._build_recent_turn_datasource_usage(conversation_id_int, limit_turns=3),
            "recent_execution_summaries": self._build_recent_execution_summaries(conversation_id_int, limit_items=3),
            "latest_execution": self._build_latest_execution_summary(conversation_id_int),
            "latest_artifacts": [
                artifact.to_dict()
                for artifact in self.get_recent_artifacts(conversation_id_int, limit_items=3)
            ],
            "last_turn_no": conversation.last_turn_no,
        }

    def get_active_datasource_snapshot(self, conversation_id: Any) -> dict[str, Any]:
        conversation = self._get_conversation_by_id(to_int(conversation_id, 0))
        if conversation is None:
            return {}
        return safe_json_loads(conversation.active_datasource_snapshot, {})

    def _create_conversation(self, username: str, namespace_id: Any, user_message: str) -> InsightNsConversation:
        conversation = InsightNsConversation(
            username=username,
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

    def _next_seq_no(self, conversation_id: int, turn_no: int) -> int:
        last_seq = self.session.query(func.max(InsightNsMessage.seq_no)).filter(
            InsightNsMessage.insight_conversation_id == conversation_id,
            InsightNsMessage.turn_no == turn_no,
            InsightNsMessage.is_deleted == 0,
        ).scalar()
        return int(last_seq or 0) + 1

    def _next_turn_no(self, conversation_id: int, current_turn_no: int) -> int:
        max_turn_no = self.session.query(func.max(InsightNsTurn.turn_no)).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        ).scalar()
        return max(int(current_turn_no or 0), int(max_turn_no or 0)) + 1

    def _get_accessible_conversation(self, username: str, conversation_id: Any) -> InsightNsConversation | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.username == username,
            InsightNsConversation.is_deleted == 0,
        ).first()

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
            InsightDatasource.insight_namespace_id == self._get_conversation_namespace_id_subquery(conversation_id),
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
            InsightDatasource.insight_namespace_id == self._get_conversation_namespace_id_subquery(conversation_id),
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

        rows = self.session.query(InsightDatasource).join(
            InsightNsRelDatasource,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id_int,
            InsightDatasource.id.in_(normalized_ids),
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.insight_namespace_id == self._get_conversation_namespace_id_subquery(conversation_id_int),
            InsightDatasource.is_deleted == 0,
        ).all()

        payload_map = {
            item["datasource_id"]: item
            for item in (self._build_datasource_snapshot_item(datasource) for datasource in rows)
        }
        return [payload_map[datasource_id] for datasource_id in normalized_ids if datasource_id in payload_map]

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
                InsightNsRelDatasource.insight_namespace_id == conversation.insight_namespace_id,
                InsightNsRelDatasource.insight_conversation_id == 0,
                InsightNsRelDatasource.is_deleted == 0,
            ).order_by(
                InsightNsRelDatasource.sort_no.asc(),
                InsightNsRelDatasource.id.asc(),
            ).all()
            for row in namespace_rows:
                self.session.add(InsightNsRelDatasource(
                    insight_namespace_id=conversation.insight_namespace_id,
                    insight_conversation_id=conversation.id,
                    datasource_id=row.datasource_id,
                    is_active=row.is_active,
                    sort_no=row.sort_no,
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
        return {
            "datasource_id": datasource.id,
            "datasource_type": normalize_datasource_type(datasource.datasource_type),
            "datasource_name": datasource.datasource_name,
            "datasource_identifier": extract_datasource_identifier(datasource, config_json),
            "metadata_schema": extract_datasource_schema(datasource, config_json),
        }

    def _create_artifact(
        self,
        conversation_id: int,
        turn_id: int,
        execution_id: int,
        artifact_type: str,
        title: str,
        file_id: str,
        summary_text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = InsightNsArtifact(
            conversation_id=conversation_id,
            turn_id=turn_id,
            execution_id=execution_id,
            artifact_type=artifact_type,
            title=title,
            file_id=file_id or '',
            summary_text=summary_text or '',
            metadata_json=dump_json(metadata),
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact.to_dict()

    def _refresh_memories(self, conversation: InsightNsConversation) -> None:
        """刷新下一轮 Prompt 组装要使用的压缩记忆。"""
        summary_text = self.build_runtime_summary_text(conversation.id)
        conversation.summary_text = summary_text

        # `rolling_summary` 保存自然语言压缩后的历史摘要。
        self._upsert_memory(conversation.id, 'rolling_summary', {
            "summary_text": summary_text,
            "updated_at": _now().isoformat(),
        })

        # `analysis_state` 保存下一轮继续分析时要读取的结构化状态。
        self._upsert_memory(
            conversation.id,
            'analysis_state',
            self.build_runtime_analysis_state(conversation.id),
        )

    def _build_summary_text(self, conversation_id: int) -> str:
        """根据最近几轮构建紧凑的自然语言摘要。"""
        turns = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        ).order_by(InsightNsTurn.turn_no.desc()).limit(6).all()
        turns.reverse()
        if not turns:
            return ''

        successful_turn_ids = self._get_successful_execution_turn_ids(conversation_id)
        parts: list[str] = []
        for turn in turns:
            question = (turn.user_query or '').strip().replace('\n', ' ')[:120]
            answer = (turn.final_answer or turn.error_message or '').strip().replace('\n', ' ')[:200]
            if turn.id in successful_turn_ids and answer and '<tool_call>' not in answer:
                parts.append(f"第{turn.turn_no}轮 用户: {question}；系统结论: {answer}")
            else:
                parts.append(f"第{turn.turn_no}轮 用户: {question}")
        return '\n'.join(parts)

    def _build_recent_turn_datasource_usage(self, conversation_id: int, limit_turns: int = 3) -> list[dict[str, Any]]:
        """收集最近几轮使用过的数据源 ID。"""
        turns = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation_id,
            InsightNsTurn.is_deleted == 0,
        ).order_by(InsightNsTurn.turn_no.desc()).limit(limit_turns).all()
        turns.reverse()
        return [
            {
                "turn_id": turn.id,
                "turn_no": turn.turn_no,
                "selected_datasource_ids": safe_json_loads(turn.selected_datasource_ids_json, []),
            }
            for turn in turns
        ]

    def _build_recent_execution_summaries(self, conversation_id: int, limit_items: int = 3) -> list[dict[str, Any]]:
        """收集最近的执行摘要，供结构化记忆使用。"""
        executions = self.get_recent_executions(conversation_id, limit_items=limit_items)
        return [self._build_execution_summary_item(execution) for execution in executions]

    def _build_latest_execution_summary(self, conversation_id: int) -> dict[str, Any]:
        """返回最近一次执行摘要，并附带完整代码以支撑后续追问。"""
        executions = self.get_recent_executions(conversation_id, limit_items=1)
        if not executions:
            return {}
        return self._build_execution_summary_item(executions[-1], include_code=True)

    def _build_execution_summary_item(
        self,
        execution: InsightNsExecution,
        include_code: bool = False,
    ) -> dict[str, Any]:
        """把一条执行记录转换成适合写入记忆的摘要结构。"""
        payload = {
            "execution_id": execution.id,
            "turn_id": execution.turn_id,
            "tool_call_id": execution.tool_call_id,
            "title": execution.title,
            "description": execution.description,
            "execution_status": execution.execution_status,
            "result_file_id": execution.result_file_id,
            "analysis_report": execution.analysis_report,
            "error_message": execution.error_message,
            "execution_seconds": execution.execution_seconds,
            "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        }
        if include_code:
            payload["generated_code"] = execution.generated_code or ''
        else:
            payload["generated_code_preview"] = (execution.generated_code or '')[:1200]
        return payload

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
        ).all()
        return {to_int(row[0], 0) for row in rows if to_int(row[0], 0) > 0}

