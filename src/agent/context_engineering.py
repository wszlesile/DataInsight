import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

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


NEW_ANALYSIS_SIGNAL_PATTERN = re.compile(
    r"(今天|昨天|前天|近\d+天|最近|本周|上周|本月|上月|Q[1-4]|季度|今年|去年|同比|环比|"
    r"\d{1,2}月\d{1,2}号|\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|明细|详情|图表|趋势|统计|分析)"
)


class CustomContext(BaseModel):
    """在 Agent 编排与工具执行链路中传递的运行时上下文。"""

    username: str
    namespace_id: int = 0
    conversation_id: int = 0
    turn_id: int = 0


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


def _should_inject_execution_context(user_message: str) -> bool:
    """
    判断当前问题是否应该注入最近执行代码与执行产物。

    只有明显承接上一轮逻辑的追问，才应该把最近执行代码完整带回给模型。
    如果用户当前问题出现了新的日期、过滤条件、统计口径、明细或图表要求，
    更适合按一轮新的分析任务处理，此时只保留摘要记忆，不注入旧执行代码。
    """
    text = (user_message or '').strip()
    if not text:
        return True

    lower_text = text.lower()
    carry_on_keywords = (
        "继续", "刚才", "上次", "上一轮", "沿用", "基于刚才", "按刚才",
        "continue", "previous", "last result", "same logic",
    )
    if any(keyword in text for keyword in carry_on_keywords) or any(keyword in lower_text for keyword in carry_on_keywords):
        return True

    if NEW_ANALYSIS_SIGNAL_PATTERN.search(text):
        return False

    return True


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


def get_history_messages(conversation_id: int, max_turn_no: int | None = None) -> list[Any]:
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

    result: list[Any] = []
    for item in messages:
        text = (item.content or '').strip()
        if not text:
            continue
        # 原始历史重放只带用户问题和助手最终回答。
        # 工具与执行细节会通过记忆消息单独注入。
        if item.role == 'user':
            result.append(HumanMessage(text))
        elif item.role == 'assistant':
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

        summary_text = service.build_runtime_summary_text(conversation_id, max_turn_no=max_turn_no)
        if summary_text:
            messages.append(SystemMessage(f"历史摘要：\n{summary_text}"))

        analysis_state_payload = service.build_runtime_analysis_state(
            conversation_id,
            max_turn_no=max_turn_no,
            active_snapshot_override=active_snapshot_override,
        )
        if analysis_state_payload:
            messages.append(SystemMessage(
                "当前分析状态：\n"
                f"{json.dumps(analysis_state_payload, ensure_ascii=False, indent=2)}"
            ))

        # 执行代码和派生产物属于更强的历史承接信息。
        # 只有明显“继续上一轮”的追问，才把它们完整注入；
        # 如果当前问题已经是新的分析请求，则避免旧执行逻辑把模型带偏。
        if _should_inject_execution_context(user_message):
            recent_executions = service.get_recent_executions(
                conversation_id,
                limit_items=3,
                max_turn_no=max_turn_no,
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
                artifact_payload = [artifact.to_dict() for artifact in recent_artifacts]
                messages.append(SystemMessage(
                    "最近派生产物摘要：\n"
                    f"{json.dumps(artifact_payload, ensure_ascii=False, indent=2)}"
                ))

        return messages
    finally:
        session.close()
