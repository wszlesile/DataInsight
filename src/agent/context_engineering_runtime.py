import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config import Config
from config.database import SessionLocal
from model import InsightDatasource, InsightNsConversation, InsightNsExecution, InsightNsRelDatasource
from service.conversation_context_service import ConversationContextService
from utils.context_compression import clip_text
from utils.datasource_utils import (
    extract_datasource_identifier,
    extract_datasource_schema,
    normalize_datasource_type,
    recommend_local_file_loader,
    safe_json_loads,
    to_int,
)
from utils.token_budget import fit_system_messages_within_budget, take_tail_messages_within_budget


ANALYSIS_SIGNAL_PATTERN = re.compile(
    r"(今天|昨天|前天|最近|本周|上周|本月|上月|今年|去年|季度|Q[1-4]|同比|环比|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}日|"
    r"明细|详情|图表|趋势|统计|分析|根因|时间线)"
)
FOLLOWUP_ANALYSIS_KEYWORDS = (
    "继续", "刚才", "上次", "上一轮", "沿用", "基于刚才", "按刚才",
    "继续上一个", "继续上一轮", "修复一个", "重试", "重新生成", "继续分析",
    "continue", "previous", "last result", "same logic",
)


class CustomContext(BaseModel):
    """Runtime context passed through the agent and tool layers."""

    username: str
    namespace_id: int = 0
    conversation_id: int = 0
    turn_id: int = 0
    auth_token: str = ''
    database_conn_info: dict[str, Any] = Field(default_factory=dict)


def classify_analysis_context_mode(user_message: str) -> str:
    text = (user_message or '').strip()
    if not text:
        return 'general'

    lower_text = text.lower()
    if any(keyword in text for keyword in FOLLOWUP_ANALYSIS_KEYWORDS) or any(
        keyword in lower_text for keyword in FOLLOWUP_ANALYSIS_KEYWORDS
    ):
        return 'followup'
    if ANALYSIS_SIGNAL_PATTERN.search(text):
        return 'fresh_analysis'
    return 'general'


def is_analysis_like_request(user_message: str) -> bool:
    return classify_analysis_context_mode(user_message) in ('followup', 'fresh_analysis')


def _build_success_fact_execution_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": item.get("execution_id"),
        "turn_id": item.get("turn_id"),
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "execution_status": item.get("execution_status", ""),
        "analysis_report_preview": item.get("analysis_report_preview", ""),
        "chart_count": item.get("chart_count", 0),
        "table_count": item.get("table_count", 0),
        "execution_seconds": item.get("execution_seconds"),
        "finished_at": item.get("finished_at"),
    }


def _prune_analysis_state_payload(payload: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == 'followup':
        return payload

    sanitized = dict(payload)
    recent_execution_summaries = payload.get("recent_execution_summaries") or []
    successful_summaries = [
        _build_success_fact_execution_summary(item)
        for item in recent_execution_summaries
        if isinstance(item, dict) and item.get("execution_status") == "success"
    ]

    if mode == 'fresh_analysis':
        sanitized["recent_execution_summaries"] = successful_summaries[-2:]
        latest_execution = payload.get("latest_execution") or {}
        if isinstance(latest_execution, dict) and latest_execution.get("execution_status") == "success":
            sanitized["latest_execution"] = _build_success_fact_execution_summary(latest_execution)
        else:
            sanitized["latest_execution"] = {}
        return sanitized

    sanitized["recent_execution_summaries"] = []
    sanitized["latest_execution"] = {}
    return sanitized


def _build_datasource_payload_item(datasource: InsightDatasource) -> dict[str, Any]:
    config_json = safe_json_loads(datasource.datasource_config_json, {})
    datasource_type = normalize_datasource_type(datasource.datasource_type)
    payload = {
        "datasource_id": datasource.id,
        "datasource_type": datasource_type,
        "datasource_name": datasource.datasource_name,
        "datasource_identifier": extract_datasource_identifier(datasource, config_json),
        "metadata_schema": extract_datasource_schema(datasource, config_json),
    }
    if datasource_type == "local_file":
        payload["recommended_loader"] = recommend_local_file_loader(
            config_json,
            Config.LOCAL_FILE_LOW_MEMORY_THRESHOLD_BYTES,
        )
    sheet_name = str(config_json.get("sheet_name") or "").strip()
    if sheet_name:
        payload["sheet_name"] = sheet_name
    return payload


def _build_execution_context_item(execution: InsightNsExecution, include_code: bool = False) -> dict[str, Any]:
    payload = {
        "execution_id": execution.id,
        "turn_id": execution.turn_id,
        "title": execution.title,
        "description": execution.description,
        "execution_status": execution.execution_status,
        "analysis_report": clip_text(
            execution.analysis_report or '',
            Config.CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS,
        ),
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


def _load_conversation(conversation_id: int) -> InsightNsConversation | None:
    if conversation_id <= 0:
        return None

    session = SessionLocal()
    try:
        return session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
    finally:
        session.close()


def _load_conversation_datasources(conversation_id: int) -> list[InsightDatasource]:
    if conversation_id <= 0:
        return []

    session = SessionLocal()
    try:
        return session.query(InsightDatasource).join(
            InsightNsRelDatasource,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightNsRelDatasource.sort_no.asc(),
            InsightNsRelDatasource.id.asc(),
        ).all()
    finally:
        session.close()


def get_datasource_message(
    namespace_id: int,
    conversation_id: int,
    snapshot_override: dict[str, Any] | None = None,
) -> SystemMessage | None:
    namespace_id_int = to_int(namespace_id, 0)
    conversation_id_int = to_int(conversation_id, 0)
    if conversation_id_int <= 0 and namespace_id_int <= 0:
        return None

    snapshot: dict[str, Any] = snapshot_override or {}
    if not snapshot:
        session = SessionLocal()
        try:
            snapshot = ConversationContextService(session).get_active_datasource_snapshot(conversation_id_int)
        finally:
            session.close()

    conversation = _load_conversation(conversation_id_int)
    if conversation is not None:
        namespace_id_int = conversation.insight_namespace_id
    elif namespace_id_int <= 0:
        namespace_id_int = to_int(snapshot.get('namespace_id'), 0)

    selected_ids = [
        item
        for item in snapshot.get('selected_datasource_ids', [])
        if item
    ]
    selected_items = [
        item
        for item in snapshot.get('selected_datasource_snapshot', [])
        if isinstance(item, dict) and item
    ]
    datasource_limit = max(int(getattr(Config, 'DATASOURCE_CONTEXT_MAX_COUNT', 10) or 0), 0)
    datasource_count = len(selected_ids) if selected_ids else len(selected_items)
    if datasource_limit > 0 and datasource_count > datasource_limit:
        policy = {
            "status": "too_many_datasources",
            "bound_datasource_count": datasource_count,
            "max_datasource_count": datasource_limit,
        }
        payload: dict[str, Any] = {
            "datasources": [],
            "selected_datasource_ids": [],
            "datasource_context_policy": policy,
        }
        if namespace_id_int > 0:
            payload["namespace_id"] = namespace_id_int
        return SystemMessage(
            "当前会话关联的数据源数量超过当前安全上下文上限。\n"
            "- 如果用户当前是在发起数据分析、统计、绘图、报表或依赖数据源内容的问题，不要调用 `execute_python`，不要生成 Python 代码。\n"
            "- 请直接用自然语言告诉用户：当前会话关联的数据源过多，需要先减少到更聚焦的范围后再进行分析。\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    snapshot_items = snapshot.get('selected_datasource_snapshot', [])
    if snapshot_items:
        datasource_items = snapshot_items
    else:
        datasource_items = [
            _build_datasource_payload_item(datasource)
            for datasource in _load_conversation_datasources(conversation_id_int)
        ]
    if not datasource_items:
        payload: dict[str, Any] = {"datasources": [], "selected_datasource_ids": []}
        if namespace_id_int > 0:
            payload["namespace_id"] = namespace_id_int
        return SystemMessage(
            "当前会话没有关联任何可直接使用的数据源。\n"
            "- 如果用户当前是在发起数据分析、统计、绘图、报表或依赖数据源内容的问题，不要调用 `execute_python`，不要生成 Python 代码。\n"
            "- 请直接用自然语言告诉用户：当前会话还没有关联数据源，需要先关联相关数据源后再进行分析。\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    payload: dict[str, Any] = {"datasources": datasource_items}
    selected_ids = [
        to_int(item, 0)
        for item in snapshot.get('selected_datasource_ids', [])
        if to_int(item, 0) > 0
    ]
    if selected_ids:
        payload["selected_datasource_ids"] = selected_ids
    if namespace_id_int > 0:
        payload["namespace_id"] = namespace_id_int

    instruction_lines = [
        "当前洞察空间可用的数据源信息：",
        "- `datasources` 是当前会话可直接使用的数据源全集。",
        "- 如果 `local_file` 数据源包含 `recommended_loader`，必须优先遵守该推荐加载函数。",
        "- `recommended_loader=load_local_file` 表示普通文件读取，返回完整 DataFrame；`recommended_loader=load_local_file_low_memory` 表示大文件分批读取，返回批次迭代器，必须用 `for chunk in ...` 逐批处理。",
    ]
    if selected_ids:
        instruction_lines.append(
            f"- `selected_datasource_ids` 当前为 {selected_ids}，表示这些数据源已经被当前会话选中，可直接用于本轮分析。"
        )
    else:
        instruction_lines.append(
            "- 当前没有显式选中的数据源；只有在 `selected_datasource_ids` 缺失或为空时，才需要判断是否向用户补充确认。"
        )

    if any(str(item.get("sheet_name") or "").strip() for item in datasource_items):
        instruction_lines.append(
            "- 如果某个 `local_file` 数据源带有 `sheet_name`，它只代表该 Excel 的一个工作表；调用 `load_local_file(...)` 时必须显式传入同名 `sheet_name`。"
        )
        instruction_lines.append(
            "- 同一个 Excel 文件可能拆成多个数据源，它们可以共享同一个 `datasource_identifier`，但会通过 `datasource_id`、`datasource_name` 和 `sheet_name` 区分不同工作表。"
        )

    return SystemMessage(
        "\n".join(instruction_lines) + "\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def get_history_messages(
    conversation_id: int,
    max_turn_no: int | None = None,
    user_message: str = '',
    token_budget: int | None = None,
) -> list[Any]:
    if not conversation_id:
        return []

    session = SessionLocal()
    try:
        service = ConversationContextService(session)
        rows = service.get_recent_prompt_messages(
            conversation_id,
            limit_messages=Config.CONTEXT_COMPRESSION_HISTORY_MESSAGE_LIMIT,
            max_turn_no=max_turn_no,
        )
    finally:
        session.close()

    context_mode = classify_analysis_context_mode(user_message)
    result: list[Any] = []
    last_human_text = ''
    for row in rows:
        text = (row.content or '').strip()
        if not text:
            continue
        if row.role == 'user':
            if context_mode == 'fresh_analysis' and text == last_human_text:
                continue
            last_human_text = text
            result.append(HumanMessage(text))
        elif row.role == 'assistant':
            if context_mode == 'fresh_analysis':
                continue
            result.append(AIMessage(text))

    return take_tail_messages_within_budget(result, token_budget)


def get_memory_messages(
    conversation_id: int,
    user_message: str = '',
    max_turn_no: int | None = None,
    active_snapshot_override: dict[str, Any] | None = None,
    token_budget: int | None = None,
) -> list[SystemMessage]:
    if not conversation_id:
        return []

    session = SessionLocal()
    try:
        service = ConversationContextService(session)
        messages: list[SystemMessage] = []
        context_mode = classify_analysis_context_mode(user_message)

        summary_text = service.build_runtime_summary_text(conversation_id, max_turn_no=max_turn_no)
        if summary_text:
            messages.append(SystemMessage(f"历史摘要：\n{summary_text}"))

        compression_metadata = service.get_context_compression_metadata(
            conversation_id,
            max_turn_no=max_turn_no,
        )
        if compression_metadata:
            messages.append(SystemMessage(
                "上下文压缩元数据：\n"
                f"{json.dumps(compression_metadata, ensure_ascii=False, indent=2)}"
            ))

        analysis_state_payload = service.build_runtime_analysis_state(
            conversation_id,
            max_turn_no=max_turn_no,
            active_snapshot_override=active_snapshot_override,
        )
        if analysis_state_payload:
            analysis_state_payload = _prune_analysis_state_payload(
                analysis_state_payload,
                mode=context_mode,
            )
            messages.append(SystemMessage(
                "当前分析状态：\n"
                f"{json.dumps(analysis_state_payload, ensure_ascii=False, indent=2)}"
            ))

        if context_mode == 'followup':
            recent_executions = service.get_recent_executions(
                conversation_id,
                limit_items=3,
                max_turn_no=max_turn_no,
                terminal_only=True,
            )
            if recent_executions:
                execution_payload = [
                    _build_execution_context_item(execution)
                    for execution in recent_executions
                ]
                messages.append(SystemMessage(
                    "最近代码执行记录：\n"
                    f"{json.dumps(execution_payload, ensure_ascii=False, indent=2)}"
                ))

                latest_execution = recent_executions[-1]
                if latest_execution.generated_code:
                    messages.append(SystemMessage(
                        "最近一次成功或最新执行的 Python 分析代码：\n"
                        f"{clip_text(latest_execution.generated_code, Config.CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS)}"
                    ))

            recent_artifacts = service.get_recent_artifacts(
                conversation_id,
                limit_items=3,
                max_turn_no=max_turn_no,
            )
            if recent_artifacts:
                artifact_payload = [
                    service._build_artifact_summary_item(artifact)
                    for artifact in recent_artifacts
                ]
                messages.append(SystemMessage(
                    "最近派生产物摘要：\n"
                    f"{json.dumps(artifact_payload, ensure_ascii=False, indent=2)}"
                ))

        return fit_system_messages_within_budget(messages, token_budget)
    finally:
        session.close()
