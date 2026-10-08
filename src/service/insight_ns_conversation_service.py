import base64
import html
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from sqlalchemy import or_
from sqlalchemy.orm import Session
from service.analysis_progress_service import group_latest_progress, load_progress

from model import (
    InsightAnalysisTask,
    InsightNsArtifact,
    InsightNsConversation,
    InsightNsConversationPrompt,
    InsightNsExecutionEvidence,
    InsightNsExecution,
    InsightNsMemory,
    InsightNsMessage,
    InsightNsRelDatasource,
    InsightNsRelKnowledge,
    InsightNamespace,
    InsightNsTurn,
    InsightUserCollect,
)
from utils import (
    build_conversation_title,
    logger,
    normalize_chart_spec,
    render_chart_spec_to_png,
    to_int,
)

HTML = None
WEASYPRINT_IMPORT_ERROR = None
WEASYPRINT_DLL_DIRECTORY_HANDLES = []
WEASYPRINT_DLL_DIRECTORIES = set()
EMOJI_BASE_PATTERN = (
    r'[\u2600-\u27bf\U0001f1e6-\U0001f1ff\U0001f300-\U0001f5ff'
    r'\U0001f600-\U0001f64f\U0001f680-\U0001f6ff\U0001f700-\U0001f77f'
    r'\U0001f780-\U0001f7ff\U0001f800-\U0001f8ff\U0001f900-\U0001faff]'
)
EMOJI_CLUSTER_RE = re.compile(
    rf'{EMOJI_BASE_PATTERN}[\ufe0e\ufe0f]?(?:[\U0001f3fb-\U0001f3ff])?'
    rf'(?:\u200d{EMOJI_BASE_PATTERN}[\ufe0e\ufe0f]?(?:[\U0001f3fb-\U0001f3ff])?)*'
)
TWEMOJI_BASE_URL = 'https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/svg'


class InsightNsConversationService:
    """会话历史、轮次详情与导出相关的查询服务。"""

    NEW_CONVERSATION_NAME = '新建会话'

    def __init__(self, session: Session):
        self.session = session

    def list_conversations(self, username: str, namespace_id: Any) -> list[dict[str, Any]]:
        """按空间查询当前用户的会话列表。"""
        namespace_id_int = to_int(namespace_id, 0)
        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == namespace_id_int,
            InsightNamespace.username == username,
            InsightNamespace.namespace_kind == 'interactive',
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            return []

        conversations = self.session.query(InsightNsConversation).filter(
            InsightNsConversation.insight_namespace_id == namespace_id_int,
            InsightNsConversation.is_deleted == 0,
        ).order_by(
            InsightNsConversation.last_message_at.desc(),
            InsightNsConversation.id.desc(),
        ).all()
        return [conversation.to_dict() for conversation in conversations]

    def create_conversation(
        self,
        username: str,
        namespace_id: Any,
        title: str = '',
    ) -> dict[str, Any] | None:
        """在指定洞察空间下创建一条新的空会话。"""
        namespace_id_int = to_int(namespace_id, 0)
        if namespace_id_int <= 0:
            return None

        namespace = self.session.query(InsightNamespace).filter(
            InsightNamespace.id == namespace_id_int,
            InsightNamespace.username == username,
            InsightNamespace.namespace_kind == 'interactive',
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            return None

        normalized_title = (title or '').strip()[:255] or '新建会话'

        conversation = InsightNsConversation(
            insight_namespace_id=namespace_id_int,
            title=normalized_title,
            status='active',
            summary_text='',
            active_datasource_snapshot='{}',
            last_turn_no=0,
            last_message_at=datetime.now(),
            user_message='',
            insight_result='',
            is_deleted=0,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self.session.add(conversation)
        self.session.commit()
        self.session.refresh(conversation)
        return conversation.to_dict()

    def rename_conversation(self, username: str, conversation_id: Any, title: str) -> dict[str, Any] | None:
        """更新会话标题；空标题时回退到默认标题生成逻辑。"""
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

    def delete_conversation(self, username: str, conversation_id: Any) -> bool:
        """软删除单个会话及其会话级上下文数据，不删除空间和空间级数据源。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return False

        now = datetime.now()
        turn_ids = [
            row[0]
            for row in self.session.query(InsightNsTurn.id).filter(
                InsightNsTurn.conversation_id == conversation.id,
                InsightNsTurn.is_deleted == 0,
            ).all()
        ]
        artifact_ids = [
            row[0]
            for row in self.session.query(InsightNsArtifact.id).filter(
                InsightNsArtifact.conversation_id == conversation.id,
                InsightNsArtifact.is_deleted == 0,
            ).all()
        ]

        self.session.query(InsightNsTurn).filter(
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).update(
            {
                InsightNsTurn.is_deleted: 1,
                InsightNsTurn.finished_at: now,
            },
            synchronize_session=False,
        )
        self.session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == conversation.id,
            InsightNsMessage.is_deleted == 0,
        ).update(
            {InsightNsMessage.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.is_deleted == 0,
        ).update(
            {InsightNsExecution.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.is_deleted == 0,
        ).update(
            {InsightNsArtifact.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightNsMemory).filter(
            InsightNsMemory.conversation_id == conversation.id,
            InsightNsMemory.is_deleted == 0,
        ).update(
            {InsightNsMemory.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightNsRelDatasource).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation.id,
            InsightNsRelDatasource.is_deleted == 0,
        ).update(
            {
                InsightNsRelDatasource.is_deleted: 1,
                InsightNsRelDatasource.updated_at: now,
            },
            synchronize_session=False,
        )
        self.session.query(InsightNsRelKnowledge).filter(
            InsightNsRelKnowledge.insight_conversation_id == conversation.id,
            InsightNsRelKnowledge.is_deleted == 0,
        ).update(
            {InsightNsRelKnowledge.is_deleted: 1},
            synchronize_session=False,
        )
        self.session.query(InsightNsConversationPrompt).filter(
            InsightNsConversationPrompt.insight_conversation_id == conversation.id,
            InsightNsConversationPrompt.is_deleted == 0,
        ).update(
            {
                InsightNsConversationPrompt.is_deleted: 1,
                InsightNsConversationPrompt.is_enabled: 0,
                InsightNsConversationPrompt.updated_at: now,
            },
            synchronize_session=False,
        )

        collect_filters = [
            InsightUserCollect.insight_conversation_id == conversation.id,
        ]
        if turn_ids:
            collect_filters.append(
                (InsightUserCollect.collect_type == 'turn') & (InsightUserCollect.target_id.in_(turn_ids))
            )
        if artifact_ids:
            collect_filters.append(
                (InsightUserCollect.collect_type == 'artifact') & (InsightUserCollect.target_id.in_(artifact_ids))
            )
        self.session.query(InsightUserCollect).filter(
            InsightUserCollect.username == username,
            InsightUserCollect.is_deleted == 0,
            or_(*collect_filters),
        ).update(
            {InsightUserCollect.is_deleted: 1},
            synchronize_session=False,
        )

        conversation.is_deleted = 1
        conversation.status = 'archived'
        conversation.updated_at = now
        self.session.commit()
        return True

    def get_conversation_history(self, username: str, conversation_id: Any) -> dict[str, Any] | None:
        """
        返回主聊天区使用的轮次历史。

        这里会把 turn、latest execution 和 artifacts 聚合成前端直接消费的结果卡结构。
        """
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
            InsightNsExecution.content_enabled == 1,
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.is_deleted == 0,
            InsightNsArtifact.content_enabled == 1,
        ).order_by(
            InsightNsArtifact.sort_no.asc(),
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        execution_map = self._group_executions_by_turn(executions)
        progress_map = group_latest_progress(self.session, conversation.id)
        artifact_map = self._group_artifacts_by_turn(artifacts)

        history: list[dict[str, Any]] = []
        for turn in turns:
            turn_dict = turn.to_dict()
            turn_executions = execution_map.get(turn.id, [])
            turn_artifacts = artifact_map.get(turn.id, self._empty_turn_artifacts())

            primary_chart = turn_artifacts['charts'][0] if turn_artifacts['charts'] else {}
            history.append({
                "turn_id": turn.id,
                "turn_no": turn.turn_no,
                "question": turn.user_query,
                "selected_datasource_ids": turn_dict.get("selected_datasource_ids", []),
                "selected_datasource_snapshot": turn_dict.get("selected_datasource_snapshot", []),
                "report": turn_artifacts.get('analysis_report') or turn.final_answer,
                "report_evidence_refs": turn_artifacts.get('report_evidence_refs', {}),
                "charts": turn_artifacts.get('charts', []),
                "tables": turn_artifacts.get('tables', []),
                "chart_artifact_id": primary_chart.get('id', 0),
                "chart_artifact_ids": [item.get('id', 0) for item in turn_artifacts.get('charts', []) if item.get('id')],
                "latest_execution": self._build_execution_summary(turn_executions[-1]) if turn_executions else None,
                "execution_count": len(turn_executions),
                "status": turn.status,
                "error_message": turn.error_message or '',
                "started_at": turn.started_at.isoformat() if turn.started_at else None,
                "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
                "progress": progress_map.get(turn.id, []),
            })

        return {
            "conversation": conversation.to_dict(),
            "history": history,
        }

    def get_turn_detail(self, username: str, conversation_id: Any, turn_id: Any) -> dict[str, Any] | None:
        """返回单轮完整详情，用于详情抽屉和调试场景。"""
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
            InsightNsMessage.content_enabled == 1,
        ).order_by(
            InsightNsMessage.seq_no.asc(),
            InsightNsMessage.id.asc(),
        ).all()
        executions = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
            InsightNsArtifact.content_enabled == 1,
        ).order_by(
            InsightNsArtifact.sort_no.asc(),
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()
        latest_evidence = None
        if executions:
            latest_evidence = self.session.query(InsightNsExecutionEvidence).filter(
                InsightNsExecutionEvidence.execution_id == executions[-1].id,
                InsightNsExecutionEvidence.is_deleted == 0,
            ).first()

        return {
            "conversation": conversation.to_dict(),
            "turn": turn.to_dict(),
            "messages": [message.to_dict() for message in messages],
            "executions": [execution.to_dict() for execution in executions],
            "latest_execution": executions[-1].to_dict() if executions else None,
            "artifacts": [self._artifact_to_view(artifact) for artifact in artifacts],
            "evidence_compilation": ({
                "status": latest_evidence.compilation_status,
                "diagnostics": self._safe_json_loads(latest_evidence.diagnostics_json, {}),
                "updated_at": latest_evidence.updated_at.isoformat() if latest_evidence.updated_at else None,
            } if latest_evidence is not None else None),
            "progress": load_progress(self.session, conversation.id, turn.id),
        }

    def get_evidence_compilation_status(
        self,
        username: str,
        conversation_id: Any,
        turn_id: Any,
    ) -> dict[str, Any] | None:
        """Return only the small, pollable state for post-analysis Evidence."""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None
        turn = self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == to_int(turn_id, 0),
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if turn is None:
            return None
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.content_enabled == 1,
        ).order_by(
            InsightNsExecution.created_at.desc(),
            InsightNsExecution.id.desc(),
        ).first()
        if execution is None:
            return {"status": "unavailable", "diagnostics": {}, "updated_at": None}
        evidence = self.session.query(InsightNsExecutionEvidence).filter(
            InsightNsExecutionEvidence.execution_id == execution.id,
            InsightNsExecutionEvidence.is_deleted == 0,
        ).first()
        if evidence is None:
            return {"status": "unavailable", "diagnostics": {}, "updated_at": None}
        return {
            "status": evidence.compilation_status,
            "diagnostics": self._safe_json_loads(evidence.diagnostics_json, {}),
            "updated_at": evidence.updated_at.isoformat() if evidence.updated_at else None,
        }

    def resolve_evidence(
        self,
        username: str,
        conversation_id: Any,
        turn_id: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None
        turn = self._get_turn(conversation, turn_id)
        if turn is None:
            return None

        artifact_id = to_int(payload.get('artifact_id'), 0)
        evidence_id = str(payload.get('evidence_id') or '').strip()
        if artifact_id <= 0 or not evidence_id:
            return {
                "traceability_status": "invalid_request",
                "message": "artifact_id 和 evidence_id 不能为空。",
            }

        artifact = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.id == artifact_id,
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
        ).first()
        if artifact is None:
            return None

        evidence_bundle = self.session.query(InsightNsExecutionEvidence).filter(
            InsightNsExecutionEvidence.execution_id == artifact.execution_id,
            InsightNsExecutionEvidence.is_deleted == 0,
        ).first()
        if evidence_bundle is None:
            return {
                "traceability_status": "unavailable",
                "message": "当前产物没有可用证据链。",
            }

        evidence_map = self._safe_json_loads(evidence_bundle.evidence_map_json, {})
        query_snapshots = self._safe_json_loads(evidence_bundle.query_snapshots_json, {})
        evidence = evidence_map.get(evidence_id) if isinstance(evidence_map, dict) else None
        if not isinstance(evidence, dict):
            return {
                "traceability_status": "not_found",
                "message": "未找到对应 Evidence。",
            }

        artifact_view = self._artifact_to_view(artifact)
        if not self._artifact_has_evidence_id(artifact_view, evidence_id):
            return {
                "traceability_status": "not_found",
                "message": "当前产物位置未关联该 Evidence。",
            }
        selected_context = self._build_selected_context(artifact_view, payload.get('selection') or {})
        selected_context = self._normalize_selected_context_value(selected_context, evidence)
        detail_level = str(payload.get('detail_level') or 'summary').strip().lower()
        execution = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.id == artifact.execution_id,
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
        ).first()
        safe_query_snapshots = query_snapshots if isinstance(query_snapshots, dict) else {}
        return {
            "traceability_status": "available",
            "metric_name": evidence.get("metric_name") or '',
            "selected_context": selected_context,
            "chain_steps": self._filter_chain_steps(
                self._build_evidence_chain_steps(
                evidence=evidence,
                query_snapshots=safe_query_snapshots,
                selected_context=selected_context,
                ),
                detail_level=detail_level,
            ),
            "execution_ref": evidence.get("execution_ref") or {
                "turn_id": turn.id,
                "execution_id": artifact.execution_id,
            },
            "debug_sql": self._extract_debug_sql(safe_query_snapshots, evidence),
            "generated_code": execution.generated_code if execution is not None else '',
        }

    @staticmethod
    def _safe_json_loads(value: Any, fallback: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value or '')
        except Exception:
            return fallback

    @staticmethod
    def _extract_debug_sql(query_snapshots: dict[str, Any], evidence: dict[str, Any]) -> str:
        query_refs = [str(item) for item in (evidence.get('query_refs') or [])]
        sql_parts: list[str] = []
        for query_ref in query_refs:
            snapshot = query_snapshots.get(query_ref)
            if not isinstance(snapshot, dict):
                continue
            implementation = snapshot.get('implementation') if isinstance(snapshot.get('implementation'), dict) else {}
            sql = str(implementation.get('sql') or '').strip()
            if sql:
                sql_parts.append(sql)
        if not sql_parts:
            for snapshot in query_snapshots.values():
                if not isinstance(snapshot, dict):
                    continue
                implementation = snapshot.get('implementation') if isinstance(snapshot.get('implementation'), dict) else {}
                sql = str(implementation.get('sql') or '').strip()
                if sql:
                    sql_parts.append(sql)
        return "\n\n-- next query --\n\n".join(sql_parts)

    def _build_selected_context(self, artifact_view: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
        content = artifact_view.get('content_json') if isinstance(artifact_view.get('content_json'), dict) else {}
        artifact_type = artifact_view.get('artifact_type')
        context: dict[str, Any] = {
            "artifact_id": artifact_view.get("id"),
            "artifact_type": artifact_type,
            "selection": selection if isinstance(selection, dict) else {},
        }
        if artifact_type == 'table':
            row_index = to_int(selection.get('row_index'), -1) if isinstance(selection, dict) else -1
            column_key = str(selection.get('column_key') or selection.get('value_column') or '').strip() if isinstance(selection, dict) else ''
            rows = content.get('rows') or []
            if 0 <= row_index < len(rows) and isinstance(rows[row_index], dict) and column_key:
                context["raw_value"] = rows[row_index].get(column_key)
                context["display_value"] = str(rows[row_index].get(column_key))
        elif artifact_type == 'chart':
            data_index = to_int(selection.get('data_index'), -1) if isinstance(selection, dict) else -1
            value_field = str(selection.get('value_field') or '').strip() if isinstance(selection, dict) else ''
            dataset = ((content.get('chart_document') or {}).get('dataset') or {})
            rows = dataset.get('rows') if isinstance(dataset, dict) else None
            if isinstance(rows, list) and 0 <= data_index < len(rows) and isinstance(rows[data_index], dict) and value_field:
                context["raw_value"] = rows[data_index].get(value_field)
                context["display_value"] = str(rows[data_index].get(value_field))
                context["dimensions"] = {
                    key: value for key, value in rows[data_index].items() if key != value_field
                }
            elif data_index >= 0 and value_field:
                compiled_context = self._compiled_chart_point_context(
                    content,
                    selection if isinstance(selection, dict) else {},
                    data_index=data_index,
                    value_field=value_field,
                )
                context.update(compiled_context)
            elif isinstance(selection, dict) and selection.get('raw_value') not in (None, ''):
                context["raw_value"] = selection.get('raw_value')
                context["display_value"] = str(selection.get('raw_value'))
        elif artifact_type == 'report':
            start = to_int(selection.get('start'), -1) if isinstance(selection, dict) else -1
            end = to_int(selection.get('end'), -1) if isinstance(selection, dict) else -1
            report = str(content.get('report_markdown') or artifact_view.get('summary_text') or '')
            if 0 <= start <= end <= len(report):
                context["raw_value"] = report[start:end]
                context["display_value"] = report[start:end]
        return context

    @staticmethod
    def _compiled_chart_point_context(
        content: dict[str, Any],
        selection: dict[str, Any],
        *,
        data_index: int,
        value_field: str,
    ) -> dict[str, Any]:
        chart_spec = content.get('chart_spec') if isinstance(content.get('chart_spec'), dict) else {}
        series = chart_spec.get('series') if isinstance(chart_spec.get('series'), list) else []
        contract = content.get('chart_data_contract') if isinstance(content.get('chart_data_contract'), dict) else {}
        encoding = contract.get('encoding') if isinstance(contract.get('encoding'), dict) else {}
        value_fields = [str(item or '').strip() for item in (encoding.get('value_fields') or [])]
        series_index = to_int(selection.get('series_index'), -1)
        if not 0 <= series_index < len(series) and value_field in value_fields:
            series_index = value_fields.index(value_field)
        if not 0 <= series_index < len(series):
            series_index = 0 if len(series) == 1 else -1
        if series_index < 0:
            return {}

        selected_series = series[series_index] if isinstance(series[series_index], dict) else {}
        series_data = selected_series.get('data') if isinstance(selected_series.get('data'), list) else []
        if not 0 <= data_index < len(series_data):
            return {}
        point = series_data[data_index]
        raw_value = point.get('value') if isinstance(point, dict) else point
        if isinstance(raw_value, list):
            raw_value = raw_value[-1] if raw_value else None
        if raw_value is None:
            return {}

        dimensions: dict[str, Any] = {}
        category_field = str(encoding.get('category_field') or '').strip()
        category_value = point.get('name') if isinstance(point, dict) else None
        if category_value in (None, ''):
            axes = [
                chart_spec.get('xAxis') if isinstance(chart_spec.get('xAxis'), dict) else {},
                chart_spec.get('yAxis') if isinstance(chart_spec.get('yAxis'), dict) else {},
            ]
            for axis in axes:
                categories = axis.get('data') if isinstance(axis.get('data'), list) else []
                if 0 <= data_index < len(categories):
                    category_value = categories[data_index]
                    break
        if category_field and category_value not in (None, ''):
            dimensions[category_field] = category_value
        series_name = selected_series.get('name')
        if series_name not in (None, ''):
            dimensions['series_name'] = series_name
        return {
            'raw_value': raw_value,
            'display_value': str(raw_value),
            'dimensions': dimensions,
        }

    @staticmethod
    def _artifact_has_evidence_id(artifact_view: dict[str, Any], evidence_id: str) -> bool:
        metadata = artifact_view.get('metadata_json') if isinstance(artifact_view.get('metadata_json'), dict) else {}
        evidence_refs = metadata.get('evidence_refs') if isinstance(metadata.get('evidence_refs'), dict) else {}
        for refs in evidence_refs.values():
            if not isinstance(refs, list):
                continue
            if any(isinstance(ref, dict) and ref.get('evidence_id') == evidence_id for ref in refs):
                return True
        return False

    @staticmethod
    def _build_evidence_chain_steps(
        *,
        evidence: dict[str, Any],
        query_snapshots: dict[str, Any],
        selected_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        query_refs = [str(item) for item in (evidence.get('query_refs') or [])]
        queries = [query_snapshots.get(query_ref) for query_ref in query_refs if isinstance(query_snapshots.get(query_ref), dict)]
        first_query = queries[0] if queries else {}
        calculation = evidence.get('calculation') if isinstance(evidence.get('calculation'), dict) else {}
        derivation = evidence.get('derivation') if isinstance(evidence.get('derivation'), dict) else {}
        transformations = evidence.get('transformations') or []
        intermediate_outputs = evidence.get('intermediate_outputs') or []
        source_refs = evidence.get("source_refs") or []
        query_constraints = first_query.get('query_constraints') if isinstance(first_query, dict) else {}
        implementation = first_query.get('implementation') if isinstance(first_query, dict) else {}
        execution = first_query.get('execution') if isinstance(first_query, dict) else {}
        metric_fields = calculation.get('metric_fields') if isinstance(calculation.get('metric_fields'), list) else []
        quality_notes = evidence.get('quality_notes') or []
        relevant_source_refs = InsightNsConversationService._relevant_source_refs(source_refs, query_refs, queries)
        code_transformations = InsightNsConversationService._code_transformations(transformations)
        selected_context = InsightNsConversationService._normalize_selected_context_value(selected_context, evidence)

        steps = [
            {
                "type": "read_data",
                "title": "读取数据",
                "summary": InsightNsConversationService._describe_sources(relevant_source_refs, query_refs),
                "items": InsightNsConversationService._source_items(relevant_source_refs, metric_fields),
                "output": InsightNsConversationService._query_output_text(execution, intermediate_outputs),
                "details": {
                    "query_refs": query_refs,
                    "source_refs": relevant_source_refs,
                },
            },
            {
                "type": "filter_scope",
                "title": "筛选范围",
                "summary": InsightNsConversationService._describe_scope(query_constraints),
                "items": InsightNsConversationService._scope_items(query_constraints, implementation),
                "output": InsightNsConversationService._execution_status_text(execution),
                "details": {
                    "query_constraints": query_constraints,
                    "implementation": implementation,
                    "execution": execution,
                },
            },
            {
                "type": "data_filter",
                "title": "数据过滤",
                "summary": InsightNsConversationService._describe_transformations(code_transformations),
                "items": InsightNsConversationService._transformation_items(code_transformations, intermediate_outputs),
                "output": InsightNsConversationService._data_filter_output_text(execution, code_transformations),
                "details": {"transformations": code_transformations},
            },
        ]
        steps.extend([
            {
                "type": "calculation",
                "title": "数据计算",
                "summary": InsightNsConversationService._describe_data_calculation(
                    selected_context,
                    evidence,
                    derivation,
                    calculation,
                    code_transformations,
                ),
                "method": InsightNsConversationService._describe_calculation_method(calculation, derivation),
                "formula": calculation.get('formula') or '',
                "result_value": selected_context.get('display_value') or selected_context.get('raw_value'),
                "items": InsightNsConversationService._data_calculation_items(
                    selected_context,
                    evidence,
                    derivation,
                    calculation,
                    intermediate_outputs,
                    code_transformations,
                ),
                "details": {
                    "derivation": derivation,
                    "calculation": calculation,
                    "intermediate_outputs": intermediate_outputs,
                    "selected_context": selected_context,
                },
            },
            {
                "type": "reliability",
                "title": "结果校验与可信说明",
                "summary": InsightNsConversationService._describe_reliability(execution, quality_notes),
                "items": InsightNsConversationService._reliability_items(execution, quality_notes, intermediate_outputs),
                "badges": InsightNsConversationService._reliability_badges(execution, quality_notes),
                "warnings": [str(item) for item in quality_notes[:4]],
                "details": {"execution": execution, "quality_notes": quality_notes},
            },
        ])
        return steps

    @staticmethod
    def _describe_calculation(calculation: dict[str, Any], derivation: dict[str, Any] | None = None, transformations: Any = None) -> str:
        if not calculation:
            return "该步骤未提供额外说明。"
        parts = []
        derivation = derivation if isinstance(derivation, dict) else {}
        description = str(derivation.get('description') or '').strip()
        if description:
            parts.append(description)
        aggregation = calculation.get('aggregation')
        if aggregation:
            parts.append(f"按“{InsightNsConversationService._friendly_aggregation(aggregation)}”方式计算")
        group_by = calculation.get('group_by')
        if group_by:
            parts.append(f"分组维度：{InsightNsConversationService._stringify_list(group_by)}")
        formula = calculation.get('formula')
        if formula:
            parts.append(f"公式：{formula}")
        if not description and isinstance(transformations, list) and transformations:
            parts.append(str(transformations[0]))
        normalized_parts = [str(part).strip().rstrip('。；;') for part in parts if str(part).strip()]
        return "；".join(normalized_parts) + "。" if normalized_parts else "根据查询结果计算基础指标。"

    @staticmethod
    def _friendly_datasource_type(value: Any) -> str:
        mapping = {
            'local_file': '本地文件',
            'table': 'UNS 文件',
            'timeseries': 'UNS 时序型文件',
            'time_series': 'UNS 时序型文件',
            'api': '数据源',
            'minio': '数据源',
            'minio_file': '数据源',
        }
        key = str(value or '').strip()
        return mapping.get(key, key or '数据源')

    @staticmethod
    def _friendly_aggregation(value: Any) -> str:
        if isinstance(value, str) and '/' in value:
            return "、".join(
                InsightNsConversationService._friendly_aggregation(part.strip())
                for part in value.split('/')
                if part.strip()
            )
        mapping = {
            'count': '计数',
            'record_count': '记录数量',
            'sum': '求和',
            'avg': '平均值',
            'average': '平均值',
            'mean': '平均值',
            'min': '最小值',
            'max': '最大值',
            'ratio': '比例',
            'percentage': '占比',
            'count_distinct': '去重计数',
            'distinct_count': '去重计数',
        }
        key = str(value or '').strip().lower()
        return mapping.get(key, str(value or '聚合'))

    @staticmethod
    def _friendly_metric(value: Any) -> str:
        mapping = {
            'count': '命中记录数',
            'record_count': '命中记录数',
            'query_result_row_count': '查询命中记录数',
            'sum': '合计值',
            'avg': '平均值',
            'average': '平均值',
            'mean': '平均值',
            'min': '最小值',
            'max': '最大值',
        }
        if isinstance(value, list):
            return InsightNsConversationService._stringify_list([
                InsightNsConversationService._friendly_metric(item) for item in value
            ])
        key = str(value or '').strip()
        return mapping.get(key.lower(), key)

    @staticmethod
    def _friendly_grain(value: Any) -> str:
        mapping = {
            'full': '全量汇总',
            'overall': '整体汇总',
            'detail': '明细记录',
            'raw': '原始明细',
            'day': '按天统计',
            'date': '按日期统计',
            'week': '按周统计',
            'month': '按月统计',
            'quarter': '按季度统计',
            'year': '按年统计',
            'hour': '按小时统计',
            'minute': '按分钟统计',
        }
        key = str(value or '').strip().lower()
        return mapping.get(key, str(value or '未指定'))

    @staticmethod
    def _friendly_field_hint(value: Any) -> str:
        mapping = {
            'name': '名称',
            'username': '用户名',
            'created_at': '创建时间',
            'updated_at': '更新时间',
            'status': '状态',
            'type': '类型',
            'id': 'ID',
            'is_deleted': '删除状态',
        }
        if isinstance(value, list):
            fields = value
        else:
            fields = [item.strip() for item in str(value or '').replace('，', ',').split(',') if item.strip()]
        labels = [mapping.get(str(field), str(field)) for field in fields]
        return "或".join(labels) if labels else '目标字段'

    @staticmethod
    def _friendly_match_mode(value: Any) -> str:
        mapping = {
            'fuzzy': '模糊匹配',
            'contains': '包含',
            'exact': '精确匹配',
            'eq': '等于',
            'in': '属于',
            'range': '范围匹配',
            'gte': '大于等于',
            'lte': '小于等于',
            'gt': '大于',
            'lt': '小于',
        }
        key = str(value or '').strip().lower()
        return mapping.get(key, key or '匹配')

    @staticmethod
    def _describe_target_filter(filter_item: Any) -> str:
        if not isinstance(filter_item, dict):
            return str(filter_item)
        field = InsightNsConversationService._friendly_field_hint(
            filter_item.get('field_label') or filter_item.get('field_hint') or filter_item.get('field') or filter_item.get('fields')
        )
        value = filter_item.get('value')
        if value is None:
            value = filter_item.get('values')
        mode = InsightNsConversationService._friendly_match_mode(filter_item.get('match_mode') or filter_item.get('operator'))
        if isinstance(value, list):
            value_text = "、".join(str(item) for item in value)
        else:
            value_text = str(value) if value is not None else ''
        role = str(filter_item.get('semantic_role') or '').strip()
        if role == 'keyword_search':
            return f"在{field}中{mode}“{value_text}”"
        if value_text:
            return f"{field}{mode}“{value_text}”"
        return f"{field}使用{mode}条件"

    @staticmethod
    def _format_filter_rule(filter_item: Any) -> str:
        if not isinstance(filter_item, dict):
            return str(filter_item)
        field = InsightNsConversationService._friendly_field_hint(
            filter_item.get('field_label') or filter_item.get('field_hint') or filter_item.get('field') or filter_item.get('fields')
        )
        raw_field = filter_item.get('field') or filter_item.get('fields') or filter_item.get('field_hint')
        mode = InsightNsConversationService._friendly_match_mode(filter_item.get('match_mode') or filter_item.get('operator'))
        value = filter_item.get('value')
        if value is None:
            value = filter_item.get('values')
        if isinstance(value, list):
            value_text = "、".join(str(item) for item in value)
        else:
            value_text = str(value) if value is not None else '未指定'
        field_text = field
        if raw_field and str(raw_field).strip() and str(raw_field).strip() != field:
            field_text = f"{field}（{raw_field}）"
        parts = [f"字段：{field_text}", f"操作：{mode}", f"值：{value_text}"]
        return "；".join(parts)

    @staticmethod
    def _friendly_derivation_mode(value: Any) -> str:
        mapping = {
            'query_output': '查询聚合结果',
            'formula': '公式计算',
            'algorithm': '算法计算',
        }
        key = str(value or '').strip()
        return mapping.get(key, key or '直接计算')

    @staticmethod
    def _stringify_list(values: Any, *, limit: int = 6) -> str:
        if not isinstance(values, list):
            return str(values) if values not in (None, '') else ''
        visible = [str(item) for item in values[:limit] if item not in (None, '')]
        suffix = f" 等 {len(values)} 项" if len(values) > limit else ''
        return " / ".join(visible) + suffix

    @staticmethod
    def _stringify_metric_fields(values: Any, *, limit: int = 6) -> str:
        """Render an unordered field set without making it look like a formula."""
        if not isinstance(values, list):
            return (
                InsightNsConversationService._friendly_metric(values)
                if values not in (None, '')
                else ''
            )
        visible = [
            InsightNsConversationService._friendly_metric(item)
            for item in values[:limit]
            if item not in (None, '')
        ]
        suffix = f" 等 {len(values)} 项" if len(values) > limit else ''
        return "、".join(visible) + suffix

    @staticmethod
    def _relevant_source_refs(source_refs: Any, query_refs: list[str], queries: list[Any]) -> list[dict[str, Any]]:
        if not isinstance(source_refs, list):
            return []
        refs = [source for source in source_refs if isinstance(source, dict)]
        if len(refs) <= 1:
            return refs

        query_ref_set = set(query_refs)
        matched = [
            source for source in refs
            if query_ref_set.intersection({str(item) for item in source.get('query_keys') or []})
        ]
        if matched:
            return matched

        query_text = " ".join(
            str(((query or {}).get('implementation') or {}).get('sql') or '')
            for query in queries
            if isinstance(query, dict)
        ).lower()
        if query_text:
            matched = []
            for source in refs:
                identifier = str(source.get('datasource_identifier') or '').strip().lower()
                name = str(source.get('datasource_name') or '').strip().lower()
                if (identifier and identifier in query_text) or (name and name in query_text):
                    matched.append(source)
            if matched:
                return matched

        refs_with_fields = [source for source in refs if source.get('field_names') or source.get('fields')]
        return refs_with_fields or refs[:1]

    @staticmethod
    def _code_transformations(transformations: Any) -> list[str]:
        if not isinstance(transformations, list):
            return []
        query_prefixes = (
            'where ',
            'and ',
            'or ',
            'having ',
            'order by ',
            'group by ',
            'select ',
            'from ',
        )
        code_items: list[str] = []
        for item in transformations:
            text = str(item or '').strip()
            if not text:
                continue
            normalized = text.lower().lstrip('(')
            if normalized.startswith(query_prefixes):
                continue
            if ' ilike ' in normalized or ' like ' in normalized:
                continue
            code_items.append(text)
        return code_items

    @staticmethod
    def _looks_like_query_scope_text(value: Any) -> bool:
        text = str(value or '').strip().lower()
        if not text:
            return False
        return any(keyword in text for keyword in (
            'where',
            ' ilike ',
            ' like ',
            '筛选',
            '过滤',
            '包含',
            '匹配',
            '时间范围',
        ))

    @staticmethod
    def _normalize_selected_context_value(selected_context: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        context = dict(selected_context or {})
        value = context.get('display_value')
        if value is None:
            value = context.get('raw_value')
        unit = str(evidence.get('unit') or '').strip()
        if value not in (None, '') and unit:
            text = str(value)
            if unit not in text:
                context['display_value'] = f"{text}{unit}" if unit == '%' else f"{text} {unit}"
        return context

    @staticmethod
    def _describe_sources(source_refs: Any, query_refs: list[str]) -> str:
        if isinstance(source_refs, list) and source_refs:
            names = [str(item.get('datasource_name') or item.get('datasource_identifier') or '数据源') for item in source_refs[:2] if isinstance(item, dict)]
            if names:
                return f"从 {'、'.join(names)} 读取计算所需数据。"
        return f"使用 {len(query_refs)} 个查询快照作为计算输入。" if query_refs else "该指标未提供查询快照引用。"

    @staticmethod
    def _source_items(source_refs: Any, metric_fields: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(source_refs, list):
            for source in source_refs[:2]:
                if not isinstance(source, dict):
                    continue
                items.append({"label": "来源", "value": source.get('datasource_name') or source.get('datasource_identifier') or '数据源'})
                if source.get('datasource_type'):
                    items.append({"label": "类型", "value": InsightNsConversationService._friendly_datasource_type(source.get('datasource_type'))})
                break
        if metric_fields:
            items.append({
                "label": "参与计算字段",
                "value": InsightNsConversationService._stringify_metric_fields(metric_fields),
            })
        return items

    @staticmethod
    def _describe_scope(query_constraints: Any) -> str:
        if not isinstance(query_constraints, dict) or not query_constraints:
            return "按照已校验的查询口径确定统计范围。"
        notes = str(query_constraints.get('notes') or '').strip()
        filters = query_constraints.get('target_filters')
        filter_texts = [
            InsightNsConversationService._describe_target_filter(item)
            for item in filters
        ] if isinstance(filters, list) else []
        aggregation = query_constraints.get('aggregation')
        aggregation = aggregation if isinstance(aggregation, dict) else {}
        metrics = aggregation.get('metrics') or aggregation.get('metric')
        group_by = aggregation.get('group_by') or query_constraints.get('group_by')
        metric_text = InsightNsConversationService._friendly_metric(metrics) if metrics else ''

        if notes:
            return notes if notes.endswith(('。', '！', '？')) else f"{notes}。"
        if filter_texts:
            count = len(filter_texts)
            return f"按 {count} 条过滤规则限定本次查询范围。"
        if group_by and metric_text:
            return f"按{InsightNsConversationService._stringify_list(group_by if isinstance(group_by, list) else [group_by])}划定统计分组。"
        if notes:
            return notes if '筛选' in notes or '过滤' in notes or '范围' in notes else f"{notes}。"

        parts = []
        time_range = query_constraints.get('time_range')
        if isinstance(time_range, dict) and (time_range.get('start') or time_range.get('end')):
            parts.append(f"时间：{time_range.get('start') or '不限'} 至 {time_range.get('end') or '不限'}")
        grain = aggregation.get('grain')
        if grain and not metric_text:
            parts.append(f"统计口径：{InsightNsConversationService._friendly_grain(grain)}")
        return "；".join(parts) if parts else "未设置额外筛选范围。"

    @staticmethod
    def _scope_items(query_constraints: Any, implementation: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(query_constraints, dict):
            time_range = query_constraints.get('time_range')
            if isinstance(time_range, dict) and (time_range.get('start') or time_range.get('end')):
                items.append({"label": "时间", "value": f"{time_range.get('start') or '不限'} 至 {time_range.get('end') or '不限'}"})
            aggregation = query_constraints.get('aggregation')
            if isinstance(aggregation, dict):
                group_by = aggregation.get('group_by') or query_constraints.get('group_by')
                if group_by:
                    items.append({"label": "分组方式", "value": InsightNsConversationService._stringify_list(group_by if isinstance(group_by, list) else [group_by])})
                elif aggregation.get('grain') and not (isinstance(target_filters := query_constraints.get('target_filters'), list) and target_filters):
                    items.append({"label": "统计口径", "value": InsightNsConversationService._friendly_grain(aggregation.get('grain'))})
            target_filters = query_constraints.get('target_filters')
            if isinstance(target_filters, list) and target_filters:
                items.append({
                    "label": "过滤规则",
                    "value": InsightNsConversationService._stringify_list([
                        InsightNsConversationService._format_filter_rule(item)
                        for item in target_filters
                    ], limit=3)
                })
            else:
                notes = str(query_constraints.get('notes') or '').strip()
                items.append({"label": "过滤规则", "value": "未设置额外筛选范围"})
                if notes and notes != items[-1]["value"]:
                    items[-1]["value"] = notes
        return items

    @staticmethod
    def _execution_status_text(execution: Any) -> str:
        if isinstance(execution, dict) and execution.get('status'):
            row_count = execution.get('row_count')
            if row_count is not None:
                return f"查询命中 {row_count} 条记录"
            return "查询口径已通过校验" if execution.get('status') == 'validated' else f"执行状态：{execution.get('status')}"
        return ''

    @staticmethod
    def _query_output_text(execution: Any, intermediate_outputs: Any) -> str:
        if isinstance(execution, dict):
            for key in ('row_count', 'rows', 'result_count'):
                if execution.get(key) is not None:
                    return f"获得 {execution.get(key)} 条记录"
        if isinstance(intermediate_outputs, list) and intermediate_outputs:
            first = intermediate_outputs[0]
            if isinstance(first, dict) and first.get('name') and first.get('value') is not None:
                return f"{first.get('name')}：{first.get('value')}"
        return ''

    @staticmethod
    def _describe_transformations(transformations: Any) -> str:
        if isinstance(transformations, list) and transformations:
            return "；".join(str(item) for item in transformations[:3])
        return "查询结果未再经过额外代码层清洗、转换或过滤，直接进入计算。"

    @staticmethod
    def _transformation_items(transformations: Any, intermediate_outputs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not isinstance(transformations, list) or not transformations:
            return items
        items.extend({"label": "处理动作", "value": str(item)} for item in transformations[:5] if item)
        if isinstance(intermediate_outputs, list):
            for output in intermediate_outputs[:3]:
                if not isinstance(output, dict) or not output.get('name'):
                    continue
                name = str(output.get('name'))
                if any(keyword in name for keyword in ('行数', '记录', '样本', '有效', '排除', '聚合')):
                    items.append({"label": name, "value": output.get('value')})
        return items

    @staticmethod
    def _data_filter_output_text(execution: Any, transformations: Any) -> str:
        if isinstance(transformations, list) and transformations:
            return "已完成代码层数据整理"
        if isinstance(execution, dict) and execution.get('row_count') is not None:
            return f"{execution.get('row_count')} 条查询命中记录直接进入计算"
        return ''

    @staticmethod
    def _describe_calculation_method(calculation: dict[str, Any], derivation: dict[str, Any]) -> str:
        metric_fields = calculation.get('metric_fields')
        metric_text = (
            InsightNsConversationService._stringify_metric_fields(metric_fields)
            if metric_fields
            else '指标字段'
        )
        aggregation = str(calculation.get('aggregation') or '').strip()
        friendly_aggregation = InsightNsConversationService._friendly_aggregation(aggregation) if aggregation else ''
        mode = str((derivation or {}).get('mode') or '').strip()
        description = str((derivation or {}).get('description') or '').strip()
        if calculation.get('formula'):
            return f"按公式 {calculation.get('formula')} 计算。"
        if mode == 'algorithm':
            return f"使用算法计算：{description or calculation.get('formula') or metric_text}"
        if friendly_aggregation:
            return f"对 {metric_text} 执行{friendly_aggregation}计算。"
        if description:
            return description
        return "根据查询结果计算基础指标。"

    @staticmethod
    def _describe_data_calculation(
        selected_context: dict[str, Any],
        evidence: dict[str, Any],
        derivation: dict[str, Any],
        calculation: dict[str, Any],
        transformations: Any,
    ) -> str:
        method = InsightNsConversationService._describe_calculation_method(calculation, derivation).rstrip('。')
        detail = InsightNsConversationService._describe_calculation(calculation, derivation, transformations).rstrip('。')
        value = selected_context.get('display_value') or selected_context.get('raw_value')
        metric_name = evidence.get('metric_name') or '当前指标'
        parts = []
        if detail and detail not in method and not InsightNsConversationService._looks_like_query_scope_text(derivation.get('description')):
            parts.append(detail)
        if method:
            parts.append(method)
        if value is not None:
            parts.append(f"当前点击的 {metric_name} 为 {value}")
        return "；".join(parts) + "。" if parts else f"当前点击位置已关联到 {metric_name} 的计算逻辑。"

    @staticmethod
    def _calculation_items(
        calculation: dict[str, Any],
        intermediate_outputs: Any,
        derivation: dict[str, Any] | None = None,
        transformations: Any = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        derivation = derivation if isinstance(derivation, dict) else {}
        if derivation.get('description') and not InsightNsConversationService._looks_like_query_scope_text(derivation.get('description')):
            items.append({"label": "计算逻辑", "value": derivation.get('description')})
        if derivation.get('mode'):
            items.append({"label": "生成方式", "value": InsightNsConversationService._friendly_derivation_mode(derivation.get('mode'))})
        metric_fields = calculation.get('metric_fields')
        if metric_fields:
            items.append({
                "label": "参与计算字段",
                "value": InsightNsConversationService._stringify_metric_fields(metric_fields),
            })
        if calculation.get('aggregation'):
            items.append({"label": "计算方式", "value": InsightNsConversationService._friendly_aggregation(calculation.get('aggregation'))})
        if calculation.get('group_by'):
            items.append({"label": "分组维度", "value": InsightNsConversationService._stringify_list(calculation.get('group_by'))})
        if isinstance(transformations, list) and transformations:
            items.append({"label": "处理步骤", "value": "；".join(str(item) for item in transformations[:3])})
        items.extend(InsightNsConversationService._calculation_intermediate_items(
            intermediate_outputs,
            calculation,
        ))
        return items

    @staticmethod
    def _calculation_intermediate_items(
        intermediate_outputs: Any,
        calculation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not isinstance(intermediate_outputs, list):
            return []
        metric_fields = {
            str(item).strip().lower()
            for item in calculation.get('metric_fields') or []
            if str(item).strip()
        }
        formula = str(calculation.get('formula') or '').lower()
        include_keywords = (
            '分子',
            '分母',
            '比例',
            '占比',
            '平均',
            '均值',
            '最大',
            '最小',
            '合计',
            '总额',
            '结果',
            '当前',
            '基础指标',
        )
        exclude_names = {
            '总记录数',
            '活跃记录数',
            '已删除记录数',
            '用户数',
        }
        items: list[dict[str, Any]] = []
        for output in intermediate_outputs:
            if not isinstance(output, dict) or not output.get('name'):
                continue
            name = str(output.get('name')).strip()
            normalized_name = name.lower()
            if name in exclude_names or normalized_name.startswith('top'):
                continue
            is_relevant = any(keyword in name for keyword in include_keywords)
            is_relevant = is_relevant or any(field and field in normalized_name for field in metric_fields)
            is_relevant = is_relevant or (formula and normalized_name in formula)
            if is_relevant:
                items.append({"label": name, "value": output.get('value')})
            if len(items) >= 4:
                break
        return items

    @staticmethod
    def _data_calculation_items(
        selected_context: dict[str, Any],
        evidence: dict[str, Any],
        derivation: dict[str, Any],
        calculation: dict[str, Any],
        intermediate_outputs: Any,
        transformations: Any,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        value = selected_context.get('display_value') or selected_context.get('raw_value')
        if value is not None:
            items.append({"label": "当前值", "value": value})
        if calculation.get('formula'):
            items.append({"label": "计算公式", "value": calculation.get('formula')})
        items.extend(InsightNsConversationService._calculation_items(
            calculation,
            intermediate_outputs,
            derivation,
            transformations,
        ))
        for item in InsightNsConversationService._current_value_items(selected_context, evidence, derivation, calculation):
            if item.get('label') in {'原始值'} and value is not None:
                continue
            if not any(existing.get('label') == item.get('label') and existing.get('value') == item.get('value') for existing in items):
                items.append(item)
        return items

    @staticmethod
    def _derivation_items(derivation: dict[str, Any], calculation: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if derivation.get('mode'):
            items.append({"label": "生成方式", "value": derivation.get('mode')})
        if calculation.get('format'):
            items.append({"label": "展示格式", "value": calculation.get('format')})
        return items

    @staticmethod
    def _describe_current_value(
        selected_context: dict[str, Any],
        evidence: dict[str, Any],
        derivation: dict[str, Any],
        calculation: dict[str, Any],
    ) -> str:
        value = selected_context.get('display_value') or selected_context.get('raw_value')
        metric_name = evidence.get('metric_name') or '当前指标'
        description = derivation.get('description') or calculation.get('formula') or ''
        if value is not None and description:
            return f"当前点击的 {metric_name} 数值为 {value}，它由上述计算逻辑得到：{description}"
        if value is not None:
            return f"当前点击的 {metric_name} 数值为 {value}，它复用上述计算逻辑得到。"
        return f"当前点击位置已关联到 {metric_name} 的证据链。"

    @staticmethod
    def _current_value_items(
        selected_context: dict[str, Any],
        evidence: dict[str, Any],
        derivation: dict[str, Any],
        calculation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if evidence.get('metric_name'):
            items.append({"label": "对应指标", "value": evidence.get('metric_name')})
        if selected_context.get('raw_value') is not None:
            items.append({"label": "原始值", "value": selected_context.get('raw_value')})
        if evidence.get('unit'):
            items.append({"label": "单位", "value": evidence.get('unit')})
        if derivation.get('mode'):
            items.append({"label": "生成方式", "value": InsightNsConversationService._friendly_derivation_mode(derivation.get('mode'))})
        if calculation.get('format'):
            items.append({"label": "展示格式", "value": calculation.get('format')})
        dimensions = selected_context.get('dimensions')
        if isinstance(dimensions, dict):
            for key, value in list(dimensions.items())[:4]:
                items.append({"label": str(key), "value": value})
        return items

    @staticmethod
    def _describe_reliability(execution: Any, quality_notes: Any) -> str:
        validated = isinstance(execution, dict) and execution.get('status') in {'validated', 'success'}
        if isinstance(quality_notes, list) and quality_notes:
            prefix = "查询口径已通过校验。" if validated else "已记录本次计算的质量说明。"
            return f"{prefix}{str(quality_notes[0])}"
        if validated:
            return "查询口径已通过校验，当前证据链记录了数据来源、统计范围和计算逻辑。"
        return "当前证据链记录了数据来源、统计范围和计算逻辑，可展开查看查询口径与执行信息。"

    @staticmethod
    def _reliability_items(execution: Any, quality_notes: Any, intermediate_outputs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(execution, dict) and execution.get('status'):
            items.append({"label": "口径校验", "value": "已通过" if execution.get('status') in {'validated', 'success'} else execution.get('status')})
            if execution.get('row_count') is not None:
                items.append({"label": "查询命中", "value": f"{execution.get('row_count')} 条记录"})
        if isinstance(quality_notes, list) and quality_notes:
            items.append({"label": "质量说明", "value": str(quality_notes[0])})
        return items

    @staticmethod
    def _reliability_badges(execution: Any, quality_notes: Any) -> list[str]:
        badges: list[str] = []
        if isinstance(execution, dict) and execution.get('status') in {'validated', 'success'}:
            badges.append("查询口径已校验")
        if isinstance(quality_notes, list) and quality_notes:
            risky_keywords = ('LIMIT', 'TopN', '抽样', '采样', 'head', 'tail', 'sample', '截取', '缺失', '排除')
            if any(any(keyword in str(note) for keyword in risky_keywords) for note in quality_notes):
                badges.append("存在口径说明")
            else:
                badges.append("无明显截断说明")
        return badges or ["证据链已生成"]

    @staticmethod
    def _filter_chain_steps(chain_steps: list[dict[str, Any]], *, detail_level: str) -> list[dict[str, Any]]:
        if detail_level == 'full':
            return chain_steps
        filtered: list[dict[str, Any]] = []
        for step in chain_steps:
            item = dict(step)
            details = item.get('details')
            if isinstance(details, dict):
                safe_details = {
                    key: value for key, value in details.items()
                    if key not in {'implementation'}
                }
                if safe_details:
                    item['details'] = safe_details
                else:
                    item.pop('details', None)
            filtered.append(item)
        return filtered

    def list_turn_versions(self, username: str, conversation_id: Any, turn_id: Any) -> dict[str, Any] | None:
        """返回某个轮次可回溯的分析任务版本列表。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None
        turn = self._get_turn(conversation, turn_id)
        if turn is None:
            return None

        tasks = self.session.query(InsightAnalysisTask).filter(
            InsightAnalysisTask.conversation_id == conversation.id,
            InsightAnalysisTask.turn_id == turn.id,
        ).order_by(
            InsightAnalysisTask.created_at.desc(),
            InsightAnalysisTask.id.desc(),
        ).all()

        versions = [self._build_task_version_item(task, turn) for task in tasks]
        initial_version = self._build_initial_version_item(turn)
        if initial_version is not None:
            versions.append(initial_version)

        return {
            "conversation": conversation.to_dict(),
            "turn": turn.to_dict(),
            "versions": versions,
        }

    def get_turn_version_detail(
        self,
        username: str,
        conversation_id: Any,
        turn_id: Any,
        analysis_task_id: Any = None,
    ) -> dict[str, Any] | None:
        """按分析任务查看某个轮次版本的完整执行产物。"""
        conversation = self._get_accessible_conversation(username, conversation_id)
        if conversation is None:
            return None
        turn = self._get_turn(conversation, turn_id)
        if turn is None:
            return None

        task_id_int = to_int(analysis_task_id, 0)
        if task_id_int > 0:
            task = self.session.query(InsightAnalysisTask).filter(
                InsightAnalysisTask.id == task_id_int,
                InsightAnalysisTask.conversation_id == conversation.id,
                InsightAnalysisTask.turn_id == turn.id,
            ).first()
            if task is None:
                return None
            task_filter = lambda model: model.analysis_task_id == task_id_int
            version = self._build_task_version_item(task, turn)
        else:
            task_filter = lambda model: model.analysis_task_id.is_(None)
            version = self._build_initial_version_item(turn)
            if version is None:
                return None

        messages = self.session.query(InsightNsMessage).filter(
            InsightNsMessage.insight_conversation_id == conversation.id,
            InsightNsMessage.turn_id == turn.id,
            InsightNsMessage.is_deleted == 0,
            task_filter(InsightNsMessage),
        ).order_by(
            InsightNsMessage.seq_no.asc(),
            InsightNsMessage.id.asc(),
        ).all()
        executions = self.session.query(InsightNsExecution).filter(
            InsightNsExecution.conversation_id == conversation.id,
            InsightNsExecution.turn_id == turn.id,
            InsightNsExecution.is_deleted == 0,
            task_filter(InsightNsExecution),
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.turn_id == turn.id,
            InsightNsArtifact.is_deleted == 0,
            task_filter(InsightNsArtifact),
        ).order_by(
            InsightNsArtifact.sort_no.asc(),
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        return {
            "conversation": conversation.to_dict(),
            "turn": turn.to_dict(),
            "version": version,
            "messages": [message.to_dict() for message in messages],
            "executions": [execution.to_dict() for execution in executions],
            "latest_execution": executions[-1].to_dict() if executions else None,
            "artifacts": [self._artifact_to_view(artifact) for artifact in artifacts],
        }

    def export_turn_pdf(
        self,
        username: str,
        conversation_id: Any,
        turn_id: Any,
    ) -> tuple[bytes, str] | None:
        """把单轮分析结果导出为 PDF。"""
        detail = self.get_turn_detail(username=username, conversation_id=conversation_id, turn_id=turn_id)
        if detail is None:
            return None

        turn = detail["turn"]
        artifacts = detail["artifacts"]
        latest_execution = detail.get("latest_execution") or {}

        report_text = (
            next(
                (
                    artifact.get("content_json", {}).get("report_markdown", '')
                    for artifact in artifacts
                    if artifact.get("artifact_type") == 'report' and artifact.get("content_json", {}).get("report_markdown")
                ),
                '',
            )
            or turn.get("final_answer", '')
            or latest_execution.get("analysis_report", '')
        )
        chart_artifacts = [artifact for artifact in artifacts if artifact.get("artifact_type") == 'chart']

        pdf_bytes = self._build_turn_pdf_bytes(
            title='',
            chart_artifacts=chart_artifacts,
            report_text=report_text,
        )
        filename = self._build_pdf_filename('analysis-result')
        return pdf_bytes, filename

    def _get_accessible_conversation(self, username: str, conversation_id: Any) -> InsightNsConversation | None:
        conversation_id_int = to_int(conversation_id, 0)
        if conversation_id_int <= 0:
            return None
        return self.session.query(InsightNsConversation).join(
            InsightNamespace,
            InsightNamespace.id == InsightNsConversation.insight_namespace_id,
        ).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.is_deleted == 0,
            InsightNamespace.username == username,
            InsightNamespace.namespace_kind == 'interactive',
            InsightNamespace.is_deleted == 0,
        ).first()

    def _get_turn(self, conversation: InsightNsConversation, turn_id: Any) -> InsightNsTurn | None:
        turn_id_int = to_int(turn_id, 0)
        if turn_id_int <= 0:
            return None
        return self.session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id_int,
            InsightNsTurn.conversation_id == conversation.id,
            InsightNsTurn.is_deleted == 0,
        ).first()

    def _count_version_rows(self, model, turn_id: int, analysis_task_id: int | None, enabled_only: bool = False) -> int:
        query = self.session.query(model.id).filter(
            model.turn_id == turn_id,
            model.is_deleted == 0,
        )
        if analysis_task_id is None:
            query = query.filter(model.analysis_task_id.is_(None))
        else:
            query = query.filter(model.analysis_task_id == analysis_task_id)
        if enabled_only:
            query = query.filter(model.content_enabled == 1)
        return query.count()

    def _build_task_version_item(self, task: InsightAnalysisTask, turn: InsightNsTurn) -> dict[str, Any]:
        task_id = int(task.id or 0)
        message_count = self._count_version_rows(InsightNsMessage, turn.id, task_id)
        execution_count = self._count_version_rows(InsightNsExecution, turn.id, task_id)
        artifact_count = self._count_version_rows(InsightNsArtifact, turn.id, task_id)
        current_count = (
            self._count_version_rows(InsightNsMessage, turn.id, task_id, enabled_only=True)
            + self._count_version_rows(InsightNsExecution, turn.id, task_id, enabled_only=True)
            + self._count_version_rows(InsightNsArtifact, turn.id, task_id, enabled_only=True)
        )
        return {
            "analysis_task_id": task_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "status": task.status,
            "is_current": current_count > 0,
            "message_count": message_count,
            "execution_count": execution_count,
            "artifact_count": artifact_count,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "label": "刷新分析" if task.task_type == "rerun" else "分析",
        }

    def _build_initial_version_item(self, turn: InsightNsTurn) -> dict[str, Any] | None:
        message_count = self._count_version_rows(InsightNsMessage, turn.id, None)
        execution_count = self._count_version_rows(InsightNsExecution, turn.id, None)
        artifact_count = self._count_version_rows(InsightNsArtifact, turn.id, None)
        assistant_message_count = self.session.query(InsightNsMessage.id).filter(
            InsightNsMessage.turn_id == turn.id,
            InsightNsMessage.analysis_task_id.is_(None),
            InsightNsMessage.role == 'assistant',
            InsightNsMessage.is_deleted == 0,
        ).count()
        if assistant_message_count + execution_count + artifact_count <= 0:
            return None
        current_count = (
            self.session.query(InsightNsMessage.id).filter(
                InsightNsMessage.turn_id == turn.id,
                InsightNsMessage.analysis_task_id.is_(None),
                InsightNsMessage.role == 'assistant',
                InsightNsMessage.is_deleted == 0,
                InsightNsMessage.content_enabled == 1,
            ).count()
            + self._count_version_rows(InsightNsExecution, turn.id, None, enabled_only=True)
            + self._count_version_rows(InsightNsArtifact, turn.id, None, enabled_only=True)
        )
        return {
            "analysis_task_id": None,
            "task_id": None,
            "task_type": "initial",
            "status": turn.status,
            "is_current": current_count > 0,
            "message_count": message_count,
            "execution_count": execution_count,
            "artifact_count": artifact_count,
            "created_at": turn.created_at.isoformat() if turn.created_at else None,
            "started_at": turn.started_at.isoformat() if turn.started_at else None,
            "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
            "label": "初始分析",
        }

    def _group_executions_by_turn(self, executions: list[InsightNsExecution]) -> dict[int, list[dict[str, Any]]]:
        execution_map: dict[int, list[dict[str, Any]]] = {}
        for execution in executions:
            execution_map.setdefault(execution.turn_id, []).append(execution.to_dict())
        return execution_map

    def _empty_turn_artifacts(self) -> dict[str, Any]:
        return {
            'analysis_report': '',
            'report_evidence_refs': {},
            'charts': [],
            'tables': [],
        }

    def _group_artifacts_by_turn(self, artifacts: list[InsightNsArtifact]) -> dict[int, dict[str, Any]]:
        """按轮次聚合产物，便于历史列表直接渲染结果卡。"""
        artifact_map: dict[int, dict[str, Any]] = {}
        for artifact in artifacts:
            turn_artifacts = artifact_map.setdefault(artifact.turn_id, self._empty_turn_artifacts())
            artifact_view = self._artifact_to_view(artifact)

            if artifact_view['artifact_type'] == 'chart':
                turn_artifacts['charts'].append({
                    'id': artifact_view['id'],
                    'title': artifact_view['title'],
                    'summary_text': artifact_view['summary_text'],
                    'chart_type': artifact_view['content_json'].get('chart_type', ''),
                    'chart_spec': artifact_view['content_json'].get('chart_spec', {}),
                    'evidence_refs': artifact_view.get('metadata_json', {}).get('evidence_refs', {}),
                    'sort_no': artifact_view.get('sort_no', 0),
                })
            elif artifact_view['artifact_type'] == 'report':
                report_markdown = artifact_view['content_json'].get('report_markdown') or artifact_view['summary_text']
                if report_markdown:
                    turn_artifacts['analysis_report'] = report_markdown
                    turn_artifacts['report_evidence_refs'] = artifact_view.get('metadata_json', {}).get('evidence_refs', {})
            elif artifact_view['artifact_type'] == 'table':
                turn_artifacts['tables'].append({
                    'id': artifact_view['id'],
                    'title': artifact_view['title'],
                    'summary_text': artifact_view['summary_text'],
                    'columns': artifact_view['content_json'].get('columns', []),
                    'rows': artifact_view['content_json'].get('rows', []),
                    'evidence_refs': artifact_view.get('metadata_json', {}).get('evidence_refs', {}),
                    'sort_no': artifact_view.get('sort_no', 0),
                })

        for turn_artifacts in artifact_map.values():
            turn_artifacts['charts'].sort(key=lambda item: (item.get('sort_no', 0), item.get('id', 0)))
            turn_artifacts['tables'].sort(key=lambda item: (item.get('sort_no', 0), item.get('id', 0)))
        return artifact_map

    def _artifact_to_view(self, artifact: InsightNsArtifact) -> dict[str, Any]:
        """
        统一解析产物中的 JSON 字段。

        chart_spec 会在这里做一次轻量规范化，保证前端展示和导出链看到的是同一份图表定义。
        """
        payload = artifact.to_dict()
        content_json = payload.get('content_json')
        if isinstance(content_json, str):
            try:
                content_json = json.loads(content_json)
            except Exception:
                content_json = {}
        payload['content_json'] = content_json if isinstance(content_json, dict) else {}
        if (
            payload.get('artifact_type') == 'chart'
            and isinstance(payload['content_json'].get('chart_spec'), dict)
        ):
            payload['content_json']['chart_spec'] = normalize_chart_spec(payload['content_json']['chart_spec'])
        metadata_json = payload.get('metadata_json')
        if isinstance(metadata_json, str):
            try:
                metadata_json = json.loads(metadata_json)
            except Exception:
                metadata_json = {}
        payload['metadata_json'] = metadata_json if isinstance(metadata_json, dict) else {}
        return payload

    def _build_execution_summary(self, execution: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": execution.get("id"),
            "turn_id": execution.get("turn_id"),
            "title": execution.get("title", ''),
            "description": execution.get("description", ''),
            "execution_status": execution.get("execution_status", ''),
            "is_runnable": bool(
                execution.get("is_runnable")
                if "is_runnable" in execution
                else (
                    execution.get("execution_status") == "success"
                    and str(execution.get("generated_code") or '').strip()
                )
            ),
            "result_payload_json": execution.get("result_payload_json", '{}'),
            "analysis_report": execution.get("analysis_report", ''),
            "analysis_parameters_schema": execution.get("analysis_parameters_schema", {}),
            "analysis_parameters": execution.get("analysis_parameters", {}),
            "analysis_logic_signature": execution.get("analysis_logic_signature", ''),
            "parent_execution_id": execution.get("parent_execution_id"),
            "execution_source": execution.get("execution_source", ''),
            "error_message": execution.get("error_message", ''),
            "execution_seconds": execution.get("execution_seconds", 0),
            "finished_at": execution.get("finished_at"),
        }

    def _build_pdf_filename(self, title: str) -> str:
        normalized = re.sub(r'[\\/:*?"<>|]+', '-', title or 'analysis-result')
        normalized = re.sub(r'\s+', '-', normalized).strip('-')[:80] or 'analysis-result'
        return f"{normalized}.pdf"

    def _build_turn_pdf_bytes(
        self,
        title: str,
        chart_artifacts: list[dict[str, Any]],
        report_text: str,
    ) -> bytes:
        """Use an HTML/CSS print pipeline so Markdown styles survive PDF export."""
        document_html = self._build_turn_pdf_html(
            title=title or '分析结果',
            chart_artifacts=chart_artifacts,
            report_text=report_text,
        )
        html_renderer = self._get_weasyprint_html_class()
        return html_renderer(string=document_html, base_url='.').write_pdf()

    def _get_weasyprint_html_class(self):
        global HTML, WEASYPRINT_IMPORT_ERROR
        if HTML is not None:
            return HTML
        self._register_weasyprint_dll_directories()
        try:
            from weasyprint import HTML as weasyprint_html
        except OSError as exc:
            WEASYPRINT_IMPORT_ERROR = exc
            raise RuntimeError(f"WeasyPrint is unavailable: {exc}") from exc
        HTML = weasyprint_html
        return HTML

    def _register_weasyprint_dll_directories(self) -> None:
        if os.name != 'nt' or not hasattr(os, 'add_dll_directory'):
            return
        candidate_dirs = []
        configured = os.environ.get('WEASYPRINT_DLL_PATH', '')
        if configured:
            candidate_dirs.extend(path for path in configured.split(os.pathsep) if path)
        candidate_dirs.extend([
            r'D:\msys64\ucrt64\bin',
            r'C:\msys64\ucrt64\bin',
        ])
        for dll_dir in candidate_dirs:
            if not os.path.isdir(dll_dir):
                continue
            normalized_dir = os.path.normcase(os.path.abspath(dll_dir))
            if normalized_dir in WEASYPRINT_DLL_DIRECTORIES:
                continue
            handle = os.add_dll_directory(dll_dir)
            WEASYPRINT_DLL_DIRECTORY_HANDLES.append(handle)
            WEASYPRINT_DLL_DIRECTORIES.add(normalized_dir)

    def _build_turn_pdf_html(
        self,
        title: str,
        chart_artifacts: list[dict[str, Any]],
        report_text: str,
    ) -> str:
        report_html = self._markdown_to_html(report_text) if report_text else '<p class="empty-report">当前轮次暂无可导出的分析报告内容。</p>'
        chart_html = self._chart_artifacts_to_html(chart_artifacts)
        escaped_title = html.escape(title or '分析结果')
        header_html = f"""<header class="report-header">
      <h1>{escaped_title}</h1>
    </header>""" if title else ''
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{escaped_title}</title>
  <style>
{self._pdf_print_css()}
  </style>
</head>
<body>
  <main class="report-page">
    {header_html}
    {chart_html}
    <section class="markdown-body">
      {report_html}
    </section>
  </main>
</body>
</html>"""

    def _markdown_to_html(self, text: str) -> str:
        md = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table')
        return self._replace_emoji_with_images(md.render(text or ''))

    def _replace_emoji_with_images(self, rendered_html: str) -> str:
        def replace_match(match: re.Match[str]) -> str:
            emoji_text = match.group(0)
            data_uri = self._get_twemoji_data_uri(emoji_text)
            if not data_uri:
                return emoji_text
            escaped_emoji = html.escape(emoji_text, quote=True)
            return f'<img class="emoji" draggable="false" alt="{escaped_emoji}" src="{data_uri}">'

        return EMOJI_CLUSTER_RE.sub(replace_match, rendered_html or '')

    def _get_twemoji_data_uri(self, emoji_text: str) -> str:
        emoji_key = self._twemoji_asset_key(emoji_text)
        if not emoji_key:
            return ''
        cache_path = self._twemoji_cache_dir() / f'{emoji_key}.svg'
        if not cache_path.exists():
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(f'{TWEMOJI_BASE_URL}/{emoji_key}.svg', timeout=5) as response:
                    cache_path.write_bytes(response.read())
            except Exception as exc:
                logger.warning("Twemoji 图标获取失败 emoji=%s key=%s error=%s", emoji_text, emoji_key, exc)
                return ''
        svg_bytes = cache_path.read_bytes()
        return 'data:image/svg+xml;base64,' + base64.b64encode(svg_bytes).decode('ascii')

    def _twemoji_asset_key(self, emoji_text: str) -> str:
        codepoints = [
            f'{ord(char):x}'
            for char in emoji_text
            if ord(char) not in (0xfe0e, 0xfe0f)
        ]
        return '-'.join(codepoints)

    def _twemoji_cache_dir(self) -> Path:
        return Path(os.environ.get('TEMP_DIR') or 'temp') / 'twemoji'

    def _chart_artifacts_to_html(self, chart_artifacts: list[dict[str, Any]]) -> str:
        chart_blocks = []
        for artifact in chart_artifacts:
            image_bytes = self._render_chart_artifact_png(artifact)
            if image_bytes is None:
                continue
            title = html.escape(str(artifact.get('title') or '图表'))
            data_uri = base64.b64encode(image_bytes).decode('ascii')
            chart_blocks.append(
                f"""<figure class="chart-block">
  <figcaption>{title}</figcaption>
  <img src="data:image/png;base64,{data_uri}" alt="{title}">
</figure>"""
            )
        if not chart_blocks:
            return ''
        return '<section class="chart-section">\n' + '\n'.join(chart_blocks) + '\n</section>'

    def _render_chart_artifact_png(self, artifact: dict[str, Any]) -> bytes | None:
        content_json = artifact.get('content_json', {})
        if not isinstance(content_json, dict):
            content_json = {}
        chart_spec = content_json.get('chart_spec')
        if isinstance(chart_spec, dict) and chart_spec:
            image_bytes = render_chart_spec_to_png(chart_spec)
            if image_bytes is not None:
                return image_bytes
        logger.warning(
            "PDF 图表渲染被跳过 artifact_id=%s title=%s has_chart_spec=%s",
            artifact.get('id', 0),
            artifact.get('title', ''),
            bool(isinstance(chart_spec, dict) and chart_spec),
        )
        return None

    def _pdf_print_css(self) -> str:
        return """
@page {
  size: A4;
  margin: 16mm 18mm;
}
html {
  color: #0f172a;
  font-family: "Microsoft YaHei", "Segoe UI Emoji", "Noto Color Emoji", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
  font-size: 14px;
  line-height: 1.75;
}
body {
  margin: 0;
  background: #ffffff;
}
.report-page {
  width: 100%;
}
.report-header {
  margin: 0 0 12px;
}
.report-header h1 {
  color: #0f172a;
  margin: 0 0 12px;
}
.chart-section {
  margin: 0 0 14px;
}
.chart-block {
  break-inside: avoid;
  margin: 0 0 14px;
}
.chart-block figcaption {
  color: #0f172a;
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 12px;
}
.chart-block img {
  display: block;
  height: auto;
  max-width: 100%;
}
.emoji {
  display: inline-block;
  height: 1.15em;
  margin: 0 0.04em;
  vertical-align: -0.18em;
  width: 1.15em;
}
.markdown-body h1,
.markdown-body h2,
.markdown-body h3,
.markdown-body h4 {
  break-after: avoid;
  font-weight: 700;
  color: #0f172a;
  margin: 0 0 12px;
}
.markdown-body h1 {
  font-size: 2em;
}
.markdown-body h2 {
  font-size: 1.5em;
}
.markdown-body h3 {
  font-size: 1.17em;
}
.markdown-body h4 {
  font-size: 1em;
}
.markdown-body p {
  margin: 0 0 12px;
}
.markdown-body strong {
  font-weight: 700;
}
.markdown-body ul,
.markdown-body ol {
  margin: 0 0 12px 20px;
  padding: 0;
}
.markdown-body li {
  margin: 3px 0;
}
.markdown-body blockquote {
  margin: 0 0 12px;
}
.markdown-body code {
  font-family: Consolas, "Liberation Mono", monospace;
}
.markdown-body pre {
  font-family: Consolas, "Liberation Mono", monospace;
  margin: 0 0 12px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.markdown-body pre code {
  background: transparent;
  color: inherit;
  padding: 0;
}
.markdown-body table {
  border-collapse: collapse;
  margin: 12px 0 16px;
  table-layout: fixed;
  width: 100%;
}
.markdown-body th,
.markdown-body td {
  border: 1px solid #dbe3ef;
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}
.markdown-body th {
  font-weight: 700;
}
.empty-report {
  color: #6b7280;
}
"""
