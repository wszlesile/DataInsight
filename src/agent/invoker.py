import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

from agent import CustomContext, get_input, insight_agent
from agent.context_engineering import is_analysis_like_request
from config.database import SessionLocal
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from utils import logger

THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
TOOL_CALL_BLOCK_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", flags=re.DOTALL)
UNEXPECTED_KWARG_PATTERN = re.compile(
    r"(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\.__init__\(\) got an unexpected keyword argument '(?P<arg>[^']+)'"
)
MISSING_DEP_PATTERN = re.compile(r"No module named '(?P<module>[^']+)'")
MAX_ANALYSIS_AGENT_ROUNDS = 3
REGENERATE_REQUEST_PATTERN = re.compile(r'^(重新生成|重试|重新执行|重新跑一次|再生成一次|再来一次)$')


@dataclass
class AgentRequest:
    """控制器层传入 Agent 层的标准化请求对象。"""

    username: str
    namespace_id: str
    conversation_id: str
    user_message: str
    auth_token: str = ''
    database_conn_info: dict[str, Any] | None = None


@dataclass
class AgentResponse:
    """返回给控制器层的标准化分析结果。"""

    username: str
    message: str
    conversation_id: int
    turn_id: int
    charts: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    analysis_report: str = ''
    chart_artifact_id: int = 0
    chart_artifact_ids: list[int] | None = None


def _clean_message_content(content: Any) -> str:
    """去掉模型内部思考块，只保留用户可见文本。"""
    text = content if isinstance(content, str) else str(content)
    return THINK_BLOCK_PATTERN.sub("", text).strip()


def _extract_raw_tool_call(content: Any) -> dict[str, Any] | None:
    """识别模型误输出的原始工具调用文本。"""
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


def _is_internal_assistant_message(content: Any) -> bool:
    """识别不应展示给前端的内部规划或代码文本。"""
    text = _clean_message_content(content)
    if not text:
        return False

    internal_signals = (
        'save_analysis_result(',
        'load_data_with_',
        'load_local_file(',
        'load_minio_file(',
        'import pandas as pd',
        'from pyecharts',
        '```python',
    )
    return any(signal in text for signal in internal_signals)


def _parse_structured_content(content: Any) -> dict[str, Any]:
    """解析工具层返回的 StructuredResult JSON。"""
    text = content if isinstance(content, str) else str(content)
    if not text.startswith('{'):
        return {}

    try:
        result_data = json.loads(text)
    except Exception as exc:
        logger.info(f"[DEBUG] JSON parse error: {exc}")
        return {}

    if not isinstance(result_data, dict):
        return {}

    return {
        'analysis_report': result_data.get('analysis_report', '') or '',
        'charts': result_data.get('charts') or [],
        'tables': result_data.get('tables') or [],
    }


def _load_latest_execution_result(
    service: ConversationContextService,
    runtime: ConversationRunContext,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    从当前轮最近一次成功执行记录中读取完整结构化结果。

    execute_python 成功后只向模型返回轻量摘要，
    真正完整的 charts/tables 结果以 execution.result_payload_json 为准。
    """
    latest_execution = service.get_latest_execution(
        runtime.turn.id,
        started_at=runtime.turn.started_at,
    )
    if latest_execution is None or latest_execution.execution_status != 'success':
        return '', [], []

    try:
        payload = json.loads(latest_execution.result_payload_json or '{}')
    except Exception:
        return '', [], []

    if not isinstance(payload, dict):
        return '', [], []

    return (
        payload.get('analysis_report', '') or '',
        payload.get('charts') or [],
        payload.get('tables') or [],
    )


def _resolve_analysis_outputs(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    analysis_report: str,
    charts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    统一收口本轮完整分析结果。

    优先使用最新成功执行记录里的完整 payload，
    再回退到当前回合直接解析到的结构化结果。
    """
    execution_report, execution_charts, execution_tables = _load_latest_execution_result(service, runtime)
    final_report = execution_report or analysis_report or ''
    final_charts = execution_charts or (charts or [])
    final_tables = execution_tables or (tables or [])
    return final_report, final_charts, final_tables


def _is_internal_tool_feedback(content: Any) -> bool:
    """识别只用于模型自修复的 execute_python 错误反馈。"""
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
    """构造适合通过 SSE 下发的事件。"""
    return {'type': event_type, **payload}


def _build_agent_context(agent_request: AgentRequest, runtime: ConversationRunContext) -> CustomContext:
    """构造 Agent 与工具层使用的运行上下文。"""
    return CustomContext(
        username=agent_request.username,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        auth_token=agent_request.auth_token or '',
        database_conn_info=dict(agent_request.database_conn_info or {}),
    )


def _build_agent_input_with_runtime_instruction(
    agent_request: AgentRequest,
    runtime: ConversationRunContext,
    runtime_instruction: str = '',
):
    """在必要时追加一条很短的运行时修复指令。"""
    extra_system_messages = [runtime_instruction] if runtime_instruction else None
    return get_input(
        agent_request.user_message,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        extra_system_messages=extra_system_messages,
        history_turn_limit=runtime.history_turn_limit,
        datasource_snapshot_override=runtime.active_datasource_snapshot,
    )


def _is_regenerate_request(user_message: str) -> bool:
    """识别明显指向“重跑上一轮分析”的简短省略指令。"""
    normalized = re.sub(r'\s+', '', (user_message or '').strip())
    return bool(REGENERATE_REQUEST_PATTERN.fullmatch(normalized))


def _build_regenerate_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
) -> str:
    """
    把“重新生成/重试”这类省略指代还原成最近一次成功分析任务。

    这里只补充最短的运行时约束，不复制 sys_prompt 里的通用业务规则。
    """
    if not _is_regenerate_request(user_message):
        return ''

    latest_turn = service.get_latest_successful_analysis_turn(
        runtime.conversation.id,
        max_turn_no=runtime.history_turn_limit,
    )
    if latest_turn is None:
        return ''

    latest_question = (latest_turn.user_query or '').strip()
    if not latest_question:
        return ''

    return (
        '当前用户输入“重新生成/重试”明确指向最近一次成功分析任务。'
        f'请把本轮目标理解为：重新执行第 {latest_turn.turn_no} 轮的分析请求：'
        f'「{latest_question}」。'
        '不要复述历史结论，必须重新调用 execute_python 基于当前会话数据源重新完成分析。'
    )


def _build_rerun_instruction(runtime: ConversationRunContext) -> str:
    """
    为“分析刷新/原轮次重跑”补一条首轮运行时指令。

    这里不改变原问题文本，只明确告诉模型：
    当前不是普通续问，也不是让它复述历史答案，而是要基于同一轮问题重新执行分析。
    """
    if not runtime.is_rerun:
        return ''

    return (
        '当前操作是“刷新分析”，需要在同一轮内重新执行这条分析请求。'
        '请基于本轮原始问题、当前会话数据源和已有历史上下文重新完成分析，'
        '不要直接复述之前的结论，必须重新调用 execute_python 生成新的分析结果。'
    )


def _finalize_run(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    username: str,
    user_message: str,
    analysis_flow_started: bool,
    assistant_message: str,
    analysis_report: str,
    charts: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    allow_report_only: bool = False,
) -> AgentResponse:
    """写回最终结果并转换成控制器层可直接使用的响应对象。"""
    has_visible_output = bool((assistant_message or '').strip() or (analysis_report or '').strip() or charts or tables)
    if not has_visible_output:
        raise ValueError(_build_empty_response_error(user_message, analysis_flow_started))
    if is_analysis_like_request(user_message) and not analysis_flow_started and not allow_report_only:
        raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

    run_result = service.complete_run(
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        assistant_message=assistant_message,
        analysis_report=analysis_report,
        charts=charts,
        tables=tables,
        replace_existing_results=runtime.is_rerun,
    )
    chart_artifact_ids: list[int] = []
    for artifact in run_result.get('artifacts', []):
        if artifact.get('artifact_type') == 'chart':
            chart_artifact_ids.append(int(artifact.get('id', 0) or 0))

    return AgentResponse(
        username=username,
        message=assistant_message,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        charts=run_result.get('charts', charts),
        tables=run_result.get('tables', tables),
        analysis_report=analysis_report,
        chart_artifact_id=chart_artifact_ids[0] if chart_artifact_ids else 0,
        chart_artifact_ids=chart_artifact_ids,
    )


def _build_empty_response_error(user_message: str, analysis_flow_started: bool) -> str:
    """按请求类型生成更准确的兜底错误。"""
    if analysis_flow_started:
        return '本轮分析已启动，但未生成可展示结果，请重试。'
    if is_analysis_like_request(user_message):
        return '本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。'
    return '模型本轮没有返回有效回复，请重试。'


def _ensure_analysis_result_ready(
    analysis_report: str,
    charts: list[dict[str, Any]] | None = None,
) -> None:
    """分析型请求必须真实产出结构化分析结果。"""
    if analysis_report and (charts or []):
        return
    raise ValueError('本轮未生成完整分析产物：缺少图表结果或分析报告，请重新执行分析。')


def _build_analysis_start_instruction(user_message: str) -> str:
    """当模型没有进入分析流时，补一条最小运行时纠偏指令。"""
    return (
        '当前用户输入是明确的数据分析任务，不能按普通问答结束。'
        f'请围绕这条请求真正启动分析执行：{user_message}。'
        '必须调用 execute_python 读取当前会话已绑定的数据源，生成图表和分析报告；'
        '不要只给自然语言说明，也不要提前结束本轮。'
    )


def _should_use_report_only_fallback(
    *,
    analysis_request_expected: bool,
    round_index: int,
    has_any_artifact: bool,
    assistant_message: str,
) -> bool:
    """判断是否允许把最后一条可见回复直接当作报告产物收口。"""
    if not analysis_request_expected:
        return False
    if has_any_artifact:
        return False
    if not (assistant_message or '').strip():
        return False
    return round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1


def _promote_assistant_message_to_report(assistant_message: str, analysis_report: str) -> str:
    """在 report-only 兜底时，把可见回复提升为分析报告内容。"""
    return (analysis_report or '').strip() or (assistant_message or '').strip()


def _get_selected_datasource_names(runtime: ConversationRunContext) -> list[str]:
    """提取当前会话已绑定数据源名称，供失败原因说明使用。"""
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = snapshot.get('selected_datasource_snapshot') or []
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get('datasource_name') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _build_failure_reason_reply(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
) -> str:
    """当最后没有自然语言回复时，构造一条简短的失败原因说明。"""
    executions = service.get_turn_executions(runtime.turn.id, started_at=runtime.turn.started_at)
    latest_errors = [
        (getattr(execution, 'error_message', '') or '').strip()
        for execution in executions
        if getattr(execution, 'execution_status', '') != 'success' and (getattr(execution, 'error_message', '') or '').strip()
    ][-3:]
    datasource_names = _get_selected_datasource_names(runtime)

    parts = [
        f"这次没有顺利完成“{(user_message or '').strip()}”的分析。"
    ]
    if latest_errors:
        parts.append(f"目前能确认的直接原因是：{latest_errors[-1]}。")
    else:
        parts.append("这轮分析已经进入执行阶段，但最后没有成功生成可展示的图表或分析结论。")
    if datasource_names:
        parts.append(f"当前会话里已绑定的数据源包括：{', '.join(datasource_names)}。")
    parts.append("你可以检查运行环境依赖是否完整，或者再明确一下分析对象、时间范围和期望指标后重试。")
    return '\n\n'.join(parts).strip()


def _should_use_failure_reason_fallback(
    *,
    analysis_request_expected: bool,
    round_index: int,
    has_any_artifact: bool,
    assistant_message: str,
) -> bool:
    """当最后没有任何自然语言回复时，补一条失败原因说明产物。"""
    if not analysis_request_expected:
        return False
    if has_any_artifact:
        return False
    if (assistant_message or '').strip():
        return False
    return round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1


def _did_enter_analysis_flow(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    raw_tool_call_detected: bool,
    analysis_report: str,
    charts: list[dict[str, Any]] | None = None,
) -> bool:
    """
    判断当前轮次是否已经进入真实的数据分析执行链。

    这里不再额外请求模型做意图分流，只依据本轮是否出现过工具执行痕迹来判断。
    """
    if service.get_latest_execution(runtime.turn.id, started_at=runtime.turn.started_at) is not None:
        return True
    if raw_tool_call_detected:
        return True
    if runtime.is_rerun:
        return False
    if analysis_report or (charts or []):
        return True
    return False


def _format_tool_call_message(tool_call: dict[str, Any]) -> str:
    """把工具调用元数据转换成前端可展示的执行阶段文案。"""
    tool_name = tool_call.get('name', 'unknown_tool')
    args = tool_call.get('args') or {}

    if tool_name == 'execute_python':
        title = args.get('title') or '数据分析任务'
        return f"已生成分析代码，准备执行：{title}"

    return f"准备调用工具：{tool_name}"


def _build_failed_pattern_hints(executions: list[Any]) -> list[str]:
    """从当前轮失败记录中提取“不要重复犯错”的提示。"""
    hints: list[str] = []
    seen: set[str] = set()

    for execution in executions:
        error_message = (getattr(execution, 'error_message', '') or '').strip()
        if not error_message:
            continue

        unexpected_kwarg_match = UNEXPECTED_KWARG_PATTERN.search(error_message)
        if unexpected_kwarg_match:
            owner = unexpected_kwarg_match.group('owner')
            arg = unexpected_kwarg_match.group('arg')
            hint = f'{owner} 不支持参数 `{arg}`，下次只修这个参数写法，不要重写整张图表的其他配置。'
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)
            continue

        missing_dep_match = MISSING_DEP_PATTERN.search(error_message)
        if missing_dep_match:
            module = missing_dep_match.group('module')
            hint = f'当前环境缺少模块 `{module}`，不要再次引入它，优先使用内置辅助函数或标准库。'
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)
            continue

        normalized_error = error_message[:160]
        if normalized_error not in seen:
            seen.add(normalized_error)
            hints.append(f'避免再次触发已出现过的错误：{normalized_error}')

    return hints[:5]


def _get_latest_failed_generated_code(executions: list[Any]) -> str:
    """返回最近一次失败执行的完整代码，用于引导模型做最小修补。"""
    for execution in reversed(executions):
        if getattr(execution, 'execution_status', '') == 'success':
            continue
        generated_code = (getattr(execution, 'generated_code', '') or '').strip()
        if generated_code:
            return generated_code
    return ''


def _build_analysis_recovery_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    round_index: int,
) -> str:
    """为同一轮分析构造后续修复回合的运行时指令。"""
    executions = service.get_turn_executions(
        runtime.turn.id,
        started_at=runtime.turn.started_at,
    )
    failed_errors = [
        execution.error_message.strip()
        for execution in executions
        if execution.execution_status != 'success' and (execution.error_message or '').strip()
    ]
    latest_errors = failed_errors[-3:]
    failed_pattern_hints = _build_failed_pattern_hints(executions)
    latest_failed_code = _get_latest_failed_generated_code(executions)

    lines = [
        '本轮已经进入数据分析执行阶段。',
        f'当前轮次累计执行了 {len(executions)} 次 Python 分析代码，但仍未成功生成完整分析产物。',
    ]
    if latest_errors:
        lines.append('最近的执行错误如下：')
        lines.extend(f'- {item}' for item in latest_errors)

    if failed_pattern_hints:
        lines.append('本轮已经确认的失败写法如下，新的修复代码中不要再次出现：')
        lines.extend(f'- {item}' for item in failed_pattern_hints)

    lines.extend([
        '请基于上一版失败代码做最小必要修补，不要整段重写与当前错误无关的逻辑。',
        '请直接修正完整 Python 代码并再次调用 execute_python。',
        '优先检查：时间过滤是否命中数据、过滤后数据集是否为空、关联结果是否为空；如果为空，应调用 raise_no_data_error(...) 把“无数据”返回给 execute_python，再由上层继续重试，而不是继续误改字段名或图表参数，更不要输出空图表收口。',
        '不要向用户解释工具错误，不要再次询问用户，也不要用自然语言提前结束本轮。',
        '只有成功生成完整的结构化图表结果和分析报告，这一轮分析才算完成。',
    ])

    if latest_failed_code:
        lines.extend([
            '下面是本轮最近一次失败的完整代码，请以它为基础只做最小必要修补：',
            '```python',
            latest_failed_code,
            '```',
        ])

    if round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1:
        lines.append('这已经是本轮最后一次修复机会；如果仍无法完成，请让本轮以失败结束，而不是伪装成成功。')

    return '\n'.join(lines)


def _extract_invoke_result(messages: list[Any]) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]], bool]:
    """从 invoke 结果中提取最终助手消息和结构化工具结果。"""
    if not messages:
        return '', '', [], [], False

    assistant_message = ''
    analysis_report = ''
    charts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    raw_tool_call_detected = False

    for message in reversed(messages):
        content = message.content if hasattr(message, 'content') else str(message)
        cleaned = _clean_message_content(content)
        if not cleaned:
            continue

        if _extract_raw_tool_call(cleaned):
            raw_tool_call_detected = True
            continue

        parsed_result = _parse_structured_content(cleaned)
        if parsed_result:
            analysis_report = parsed_result.get('analysis_report', '') or analysis_report
            charts = parsed_result.get('charts') or charts
            tables = parsed_result.get('tables') or tables
            continue

        if _is_internal_tool_feedback(cleaned):
            continue
        if _is_internal_assistant_message(cleaned):
            continue

        if not assistant_message:
            assistant_message = cleaned

    return assistant_message, analysis_report, charts, tables, raw_tool_call_detected


def invoke_agent(agent_request: AgentRequest) -> AgentResponse:
    """执行一次非流式分析请求。"""
    session = SessionLocal()
    service = ConversationContextService(session)
    runtime = service.start_run(
        username=agent_request.username,
        namespace_id=agent_request.namespace_id,
        conversation_id=agent_request.conversation_id,
        user_message=agent_request.user_message,
    )

    try:
        assistant_message = ''
        analysis_report = ''
        charts: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        analysis_flow_started = False
        analysis_request_expected = is_analysis_like_request(agent_request.user_message)
        allow_report_only = False

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            runtime_instruction = ''
            if analysis_flow_started:
                runtime_instruction = _build_analysis_recovery_instruction(
                    service=service,
                    runtime=runtime,
                    round_index=round_index,
                )
            elif runtime.is_rerun:
                runtime_instruction = _build_rerun_instruction(runtime)
            elif round_index == 0:
                runtime_instruction = _build_regenerate_instruction(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
            elif analysis_request_expected:
                runtime_instruction = _build_analysis_start_instruction(agent_request.user_message)

            agent_response = insight_agent.invoke(
                _build_agent_input_with_runtime_instruction(
                    agent_request=agent_request,
                    runtime=runtime,
                    runtime_instruction=runtime_instruction,
                ),
                context=_build_agent_context(agent_request, runtime),
            )
            assistant_message, analysis_report, charts, tables, raw_tool_call_detected = _extract_invoke_result(
                agent_response.get('messages', [])
            )

            entered_analysis_flow = _did_enter_analysis_flow(
                service=service,
                runtime=runtime,
                raw_tool_call_detected=raw_tool_call_detected,
                analysis_report=analysis_report,
                charts=charts,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow
            analysis_report, charts, tables = _resolve_analysis_outputs(
                service=service,
                runtime=runtime,
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
            )

            if raw_tool_call_detected and not (analysis_report or charts):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('模型返回了未执行的工具调用内容，未生成最终分析结果，请重试。')

            if _should_use_report_only_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
            ):
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if _should_use_failure_reason_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
            ):
                assistant_message = _build_failure_reason_reply(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if analysis_flow_started:
                if analysis_report and charts:
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                _ensure_analysis_result_ready(analysis_report=analysis_report, charts=charts)
                break

            if runtime.is_rerun:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('刷新分析未重新执行代码，已保留本轮原有分析结果，请重试。')

            if analysis_request_expected:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

            break

        return _finalize_run(
            service=service,
            runtime=runtime,
            username=agent_request.username,
            user_message=agent_request.user_message,
            analysis_flow_started=analysis_flow_started,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            charts=charts,
            tables=tables,
            allow_report_only=allow_report_only,
        )
    except Exception as exc:
        session.rollback()
        service.fail_run(
            runtime.conversation.id,
            runtime.turn.id,
            str(exc),
            preserve_existing_results=runtime.is_rerun,
        )
        raise
    finally:
        session.close()


def _stream_with_runtime(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
) -> Iterator[dict[str, Any]]:
    """在给定上下文中执行流式分析或重跑，并持续产出 SSE 事件。"""
    assistant_message = ''
    analysis_report = ''
    charts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    log_context_token = logger.bind_context(
        username=agent_request.username,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
    )
    # “原轮次重跑”并不等于“已经进入失败后的修复回合”。
    # 只有当前这次重跑真的执行过分析、且尚未成功收口时，才进入 retry 分支。
    analysis_flow_started = False
    analysis_request_expected = is_analysis_like_request(agent_request.user_message)
    allow_report_only = False

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
    if runtime.is_rerun:
        yield _build_progress_event(
            'status',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='rerun',
            level='info',
            message='正在重新执行本轮分析。',
        )

    try:
        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            raw_tool_call_detected = False
            runtime_instruction = ''
            tool_result_ready = False
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
            elif runtime.is_rerun:
                runtime_instruction = _build_rerun_instruction(runtime)
            elif round_index == 0:
                runtime_instruction = _build_regenerate_instruction(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
            elif analysis_request_expected:
                runtime_instruction = _build_analysis_start_instruction(agent_request.user_message)
                yield _build_progress_event(
                    'status',
                    conversation_id=runtime.conversation.id,
                    turn_id=runtime.turn.id,
                    stage='analysis_retry',
                    level='warning',
                    message='正在重新引导模型进入分析执行阶段。',
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
                    for _, payload in chunk.items():
                        messages = payload.get("messages", [])
                        for message in messages:
                            tool_calls = getattr(message, 'tool_calls', None)
                            raw_tool_call = _extract_raw_tool_call(getattr(message, 'content', ''))
                            cleaned = _clean_message_content(getattr(message, 'content', ''))

                            if tool_calls:
                                if cleaned and not _is_internal_assistant_message(cleaned):
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

                            parsed_result = _parse_structured_content(cleaned)
                            if parsed_result:
                                analysis_report = parsed_result.get('analysis_report', '') or analysis_report
                                charts = parsed_result.get('charts') or charts
                                tables = parsed_result.get('tables') or tables
                                continue

                            if _is_internal_tool_feedback(cleaned):
                                continue
                            if _is_internal_assistant_message(cleaned):
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
                    if isinstance(chunk, dict):
                        chunk.setdefault('conversation_id', runtime.conversation.id)
                        chunk.setdefault('turn_id', runtime.turn.id)
                        yield chunk
                        if chunk.get('stage') == 'tool_result':
                            execution_report, execution_charts, execution_tables = _load_latest_execution_result(
                                service=service,
                                runtime=runtime,
                            )
                            if execution_report and execution_charts:
                                analysis_report = execution_report
                                charts = execution_charts
                                tables = execution_tables
                                tool_result_ready = True
                                break
                    elif chunk:
                        yield _build_progress_event(
                            'status',
                            conversation_id=runtime.conversation.id,
                            turn_id=runtime.turn.id,
                            stage='tool',
                            level='info',
                            message=str(chunk),
                        )

            if tool_result_ready:
                analysis_flow_started = True
                break

            entered_analysis_flow = _did_enter_analysis_flow(
                service=service,
                runtime=runtime,
                raw_tool_call_detected=raw_tool_call_detected,
                analysis_report=analysis_report,
                charts=charts,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow
            analysis_report, charts, tables = _resolve_analysis_outputs(
                service=service,
                runtime=runtime,
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
            )

            if raw_tool_call_detected and not (analysis_report or charts):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('模型返回了未执行的工具调用内容，未生成最终分析结果，请重试。')

            if _should_use_report_only_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
            ):
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if _should_use_failure_reason_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
            ):
                assistant_message = _build_failure_reason_reply(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if analysis_flow_started:
                if analysis_report and charts:
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                _ensure_analysis_result_ready(analysis_report=analysis_report, charts=charts)
                break

            if runtime.is_rerun:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('刷新分析未重新执行代码，已保留本轮原有分析结果，请重试。')

            if analysis_request_expected:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

            break

        response = _finalize_run(
            service=service,
            runtime=runtime,
            username=agent_request.username,
            user_message=agent_request.user_message,
            analysis_flow_started=analysis_flow_started,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            charts=charts,
            tables=tables,
            allow_report_only=allow_report_only,
        )
        if response.chart_artifact_ids or response.analysis_report:
            yield _build_progress_event(
                'result',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage='result',
                analysis_report=analysis_report,
                charts=response.charts,
                tables=response.tables,
                chart_artifact_id=response.chart_artifact_id,
                chart_artifact_ids=response.chart_artifact_ids,
            )
        yield _build_progress_event(
            'done',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
        )
    except Exception as exc:
        service.session.rollback()
        service.fail_run(
            runtime.conversation.id,
            runtime.turn.id,
            str(exc),
            preserve_existing_results=runtime.is_rerun,
        )
        yield _build_progress_event(
            'error',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='error',
            level='error',
            message=str(exc),
        )
    finally:
        logger.reset_context(log_context_token)


def stream_invoke_agent(agent_request: AgentRequest) -> Iterator[dict[str, Any]]:
    """执行一次流式分析请求，并持续产出适合 SSE 的事件。"""
    session = SessionLocal()
    service = ConversationContextService(session)
    runtime = service.start_run(
        username=agent_request.username,
        namespace_id=agent_request.namespace_id,
        conversation_id=agent_request.conversation_id,
        user_message=agent_request.user_message,
    )

    try:
        yield from _stream_with_runtime(service, runtime, agent_request)
    finally:
        session.close()


def _build_rerun_agent_request(
    username: str,
    runtime: ConversationRunContext,
    auth_token: str = '',
    database_conn_info: dict[str, Any] | None = None,
) -> AgentRequest:
    """构造刷新分析复用的 AgentRequest，并透传用户数据库连接信息。"""
    return AgentRequest(
        username=username,
        namespace_id=str(runtime.conversation.insight_namespace_id),
        conversation_id=str(runtime.conversation.id),
        user_message=runtime.turn.user_query,
        auth_token=auth_token or '',
        database_conn_info=dict(database_conn_info or {}),
    )


def stream_rerun_turn(
    username: str,
    conversation_id: Any,
    turn_id: Any,
    auth_token: str = '',
    database_conn_info: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """在同一轮次内重新执行一次分析，并把新结果回写到该轮。"""
    session = SessionLocal()
    service = ConversationContextService(session)
    runtime = service.start_rerun(
        username=username,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )

    if runtime is None:
        session.close()
        raise ValueError('轮次详情不存在')

    agent_request = _build_rerun_agent_request(
        username=username,
        runtime=runtime,
        auth_token=auth_token,
        database_conn_info=database_conn_info,
    )

    try:
        yield from _stream_with_runtime(service, runtime, agent_request)
    finally:
        session.close()
