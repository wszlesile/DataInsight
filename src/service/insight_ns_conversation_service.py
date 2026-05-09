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
