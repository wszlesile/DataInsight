from dataclasses import dataclass
from typing import TypeVar, Generic, Any, List

from langchain.agents import create_agent, AgentState
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen
from pydantic import BaseModel, Field

from agent.context_engineering import get_history_message
from agent.tools import execute_python, get_file_temp_save_path, save_tile_description
from config import Config

"""结构化输出"""
class Result(BaseModel):
    """洞察结果"""
    fileId: str = Field(description="报表文件路径或文件服务的文件ID")
    description: str = Field(description="报表内容描述")
"""自定义状态"""
class CustomAgentState(AgentState):
    user_id: str


"""自定义上下文"""
class CustomContext(BaseModel):
    user_id: str


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
        model = insight_model,
        state_schema = CustomAgentState,
        context_schema = CustomContext,
        tools=[execute_python,get_file_temp_save_path,save_tile_description],
        response_format=Result
    )
    return agent


# 创建全局 agent 实例
insight_agent = create_data_insight_agent()


@dataclass
class AgentRequest:
    username: str
    user_message: str


@dataclass
class AgentResponse:
    username: str
    message: str

MessageT = TypeVar("MessageT", bound=BaseMessage,covariant=True)

class InputMessage(Generic[MessageT]):
    messages: List[MessageT] = Field("消息列表")

def get_input(message: str) -> Any:
    system_message= SystemMessage("你是一个数据洞察的专家;你的能力是根据用户的输入的对话，以及提供的文件分析软件：pandas，报表分析软件：pyecharts，和数据加载工具，执行python代码工具，为用户生成一段数据分析报表的python执行代码，以及对应报表的描述;要求生成的python执行代码保存生成的报表文件以及描述")
    history_message = get_history_message()
    messages = [system_message,history_message,HumanMessage(message)]
    input_message = InputMessage()
    input_message.messages = messages
    return input_message

def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    """调用 Agent 处理请求"""
    input_message = get_input(agent_request.user_message)
    agent_invoke_response = insight_agent.invoke(input_message)
    return AgentResponse(
        username=agent_request.username,
        message=agent_invoke_response['messages']
    )


if __name__ == '__main__':
    response = invoke_agent(AgentRequest("1", "给我一个简单的例子"))
    print(response)
