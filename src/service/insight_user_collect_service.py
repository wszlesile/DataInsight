import json
from typing import Any

from sqlalchemy.orm import Session

from model import InsightUserCollect


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

    def list_collects(self, username: str, namespace_id: Any = None) -> list[dict[str, Any]]:
        query = self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.is_deleted == 0,
        )
        namespace_id_int = _to_int(namespace_id, 0)
        if namespace_id is not None and namespace_id_int > 0:
            query = query.filter(InsightUserCollect.insight_namespace_id == namespace_id_int)
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
        metadata: dict[str, Any] | None = None,
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

        collect = InsightUserCollect(
            username=username,
            collect_type=collect_type,
            target_id=target_id_int,
            title=title or '',
            summary_text=summary_text or '',
            insight_namespace_id=_to_int(namespace_id, 0),
            insight_conversation_id=_to_int(conversation_id, 0),
            insight_message_id=_to_int(message_id, 0),
            insight_artifact_id=_to_int(artifact_id, 0),
            metadata_json=_dump_json(metadata or {}),
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
