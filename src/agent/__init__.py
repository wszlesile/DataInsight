from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Generic, TypeVar
from zoneinfo import ZoneInfo
import re

from langchain.agents import AgentState, create_agent
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.context_engineering_runtime import (
    CustomContext,
    get_datasource_message,
    get_history_messages,
    get_memory_messages,
)
from agent.tools import execute_python
from config import Config
from utils.llm_model_factory import create_data_insight_model as _create_data_insight_model
from utils.token_budget import describe_budget_split, estimate_message_tokens, estimate_messages_tokens

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
    return _create_data_insight_model()


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


def _fit_optional_messages_within_budget(
    messages: list[BaseMessage],
    token_budget: int | None,
) -> list[BaseMessage]:
    """Strictly fit optional context; oversized optional items are dropped."""
    if token_budget is None:
        return list(messages)
    remaining = int(token_budget or 0)
    if remaining <= 0:
        return []

    selected: list[BaseMessage] = []
    used = 0
    for message in messages:
        cost = estimate_message_tokens(message)
        if used + cost > remaining:
            continue
        selected.append(message)
        used += cost
    return selected


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
    runtime_messages = [
        SystemMessage(extra_message)
        for extra_message in (extra_system_messages or [])
        if extra_message
    ]
    # Keep these sections mandatory. MiniMax still receives one merged
    # SystemMessage, but budget trimming must not drop system rules,
    # current datasource context, runtime repair hints, or the user tail.
    mandatory_system_messages: list[BaseMessage] = [
        SystemMessage(load_system_prompt()),
        _build_runtime_environment_message(),
    ]
    relative_date_hint = _build_relative_date_hint_message(user_message)
    if relative_date_hint is not None:
        mandatory_system_messages.append(relative_date_hint)
    if datasource_message is not None:
        mandatory_system_messages.append(datasource_message)
    mandatory_system_messages.extend(runtime_messages)

    mandatory_system_content = _merge_system_messages(mandatory_system_messages)
    mandatory_system_message = SystemMessage(mandatory_system_content) if mandatory_system_content else None
    user_tail_message = HumanMessage(user_message)
    mandatory_messages: list[BaseMessage] = []
    if mandatory_system_message is not None:
        mandatory_messages.append(mandatory_system_message)
    mandatory_messages.append(user_tail_message)

    budget_split = describe_budget_split(
        max_prompt_tokens=Config.CONTEXT_COMPRESSION_PROMPT_MAX_TOKENS,
        fixed_tokens=estimate_messages_tokens(mandatory_messages),
        history_ratio=Config.CONTEXT_COMPRESSION_HISTORY_TOKEN_RATIO,
        min_history_tokens=Config.CONTEXT_COMPRESSION_MIN_HISTORY_TOKENS,
        min_memory_tokens=Config.CONTEXT_COMPRESSION_MIN_MEMORY_TOKENS,
    )

    memory_messages = (
        get_memory_messages(
            conversation_id,
            user_message=user_message,
            max_turn_no=history_turn_limit,
            active_snapshot_override=datasource_snapshot_override,
            token_budget=budget_split["memory_budget"],
        )
        if budget_split["memory_budget"] > 0
        else []
    )
    memory_messages = _fit_optional_messages_within_budget(
        memory_messages,
        budget_split["memory_budget"],
    )

    merged_system_content = _merge_system_messages(
        mandatory_system_message,
        memory_messages,
    )

    messages: list[BaseMessage] = []
    if merged_system_content:
        messages.append(SystemMessage(merged_system_content))

    if budget_split["history_budget"] > 0:
        history_messages = get_history_messages(
            conversation_id,
            max_turn_no=history_turn_limit,
            user_message=user_message,
            token_budget=budget_split["history_budget"],
        )
        messages.extend(_fit_optional_messages_within_budget(
            history_messages,
            budget_split["history_budget"],
        ))
    messages.append(user_tail_message)
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


EXPLICIT_YEAR_PATTERN = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
MONTH_TOKEN_PATTERN = re.compile(r'(?<!\d)(1[0-2]|0?[1-9])月(?:份)?')


def _has_explicit_year(text: str) -> bool:
    return bool(EXPLICIT_YEAR_PATTERN.search(text or ''))


def _group_months_by_anchor_year(months: list[int], now: datetime) -> dict[int, list[int]]:
    year_to_months: dict[int, list[int]] = {}
    for month in months:
        anchor_year = now.year if month <= now.month else now.year - 1
        bucket = year_to_months.setdefault(anchor_year, [])
        if month not in bucket:
            bucket.append(month)
    for bucket in year_to_months.values():
        bucket.sort()
    return year_to_months


def _build_relative_date_hint_message(user_message: str) -> SystemMessage | None:
    """
    把用户问题里的相对日期和裸月份锚定成绝对时间提示。

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

    month_values = [int(month) for month in MONTH_TOKEN_PATTERN.findall(text)]
    if month_values:
        if '今年' in text or '去年' in text:
            anchor_year = now.year if '今年' in text else now.year - 1
            normalized_months = sorted({month for month in month_values})
            hints.append(
                f"- 本轮提到的月份应优先解释为 {anchor_year} 年的这些月份：{', '.join(str(month) for month in normalized_months)} 月。"
            )
        elif not _has_explicit_year(text):
            year_to_months = _group_months_by_anchor_year(month_values, now)
            for anchor_year in sorted(year_to_months):
                months_text = ', '.join(str(month) for month in year_to_months[anchor_year])
                hints.append(
                    f"- 本轮未显式写年份的月份，应优先按离当前日期最近的口径解释为 {anchor_year} 年的这些月份：{months_text} 月。"
                )

    if not hints:
        return None
    return SystemMessage("本轮问题的相对日期解释：\n" + "\n".join(hints))
