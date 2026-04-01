from dataclasses import dataclass
import json
import re
from typing import Any, Iterator

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
    message: str
    file_id: str = ''
    analysis_report: str = ''


def _clean_message_content(content: Any) -> str:
    text = content if isinstance(content, str) else str(content)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def _parse_structured_content(content: Any) -> tuple[str, str]:
    text = content if isinstance(content, str) else str(content)
    file_id = ''
    analysis_report = ''
    try:
        if text.startswith('{'):
            result_data = json.loads(text)
            file_id = result_data.get('file_id', '')
            analysis_report = result_data.get('analysis_report', '')
    except Exception as exc:
        logger.info(f"[DEBUG] JSON parse error: {exc}")
    return file_id, analysis_report


def _build_progress_event(event_type: str, **payload: Any) -> dict[str, Any]:
    event = {'type': event_type}
    event.update(payload)
    return event


def _format_tool_call_message(tool_call: dict[str, Any]) -> str:
    tool_name = tool_call.get('name', 'unknown_tool')
    args = tool_call.get('args', {}) or {}
    if tool_name == 'execute_python':
        title = args.get('title') or '数据分析任务'
        return f"已生成分析代码，准备执行：{title}"
    return f"准备调用工具：{tool_name}"


def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    input_message = get_input(agent_request.user_message)
    agent_invoke_response = insight_agent.invoke(
        input_message,
        context=CustomContext(username=agent_request.username)
    )

    messages = agent_invoke_response['messages']
    ai_message = messages[-1] if messages else ''
    message_content = _clean_message_content(
        ai_message.content if hasattr(ai_message, 'content') else str(ai_message)
    )

    structured_message = messages[-2] if messages else '{}'
    structured_content = (
        structured_message.content if hasattr(structured_message, 'content') else str(structured_message)
    )
    file_id, analysis_report = _parse_structured_content(structured_content)

    return AgentResponse(
        username=agent_request.username,
        message=message_content,
        file_id=file_id,
        analysis_report=analysis_report
    )


def stream_invoke_agent(agent_request: AgentRequest) -> Iterator[dict[str, Any]]:
    input_message = get_input(agent_request.user_message)
    yield _build_progress_event(
        'status',
        stage='start',
        level='info',
        message='已收到请求，正在理解分析需求'
    )

    result_emitted = False
    for stream_mode, chunk in insight_agent.stream(
        input_message,
        context=CustomContext(username=agent_request.username),
        stream_mode=["updates", "custom"]
    ):
        if stream_mode == "updates":
            for _, payload in chunk.items():
                messages = payload.get("messages", [])
                for message in messages:
                    tool_calls = getattr(message, 'tool_calls', None)
                    if tool_calls is not None:
                        cleaned = _clean_message_content(getattr(message, 'content', ''))
                        if tool_calls:
                            if cleaned:
                                yield _build_progress_event(
                                    'assistant',
                                    stage='planning',
                                    message=cleaned
                                )
                            for tool_call in tool_calls:
                                yield _build_progress_event(
                                    'status',
                                    stage='tool_call',
                                    level='info',
                                    tool=tool_call.get('name', ''),
                                    message=_format_tool_call_message(tool_call)
                                )
                        elif cleaned and not result_emitted:
                            yield _build_progress_event(
                                'assistant',
                                stage='planning',
                                message=cleaned
                            )
                    else:
                        tool_text = getattr(message, 'content', '')
                        file_id, analysis_report = _parse_structured_content(tool_text)
                        if file_id or analysis_report:
                            result_emitted = True
                            yield _build_progress_event(
                                'result',
                                stage='result',
                                file_id=file_id,
                                analysis_report=analysis_report
                            )
                            yield _build_progress_event(
                                'status',
                                stage='complete',
                                level='success',
                                message='分析完成，图表和报告已生成'
                            )
                        elif tool_text:
                            yield _build_progress_event(
                                'status',
                                stage='tool_feedback',
                                level='warning' if ('错误' in tool_text or '重新生成' in tool_text) else 'info',
                                message=tool_text
                            )
        elif stream_mode == "custom":
            if isinstance(chunk, dict):
                yield chunk
            elif chunk:
                yield _build_progress_event(
                    'status',
                    stage='tool',
                    level='info',
                    message=str(chunk)
                )

    yield _build_progress_event('done')


if __name__ == '__main__':
    response = invoke_agent(AgentRequest("John Smith", '', '', "分析2024年Q4季度的销售趋势"))
    print(response)
