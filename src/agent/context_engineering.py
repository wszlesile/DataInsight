import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config.database import SessionLocal
from model import InsightDatasource, InsightNsConversation, InsightNsExecution, InsightNsRelDatasource
from service.conversation_context_service import ConversationContextService
from utils.datasource_utils import (
    extract_datasource_identifier,
    extract_datasource_schema,
    normalize_datasource_type,
    safe_json_loads,
    to_int,
)


ANALYSIS_SIGNAL_PATTERN = re.compile(
    r"(今天|昨天|前天|最近|本周|上周|本月|上月|今年|去年|季度|Q[1-4]|同比|环比|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}号|"
    r"明细|详情|图表|趋势|统计|分析|根因|时间线)"
)
FOLLOWUP_ANALYSIS_KEYWORDS = (
    "继续", "刚才", "上次", "上一轮", "沿用", "基于刚才", "按刚才",
    "继续上一个", "继续上一轮", "修复一下", "重试", "重新生成", "继续分析",
    "continue", "previous", "last result", "same logic",
)


class CustomContext(BaseModel):
    """在 Agent 编排与工具执行链路中传递的运行时上下文。"""

    username: str
    namespace_id: int = 0
    conversation_id: int = 0
    turn_id: int = 0
    auth_token: str = ''
    database_conn_info: dict[str, Any] = Field(default_factory=dict)


def _build_datasource_payload_item(datasource: InsightDatasource) -> dict[str, Any]:
    """把数据源实体转换成 Prompt 约定的 JSON 结构。"""
    config_json = safe_json_loads(datasource.datasource_config_json, {})
    return {
        "datasource_id": datasource.id,
        "datasource_type": normalize_datasource_type(datasource.datasource_type),
        "datasource_name": datasource.datasource_name,
        "datasource_identifier": extract_datasource_identifier(datasource, config_json),
        "metadata_schema": extract_datasource_schema(datasource, config_json),
    }


def _build_execution_context_item(execution: InsightNsExecution, include_code: bool = False) -> dict[str, Any]:
    """构造一条适合写入记忆消息的执行摘要。"""
    payload = {
        "execution_id": execution.id,
        "turn_id": execution.turn_id,
        "title": execution.title,
        "description": execution.description,
        "execution_status": execution.execution_status,
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


def classify_analysis_context_mode(user_message: str) -> str:
    """区分当前请求更像“续跑分析”“新的分析任务”还是普通问答。"""
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
    """判断当前输入是否明显属于数据分析请求。"""
    return classify_analysis_context_mode(user_message) in ('followup', 'fresh_analysis')


def _should_inject_execution_context(user_message: str) -> bool:
    """只有承接上一轮分析的请求，才注入完整执行记录与代码。"""
    return classify_analysis_context_mode(user_message) == 'followup'


def _build_success_fact_execution_summary(item: dict[str, Any]) -> dict[str, Any]:
    """提炼成功执行的稳定事实，避免把失败细节和旧代码强塞给新分析。"""
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
    """
    按请求类型裁剪分析状态。

    - 新分析：保留成功执行的稳定事实，不注入失败信息和旧代码。
    - 普通问答：不强调执行上下文，避免无关分析历史干扰回答。
    - 续跑分析：保留原始结构，允许后续逻辑读取失败线索。
    """
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


def _load_conversation(conversation_id: int) -> InsightNsConversation | None:
    """按会话 ID 加载一条未删除的会话记录。"""
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
    """加载当前会话绑定的全部有效数据源。"""
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
    """
    注入 `sys_prompt.md` 约定的数据源上下文。

    前端不会直接传入完整数据源定义，
    运行时数据源信息以后端数据库中的会话绑定关系为准。
    """
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

    snapshot_items = snapshot.get('selected_datasource_snapshot', [])
    if snapshot_items:
        datasource_items = snapshot_items
    else:
        datasource_items = [
            _build_datasource_payload_item(datasource)
            for datasource in _load_conversation_datasources(conversation_id_int)
        ]
    if not datasource_items:
        return None

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
    ]
    if selected_ids:
        instruction_lines.append(
            f"- `selected_datasource_ids` 当前为 {selected_ids}，表示这些数据源已经被当前会话选中，可直接用于本轮分析，不需要再次向用户确认数据源。"
        )
    else:
        instruction_lines.append(
            "- 当前没有显式选中的数据源；只有在 `selected_datasource_ids` 缺失或为空时，才需要判断是否向用户补充确认。"
        )

    return SystemMessage(
        "\n".join(instruction_lines) + "\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def get_history_messages(
    conversation_id: int,
    max_turn_no: int | None = None,
    user_message: str = '',
) -> list[Any]:
    """加载最近的用户问题和最终回答消息，用于历史上下文重放。"""
    if not conversation_id:
        return []

    session = SessionLocal()
    try:
        service = ConversationContextService(session)
        messages = service.get_recent_prompt_messages(
            conversation_id,
            limit_messages=10,
            max_turn_no=max_turn_no,
        )
    finally:
        session.close()

    context_mode = classify_analysis_context_mode(user_message)
    result: list[Any] = []
    last_human_text = ''
    for item in messages:
        text = (item.content or '').strip()
        if not text:
            continue
        # 原始历史重放只带用户问题和助手最终回答。
        # 工具与执行细节会通过记忆消息单独注入。
        if item.role == 'user':
            if context_mode == 'fresh_analysis' and text == last_human_text:
                continue
            last_human_text = text
            result.append(HumanMessage(text))
        elif item.role == 'assistant':
            if context_mode == 'fresh_analysis':
                continue
            result.append(AIMessage(text))
    return result


def get_memory_messages(
    conversation_id: int,
    user_message: str = '',
    max_turn_no: int | None = None,
    active_snapshot_override: dict[str, Any] | None = None,
) -> list[SystemMessage]:
    """
    构造下一轮分析使用的压缩记忆消息。

    这里负责把数据库中的上下文工程结果重新翻译成可直接注入 Prompt 的系统消息。
    """
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

        # 执行代码和派生产物属于更强的历史承接信息。
        # 只有明显“继续上一轮”的追问，才把它们完整注入；
        # 如果当前问题已经是新的分析请求，则避免旧执行逻辑把模型带偏。
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
                        f"{latest_execution.generated_code}"
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

        return messages
    finally:
        session.close()


def _build_datasource_payload_item(datasource: InsightDatasource) -> dict[str, Any]:
    """把数据源实体转换成 Prompt 约定的 JSON 结构。"""
    config_json = safe_json_loads(datasource.datasource_config_json, {})
    payload = {
        "datasource_id": datasource.id,
        "datasource_type": normalize_datasource_type(datasource.datasource_type),
        "datasource_name": datasource.datasource_name,
        "datasource_identifier": extract_datasource_identifier(datasource, config_json),
        "metadata_schema": extract_datasource_schema(datasource, config_json),
    }
    sheet_name = str(config_json.get("sheet_name") or "").strip()
    if sheet_name:
        payload["sheet_name"] = sheet_name
    return payload


def get_datasource_message(
    namespace_id: int,
    conversation_id: int,
    snapshot_override: dict[str, Any] | None = None,
) -> SystemMessage | None:
    """注入运行时数据源上下文，并明确 Excel 工作表数据源的读取规则。"""
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

    snapshot_items = snapshot.get('selected_datasource_snapshot', [])
    if snapshot_items:
        datasource_items = snapshot_items
    else:
        datasource_items = [
            _build_datasource_payload_item(datasource)
            for datasource in _load_conversation_datasources(conversation_id_int)
        ]
    if not datasource_items:
        return None

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
    ]
    if selected_ids:
        instruction_lines.append(
            f"- `selected_datasource_ids` 当前为 {selected_ids}，表示这些数据源已经被当前会话选中，可直接用于本轮分析，不需要再次向用户确认数据源。"
        )
    else:
        instruction_lines.append(
            "- 当前没有显式选中的数据源；只有在 `selected_datasource_ids` 缺失或为空时，才需要判断是否向用户补充确认。"
        )

    if any(str(item.get("sheet_name") or "").strip() for item in datasource_items):
        instruction_lines.append(
            "- 若某个 `local_file` 数据源带有 `sheet_name`，它只代表该 Excel 的一个工作表；调用 `load_local_file(...)` 时必须显式传入同名 `sheet_name`，不要默认读取第一张工作表。"
        )
        instruction_lines.append(
            "- 同一个 Excel 文件可能拆成多个数据源，它们可以共享同一个文件路径 `datasource_identifier`，但会通过 `datasource_id`、`datasource_name` 和 `sheet_name` 区分不同工作表。"
        )

    return SystemMessage(
        "\n".join(instruction_lines) + "\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
