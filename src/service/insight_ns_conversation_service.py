from typing import Any

from sqlalchemy.orm import Session

from model import InsightNsArtifact, InsightNsConversation, InsightNsExecution, InsightNsMessage, InsightNsTurn
from utils.datasource_utils import build_conversation_title, to_int


class InsightNsConversationService:
    """供常规 Web 页面和详情面板使用的会话查询服务。"""

    def __init__(self, session: Session):
        self.session = session

    def list_conversations(self, username: str, namespace_id: Any) -> list[dict[str, Any]]:
        namespace_id_int = to_int(namespace_id, 0)
        conversations = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.username == username,
            InsightNsConversation.insight_namespace_id == namespace_id_int,
            InsightNsConversation.is_deleted == 0,
        ).order_by(
            InsightNsConversation.last_message_at.desc(),
            InsightNsConversation.id.desc(),
        ).all()
        return [conversation.to_dict() for conversation in conversations]

    def rename_conversation(self, username: str, conversation_id: Any, title: str) -> dict[str, Any] | None:
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None

        normalized_title = (title or '').strip()[:255]
        if not normalized_title:
            normalized_title = build_conversation_title(conversation.user_message)

        conversation.title = normalized_title
        self.session.commit()
        self.session.refresh(conversation)
        return conversation.to_dict()

    def get_conversation_history(self, username: str, conversation_id: Any) -> dict[str, Any] | None:
        """返回历史侧栏或历史页使用的会话时间线数据。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None

        turns = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).order_by(InsightNsTurn.turn_no.asc()).all()
        executions = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.is_deleted == 0,
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.is_deleted == 0,
        ).order_by(
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        execution_map = self._group_executions_by_turn(executions)
        artifact_map = self._group_artifacts_by_turn(artifacts)

        history = []
        for turn in turns:
            turn_dict = turn.to_dict()
            turn_executions = execution_map.get(turn.id, [])
            turn_artifacts = artifact_map.get(turn.id, {})
            # 历史列表故意只保留轻量执行摘要，
            # 完整代码和日志只在轮次详情接口中返回。
            history.append({
                "turn_id": turn.id,
                "turn_no": turn.turn_no,
                "question": turn.user_query,
                "selected_datasource_ids": turn_dict.get("selected_datasource_ids", []),
                "selected_datasource_snapshot": turn_dict.get("selected_datasource_snapshot", []),
                "report": turn_artifacts.get('analysis_report') or turn.final_answer,
                "file_id": turn_artifacts.get('file_id', ''),
                "chart_artifact_id": turn_artifacts.get('chart_artifact_id', 0),
                "latest_execution": self._build_execution_summary(turn_executions[-1]) if turn_executions else None,
                "execution_count": len(turn_executions),
                "status": turn.status,
                "started_at": turn.started_at.isoformat() if turn.started_at else None,
                "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
            })

        return {
            "conversation": conversation.to_dict(),
            "history": history,
        }

    def get_turn_detail(self, username: str, conversation_id: Any, turn_id: Any) -> dict[str, Any] | None:
        """返回某一轮的完整详情，包括消息、执行记录和派生产物。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None

        turn_id_int = to_int(turn_id, 0)
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id_int,
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            return None

        messages = self.session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == conversation.id,
            InsightNsMessage.turn_id == turn.id,
            InsightNsMessage.is_deleted == 0,
        ).order_by(
            InsightNsMessage.seq_no.asc(),
            InsightNsMessage.id.asc(),
        ).all()
        executions = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
        ).order_by(
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        return {
            "conversation": conversation.to_dict(),
            "turn": turn.to_dict(),
            "messages": [message.to_dict() for message in messages],
            "executions": [execution.to_dict() for execution in executions],
            "latest_execution": executions[-1].to_dict() if executions else None,
            "artifacts": [artifact.to_dict() for artifact in artifacts],
        }

    def _get_accessible_conversation(self, username: str, conversation_id: Any) -> InsightNsConversation | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.username == username,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _group_executions_by_turn(self, executions: list[InsightNsExecution]) -> dict[int, list[dict[str, Any]]]:
        """按 turn 对执行记录分组，方便后续组装历史数据。"""
        execution_map: dict[int, list[dict[str, Any]]] = {}
        for execution in executions:
            execution_map.setdefault(execution.turn_id, []).append(execution.to_dict())
        return execution_map

    def _group_artifacts_by_turn(self, artifacts: list[InsightNsArtifact]) -> dict[int, dict[str, Any]]:
        """按 turn 对派生产物分组，供历史卡片级展示使用。"""
        artifact_map: dict[int, dict[str, Any]] = {}
        for artifact in artifacts:
            turn_artifacts = artifact_map.setdefault(artifact.turn_id, {})
            if artifact.artifact_type == 'chart' and artifact.file_id:
                turn_artifacts['file_id'] = artifact.file_id
                turn_artifacts['chart_artifact_id'] = artifact.id
            if artifact.artifact_type == 'report' and artifact.summary_text:
                turn_artifacts['analysis_report'] = artifact.summary_text
        return artifact_map

    def _build_execution_summary(self, execution: dict[str, Any]) -> dict[str, Any]:
        """只返回摘要字段，保证历史列表接口保持轻量。"""
        return {
            "id": execution.get("id"),
            "turn_id": execution.get("turn_id"),
            "title": execution.get("title", ''),
            "description": execution.get("description", ''),
            "execution_status": execution.get("execution_status", ''),
            "result_file_id": execution.get("result_file_id", ''),
            "analysis_report": execution.get("analysis_report", ''),
            "error_message": execution.get("error_message", ''),
            "execution_seconds": execution.get("execution_seconds", 0),
            "finished_at": execution.get("finished_at"),
        }
