import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from agent import CustomContext, get_input, insight_agent
from config.database import SessionLocal
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from utils import logger

THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.DOTALL)


@dataclass
class AgentRequest:
    """从控制器层传入 Agent 层的标准化请求对象。"""

    username: str
    namespace_id: str
    conversation_id: str
    user_message: str


@dataclass
class AgentResponse:
    """返回给控制器层的标准化分析结果。"""

    username: str
    message: str
    conversation_id: int
    turn_id: int
    file_id: str = ''
    analysis_report: str = ''


def _clean_message_content(content: Any) -> str:
    """去掉模型内部思考块，仅保留对用户可见的最终消息。"""
    text = content if isinstance(content, str) else str(content)
    return THINK_BLOCK_PATTERN.sub("", text).strip()


def _parse_structured_content(content: Any) -> tuple[str, str]:
    """解析工具层返回的 StructuredResult JSON。"""
    text = content if isinstance(content, str) else str(content)
    if not text.startswith('{'):
        return '', ''

    try:
        result_data = json.loads(text)
    except Exception as exc:
        logger.info(f"[DEBUG] JSON parse error: {exc}")
        return '', ''

    return result_data.get('file_id', ''), result_data.get('analysis_report', '')


def _build_progress_event(event_type: str, **payload: Any) -> dict[str, Any]:
    """构造一条适合通过 SSE 下发的事件载荷。"""
    return {'type': event_type, **payload}


def _build_agent_context(agent_request: AgentRequest, runtime: ConversationRunContext) -> CustomContext:
    """把运行时会话信息转换成 Agent 与工具层使用的上下文对象。"""
    return CustomContext(
        username=agent_request.username,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
    )


def _build_agent_input(agent_request: AgentRequest, runtime: ConversationRunContext):
    """在会话与轮次已持久化后，组装真正传给模型的输入。"""
    return get_input(
        agent_request.user_message,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
    )


def _finalize_run(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    assistant_message: str,
    analysis_report: str,
    file_id: str,
) -> AgentResponse:
    """写回本轮最终结果，并转换成控制器层可直接使用的响应对象。"""
    service.complete_run(
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        assistant_message=assistant_message,
        analysis_report=analysis_report,
        file_id=file_id,
    )
    return AgentResponse(
        username=runtime.conversation.username,
        message=assistant_message,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        file_id=file_id,
        analysis_report=analysis_report,
    )


def _format_tool_call_message(tool_call: dict[str, Any]) -> str:
    """把工具调用元数据转换成前端可直接展示的执行阶段文案。"""
    tool_name = tool_call.get('name', 'unknown_tool')
    args = tool_call.get('args') or {}

    if tool_name == 'execute_python':
        title = args.get('title') or '数据分析任务'
        return f"已生成分析代码，准备执行：{title}"

    return f"准备调用工具：{tool_name}"


def _extract_invoke_result(messages: list[Any]) -> tuple[str, str, str]:
    """
    从 invoke 返回结果中提取最终助手消息和结构化工具结果。

    当前工具契约仍然是 StructuredResult(file_id, analysis_report)，
    所以这里只解释现有约定，不改变既有主流程。
    """
    if not messages:
        return '', '', ''

    ai_message = messages[-1]
    assistant_message = _clean_message_content(
        ai_message.content if hasattr(ai_message, 'content') else str(ai_message)
    )

    structured_message = messages[-2] if len(messages) >= 2 else '{}'
    structured_content = (
        structured_message.content if hasattr(structured_message, 'content') else str(structured_message)
    )
    file_id, analysis_report = _parse_structured_content(structured_content)
    return assistant_message, analysis_report, file_id


def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    """端到端执行一次非流式分析请求。"""
    session = SessionLocal()
    service = ConversationContextService(session)
    runtime = service.start_run(
        username=agent_request.username,
        namespace_id=agent_request.namespace_id,
        conversation_id=agent_request.conversation_id,
        user_message=agent_request.user_message,
    )

    try:
        # 先把本轮 turn 落库，再执行 Agent。
        # 这样工具层从一开始就能拿到最新的 conversation_id 与 turn_id。
        agent_response = insight_agent.invoke(
            _build_agent_input(agent_request, runtime),
            context=_build_agent_context(agent_request, runtime),
        )
        assistant_message, analysis_report, file_id = _extract_invoke_result(agent_response.get('messages', []))
        return _finalize_run(
            service=service,
            runtime=runtime,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            file_id=file_id,
        )
    except Exception as exc:
        service.fail_run(runtime.conversation.id, runtime.turn.id, str(exc))
        raise
    finally:
        session.close()


def stream_invoke_agent(agent_request: AgentRequest) -> Iterator[dict[str, Any]]:
    """执行一次流式分析请求，并持续产出适合 SSE 下发的进度事件。"""
    session = SessionLocal()
    service = ConversationContextService(session)
    runtime = service.start_run(
        username=agent_request.username,
        namespace_id=agent_request.namespace_id,
        conversation_id=agent_request.conversation_id,
        user_message=agent_request.user_message,
    )

    assistant_message = ''
    file_id = ''
    analysis_report = ''

    yield _build_progress_event(
        'session',
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        namespace_id=runtime.conversation.insight_namespace_id,
        title=runtime.conversation.title,
    )
    yield _build_progress_event(
        'status',
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        stage='start',
        level='info',
        message='已收到请求，正在理解分析需求。',
    )

    try:
        for stream_mode, chunk in insight_agent.stream(
            _build_agent_input(agent_request, runtime),
            context=_build_agent_context(agent_request, runtime),
            stream_mode=["updates", "custom"],
        ):
            if stream_mode == "updates":
                # `updates` 承载模型与工具节点的增量消息。
                # 这里会把内部消息转换成前端可直接消费的进度事件。
                for _, payload in chunk.items():
                    messages = payload.get("messages", [])
                    for message in messages:
                        tool_calls = getattr(message, 'tool_calls', None)
                        cleaned = _clean_message_content(getattr(message, 'content', ''))

                        if tool_calls:
                            if cleaned:
                                assistant_message = cleaned
                                yield _build_progress_event(
                                    'assistant',
                                    conversation_id=runtime.conversation.id,
                                    turn_id=runtime.turn.id,
                                    stage='planning',
                                    message=cleaned,
                                )
                            for tool_call in tool_calls:
                                yield _build_progress_event(
                                    'status',
                                    conversation_id=runtime.conversation.id,
                                    turn_id=runtime.turn.id,
                                    stage='tool_call',
                                    level='info',
                                    tool=tool_call.get('name', ''),
                                    message=_format_tool_call_message(tool_call),
                                )
                            continue

                        if not cleaned:
                            continue

                        # 工具返回值仍然遵循 StructuredResult(file_id, analysis_report)。
                        parsed_file_id, parsed_analysis_report = _parse_structured_content(cleaned)
                        if parsed_file_id or parsed_analysis_report:
                            file_id = parsed_file_id or file_id
                            analysis_report = parsed_analysis_report or analysis_report
                            yield _build_progress_event(
                                'result',
                                conversation_id=runtime.conversation.id,
                                turn_id=runtime.turn.id,
                                stage='result',
                                file_id=file_id,
                                analysis_report=analysis_report,
                            )
                            continue

                        assistant_message = cleaned
                        yield _build_progress_event(
                            'assistant',
                            conversation_id=runtime.conversation.id,
                            turn_id=runtime.turn.id,
                            stage='planning',
                            message=cleaned,
                        )

            elif stream_mode == "custom":
                # `custom` 承载工具侧主动发出的事件，
                # 比如 execute_python 的执行进度。
                if isinstance(chunk, dict):
                    chunk.setdefault('conversation_id', runtime.conversation.id)
                    chunk.setdefault('turn_id', runtime.turn.id)
                    yield chunk
                elif chunk:
                    yield _build_progress_event(
                        'status',
                        conversation_id=runtime.conversation.id,
                        turn_id=runtime.turn.id,
                        stage='tool',
                        level='info',
                        message=str(chunk),
                    )

        _finalize_run(
            service=service,
            runtime=runtime,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            file_id=file_id,
        )
        yield _build_progress_event(
            'done',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
        )
    except Exception as exc:
        service.fail_run(runtime.conversation.id, runtime.turn.id, str(exc))
        yield _build_progress_event(
            'error',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='error',
            level='error',
            message=str(exc),
        )
    finally:
        session.close()
