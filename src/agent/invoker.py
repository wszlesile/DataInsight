import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from agent import CustomContext, get_input, insight_agent
from config.database import SessionLocal
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from utils import logger

THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
TOOL_CALL_BLOCK_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", flags=re.DOTALL)
MAX_ANALYSIS_AGENT_ROUNDS = 3


@dataclass
class AgentRequest:
    """从控制器层传给 Agent 层的标准化请求对象。"""

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
    chart_artifact_id: int = 0


def _clean_message_content(content: Any) -> str:
    """去掉模型内部思考块，仅保留对用户可见的最终消息。"""
    text = content if isinstance(content, str) else str(content)
    return THINK_BLOCK_PATTERN.sub("", text).strip()


def _extract_raw_tool_call(content: Any) -> dict[str, Any] | None:
    """识别模型误输出的原始工具调用文本，并尽量解析其中的 JSON 载荷。"""
    text = _clean_message_content(content)
    if not text or '<tool_call>' not in text:
        return None

    match = TOOL_CALL_BLOCK_PATTERN.search(text)
    if not match:
        return {'name': 'unknown_tool', 'args': {}}

    try:
        payload = json.loads(match.group(1))
    except Exception:
        return {'name': 'unknown_tool', 'args': {}}

    return {
        'name': payload.get('name', 'unknown_tool'),
        'args': payload.get('arguments') or payload.get('args') or {},
    }


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


def _is_internal_tool_feedback(content: Any) -> bool:
    """
    识别只用于模型自修正的工具错误反馈 JSON。

    这类内容属于内部协议，不应展示给前端，也不应作为最终助手回复保存。
    """
    text = content if isinstance(content, str) else str(content)
    if not text.startswith('{'):
        return False

    try:
        payload = json.loads(text)
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False

    return (
        payload.get('tool') == 'execute_python'
        and payload.get('status') == 'failed'
        and isinstance(payload.get('error_type'), str)
        and isinstance(payload.get('error_message'), str)
        and isinstance(payload.get('repair_instructions'), list)
    )


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


def _build_agent_input_with_runtime_instruction(
    agent_request: AgentRequest,
    runtime: ConversationRunContext,
    runtime_instruction: str = '',
):
    """在必要时为当前轮追加极短的运行时约束消息。"""
    extra_system_messages = [runtime_instruction] if runtime_instruction else None
    return get_input(
        agent_request.user_message,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        extra_system_messages=extra_system_messages,
    )


def _finalize_run(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    assistant_message: str,
    analysis_report: str,
    file_id: str,
) -> AgentResponse:
    """写回本轮最终结果，并转换成控制器层可直接使用的响应对象。"""
    run_result = service.complete_run(
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        assistant_message=assistant_message,
        analysis_report=analysis_report,
        file_id=file_id,
    )
    chart_artifact_id = 0
    for artifact in run_result.get('artifacts', []):
        if artifact.get('artifact_type') == 'chart':
            chart_artifact_id = int(artifact.get('id', 0) or 0)
            break
    return AgentResponse(
        username=runtime.conversation.username,
        message=assistant_message,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        file_id=file_id,
        analysis_report=analysis_report,
        chart_artifact_id=chart_artifact_id,
    )


def _ensure_analysis_result_ready(file_id: str, analysis_report: str) -> None:
    """分析型请求必须真实产出图表文件和分析报告。"""
    if file_id and analysis_report:
        return
    raise ValueError('本轮未生成完整分析产物：缺少图表文件或分析报告，请重新执行分析。')


def _did_enter_analysis_flow(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    raw_tool_call_detected: bool,
    file_id: str,
    analysis_report: str,
) -> bool:
    """
    判断本轮是否已经进入真实分析执行链。

    这里不再额外请求模型做意图分类，也不在代码里重复维护意图分流规则。
    单次请求中：
    - 如果模型真实调用了工具并留下执行记录，说明它已经按分析任务处理
    - 如果模型输出了原始工具调用文本或已经产出了结构化分析结果，也视为分析链路
    - 否则就把它解释为普通自然语言回复
    """
    if service.get_latest_execution(runtime.turn.id) is not None:
        return True
    if raw_tool_call_detected:
        return True
    if file_id or analysis_report:
        return True
    return False


def _format_tool_call_message(tool_call: dict[str, Any]) -> str:
    """把工具调用元数据转换成前端可直接展示的执行阶段文案。"""
    tool_name = tool_call.get('name', 'unknown_tool')
    args = tool_call.get('args') or {}

    if tool_name == 'execute_python':
        title = args.get('title') or '数据分析任务'
        return f"已生成分析代码，准备执行：{title}"

    return f"准备调用工具：{tool_name}"


def _build_analysis_recovery_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    round_index: int,
) -> str:
    """
    为同一轮分析的后续修正回合构造运行时控制消息。

    这里只描述当前轮必须继续完成分析闭环，不复制系统业务规则。
    """
    executions = service.get_turn_executions(runtime.turn.id)
    failed_errors = [
        execution.error_message.strip()
        for execution in executions
        if execution.execution_status != 'success' and (execution.error_message or '').strip()
    ]
    latest_errors = failed_errors[-3:]

    lines = [
        '本轮已经进入数据分析执行阶段。',
        f'当前轮次已累计执行 {len(executions)} 次 Python 分析代码，但仍未成功生成完整分析产物。',
    ]
    if latest_errors:
        lines.append('最近的执行错误如下：')
        lines.extend(f'- {item}' for item in latest_errors)

    lines.extend([
        '请直接根据这些执行错误修正完整 Python 代码，并再次调用 execute_python。',
        '不要向用户解释工具错误，不要再次询问用户，也不要用自然语言提前结束本轮。',
        '只有成功生成完整的 file_id 和 analysis_report，这一轮分析才算完成。',
    ])

    if round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1:
        lines.append('这已经是本轮最后一次修正机会；如果仍无法完成，请让本轮以失败结束，而不是伪装成成功。')

    return '\n'.join(lines)


def _extract_invoke_result(messages: list[Any]) -> tuple[str, str, str, bool]:
    """
    从 invoke 返回结果中提取最终助手消息和结构化工具结果。

    当前工具契约仍然是 StructuredResult(file_id, analysis_report)，
    所以这里仅解析现有约定，不改变既有主流程。
    """
    if not messages:
        return '', '', '', False

    assistant_message = ''
    analysis_report = ''
    file_id = ''
    raw_tool_call_detected = False

    for message in reversed(messages):
        content = message.content if hasattr(message, 'content') else str(message)
        cleaned = _clean_message_content(content)
        if not cleaned:
            continue

        if _extract_raw_tool_call(cleaned):
            raw_tool_call_detected = True
            continue

        parsed_file_id, parsed_analysis_report = _parse_structured_content(cleaned)
        if parsed_file_id or parsed_analysis_report:
            file_id = parsed_file_id or file_id
            analysis_report = parsed_analysis_report or analysis_report
            continue

        if _is_internal_tool_feedback(cleaned):
            continue

        if not assistant_message:
            assistant_message = cleaned

    return assistant_message, analysis_report, file_id, raw_tool_call_detected


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
        assistant_message = ''
        analysis_report = ''
        file_id = ''
        analysis_flow_started = False

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            runtime_instruction = ''
            if analysis_flow_started:
                runtime_instruction = _build_analysis_recovery_instruction(
                    service=service,
                    runtime=runtime,
                    round_index=round_index,
                )

            agent_response = insight_agent.invoke(
                _build_agent_input_with_runtime_instruction(
                    agent_request=agent_request,
                    runtime=runtime,
                    runtime_instruction=runtime_instruction,
                ),
                context=_build_agent_context(agent_request, runtime),
            )
            assistant_message, analysis_report, file_id, raw_tool_call_detected = _extract_invoke_result(
                agent_response.get('messages', [])
            )

            entered_analysis_flow = _did_enter_analysis_flow(
                service=service,
                runtime=runtime,
                raw_tool_call_detected=raw_tool_call_detected,
                file_id=file_id,
                analysis_report=analysis_report,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow

            if raw_tool_call_detected and not (file_id or analysis_report):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('模型返回了未执行的工具调用内容，未生成最终分析结果，请重试。')

            if analysis_flow_started:
                if file_id and analysis_report:
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                _ensure_analysis_result_ready(file_id=file_id, analysis_report=analysis_report)
                break

            break

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
    analysis_flow_started = False

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
        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            raw_tool_call_detected = False
            runtime_instruction = ''
            if analysis_flow_started:
                runtime_instruction = _build_analysis_recovery_instruction(
                    service=service,
                    runtime=runtime,
                    round_index=round_index,
                )
                yield _build_progress_event(
                    'status',
                    conversation_id=runtime.conversation.id,
                    turn_id=runtime.turn.id,
                    stage='retry',
                    level='warning',
                    message='本轮分析尚未完成，正在根据最近执行错误继续修正代码。',
                )

            for stream_mode, chunk in insight_agent.stream(
                _build_agent_input_with_runtime_instruction(
                    agent_request=agent_request,
                    runtime=runtime,
                    runtime_instruction=runtime_instruction,
                ),
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
                            raw_tool_call = _extract_raw_tool_call(getattr(message, 'content', ''))
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

                            if raw_tool_call:
                                raw_tool_call_detected = True
                                yield _build_progress_event(
                                    'status',
                                    conversation_id=runtime.conversation.id,
                                    turn_id=runtime.turn.id,
                                    stage='tool_call',
                                    level='info',
                                    tool=raw_tool_call.get('name', ''),
                                    message=_format_tool_call_message(raw_tool_call),
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

                            if _is_internal_tool_feedback(cleaned):
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

            entered_analysis_flow = _did_enter_analysis_flow(
                service=service,
                runtime=runtime,
                raw_tool_call_detected=raw_tool_call_detected,
                file_id=file_id,
                analysis_report=analysis_report,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow

            if raw_tool_call_detected and not (file_id or analysis_report):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('模型返回了未执行的工具调用内容，未生成最终分析结果，请重试。')

            if analysis_flow_started:
                if file_id and analysis_report:
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                _ensure_analysis_result_ready(file_id=file_id, analysis_report=analysis_report)
                break

            break

        response = _finalize_run(
            service=service,
            runtime=runtime,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            file_id=file_id,
        )
        if response.chart_artifact_id:
            yield _build_progress_event(
                'result',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage='result',
                file_id=file_id,
                analysis_report=analysis_report,
                chart_artifact_id=response.chart_artifact_id,
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
