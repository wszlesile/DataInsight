from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from model import (
    InsightDatasource,
    InsightNamespace,
    InsightNsArtifact,
    InsightNsConversation,
    InsightNsExecution,
    InsightNsMemory,
    InsightNsMessage,
    InsightNsRelDatasource,
    InsightNsRelKnowledge,
    InsightNsTurn,
    InsightUserCollect,
)


def _now() -> datetime:
    return datetime.now()


class InsightNamespaceService:
    """负责洞察空间的创建、查询和删除。"""

    def __init__(self, session: Session):
        self.session = session

    def list_namespaces(self, username: str) -> list[dict[str, Any]]:
        """返回当前用户可见的空间列表。"""
        namespaces = self.session.query(InsightNamespace).filter(
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).order_by(
            InsightNamespace.created_at.desc(),
            InsightNamespace.id.desc(),
        ).all()
        return [self._to_dict(item) for item in namespaces]

    def create_namespace(self, username: str, name: str) -> dict[str, Any]:
        """
        创建空间，并同步创建一条默认会话。

        当前产品进入空间后就允许直接开始对话，因此把空间初始化和默认会话
        初始化放在同一事务里完成。
        """
        normalized_name = (name or '').strip()[:128]
        if not normalized_name:
            raise ValueError('洞察空间名称不能为空')

        duplicated = self.session.query(InsightNamespace.id).filter(
            InsightNamespace.username == username,
            InsightNamespace.name == normalized_name,
            InsightNamespace.is_deleted == 0,
        ).first()
        if duplicated is not None:
            raise ValueError('洞察空间名称已存在')

        namespace = InsightNamespace(
            username=username,
            name=normalized_name,
            is_deleted=0,
            created_at=_now(),
        )
        self.session.add(namespace)
        self.session.flush()

        # 当前业务下空间与会话是 1:1，因此创建空间时同步创建一条真实会话。
        conversation = InsightNsConversation(
            insight_namespace_id=namespace.id,
            title=normalized_name,
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=0,
            last_message_at=_now(),
            user_message='',
            insight_result='',
            is_deleted=0,
            created_at=_now(),
            updated_at=_now(),
        )
        self.session.add(conversation)
        self.session.commit()
        self.session.refresh(namespace)
        self.session.refresh(conversation)

        return {
            "namespace": self._to_dict(namespace),
            "conversation": conversation.to_dict(),
        }

    def rename_namespace(self, username: str, namespace_id: Any, name: str) -> dict[str, Any] | None:
        """更新空间名称，并做同用户下的唯一性校验。"""
        normalized_name = (name or '').strip()[:128]
        if not normalized_name:
            raise ValueError('洞察空间名称不能为空')

        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == int(namespace_id),
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            return None

        duplicated = self.session.query(InsightNamespace.id).filter(
            InsightNamespace.username == username,
            InsightNamespace.name == normalized_name,
            InsightNamespace.is_deleted == 0,
            InsightNamespace.id != namespace.id,
        ).first()
        if duplicated is not None:
            raise ValueError('洞察空间名称已存在')

        namespace.name = normalized_name
        self.session.commit()
        self.session.refresh(namespace)
        return self._to_dict(namespace)

    def delete_namespace(self, username: str, namespace_id: Any) -> bool:
        """
        软删除空间及其下所有会话级上下文数据。

        不改变当前架构设计，只在一个地方集中收口空间删除时需要级联清理的
        会话、轮次、消息、执行、产物、记忆、绑定关系和收藏。
        """
        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == int(namespace_id),
            InsightNamespace.username == username,
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            return False

        conversation_ids = [
            row[0]
            for row in self.session.query(InsightNsConversation.id).filter(
                InsightNsConversation.insight_namespace_id == namespace.id,
                InsightNsConversation.is_deleted == 0,
            ).all()
        ]

        now = _now()

        # 空间本身软删除。
        namespace.is_deleted = 1

        # 当前业务下空间与会话为 1:1，但这里仍按集合方式删除，便于未来 1:N 平滑演进。
        if conversation_ids:
            self.session.query(InsightNsTurn).filter(
                InsightNsTurn.conversation_id.in_(conversation_ids),
                InsightNsTurn.is_deleted == 0,
            ).update(
                {
                    InsightNsTurn.is_deleted: 1,
                    InsightNsTurn.finished_at: now,
                },
                synchronize_session=False,
            )
            self.session.query(InsightNsMessage).filter(
                InsightNsMessage.insight_conversation_id.in_(conversation_ids),
                InsightNsMessage.is_deleted == 0,
            ).update(
                {InsightNsMessage.is_deleted: 1},
                synchronize_session=False,
            )
            self.session.query(InsightNsExecution).filter(
                InsightNsExecution.conversation_id.in_(conversation_ids),
                InsightNsExecution.is_deleted == 0,
            ).update(
                {InsightNsExecution.is_deleted: 1},
                synchronize_session=False,
            )
            self.session.query(InsightNsArtifact).filter(
                InsightNsArtifact.conversation_id.in_(conversation_ids),
                InsightNsArtifact.is_deleted == 0,
            ).update(
                {InsightNsArtifact.is_deleted: 1},
                synchronize_session=False,
            )
            self.session.query(InsightNsMemory).filter(
                InsightNsMemory.conversation_id.in_(conversation_ids),
                InsightNsMemory.is_deleted == 0,
            ).update(
                {InsightNsMemory.is_deleted: 1},
                synchronize_session=False,
            )
            self.session.query(InsightNsConversation).filter(
                InsightNsConversation.id.in_(conversation_ids),
                InsightNsConversation.is_deleted == 0,
            ).update(
                {
                    InsightNsConversation.is_deleted: 1,
                    InsightNsConversation.updated_at: now,
                    InsightNsConversation.status: 'archived',
                },
                synchronize_session=False,
            )

        # 会话级关系和默认虚拟会话资源关系都属于该空间的一部分，应一并删除。
        self.session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_namespace_id == namespace.id,
            InsightNsRelDatasource.is_deleted == 0,
        ).update(
            {
                InsightNsRelDatasource.is_deleted: 1,
                InsightNsRelDatasource.updated_at: now,
            },
            synchronize_session=False,
        )
        self.session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_namespace_id == namespace.id,
            InsightNsRelKnowledge.is_deleted == 0,
        ).update(
            {InsightNsRelKnowledge.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == namespace.id,
            InsightDatasource.is_deleted == 0,
        ).update(
            {
                InsightDatasource.is_deleted: 1,
                InsightDatasource.updated_at: now,
            },
            synchronize_session=False,
        )
        self.session.query(InsightUserCollect).filter(
            InsightUserCollect.insight_namespace_id == namespace.id,
            InsightUserCollect.is_deleted == 0,
        ).update(
            {InsightUserCollect.is_deleted: 1},
            synchronize_session=False,
        )

        self.session.commit()
        return True

    def _to_dict(self, namespace: InsightNamespace) -> dict[str, Any]:
        return {
            "id": namespace.id,
            "username": namespace.username,
            "name": namespace.name,
            "created_at": namespace.created_at.isoformat() if namespace.created_at else None,
        }
