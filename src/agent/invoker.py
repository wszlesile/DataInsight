from dataclasses import dataclass
from typing import Any

from agent import get_input, insight_agent, CustomContext
from utils import logger


@dataclass
class AgentRequest:
    username: str
    namespace_id: str
    conversation_id: str
    user_message: str


@dataclass
class AgentResponse:
    username: str
    message: str  # 对话消息
    file_id: str = ''  # 图表文件路径
    analysis_report: str = ''  # 分析报告


def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    #获取执行上下文消息
    input_message = get_input(agent_request.user_message)
    #执行agent请求
    agent_invoke_response = insight_agent.invoke(input_message, context=CustomContext(username=agent_request.username))
    #解析返回
    messages = agent_invoke_response['messages']
    # 获取最后一条AI消息的内容
    ai_message = messages[-1] if messages else ''
    message_content = ai_message.content if hasattr(ai_message, 'content') else str(ai_message)
    structured_message = messages[-2] if messages else '{}'
    structured_content = structured_message.content if hasattr(ai_message, 'content') else str(ai_message)

    # 从消息中解析 StructuredResult（如果有）
    file_id = ''
    analysis_report = structured_content

    # 检查是否有结构化结果（file_id 和 analysis_report）
    import json
    try:
        if isinstance(structured_content, str) and structured_content.startswith('{'):
            result_data = json.loads(structured_content)
            file_id = result_data.get('file_id', '')
            analysis_report = result_data.get('analysis_report', structured_content)
    except Exception as e:
        logger.info(f"[DEBUG] JSON parse error: {e}")

    return AgentResponse(
        username=agent_request.username,
        message=message_content,
        file_id=file_id,
        analysis_report=analysis_report
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
    response = invoke_agent(AgentRequest("John Smith", '', '', "今天一共有多少个报警？看一下明细"))
    print(response)
