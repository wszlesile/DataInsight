from functools import lru_cache
from pathlib import Path
from typing import Any, Generic, TypeVar

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
from agent.tools import StructuredResult, execute_python
from config import Config

MessageT = TypeVar("MessageT", bound=BaseMessage, covariant=True)
SYS_PROMPT_PATH = Path(__file__).resolve().parents[2] / 'sys_prompt.md'


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
        response_format=StructuredResult,
    )


insight_agent = create_data_insight_agent()


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
    messages: list[BaseMessage] = [
        SystemMessage(load_system_prompt()),
    ]

    # 先注入数据源上下文，再注入历史消息，
    # 这样模型会先知道当前可分析的数据范围。
    datasource_message = get_datasource_message(
        namespace_id=namespace_id,
        conversation_id=conversation_id,
        snapshot_override=datasource_snapshot_override,
    )
    if datasource_message is not None:
        messages.append(datasource_message)

    # 记忆消息是从执行记录、派生产物和历史消息中提炼出的压缩上下文，
    # 因此应该放在原始历史消息之前。
    messages.extend(get_memory_messages(
        conversation_id,
        user_message=user_message,
        max_turn_no=history_turn_limit,
        active_snapshot_override=datasource_snapshot_override,
    ))
    messages.extend(get_history_messages(conversation_id, max_turn_no=history_turn_limit))
    for extra_message in extra_system_messages or []:
        if extra_message:
            messages.append(SystemMessage(extra_message))
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
