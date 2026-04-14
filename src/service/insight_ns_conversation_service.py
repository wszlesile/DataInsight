import json
import re
from datetime import datetime
from io import BytesIO
from typing import Any

from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy import or_
from sqlalchemy.orm import Session

from model import (
    InsightNsArtifact,
    InsightNsConversation,
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


class InsightNsConversationService:
    """会话历史、轮次详情与导出相关的查询服务。"""

    def __init__(self, session: Session):
        self.session = session

    def list_conversations(self, username: str, namespace_id: Any) -> list[dict[str, Any]]:
        """按空间查询当前用户的会话列表。"""
        namespace_id_int = to_int(namespace_id, 0)
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
        ).order_by(
            InsightNsExecution.created_at.asc(),
            InsightNsExecution.id.asc(),
        ).all()
        artifacts = self.session.query(InsightNsArtifact).filter(
            InsightNsArtifact.conversation_id == conversation.id,
            InsightNsArtifact.is_deleted == 0,
        ).order_by(
            InsightNsArtifact.sort_no.asc(),
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        execution_map = self._group_executions_by_turn(executions)
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
                "charts": turn_artifacts.get('charts', []),
                "tables": turn_artifacts.get('tables', []),
                "chart_artifact_id": primary_chart.get('id', 0),
                "chart_artifact_ids": [item.get('id', 0) for item in turn_artifacts.get('charts', []) if item.get('id')],
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
            InsightNsArtifact.sort_no.asc(),
            InsightNsArtifact.created_at.asc(),
            InsightNsArtifact.id.asc(),
        ).all()

        return {
            "conversation": conversation.to_dict(),
            "turn": turn.to_dict(),
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
        return self.session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.is_deleted == 0,
        ).first()

    def _group_executions_by_turn(self, executions: list[InsightNsExecution]) -> dict[int, list[dict[str, Any]]]:
        execution_map: dict[int, list[dict[str, Any]]] = {}
        for execution in executions:
            execution_map.setdefault(execution.turn_id, []).append(execution.to_dict())
        return execution_map

    def _empty_turn_artifacts(self) -> dict[str, Any]:
        return {
            'analysis_report': '',
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
                    'sort_no': artifact_view.get('sort_no', 0),
                })
            elif artifact_view['artifact_type'] == 'report':
                report_markdown = artifact_view['content_json'].get('report_markdown') or artifact_view['summary_text']
                if report_markdown:
                    turn_artifacts['analysis_report'] = report_markdown
            elif artifact_view['artifact_type'] == 'table':
                turn_artifacts['tables'].append({
                    'id': artifact_view['id'],
                    'title': artifact_view['title'],
                    'summary_text': artifact_view['summary_text'],
                    'columns': artifact_view['content_json'].get('columns', []),
                    'rows': artifact_view['content_json'].get('rows', []),
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
            "result_payload_json": execution.get("result_payload_json", '{}'),
            "analysis_report": execution.get("analysis_report", ''),
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
        """把图表与分析报告排版成 PDF 字节流。"""
        self._ensure_pdf_font_registered()

        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title=title or '分析结果',
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'InsightPdfTitle',
            parent=styles['Title'],
            fontName='STSong-Light',
            fontSize=18,
            leading=24,
            spaceAfter=10,
        )
        section_style = ParagraphStyle(
            'InsightPdfSection',
            parent=styles['Heading2'],
            fontName='STSong-Light',
            fontSize=12,
            leading=18,
            spaceBefore=8,
            spaceAfter=8,
        )
        body_style = ParagraphStyle(
            'InsightPdfBody',
            parent=styles['BodyText'],
            fontName='STSong-Light',
            fontSize=10.5,
            leading=17,
            spaceAfter=6,
        )

        story = []
        if title:
            story.append(Paragraph(self._escape_pdf_text(title), title_style))

        for artifact in chart_artifacts:
            chart_image = self._build_chart_pdf_image(artifact)
            if chart_image is None:
                continue
            story.append(chart_image)
            story.append(Spacer(1, 8))

        if report_text:
            for block in self._markdown_to_pdf_blocks(report_text):
                story.append(Paragraph(self._escape_pdf_text(block).replace('\n', '<br/>'), body_style))
        else:
            story.append(Paragraph('当前轮次暂无可导出的分析报告内容。', body_style))

        document.build(story)
        return buffer.getvalue()

    def _build_chart_pdf_image(self, artifact: dict[str, Any]) -> Image | None:
        content_json = artifact.get('content_json', {})
        if not isinstance(content_json, dict):
            content_json = {}

        image_bytes = None
        chart_spec = content_json.get('chart_spec')
        if isinstance(chart_spec, dict) and chart_spec:
            image_bytes = render_chart_spec_to_png(chart_spec)
        if image_bytes is None:
            logger.warning(
                "PDF 图表渲染被跳过: artifact_id=%s title=%s has_chart_spec=%s",
                artifact.get('id', 0),
                artifact.get('title', ''),
                bool(isinstance(chart_spec, dict) and chart_spec),
            )
            return None

        pil_image = PILImage.open(BytesIO(image_bytes))
        width, height = pil_image.size
        max_width = A4[0] - 36 * mm
        draw_width = min(float(width), max_width)
        draw_height = draw_width * float(height) / float(width)
        return Image(BytesIO(image_bytes), width=draw_width, height=draw_height)

    def _markdown_to_pdf_blocks(self, text: str) -> list[str]:
        normalized = (text or '').replace('\r\n', '\n').strip()
        if not normalized:
            return []
        normalized = re.sub(r'```[\s\S]*?```', '', normalized)
        normalized = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', normalized)
        normalized = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', normalized)
        normalized = re.sub(r'(?m)^>\s?', '', normalized)
        normalized = re.sub(r'(?m)^#{1,6}\s*', '', normalized)
        normalized = re.sub(r'(?m)^-{3,}\s*$', '', normalized)
        normalized = re.sub(r'\*\*(.*?)\*\*', r'\1', normalized)
        normalized = re.sub(r'__(.*?)__', r'\1', normalized)
        normalized = re.sub(r'(?<!\*)\*(?!\*)(.*?)\*(?<!\*)', r'\1', normalized)
        normalized = re.sub(r'(?<!_)_(?!_)(.*?)_(?<!_)', r'\1', normalized)

        blocks = []
        for block in re.split(r'\n\s*\n', normalized):
            cleaned = block.strip()
            if not cleaned:
                continue
            lines = []
            for line in cleaned.split('\n'):
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith('|') and stripped.endswith('|'):
                    stripped = ' '.join(part.strip() for part in stripped.strip('|').split('|') if part.strip())
                lines.append(stripped)
            if lines:
                blocks.append('\n'.join(lines))
        return blocks

    def _escape_pdf_text(self, text: str) -> str:
        return (
            (text or '')
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )

    def _ensure_pdf_font_registered(self) -> None:
        if 'STSong-Light' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
