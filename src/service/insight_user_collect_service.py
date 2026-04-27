import json
from typing import Any

from sqlalchemy.orm import Session

from api import supos_kernel_api
from model import (
    InsightNsArtifact,
    InsightNsConversation,
    InsightNsTurn,
    InsightUserCollect,
)
from utils import logger


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class InsightUserCollectService:
    """收藏查询与操作服务。"""

    def __init__(self, session: Session):
        self.session = session

    def list_collects(self, username: str) -> list[dict[str, Any]]:
        query = self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.is_deleted == 0,
        )
        collects = query.order_by(InsightUserCollect.created_at.desc(), InsightUserCollect.id.desc()).all()
        return [collect.to_dict() for collect in collects]

    def create_collect(
        self,
        username: str,
        collect_type: str,
        target_id: Any,
        title: str = '',
        summary_text: str = '',
        namespace_id: Any = 0,
        conversation_id: Any = 0,
        message_id: Any = 0,
        artifact_id: Any = 0,
    ) -> dict[str, Any]:
        target_id_int = _to_int(target_id, 0)
        existing = self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.collect_type == collect_type,
            InsightUserCollect.target_id == target_id_int,
            InsightUserCollect.is_deleted == 0,
        ).first()
        if existing:
            return existing.to_dict()

        resolved = self._resolve_collect_payload(
            collect_type=collect_type,
            target_id=target_id_int,
            title=title,
            summary_text=summary_text,
            namespace_id=_to_int(namespace_id, 0),
            conversation_id=_to_int(conversation_id, 0),
            artifact_id=_to_int(artifact_id, 0),
        )

        collect = InsightUserCollect(
            username=username,
            collect_type=collect_type,
            target_id=target_id_int,
            title=resolved["title"],
            summary_text=resolved["summary_text"],
            insight_namespace_id=resolved["insight_namespace_id"],
            insight_conversation_id=resolved["insight_conversation_id"],
            insight_message_id=_to_int(message_id, 0),
            insight_artifact_id=resolved["insight_artifact_id"],
            metadata_json=_dump_json(resolved["metadata_json"]),
        )
        self.session.add(collect)
        self.session.commit()
        self.session.refresh(collect)
        return collect.to_dict()

    def remove_collect(self, username: str, collect_type: str, target_id: Any) -> bool:
        target_id_int = _to_int(target_id, 0)
        deleted = self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.collect_type == collect_type,
            InsightUserCollect.target_id == target_id_int,
            InsightUserCollect.is_deleted == 0,
        ).update({"is_deleted": 1}, synchronize_session=False)
        self.session.commit()
        return bool(deleted)

    def count_collects(self, username: str) -> int:
        return self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.is_deleted == 0,
        ).count()

    def count_all_collects(self) -> int:
        return self.session.query(InsightUserCollect).filter(
            InsightUserCollect.is_deleted == 0,
        ).count()

    def report_collect_statistics(self, authorization: str = '') -> None:
        authorization = (authorization or '').strip()
        if not authorization:
            logger.info("跳过收藏统计上报: 缺少 authorization")
            return

        collect_count = self.count_all_collects()
        item_list = [
            {
                "code": "collect_insight_result_count",
                "name": "收藏洞察结果数",
                "total": collect_count,
            },
        ]
        try:
            response = supos_kernel_api.track_user_statistics(
                authorization=authorization,
                item_list=item_list,
            )
            logger.info(
                "收藏统计上报完成: collect_count=%s accepted=%s",
                collect_count,
                response.get('accepted'),
            )
        except Exception as exc:
            logger.warning(
                "收藏统计上报失败: collect_count=%s error=%s",
                collect_count,
                exc,
            )

    def _resolve_collect_payload(
        self,
        collect_type: str,
        target_id: int,
        title: str,
        summary_text: str,
        namespace_id: int,
        conversation_id: int,
        artifact_id: int,
    ) -> dict[str, Any]:
        payload = {
            "title": title or '',
            "summary_text": summary_text or '',
            "insight_namespace_id": namespace_id,
            "insight_conversation_id": conversation_id,
            "insight_artifact_id": artifact_id,
            "metadata_json": {},
        }
        if collect_type == 'conversation':
            return self._resolve_conversation_collect(target_id, payload)
        if collect_type == 'turn':
            return self._resolve_turn_collect(target_id, payload)
        if collect_type == 'artifact':
            return self._resolve_artifact_collect(target_id, payload)
        return payload

    def _resolve_conversation_collect(self, target_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        conversation = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == target_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
        if conversation is None:
            payload["metadata_json"] = {"conversation_id": target_id}
            return payload

        payload["title"] = payload["title"] or conversation.title or ''
        payload["summary_text"] = payload["summary_text"] or conversation.summary_text or ''
        payload["insight_namespace_id"] = payload["insight_namespace_id"] or conversation.insight_namespace_id
        payload["insight_conversation_id"] = conversation.id
        payload["metadata_json"] = {"conversation_id": conversation.id}
        return payload

    def _resolve_turn_collect(self, target_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == target_id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            payload["metadata_json"] = {"turn_id": target_id, "charts": []}
            return payload

        conversation = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == turn.conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
        chart_artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.artifact_type == 'chart',
            InsightNsArtifact.is_deleted == 0,
        ).order_by(InsightNsArtifact.sort_no.asc(), InsightNsArtifact.id.asc()).all()
        report_artifact = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.artifact_type == 'report',
            InsightNsArtifact.is_deleted == 0,
        ).order_by(InsightNsArtifact.sort_no.asc(), InsightNsArtifact.id.asc()).first()

        payload["title"] = payload["title"] or turn.user_query or (conversation.title if conversation else '') or ''
        payload["summary_text"] = payload["summary_text"] or turn.final_answer or self._extract_report_markdown(report_artifact) or ''
        payload["insight_conversation_id"] = payload["insight_conversation_id"] or turn.conversation_id
        if conversation is not None:
            payload["insight_namespace_id"] = payload["insight_namespace_id"] or conversation.insight_namespace_id
        payload["metadata_json"] = {
            "turn_id": turn.id,
            "charts": [self._build_chart_collect_item(artifact) for artifact in chart_artifacts],
        }
        return payload

    def _resolve_artifact_collect(self, target_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        artifact = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.id == target_id,
            InsightNsArtifact.is_deleted == 0,
        ).first()
        if artifact is None:
            payload["metadata_json"] = {"turn_id": 0}
            return payload

        conversation = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == artifact.conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
        content_json = self._load_json_object(artifact.content_json)

        payload["title"] = payload["title"] or artifact.title or ''
        payload["summary_text"] = payload["summary_text"] or artifact.summary_text or ''
        payload["insight_artifact_id"] = artifact.id
        payload["insight_conversation_id"] = payload["insight_conversation_id"] or artifact.conversation_id
        if conversation is not None:
            payload["insight_namespace_id"] = payload["insight_namespace_id"] or conversation.insight_namespace_id

        metadata_json: dict[str, Any] = {
            "turn_id": artifact.turn_id,
            "artifact_type": artifact.artifact_type,
        }
        chart_spec = content_json.get("chart_spec")
        if artifact.artifact_type == 'chart' and isinstance(chart_spec, dict):
            metadata_json["chart_spec"] = chart_spec
        payload["metadata_json"] = metadata_json
        return payload

    def _build_chart_collect_item(self, artifact: InsightNsArtifact) -> dict[str, Any]:
        content_json = self._load_json_object(artifact.content_json)
        chart_spec = content_json.get("chart_spec")
        return {
            "artifact_id": artifact.id,
            "title": artifact.title or '',
            "chart_spec": chart_spec if isinstance(chart_spec, dict) else {},
        }

    def _extract_report_markdown(self, artifact: InsightNsArtifact | None) -> str:
        if artifact is None:
            return ''
        content_json = self._load_json_object(artifact.content_json)
        report_markdown = content_json.get("report_markdown")
        if isinstance(report_markdown, str):
            return report_markdown
        return artifact.summary_text or ''

    def _load_json_object(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
