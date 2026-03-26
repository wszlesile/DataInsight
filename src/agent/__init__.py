from dataclasses import dataclass
from typing import TypeVar, Generic, Any, List

from langchain.agents import create_agent, AgentState
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen
from pydantic import BaseModel, Field

from agent.context_engineering import get_history_message, CustomContext
from agent.tools import execute_python, get_file_temp_save_path, save_insight_result, StructuredResult
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
        tools=[execute_python, get_file_temp_save_path, save_insight_result],
        response_format=StructuredResult
    )
    return agent


# 创建全局 agent 实例
insight_agent = create_data_insight_agent()

MessageT = TypeVar("MessageT", bound=BaseMessage, covariant=True)


class InputMessage(Generic[MessageT]):
    messages: List[MessageT] = Field("消息列表")


def get_input(message: str) -> Any:
    system_message = SystemMessage(
        "你是一个数据洞察的专家;你的能力是根据用户的输入的对话，以及提供的文件分析软件：pandas，报表分析软件：pyecharts，和数据加载工具，执行python代码工具，为用户生成一段数据分析报表的python执行代码，以及对应报表的描述;要求生成的python执行代码保存生成的报表文件以及描述")
    history_message = get_history_message()
    messages = [system_message, history_message, HumanMessage(message)]
    input_message = InputMessage()
    input_message.messages = messages
    return input_message
