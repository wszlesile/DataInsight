from dataclasses import dataclass
from typing import TypeVar, Generic, Any, List

from langchain.agents import create_agent, AgentState
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen
from pydantic import BaseModel, Field

from agent.context_engineering import get_history_message, CustomContext, get_datasource_messages, \
    get_system_config_messages
from agent.tools import execute_python, save_analysis_result, StructuredResult
from config import Config

"""自定义状态"""


class CustomAgentState(AgentState):
    username: str


def create_data_insight_model():
    if 'MiniMax-M2.5' == Config.LLM_MODEL_ACTIVE:
        """创建基于 MiniMax 的 LangChain LLM Model"""
        # 使用 OpenAI 兼容接口连接 MiniMax
        insight_model = ChatOpenAI(
            model=Config.MINIMAX_M2_5_MODEL,
            api_key=Config.MINIMAX_M2_5_API_KEY,
            base_url=Config.MINIMAX_M2_5_BASE_URL,
            # other params...
        )
        return insight_model
    else:
        """创建基于 Qwen 的 LangChain LLM Model"""
        insight_model = ChatQwen(
            model=Config.QWEN3_80B_MODEL,
            api_key=Config.QWEN3_80B_API_KEY,
            base_url=Config.QWEN3_80B_BASE_URL,
            # other params...
        )
        return insight_model


def create_data_insight_agent():
    """创建 LLM Model"""
    insight_model = create_data_insight_model()

    # 创建简单的 agent
    agent = create_agent(
        model=insight_model,
        state_schema=CustomAgentState,
        context_schema=CustomContext,
        tools=[execute_python, save_analysis_result],
        response_format=StructuredResult
    )
    return agent


# 创建全局 agent 实例
insight_agent = create_data_insight_agent()

MessageT = TypeVar("MessageT", bound=BaseMessage, covariant=True)


class InputMessage(Generic[MessageT]):
    messages: List[MessageT] = Field("消息列表")


def get_input(message: str) -> Any:
    # 从 sys_prompt.md 读取系统提示
    import os
    sys_prompt_path = os.path.join(os.path.dirname(__file__), '..','..', 'sys_prompt.md')
    with open(sys_prompt_path, 'r', encoding='utf-8') as f:
        system_prompt_content = f.read().strip()

    system_message = SystemMessage(system_prompt_content)
    system_config_message = get_system_config_messages()
    # 获取数据源上下文
    datasource_message = get_datasource_messages(message)
    history_message = get_history_message()
    messages = [system_message,system_config_message,datasource_message, history_message, HumanMessage(message)]
    input_message = InputMessage()
    input_message.messages = messages
    return input_message
