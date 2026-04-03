from typing import Any

from sqlalchemy.orm import Session

from model import InsightDatasource, InsightNsConversation, InsightNsRelDatasource
from utils.datasource_utils import normalize_datasource_type


class InsightNsRelDatasourceService:
    """负责管理会话级数据源绑定关系。"""

    def __init__(self, session: Session):
        self.session = session

    def find_by_conversation_id(self, insight_conversation_id: int) -> list[dict[str, Any]]:
        rows = self.session.query(InsightNsRelDatasource, InsightDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightNsRelDatasource.sort_no.asc(),
            InsightNsRelDatasource.id.asc(),
        ).all()
        return [self._to_dict(rel, datasource) for rel, datasource in rows]

    def add_datasource(
        self,
        insight_conversation_id: int,
        datasource_type: str,
        datasource_name: str,
        knowledge_tag: str,
        datasource_schema: str = '',
        datasource_config_json: str = '{}',
    ) -> dict[str, Any]:
        conversation = self._get_conversation(insight_conversation_id)
        if conversation is None:
            return {"success": False, "message": "会话不存在"}

        if self._conversation_has_same_named_datasource(insight_conversation_id, datasource_name):
            return {"success": False, "message": "该会话下已存在同名数据源"}

        datasource = self._get_or_create_datasource(
            conversation=conversation,
            datasource_type=datasource_type,
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag,
            datasource_schema=datasource_schema,
            datasource_config_json=datasource_config_json,
        )

        relation = InsightNsRelDatasource(
            insight_namespace_id=conversation.insight_namespace_id,
            insight_conversation_id=insight_conversation_id,
            datasource_id=datasource.id,
            is_active=1,
            sort_no=0,
        )
        self.session.add(relation)
        self.session.commit()
        return {"success": True, "message": "添加成功", "data": self._to_dict(relation, datasource)}

    def remove_datasource(self, insight_conversation_id: int, datasource_name: str) -> dict[str, Any]:
        row = self.session.query(InsightNsRelDatasource, InsightDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
            InsightDatasource.datasource_name == datasource_name,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).first()
        if not row:
            return {"success": False, "message": "数据源关系不存在"}

        relation, datasource = row
        relation.is_deleted = 1
        self.session.flush()

        has_other_refs = self.session.query(InsightNsRelDatasource.id).filter(
            InsightNsRelDatasource.datasource_id == datasource.id,
            InsightNsRelDatasource.insight_namespace_id == relation.insight_namespace_id,
            InsightNsRelDatasource.is_deleted == 0,
        ).first()
        if has_other_refs is None:
            datasource.is_deleted = 1

        self.session.commit()
        return {"success": True, "message": "移除成功"}

    def _get_conversation(self, insight_conversation_id: int) -> InsightNsConversation | None:
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == insight_conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _conversation_has_same_named_datasource(self, insight_conversation_id: int, datasource_name: str) -> bool:
        existing = self.session.query(InsightNsRelDatasource.id).join(
            InsightDatasource,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == insight_conversation_id,
            InsightDatasource.datasource_name == datasource_name,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).first()
        return existing is not None

    def _get_or_create_datasource(
        self,
        conversation: InsightNsConversation,
        datasource_type: str,
        datasource_name: str,
        knowledge_tag: str,
        datasource_schema: str,
        datasource_config_json: str,
    ) -> InsightDatasource:
        normalized_type = normalize_datasource_type(datasource_type)
        if normalized_type == 'unknown':
            raise ValueError(f'不支持的数据源类型: {datasource_type}')

        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == conversation.insight_namespace_id,
            InsightDatasource.datasource_name == datasource_name,
            InsightDatasource.datasource_type == normalized_type,
            InsightDatasource.is_deleted == 0,
        ).first()
        if datasource is not None:
            datasource.knowledge_tag = knowledge_tag or datasource.knowledge_tag or ''
            datasource.datasource_schema = datasource_schema or datasource.datasource_schema or ''
            datasource.datasource_config_json = datasource_config_json or datasource.datasource_config_json or '{}'
            return datasource

        datasource = InsightDatasource(
            insight_namespace_id=conversation.insight_namespace_id,
            datasource_type=normalized_type,
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag or '',
            datasource_schema=datasource_schema or '',
            datasource_config_json=datasource_config_json or '{}',
        )
        self.session.add(datasource)
        self.session.flush()
        return datasource

    def _to_dict(self, rel: InsightNsRelDatasource, datasource: InsightDatasource) -> dict[str, Any]:
        return {
            "id": rel.id,
            "insight_namespace_id": rel.insight_namespace_id,
            "insight_conversation_id": rel.insight_conversation_id,
            "datasource_id": datasource.id,
            "datasource_type": datasource.datasource_type,
            "datasource_name": datasource.datasource_name,
            "knowledge_tag": datasource.knowledge_tag,
            "datasource_schema": datasource.datasource_schema,
            "datasource_config_json": datasource.datasource_config_json,
            "sort_no": rel.sort_no,
            "created_at": rel.created_at.isoformat() if rel.created_at else None,
            "updated_at": rel.updated_at.isoformat() if rel.updated_at else None,
        }
