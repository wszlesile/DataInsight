from dataclasses import dataclass
from typing import Any

from agent import get_input, insight_agent, CustomContext


@dataclass
class AgentRequest:
    username: str
    namespace_id: str
    conversation_id: str
    user_message: str


@dataclass
class AgentResponse:
    username: str
    message: str


def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    input_message = get_input(agent_request.user_message)
    agent_invoke_response = insight_agent.invoke(input_message, context=CustomContext(username=agent_request.username))
    return AgentResponse(
        username=agent_request.username,
        message=agent_invoke_response['messages']
    )


def stream_invoke_agent(agent_request: AgentRequest) -> Any:
    input_message = get_input(agent_request.user_message)
    # 调用
    for stream_mode, chunk in insight_agent.stream(
            input_message, context=CustomContext(username=agent_request.username),
            stream_mode=["updates", "custom"]
    ):
        print(f"stream_mode: {stream_mode}")
        print(f"content: {chunk}")
        print("\n")


if __name__ == '__main__':
    response = invoke_agent(AgentRequest("John Smith", '', '', "分析2024年Q4季度的销售趋势"))
    print(response)
