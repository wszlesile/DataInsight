from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Generic, TypeVar
from zoneinfo import ZoneInfo
import re

from langchain.agents import AgentState, create_agent
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen
from pydantic import BaseModel, Field

from agent.context_engineering import (
    CustomContext,
    get_datasource_message,
    get_history_messages,
    get_memory_messages,
)
from agent.tools import execute_python
from config import Config

MessageT = TypeVar("MessageT", bound=BaseMessage, covariant=True)
SYS_PROMPT_PATH = Path(__file__).resolve().with_name('sys_prompt.md')


class CustomAgentState(AgentState):
    """
    在 LangGraph 默认状态结构上补充当前用户名。

    当前运行时主要依赖 `CustomContext`，但把用户名保留在状态结构里，
    能让后续 Agent 图编排扩展更自然。
    """

    username: str


class InputMessage(BaseModel, Generic[MessageT]):
    """
    与 Agent 输入契约对齐的包装模型。

    当前 LangGraph Agent 期望顶层载荷中包含 `messages` 字段，
    所以这里保留一个轻量兼容包装层。
    """

    messages: list[MessageT] = Field(default_factory=list, description="传入 Agent 的消息列表")


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """从磁盘加载系统提示词，并做进程级缓存。"""
    # 系统提示词由独立文档维护，这里只负责读取与缓存。
    return SYS_PROMPT_PATH.read_text(encoding='utf-8').strip()


def create_data_insight_model():
    """创建当前配置的聊天模型，同时保持现有模型提供方契约不变。"""
    if Config.LLM_MODEL_ACTIVE == 'MiniMax':
        return ChatOpenAI(
            model=Config.MINIMAX_M2_5_MODEL,
            api_key=Config.MINIMAX_M2_5_API_KEY,
            base_url=Config.MINIMAX_M2_5_BASE_URL,
            temperature=0.7,
        )

    return ChatQwen(
        model=Config.QWEN3_80B_MODEL,
        api_key=Config.QWEN3_80B_API_KEY,
        base_url=Config.QWEN3_80B_BASE_URL,
    )


def create_data_insight_agent():
    """创建数据洞察 Agent，并保持现有工具契约不变。"""
    return create_agent(
        model=create_data_insight_model(),
        state_schema=CustomAgentState,
        context_schema=CustomContext,
        tools=[execute_python],
    )


insight_agent = create_data_insight_agent()


def _merge_system_messages(*parts: BaseMessage | list[BaseMessage] | None) -> str:
    """Merge all system-level context into a single system prompt string."""
    merged_parts: list[str] = []

    def append_message(message: BaseMessage | None) -> None:
        if message is None or not isinstance(message, SystemMessage):
            return
        content = (message.content or '').strip()
        if content:
            merged_parts.append(content)

    for part in parts:
        if part is None:
            continue
        if isinstance(part, list):
            for item in part:
                append_message(item)
        else:
            append_message(part)

    return '\n\n'.join(item for item in merged_parts if item)


def _build_runtime_environment_message() -> SystemMessage:
    """Provide stable temporal context for relative-date analysis requests."""
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    return SystemMessage(
        "运行时环境信息：\n"
        f"- 当前日期：{now:%Y-%m-%d}\n"
        f"- 当前时间：{now:%Y-%m-%d %H:%M:%S} Asia/Shanghai\n"
        "- 用户提到“今天 / 昨天 / 前天 / 本月 / 上月 / 今年 / 去年”等相对日期时，必须以上述当前日期和 Asia/Shanghai 业务时区为准。"
    )


def _build_relative_date_hint_message(user_message: str) -> SystemMessage | None:
    """
    把用户问题里的相对日期表达锚定成绝对时间提示。

    这里不替模型直接写 SQL，只做稳定的日期解释，避免它随手猜成 2024/2025。
    """
    text = (user_message or '').strip()
    if not text:
        return None

    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    hints: list[str] = []
    if '今年' in text:
        hints.append(f"- 本轮问题中的“今年”应解释为 {now.year} 年。")
    if '去年' in text:
        hints.append(f"- 本轮问题中的“去年”应解释为 {now.year - 1} 年。")
    if '本月' in text:
        hints.append(f"- 本轮问题中的“本月”应解释为 {now.year} 年 {now.month} 月。")
    if '上月' in text:
        last_month_year = now.year if now.month > 1 else now.year - 1
        last_month = now.month - 1 if now.month > 1 else 12
        hints.append(f"- 本轮问题中的“上月”应解释为 {last_month_year} 年 {last_month} 月。")
    if '今天' in text:
        hints.append(f"- 本轮问题中的“今天”应解释为 {now:%Y-%m-%d}。")
    if '昨天' in text:
        hints.append(f"- 本轮问题中的“昨天”应解释为 {(now.date() - timedelta(days=1)).isoformat()}。")
    if '前天' in text:
        hints.append(f"- 本轮问题中的“前天”应解释为 {(now.date() - timedelta(days=2)).isoformat()}。")

    month_matches = re.findall(r'(\d{1,2})月', text)
    if month_matches and ('今年' in text or '去年' in text):
        anchor_year = now.year if '今年' in text else now.year - 1
        normalized_months = [str(int(month)) for month in month_matches]
        hints.append(
            f"- 本轮提到的月份应优先解释为 {anchor_year} 年的这些月份：{', '.join(normalized_months)} 月。"
        )

    if not hints:
        return None
    return SystemMessage("本轮问题的相对日期解释：\n" + "\n".join(hints))


def build_prompt_messages(
    user_message: str,
    namespace_id: int = 0,
    conversation_id: int = 0,
    extra_system_messages: list[str] | None = None,
    history_turn_limit: int | None = None,
    datasource_snapshot_override: dict[str, Any] | None = None,
) -> list[BaseMessage]:
    """
    为单次分析请求组装完整 Prompt 上下文。

    组装顺序是刻意设计的：
    1. 系统提示词与运行时配置
    2. 数据源上下文
    3. 记忆与历史上下文
    4. 当前用户问题
    """
    datasource_message = get_datasource_message(
        namespace_id=namespace_id,
        conversation_id=conversation_id,
        snapshot_override=datasource_snapshot_override,
    )
    memory_messages = get_memory_messages(
        conversation_id,
        user_message=user_message,
        max_turn_no=history_turn_limit,
        active_snapshot_override=datasource_snapshot_override,
    )
    runtime_messages = [
        SystemMessage(extra_message)
        for extra_message in (extra_system_messages or [])
        if extra_message
    ]

    merged_system_content = _merge_system_messages(
        SystemMessage(load_system_prompt()),
        _build_runtime_environment_message(),
        _build_relative_date_hint_message(user_message),
        datasource_message,
        memory_messages,
        runtime_messages,
    )

    messages: list[BaseMessage] = []
    if merged_system_content:
        messages.append(SystemMessage(merged_system_content))

    messages.extend(get_history_messages(
        conversation_id,
        max_turn_no=history_turn_limit,
        user_message=user_message,
    ))
    messages.append(HumanMessage(user_message))
    return messages


def get_input(
    message: str,
    namespace_id: int = 0,
    conversation_id: int = 0,
    extra_system_messages: list[str] | None = None,
    history_turn_limit: int | None = None,
    datasource_snapshot_override: dict[str, Any] | None = None,
) -> Any:
    """构建供 invoke/stream 调用使用的 Agent 输入载荷。"""
    return InputMessage(
        messages=build_prompt_messages(
            user_message=message,
            namespace_id=namespace_id,
            conversation_id=conversation_id,
            extra_system_messages=extra_system_messages,
            history_turn_limit=history_turn_limit,
            datasource_snapshot_override=datasource_snapshot_override,
        )
    )
