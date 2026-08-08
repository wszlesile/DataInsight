import hashlib
import json
import re
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from agent import CustomContext, get_data_insight_agent, get_input
from agent.analysis_preparation import (
    BusinessFactCoverageContract,
    BusinessFactRequirementContract,
    BusinessContextContract,
    BusinessQuestionContract,
    BusinessQuestionCoverageContract,
    DatasourceSelectionContract,
    IntentContract,
    PreparationResult,
    infer_query_capabilities,
    prepare_analysis,
)
from agent.context_engineering_runtime import classify_analysis_context_mode, is_analysis_like_request
from agent.intent_classifier import resolve_context_mode_with_llm
from config import Config
from config.database import session_scope
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from utils import logger
from utils.datasource_utils import safe_json_loads
from utils.analysis_task_cancel import AnalysisTaskCancelled, CANCELLED_MESSAGE, is_analysis_task_cancelled
from utils.llm_error_utils import get_user_facing_agent_error
from utils.llm_model_factory import bind_selected_model_id, create_runtime_config, reset_selected_model_id

THINK_BLOCK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
TOOL_CALL_BLOCK_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", flags=re.DOTALL)
UNEXPECTED_KWARG_PATTERN = re.compile(
    r"(?P<owner>[A-Za-z_][A-Za-z0-9_]*)\.__init__\(\) got an unexpected keyword argument '(?P<arg>[^']+)'"
)
MISSING_DEP_PATTERN = re.compile(r"No module named '(?P<module>[^']+)'")
UNRESOLVED_COLUMN_PATTERNS = (
    re.compile(r"Column ['`\"](?P<field>[^'`\"]+)['`\"] cannot be resolved", re.IGNORECASE),
    re.compile(r"Unknown column ['`\"](?P<field>[^'`\"]+)['`\"]", re.IGNORECASE),
    re.compile(r"column ['`\"](?P<field>[^'`\"]+)['`\"] does not exist", re.IGNORECASE),
    re.compile(r"Referenced column ['`\"](?P<field>[^'`\"]+)['`\"] not found", re.IGNORECASE),
)
RELATIVE_SHORT_TIME_PATTERN = re.compile(
    r"(近|最近|过去)\s*(?P<amount>\d+)\s*(?P<unit>分钟|分|小时|时|h|H|min|mins|minute|minutes)"
)
RELATIVE_DATE_TIME_PATTERN = re.compile(
    r"(近|最近|过去)\s*(?P<amount>\d+|[一二两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>天|日|周|星期|月|个月|年)"
)
ANALYSIS_EXPLICIT_TIME_PATTERN = re.compile(
    r"(今天|昨天|前天|本周|上周|本月|上月|今年|去年|(?:近|最近|过去)\s*(?:\d+|[一二两三四五六七八九十]{1,3})\s*(?:天|日|周|星期|月|个月|年)|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}日)"
)
MAX_ANALYSIS_AGENT_ROUNDS = 3
RETRY_LATEST_ERROR_LIMIT = 3
RETRY_PATTERN_HINT_LIMIT = 5
RETRY_ERROR_MAX_CHARS = 700
RETRY_HINT_MAX_CHARS = 500
RETRY_RECOVERY_INSTRUCTION_MAX_CHARS = 14000
PROBE_NO_DATA_MEMORY_LIMIT = 3
PROBE_NO_DATA_ERROR_TYPES = {'probe_feedback', 'no_data_found'}
TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS = (
    'unknown tag',
    'no results',
)
CHART_CONTRACT_ERROR_MARKERS = (
    'chart_contract_error',
    'structured chart item',
    'chart_document',
    'chart_spec',
    'chart_kind',
    'unsupported chart_kind',
    'charts items',
    'xaxis',
    'yaxis',
    'series',
)
REGENERATE_REQUEST_PATTERN = re.compile(
    r'^(重新生成|重试|重新执行|重新分析|重新分析一下|重新分析一次|重新分析一遍|重新跑一次|再生成一次|再来一次)$'
)
HISTORY_REQUIREMENT_RECOVERY_KEYWORDS = (
    '前面对话',
    '前面的需求',
    '前面说的',
    '上面说的',
    '刚才说的',
    '不想再重复',
    '不想重复',
    '我已经说过',
    '已经说过',
    '自己看前面',
    '你自己看前面',
    '接着前面',
    '按之前的需求',
    '之前的需求',
)
NO_DATASOURCE_ANALYSIS_REPLY = (
    '当前会话还没有关联任何数据源，暂时无法执行数据分析。'
    '请先在当前会话中关联相关数据源，然后再发起分析。'
)
@dataclass
class AgentRequest:
    """控制器层传入 Agent 层的标准化请求对象。"""

    username: str
    namespace_id: str
    conversation_id: str
    user_message: str
    auth_token: str = ''
    selected_llm_model_id: str = ''
    database_conn_info: dict[str, Any] | None = None

    @property
    def database_context(self) -> dict[str, Any]:
        return dict(self.database_conn_info or {})


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
    with session_scope(expire_on_commit=False) as session:
        latest_execution = ConversationContextService(session).get_latest_execution(
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


def _month_window(year: int, month: int) -> tuple[Any, Any]:
    start = datetime(year, month, 1).date()
    if month == 12:
        end = datetime(year + 1, 1, 1).date()
    else:
        end = datetime(year, month + 1, 1).date()
    return start, end


def _parse_simple_chinese_number(text: Any) -> int:
    raw_text = str(text or '').strip()
    if not raw_text:
        return 0
    if raw_text.isdigit():
        return int(raw_text)
    digit_map = {
        '一': 1,
        '二': 2,
        '两': 2,
        '三': 3,
        '四': 4,
        '五': 5,
        '六': 6,
        '七': 7,
        '八': 8,
        '九': 9,
    }
    if raw_text == '十':
        return 10
    if '十' in raw_text:
        left, _, right = raw_text.partition('十')
        tens = digit_map.get(left, 1 if left == '' else 0)
        ones = digit_map.get(right, 0) if right else 0
        return tens * 10 + ones
    return digit_map.get(raw_text, 0)


def _shift_month(date_value: Any, months: int) -> Any:
    month_index = date_value.year * 12 + date_value.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(date_value.day, monthrange(year, month)[1])
    return date_value.replace(year=year, month=month, day=day)


def _format_relative_date_time_range(amount: int, unit: str, label: str) -> dict[str, Any]:
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    normalized_unit = str(unit or '').strip()
    if normalized_unit in {'天', '日'}:
        start = today - timedelta(days=amount)
    elif normalized_unit in {'周', '星期'}:
        start = today - timedelta(days=amount * 7)
    elif normalized_unit in {'月', '个月'}:
        start = _shift_month(today, -amount)
    elif normalized_unit == '年':
        try:
            start = today.replace(year=today.year - amount)
        except ValueError:
            start = today.replace(year=today.year - amount, day=28)
    else:
        return {}
    end = today + timedelta(days=1)
    payload = _format_expected_time_range(start, end, label)
    payload.update({
        'relative_window': True,
        'amount': amount,
        'unit': normalized_unit,
    })
    return payload


def _format_expected_time_range(start: Any, end: Any, label: str) -> dict[str, Any]:
    display_end = end - timedelta(days=1)
    return {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'end_exclusive': True,
        'display_end': display_end.isoformat(),
        'source': label,
    }


def _format_relative_short_time_range(amount: int, unit: str, label: str) -> dict[str, Any]:
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(microsecond=0)
    normalized_unit = str(unit or '').strip()
    delta = timedelta(hours=amount) if normalized_unit in {'小时', '时', 'h', 'H'} else timedelta(minutes=amount)
    start = now - delta
    return {
        'start': start.isoformat(),
        'end': now.isoformat(),
        'end_exclusive': False,
        'display_end': now.isoformat(),
        'source': label,
        'relative_window': True,
        'amount': amount,
        'unit': normalized_unit,
    }


def _format_anchor_month_time_range(text: str, today: Any) -> dict[str, Any]:
    match = re.search(r'(?P<anchor>今年|去年)\s*(?P<month>\d{1,2})月(?:(?P<day>\d{1,2})日)?', text)
    if not match:
        return {}
    year = today.year if match.group('anchor') == '今年' else today.year - 1
    month = int(match.group('month'))
    if not 1 <= month <= 12:
        return {}
    day_text = match.group('day')
    if day_text:
        day = int(day_text)
        if not 1 <= day <= monthrange(year, month)[1]:
            return {}
        start = datetime(year, month, day).date()
        return _format_expected_time_range(start, start + timedelta(days=1), match.group(0))
    start, end = _month_window(year, month)
    return _format_expected_time_range(start, end, match.group(0))


def _format_absolute_date_time_range(text: str) -> dict[str, Any]:
    date_match = re.search(r'(?P<year>\d{4})[-/年](?P<month>\d{1,2})(?:[-/月](?P<day>\d{1,2}))?', text)
    if not date_match:
        return {}
    year = int(date_match.group('year'))
    month = int(date_match.group('month'))
    day_text = date_match.group('day')
    if 1 <= month <= 12 and day_text:
        day = int(day_text)
        if 1 <= day <= monthrange(year, month)[1]:
            start = datetime(year, month, day).date()
            return _format_expected_time_range(start, start + timedelta(days=1), date_match.group(0))
    if 1 <= month <= 12:
        start, end = _month_window(year, month)
        return _format_expected_time_range(start, end, date_match.group(0))
    return {}


def _build_expected_time_range_from_user_query(user_query: str) -> dict[str, Any]:
    """Parse a small trusted time-range anchor from explicit user wording."""
    text = str(user_query or '').strip()
    if not text:
        return {}

    relative_match = RELATIVE_SHORT_TIME_PATTERN.search(text)
    if relative_match:
        amount = int(relative_match.group('amount'))
        if amount > 0:
            return _format_relative_short_time_range(
                amount=amount,
                unit=relative_match.group('unit'),
                label=relative_match.group(0),
            )

    relative_date_match = RELATIVE_DATE_TIME_PATTERN.search(text)
    if relative_date_match:
        amount = _parse_simple_chinese_number(relative_date_match.group('amount'))
        if amount > 0:
            return _format_relative_date_time_range(
                amount=amount,
                unit=relative_date_match.group('unit'),
                label=relative_date_match.group(0),
            )

    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    current_week_start = today - timedelta(days=today.weekday())

    explicit_absolute_range = _format_absolute_date_time_range(text)
    if explicit_absolute_range:
        return explicit_absolute_range

    anchored_month_range = _format_anchor_month_time_range(text, today)
    if anchored_month_range:
        return anchored_month_range

    if '本周' in text:
        return _format_expected_time_range(current_week_start, current_week_start + timedelta(days=7), '本周')
    if '上周' in text:
        start = current_week_start - timedelta(days=7)
        return _format_expected_time_range(start, current_week_start, '上周')
    if '今天' in text:
        return _format_expected_time_range(today, today + timedelta(days=1), '今天')
    if '昨天' in text:
        start = today - timedelta(days=1)
        return _format_expected_time_range(start, today, '昨天')
    if '前天' in text:
        start = today - timedelta(days=2)
        return _format_expected_time_range(start, start + timedelta(days=1), '前天')
    if '本月' in text:
        start, end = _month_window(today.year, today.month)
        return _format_expected_time_range(start, end, '本月')
    if '上月' in text:
        year = today.year if today.month > 1 else today.year - 1
        month = today.month - 1 if today.month > 1 else 12
        start, end = _month_window(year, month)
        return _format_expected_time_range(start, end, '上月')
    if '今年' in text:
        start = datetime(today.year, 1, 1).date()
        end = datetime(today.year + 1, 1, 1).date()
        return _format_expected_time_range(start, end, '今年')
    if '去年' in text:
        start = datetime(today.year - 1, 1, 1).date()
        end = datetime(today.year, 1, 1).date()
        return _format_expected_time_range(start, end, '去年')

    return {}


def _date_text(value: Any) -> str:
    text = str(value or '').strip()
    match = re.search(r'\d{4}-\d{1,2}-\d{1,2}', text)
    if not match:
        return ''
    try:
        return datetime.strptime(match.group(0), '%Y-%m-%d').date().isoformat()
    except ValueError:
        return ''


def _is_expected_time_no_data_report(user_query: str, analysis_report: str) -> bool:
    expected_time_range = _build_expected_time_range_from_user_query(user_query)
    if not expected_time_range:
        return False
    report_text = str(analysis_report or '').strip()
    if not report_text:
        return False

    no_data_markers = (
        '无数据',
        '暂无',
        '没有数据',
        '未找到',
        '未命中',
        '没有返回任何数据',
        '未返回任何数据',
    )
    if not any(marker in report_text for marker in no_data_markers):
        return False

    if expected_time_range.get('relative_window'):
        return True

    expected_start = _date_text(expected_time_range.get('start'))
    expected_end = _date_text(expected_time_range.get('end'))
    expected_display_end = _date_text(expected_time_range.get('display_end'))
    accepted_ends = {item for item in (expected_end, expected_display_end) if item}
    return bool(expected_start and expected_start in report_text and any(end in report_text for end in accepted_ends))


def _is_expected_time_no_data_execution(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
) -> bool:
    expected_time_range = _build_expected_time_range_from_user_query(user_message or getattr(runtime.turn, 'user_query', ''))
    if not expected_time_range:
        return False

    with session_scope(expire_on_commit=False) as session:
        executions = ConversationContextService(session).get_turn_executions(
            runtime.turn.id,
            started_at=runtime.turn.started_at,
        )
        if not executions:
            return False
        latest_execution = executions[-1]

    if getattr(latest_execution, 'execution_status', '') == 'success':
        return False
    if _extract_execution_error_type(latest_execution) != 'no_data_found':
        return False

    error_message = (getattr(latest_execution, 'error_message', '') or '').strip()
    if expected_time_range.get('relative_window'):
        return bool(error_message)
    return _is_expected_time_no_data_report(user_message, error_message)


def _build_expected_time_no_data_report(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
    latest_message: str,
    analysis_report: str,
) -> str:
    if _is_expected_time_no_data_report(user_message, analysis_report):
        return analysis_report.strip()
    if _is_expected_time_no_data_report(user_message, latest_message):
        return latest_message.strip()

    latest_error = ''
    with session_scope(expire_on_commit=False) as session:
        executions = ConversationContextService(session).get_turn_executions(
            runtime.turn.id,
            started_at=runtime.turn.started_at,
        )
        if executions:
            latest_error = (getattr(executions[-1], 'error_message', '') or '').strip()

    expected_time_range = _build_expected_time_range_from_user_query(user_message or getattr(runtime.turn, 'user_query', ''))
    start = str(expected_time_range.get('start') or '').strip()
    end = str(expected_time_range.get('display_end') or expected_time_range.get('end') or '').strip()
    source = str(expected_time_range.get('source') or '').strip()
    range_text = f'{source}（{start} 至 {end}）' if source and start and end else f'{start} 至 {end}'.strip(' 至')

    lines = ['## 分析结果', '当前问题指定的时间范围内暂无可分析数据。']
    if range_text:
        lines.extend(['### 时间范围', range_text])
    if latest_error:
        lines.extend(['### 详情', latest_error])
    lines.extend(['### 建议', '请确认该时间范围内的数据是否已经入库，或调整为有数据覆盖的时间范围后重新分析。'])
    return '\n\n'.join(item for item in lines if item).strip()


def _build_agent_context(
    agent_request: AgentRequest,
    runtime: ConversationRunContext,
    preparation: PreparationResult | None = None,
) -> CustomContext:
    """构造 Agent 与工具层使用的运行上下文。"""
    user_query = getattr(runtime.turn, 'user_query', '') or agent_request.user_message or ''
    return CustomContext(
        username=agent_request.username,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        user_query=user_query,
        expected_time_range=_build_expected_time_range_from_user_query(user_query),
        auth_token=agent_request.auth_token or '',
        database_conn_info=dict(agent_request.database_conn_info or {}),
        active_datasource_snapshot=dict(
            preparation.prompt_snapshot()
            if preparation is not None
            else runtime.active_datasource_snapshot or {}
        ),
        analysis_task_id=runtime.analysis_task_id,
    )


def _raise_if_analysis_task_cancelled(runtime: ConversationRunContext) -> None:
    analysis_task_id = int(getattr(runtime, 'analysis_task_id', 0) or 0)
    if analysis_task_id > 0 and is_analysis_task_cancelled(task_pk=analysis_task_id):
        raise AnalysisTaskCancelled(CANCELLED_MESSAGE)


def _build_agent_input_with_runtime_instruction(
    agent_request: AgentRequest,
    runtime: ConversationRunContext,
    runtime_instruction: str = '',
    preparation: PreparationResult | None = None,
):
    """在必要时追加一条很短的运行时修复指令。"""
    extra_system_messages = [runtime_instruction] if runtime_instruction else []
    return get_input(
        agent_request.user_message,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        extra_system_messages=extra_system_messages or None,
        history_turn_limit=runtime.history_turn_limit,
        datasource_snapshot_override=(
            preparation.prompt_snapshot()
            if preparation is not None
            else runtime.active_datasource_snapshot
        ),
        context_mode=getattr(runtime, 'context_mode', None),
    )


def _resolve_agent_runtime_config(agent_request: AgentRequest):
    return create_runtime_config(selected_model_id=agent_request.selected_llm_model_id)


def _prepare_analysis_context(
    *,
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
    runtime_config,
) -> PreparationResult:
    """Select and enrich datasource Schema without owning insight semantics."""
    cached = runtime.short_term_memory.get("preparation_result")
    if isinstance(cached, PreparationResult):
        return cached

    user_query = getattr(runtime.turn, "user_query", "") or agent_request.user_message
    conversation_context: dict[str, Any] = {}
    previous_turn_no = max(int(getattr(runtime.turn, "turn_no", 0) or 0) - 1, 0)
    if previous_turn_no > 0 and getattr(runtime, "context_mode", "") == "followup":
        with session_scope(expire_on_commit=False) as session:
            recent_messages = ConversationContextService(session).get_recent_prompt_messages(
                runtime.conversation.id,
                limit_messages=6,
                max_turn_no=previous_turn_no,
            )
        conversation_context["recent_messages"] = [
            {
                "role": getattr(item, "role", ""),
                "content": str(getattr(item, "content", "") or "")[:800],
            }
            for item in recent_messages
        ]

    source_snapshot = dict(runtime.active_datasource_snapshot or {})
    expected_time_range = _build_expected_time_range_from_user_query(user_query)
    try:
        result = prepare_analysis(
            user_query=user_query,
            context_mode=getattr(runtime, "context_mode", "") or "fresh_analysis",
            expected_time_range=expected_time_range,
            datasource_snapshot=source_snapshot,
            conversation_context=conversation_context,
            runtime_config=runtime_config,
            execution_context={
                "token": agent_request.auth_token or "",
                "database_conn_info": dict(agent_request.database_conn_info or {}),
                "namespace_id": int(runtime.conversation.insight_namespace_id or 0),
                "conversation_id": int(runtime.conversation.id or 0),
                "turn_id": int(runtime.turn.id or 0),
            },
            cancelled=lambda: _raise_if_analysis_task_cancelled(runtime),
            stage_callback=lambda stage: logger.info(
                "analysis preparation stage started: conversation_id=%s turn_id=%s stage=%s",
                runtime.conversation.id,
                runtime.turn.id,
                stage,
            ),
        )
    except AnalysisTaskCancelled:
        raise
    except Exception as exc:
        logger.warning(
            "analysis preparation unavailable; preserving original datasource context: "
            "conversation_id=%s turn_id=%s error=%s",
            runtime.conversation.id,
            runtime.turn.id,
            exc,
        )
        selected_ids = [
            int(item)
            for item in source_snapshot.get("selected_datasource_ids") or []
            if int(item or 0) > 0
        ]
        if not selected_ids:
            selected_ids = [
                int(item.get("datasource_id") or item.get("id") or 0)
                for item in source_snapshot.get("selected_datasource_snapshot") or []
                if isinstance(item, dict)
                and int(item.get("datasource_id") or item.get("id") or 0) > 0
            ]
        result = PreparationResult(
            intent=IntentContract(
                business_goal=user_query or "完成本轮数据洞察分析",
                business_questions=[BusinessQuestionContract(
                    question_id="question_1",
                    business_question=user_query or "完成本轮数据洞察分析",
                    required_business_facts=[BusinessFactRequirementContract(
                        fact_id="question_1_fact_1",
                        business_fact=user_query or "完成本轮数据洞察分析",
                        semantic_definition="用户当前明确要求理解的业务事实",
                    )],
                )],
            ),
            selection=DatasourceSelectionContract(
                selected_datasource_ids=selected_ids,
                question_coverage=[BusinessQuestionCoverageContract(
                    question_id="question_1",
                    datasource_ids=selected_ids,
                    fact_coverage=[BusinessFactCoverageContract(
                        fact_id="question_1_fact_1",
                        coverage_status="direct",
                        datasource_ids=selected_ids,
                        coverage_reason="保留原版数据源范围供正式分析核验",
                    )],
                    coverage_reason="Preparation 不可用，保留原版数据源范围",
                )],
                selection_reason="Preparation 不可用，保留原版数据源范围",
            ),
            business_context=BusinessContextContract(
                unresolved_relationships=["数据源关系尚未在 Preparation 中确认"]
                if len(selected_ids) > 1
                else [],
            ),
            selected_snapshot=source_snapshot,
            query_capabilities=infer_query_capabilities(source_snapshot),
            explicit_time_range=expected_time_range or None,
        )
    source_ids = {
        int(item.get('datasource_id') or item.get('id') or 0)
        for item in source_snapshot.get('selected_datasource_snapshot') or []
        if isinstance(item, dict)
        and int(item.get('datasource_id') or item.get('id') or 0) > 0
    }
    prepared_rows = [
        dict(item)
        for item in result.selected_snapshot.get('selected_datasource_snapshot') or []
        if isinstance(item, dict)
    ]
    prepared_ids = {
        int(item.get('datasource_id') or item.get('id') or 0)
        for item in prepared_rows
        if int(item.get('datasource_id') or item.get('id') or 0) > 0
    }
    if prepared_ids - source_ids:
        with session_scope(expire_on_commit=False) as session:
            ConversationContextService(session).merge_turn_datasource_snapshot(
                turn_id=int(runtime.turn.id or 0),
                datasource_snapshot=prepared_rows,
            )
    runtime.short_term_memory["preparation_result"] = result
    return result


def _resolve_runtime_context_mode(runtime: ConversationRunContext, user_message: str) -> str:
    if runtime.is_rerun:
        context_mode = classify_analysis_context_mode(user_message)
        runtime.context_mode = context_mode
        logger.info(
            "Resolved analysis context mode for rerun locally: conversation_id=%s turn_id=%s mode=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            context_mode,
        )
        return context_mode

    classification = resolve_context_mode_with_llm(runtime, user_message)
    runtime.context_mode = classification.mode
    logger.info(
        "Resolved analysis context mode: conversation_id=%s turn_id=%s mode=%s source=%s confidence=%s reason=%s",
        getattr(runtime.conversation, 'id', ''),
        getattr(runtime.turn, 'id', ''),
        classification.mode,
        classification.source,
        classification.confidence,
        classification.reason,
    )
    return classification.mode


def _is_analysis_request_expected(runtime: ConversationRunContext, user_message: str) -> bool:
    if runtime.is_rerun:
        return True
    if _is_regenerate_request(user_message):
        return True
    context_mode = getattr(runtime, 'context_mode', '') or classify_analysis_context_mode(user_message)
    return context_mode in {'followup', 'fresh_analysis'}


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

    with session_scope(expire_on_commit=False) as session:
        latest_turn = ConversationContextService(session).get_latest_successful_analysis_turn(
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


def _is_history_requirement_recovery_request(user_message: str) -> bool:
    text = re.sub(r'\s+', '', (user_message or '').strip())
    if not text:
        return False
    return any(keyword in text for keyword in HISTORY_REQUIREMENT_RECOVERY_KEYWORDS)


def _clip_inline_text(value: Any, max_chars: int = 240) -> str:
    text = re.sub(r'\s+', ' ', str(value or '').strip())
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + '...'


def _build_datasource_reference_lines(runtime: ConversationRunContext) -> list[str]:
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = snapshot.get('selected_datasource_snapshot') or []
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        datasource_id = row.get('datasource_id') or row.get('id') or ''
        datasource_name = str(row.get('datasource_name') or '').strip()
        datasource_identifier = str(row.get('datasource_identifier') or '').strip()
        if not datasource_id or not datasource_name:
            continue
        if datasource_identifier:
            lines.append(f'- datasource_id={datasource_id}，datasource_name="{datasource_name}"，datasource_identifier="{datasource_identifier}"')
        else:
            lines.append(f'- datasource_id={datasource_id}，datasource_name="{datasource_name}"')
    return lines


def _normalize_match_text(value: Any) -> str:
    return re.sub(r'\s+', '', str(value or '').lower())


def _datasource_match_tokens(item: dict[str, Any]) -> list[str]:
    raw_values = [
        item.get('datasource_name'),
        item.get('datasource_identifier'),
        item.get('table_name'),
        item.get('sheet_name'),
    ]
    metadata = item.get('metadata_schema') if isinstance(item.get('metadata_schema'), dict) else {}
    raw_values.append(metadata.get('name'))

    tokens: list[str] = []
    for value in raw_values:
        text = str(value or '').strip()
        if not text:
            continue
        candidates = {text}
        for sep in ('/', '\\', '.', ':'):
            if sep in text:
                tail = text.rstrip('/\\').split(sep)[-1].strip()
                if tail:
                    candidates.add(tail)
        for candidate in candidates:
            normalized = _normalize_match_text(candidate)
            if len(normalized) >= 3 and normalized not in tokens:
                tokens.append(normalized)
    return tokens


def _build_possible_datasource_selection_hints(
    runtime: ConversationRunContext,
    executions: list[Any],
) -> list[str]:
    """提示模型可能漏选了语义相关的数据源；不做 SQL 静态访问校验。"""
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = snapshot.get('selected_datasource_snapshot') or []
    selected_items = [item for item in rows if isinstance(item, dict)]
    if not selected_items:
        return []

    analysis_used_ids: set[int] = set()
    scan_parts = [getattr(runtime.turn, 'user_query', '') or '']
    untracked_mentions_by_id: dict[int, dict[str, Any]] = {}
    for execution in executions:
        generated_code = getattr(execution, 'generated_code', '') or ''
        payload = _extract_execution_payload(execution)
        is_probe = 'execution_intent = "probe"' in generated_code or payload.get('retry_type') == 'probe_feedback'
        if not is_probe:
            analysis_used_ids.update(
                int(item)
                for item in safe_json_loads(getattr(execution, 'used_datasource_ids_json', ''), [])
                if isinstance(item, int) or str(item).isdigit()
            )
        diagnostics = payload.get('diagnostics') if isinstance(payload.get('diagnostics'), dict) else {}
        for mention in diagnostics.get('untracked_datasource_mentions') or []:
            if not isinstance(mention, dict):
                continue
            datasource_id = int(mention.get('datasource_id') or 0)
            if datasource_id > 0:
                untracked_mentions_by_id[datasource_id] = mention
        scan_parts.extend([
            getattr(execution, 'error_message', '') or '',
            generated_code,
            getattr(execution, 'analysis_report', '') or '',
            json.dumps(payload, ensure_ascii=False) if payload else '',
        ])

    scan_text = _normalize_match_text('\n'.join(str(part or '') for part in scan_parts))
    user_text = _normalize_match_text(getattr(runtime.turn, 'user_query', '') or '')
    hints: list[str] = []
    for item in selected_items:
        datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        if datasource_id <= 0 or datasource_id in analysis_used_ids:
            continue
        matched_tokens = [token for token in _datasource_match_tokens(item) if token and token in scan_text]
        untracked_mention = untracked_mentions_by_id.get(datasource_id)
        if not matched_tokens and not untracked_mention:
            continue
        datasource_name = str(item.get('datasource_name') or '').strip()
        datasource_identifier = str(item.get('datasource_identifier') or '').strip()
        name_match_text = _normalize_match_text(datasource_name)
        looks_like_plan_or_target = any(token in name_match_text for token in ('计划', '目标', '标准', '额定'))
        looks_like_status_or_rate_question = any(token in user_text for token in ('情况', '开机', '运行', '稼动', '效率', '率', '达成', '完成'))
        if untracked_mention and looks_like_plan_or_target and looks_like_status_or_rate_question:
            hint = (
                f'此前正式分析结果已经提到已选数据源 datasource_id={datasource_id}'
                f'（datasource_name="{datasource_name}"），且该数据源语义上是计划/目标口径。'
                '当前用户问题属于设备开机情况/运行情况类分析，下一版正式 analysis 应优先把实际数据源与该计划/目标数据源联合查询或对齐，'
                f'并把 datasource_id={datasource_id} 加入 used_datasource_ids。'
            )
        else:
            hint = (
                f'用户问题或失败记录命中了已选数据源 datasource_id={datasource_id}'
                f'（datasource_name="{datasource_name}"），但本轮正式 analysis 的 used_datasource_ids 未包含它。'
                '下一版请重新规划数据源选择；如果该数据源与问题相关，应实际查询它并加入 used_datasource_ids。'
            )
        if datasource_identifier:
            hint += f' 访问时必须使用 datasource_identifier="{datasource_identifier}"，不要把 datasource_name 当作 SQL 表名。'
        hints.append(hint)
        if len(hints) >= 5:
            break
    return hints


def _load_recent_prompt_messages_fresh(
    runtime: ConversationRunContext,
    *,
    limit_messages: int,
    max_turn_no: int | None,
) -> list[Any]:
    try:
        with session_scope(expire_on_commit=False) as session:
            return ConversationContextService(session).get_recent_prompt_messages(
                runtime.conversation.id,
                limit_messages=limit_messages,
                max_turn_no=max_turn_no,
            )
    except SQLAlchemyError:
        logger.warning(
            "读取历史消息失败，跳过历史需求恢复上下文: conversation_id=%s turn_id=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            exc_info=True,
        )
        return []


def _build_history_requirement_recovery_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
) -> str:
    """把“看前面对话/不想重复”这类省略请求恢复成可执行分析任务。"""
    if not _is_history_requirement_recovery_request(user_message):
        return ''
    if (getattr(runtime, 'context_mode', '') or classify_analysis_context_mode(user_message)) != 'followup':
        return ''

    max_turn_no = max(int(getattr(runtime.turn, 'turn_no', 0) or 0) - 1, 0)
    if service is not None:
        rows = service.get_recent_prompt_messages(
            runtime.conversation.id,
            limit_messages=12,
            max_turn_no=max_turn_no,
        )
    else:
        rows = _load_recent_prompt_messages_fresh(runtime, limit_messages=12, max_turn_no=max_turn_no)
    if not rows:
        return ''

    user_rows = [
        row for row in rows
        if getattr(row, 'role', '') == 'user' and (getattr(row, 'content', '') or '').strip()
    ]
    if not user_rows:
        return ''

    latest_analysis_user = next(
        (
            row for row in reversed(user_rows)
            if is_analysis_like_request(getattr(row, 'content', '') or '')
            and not _is_history_requirement_recovery_request(getattr(row, 'content', '') or '')
        ),
        None,
    )
    latest_user_correction = next(
        (
            row for row in reversed(user_rows)
            if row is not latest_analysis_user
            and not _is_history_requirement_recovery_request(getattr(row, 'content', '') or '')
        ),
        None,
    )

    lines = [
        '当前用户输入是在要求你查看并承接前面对话需求，不是普通闲聊，也不是让用户重新复述。',
        '请先从历史消息中恢复本轮真实分析目标，再基于当前会话已关联的数据源重新执行分析。',
    ]
    if latest_analysis_user is not None:
        lines.append(
            f'最近一个明确分析需求来自第 {getattr(latest_analysis_user, "turn_no", "")} 轮：'
            f'{_clip_inline_text(getattr(latest_analysis_user, "content", ""), 500)}'
        )
    if latest_user_correction is not None:
        lines.append(
            f'最近的用户补充/纠错来自第 {getattr(latest_user_correction, "turn_no", "")} 轮：'
            f'{_clip_inline_text(getattr(latest_user_correction, "content", ""), 500)}'
        )
    lines.extend([
        '如果最近补充/纠错指出了遗漏的数据源、字段、关联键或计算口径，必须合并进本轮恢复后的分析目标。',
        '不要回答“没有历史对话上下文”，也不要要求用户重新描述完整需求；当前链路已经提供了历史消息、历史摘要和当前数据源快照。',
        '如果恢复出的目标需要数据分析、统计、联表、图表或报告，必须调用 execute_python 重新执行，而不是复述历史结论。',
        '访问表型数据源时，datasource_name 只用于语义匹配；真正写 SQL 必须使用 datasource_identifier。',
    ])
    datasource_lines = _build_datasource_reference_lines(runtime)
    if datasource_lines:
        lines.append('当前已选数据源如下，请按用户语义选择真正相关的数据源：')
        lines.extend(datasource_lines)
    return '\n'.join(lines)


def _load_latest_successful_followup_anchor(
    runtime: ConversationRunContext,
) -> dict[str, Any]:
    max_turn_no = max(int(getattr(runtime.turn, 'turn_no', 0) or 0) - 1, 0)
    try:
        with session_scope(expire_on_commit=False) as session:
            service = ConversationContextService(session)
            latest_turn = service.get_latest_successful_analysis_turn(
                runtime.conversation.id,
                max_turn_no=max_turn_no,
            )
            if latest_turn is None:
                return {}

            from model import InsightNsExecution

            latest_execution = session.query(InsightNsExecution).filter(
                InsightNsExecution.conversation_id == runtime.conversation.id,
                InsightNsExecution.turn_id == latest_turn.id,
                InsightNsExecution.execution_status == 'success',
                InsightNsExecution.is_deleted == 0,
                InsightNsExecution.content_enabled == 1,
            ).order_by(
                InsightNsExecution.created_at.desc(),
                InsightNsExecution.id.desc(),
            ).first()
            if latest_execution is None:
                return {}

            result_payload = safe_json_loads(latest_execution.result_payload_json, {})
            if not isinstance(result_payload, dict):
                result_payload = {}
            return {
                "turn_no": getattr(latest_turn, 'turn_no', 0) or 0,
                "turn_id": getattr(latest_turn, 'id', 0) or 0,
                "question": getattr(latest_turn, 'user_query', '') or '',
                "execution_id": getattr(latest_execution, 'id', 0) or 0,
                "title": getattr(latest_execution, 'title', '') or '',
                "used_datasource_ids": safe_json_loads(
                    getattr(latest_execution, 'used_datasource_ids_json', ''),
                    [],
                ),
                "analysis_report": (
                    getattr(latest_execution, 'analysis_report', '') or result_payload.get('analysis_report', '') or ''
                ),
                "chart_count": len(result_payload.get('charts') or []),
                "table_count": len(result_payload.get('tables') or []),
            }
    except Exception:
        logger.warning(
            "读取 followup 承接锚点失败: conversation_id=%s turn_id=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            exc_info=True,
        )
        return {}


def _build_followup_used_datasource_lines(
    runtime: ConversationRunContext,
    used_datasource_ids: list[Any],
) -> list[str]:
    used_ids = {int(item) for item in used_datasource_ids if str(item).isdigit()}
    if not used_ids:
        return []
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    selected_items = snapshot.get('selected_datasource_snapshot') or []
    lines: list[str] = []
    for item in selected_items:
        if not isinstance(item, dict):
            continue
        datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        if datasource_id not in used_ids:
            continue
        datasource_name = str(item.get('datasource_name') or '').strip()
        datasource_identifier = str(item.get('datasource_identifier') or '').strip()
        if datasource_identifier:
            lines.append(
                f'- datasource_id={datasource_id}, datasource_name="{datasource_name}", '
                f'datasource_identifier="{datasource_identifier}"'
            )
        else:
            lines.append(f'- datasource_id={datasource_id}, datasource_name="{datasource_name}"')
    return lines


def _build_followup_hint_instruction(
    user_message: str,
    context_mode: str | None = None,
    runtime: ConversationRunContext | None = None,
) -> str:
    """给 LLM 一个弱提示：本地分类认为本轮可能在承接上文。"""
    if (context_mode or classify_analysis_context_mode(user_message)) != 'followup':
        return ''
    lines = [
        '系统前置判断：当前用户输入可能是在承接上一轮或最近一次分析结果；该判断只是启发式提示，不一定完全准确。',
        '请结合历史消息、最近执行记录、当前问题和数据源上下文，自行确认本轮是否确实属于 followup。',
        '如果确认是承接问题，应优先参考最近成功执行的 used_datasource_ids、分析报告、图表/表格产物和代码。',
        '如果当前问题明确提出新的分析对象、新数据源、新时间范围、新指标，或要求覆盖全部/所有数据源，则应按新的分析目标重新规划。',
        '不要因为本提示而机械沿用上一轮数据源；也不要在没有明确新范围时无理由扩展到全部 selected_datasource_ids。',
    ]
    if runtime is None:
        return '\n'.join(lines)

    anchor_lines = _build_followup_anchor_instruction_lines(runtime)
    if anchor_lines:
        lines.extend(anchor_lines)
        lines.extend([
            '',
            '本轮 followup 规划要求：',
            '- 如果当前用户只是要求换图表、补图表、补报告、换展示方式、继续展示，且没有明确新增数据源/对象/时间范围/指标，优先围绕上述最近成功 used_datasource_ids 重新生成结果。',
            '- 如果存在 analysis_semantic_context，selected_datasource_ids 是 Preparation 为本轮追问确定的业务语义空间；否则它才是当前会话候选全集。应优先延续最近成功 used_datasource_ids，同时完整覆盖本轮明确新增的业务问题。',
            '- 对于表型数据源，SQL 必须使用 datasource_identifier；不要根据其它 selected 数据源名称自行猜 catalog、schema 或表名。',
        ])
    return '\n'.join(lines)


def _build_followup_anchor_instruction_lines(runtime: ConversationRunContext) -> list[str]:
    anchor = _load_latest_successful_followup_anchor(runtime)
    used_ids = anchor.get('used_datasource_ids') if isinstance(anchor, dict) else []
    if not anchor or not used_ids:
        return []

    lines = [
        '',
        '最近成功分析承接锚点：',
        f'- 最近成功轮次：第 {anchor.get("turn_no", "")} 轮，turn_id={anchor.get("turn_id", "")}',
        f'- 最近成功问题：{_clip_inline_text(anchor.get("question", ""), 260)}',
        f'- 最近成功执行：execution_id={anchor.get("execution_id", "")}, title="{_clip_inline_text(anchor.get("title", ""), 120)}"',
        f'- 最近成功执行实际 used_datasource_ids={used_ids}',
        f'- 最近成功结果规模：charts={anchor.get("chart_count", 0)}, tables={anchor.get("table_count", 0)}',
    ]
    datasource_lines = _build_followup_used_datasource_lines(runtime, used_ids)
    if datasource_lines:
        lines.append('- 最近成功执行实际访问的数据源：')
        lines.extend(datasource_lines)
    report_preview = _clip_inline_text(anchor.get('analysis_report', ''), 500)
    if report_preview:
        lines.append(f'- 最近成功分析报告摘要：{report_preview}')
    return lines


def _rerun_has_prior_analysis_result(
    service: ConversationContextService,
    runtime: ConversationRunContext,
) -> bool:
    """判断被刷新的原轮次是否已有真实分析执行或产物。"""
    if not runtime.is_rerun:
        return False

    from model import InsightNsArtifact, InsightNsExecution

    def _has_prior(session):
        started_at = getattr(runtime.turn, 'started_at', None)
        execution_query = session.query(InsightNsExecution.id).filter(
            InsightNsExecution.conversation_id == runtime.conversation.id,
            InsightNsExecution.turn_id == runtime.turn.id,
            InsightNsExecution.execution_status == 'success',
            InsightNsExecution.is_deleted == 0,
        )
        if started_at is not None:
            execution_query = execution_query.filter(InsightNsExecution.created_at < started_at)
        if execution_query.first() is not None:
            return True

        artifact_query = session.query(InsightNsArtifact.id).filter(
            InsightNsArtifact.conversation_id == runtime.conversation.id,
            InsightNsArtifact.turn_id == runtime.turn.id,
            InsightNsArtifact.is_deleted == 0,
        )
        if started_at is not None:
            artifact_query = artifact_query.filter(InsightNsArtifact.created_at < started_at)
        return artifact_query.first() is not None

    with session_scope(expire_on_commit=False) as session:
        return _has_prior(session)


def _should_require_rerun_analysis_execution(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    analysis_flow_started: bool,
) -> bool:
    """刷新时只有存在分析链路证据，才强制必须完成 execute_python 产物闭环。"""
    if not runtime.is_rerun:
        return False
    if analysis_flow_started:
        return True
    return _rerun_has_prior_analysis_result(service, runtime)


def _build_rerun_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
) -> str:
    """
    为“分析刷新/原轮次重跑”补一条首轮运行时指令。

    这里不改变原问题文本，只明确告诉模型：
    当前不是普通续问，也不是让它复述历史答案，而是要基于同一轮问题重新执行分析。
    """
    if not runtime.is_rerun:
        return ''

    original_user_query = getattr(runtime.turn, 'user_query', '') or ''
    if _is_regenerate_request(original_user_query):
        with session_scope(expire_on_commit=False) as session:
            latest_turn = ConversationContextService(session).get_latest_successful_analysis_turn(
                runtime.conversation.id,
                max_turn_no=runtime.history_turn_limit,
            )
        if latest_turn is not None and (latest_turn.user_query or '').strip():
            return (
                '当前操作是“刷新分析”，但本轮原始问题本身是“重新分析/重试”这类省略指令。'
                f'请把本次刷新目标还原为最近一次成功分析任务：第 {latest_turn.turn_no} 轮'
                f'「{(latest_turn.user_query or "").strip()}」。'
                '不要把“重新分析”本身当作分析对象，也不要复述历史结论；'
                '必须重新调用 execute_python，基于当前会话数据源重新完成该分析目标并生成新的分析结果。'
            )

    if not _rerun_has_prior_analysis_result(service, runtime):
        return (
            '当前操作是“刷新回复”，需要基于本轮原始问题重新生成回答。'
            '请根据本轮原始问题自行判断是否需要调用工具；'
            '如果只是问候、闲聊、概念解释或普通问答，可以直接用自然语言回答并结束。'
        )

    lines = [
        '当前操作是“刷新分析”，需要在同一轮内重新执行这条分析请求。',
        '刷新分析不是用户发起的新问题，也不是新的普通追问；不要重新判断成全新分析目标。',
        '本轮分析目标只能来自本轮原始问题；历史上下文只能用于理解背景、数据口径和歧义消解，',
        '不得把历史轮次的问题、结论、图表或表格追加、合并成本轮新的分析目标。',
        '请基于本轮原始问题和当前会话数据源重新完成分析，',
        '不要直接复述之前的结论，必须重新调用 execute_python 生成新的分析结果。',
    ]
    original_context_mode = getattr(runtime, 'context_mode', '') or classify_analysis_context_mode(
        getattr(runtime.turn, 'user_query', '') or ''
    )
    if original_context_mode == 'followup':
        anchor_lines = _build_followup_anchor_instruction_lines(runtime)
        if anchor_lines:
            lines.extend(anchor_lines)
            lines.extend([
                '',
                '本轮是刷新一个 followup 分析结果：',
                '- 如果本轮原始问题只是换图表、补图表、补报告、换展示方式或继续展示，刷新时必须优先围绕上述最近成功 used_datasource_ids 重新生成结果。',
                '- 如果存在 analysis_semantic_context，selected_datasource_ids 是 Preparation 为本轮原始问题确定的业务语义空间；否则它才是当前会话候选全集。刷新不得把范围内的数据源自行解释成新的分析目标。',
                '- 只有本轮原始问题明确新增数据源、对象、时间范围或指标时，才允许扩展到其它候选数据源。',
            ])
    return '\n'.join(lines)


def _has_bound_datasource(runtime: ConversationRunContext) -> bool:
    """判断当前会话快照中是否有可用于本轮分析的数据源。"""
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    selected_ids = [
        item
        for item in snapshot.get('selected_datasource_ids', [])
        if item
    ]
    selected_items = [
        item
        for item in snapshot.get('selected_datasource_snapshot', [])
        if isinstance(item, dict) and item
    ]
    return bool(selected_ids or selected_items)


def _should_finish_without_datasource(runtime: ConversationRunContext, user_message: str) -> bool:
    """分析型请求在当前会话无绑定数据源时，不进入 execute_python。"""
    return _is_analysis_request_expected(runtime, user_message) and not _has_bound_datasource(runtime)


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
        raise ValueError(_build_empty_response_error(runtime, user_message, analysis_flow_started))
    if _is_analysis_request_expected(runtime, user_message) and not analysis_flow_started and not allow_report_only:
        raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

    run_result = service.complete_run(
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        assistant_message=assistant_message,
        analysis_report=analysis_report,
        charts=charts,
        tables=tables,
        replace_existing_results=runtime.is_rerun,
        analysis_task_id=runtime.analysis_task_id,
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


def _has_latest_execution(runtime: ConversationRunContext) -> bool:
    def _operation(session):
        return ConversationContextService(session).get_latest_execution(
            runtime.turn.id,
            started_at=runtime.turn.started_at,
        ) is not None

    with session_scope(expire_on_commit=False) as session:
        return _operation(session)


def _open_fresh_persistence_context(
    session,
    runtime: ConversationRunContext,
) -> tuple[ConversationContextService, ConversationRunContext]:
    """Reload the turn with a new DB session before final persistence."""
    from model import InsightNsConversation, InsightNsTurn

    conversation = session.query(InsightNsConversation).filter(
        InsightNsConversation.id == runtime.conversation.id,
        InsightNsConversation.is_deleted == 0,
    ).first()
    turn = session.query(InsightNsTurn).filter(
        InsightNsTurn.id == runtime.turn.id,
        InsightNsTurn.conversation_id == runtime.conversation.id,
        InsightNsTurn.is_deleted == 0,
    ).first()
    if conversation is None or turn is None:
        raise ValueError('分析轮次不存在或已被删除')

    active_snapshot = dict(runtime.active_datasource_snapshot or {})
    if not active_snapshot:
        active_snapshot = {
            "namespace_id": conversation.insight_namespace_id,
            "conversation_id": conversation.id,
            "selected_datasource_ids": safe_json_loads(turn.selected_datasource_ids_json, []),
            "selected_datasource_snapshot": safe_json_loads(turn.selected_datasource_snapshot_json, []),
        }
    fresh_runtime = ConversationRunContext(
        conversation=conversation,
        turn=turn,
        active_datasource_snapshot=active_snapshot,
        is_rerun=runtime.is_rerun,
        history_turn_limit=runtime.history_turn_limit,
        analysis_task_id=runtime.analysis_task_id,
        context_mode=getattr(runtime, 'context_mode', '') or '',
    )
    return ConversationContextService(session), fresh_runtime


def _finalize_run_with_fresh_session(
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
    def _operation(session):
        _raise_if_analysis_task_cancelled(runtime)
        service, fresh_runtime = _open_fresh_persistence_context(session, runtime)
        _raise_if_analysis_task_cancelled(fresh_runtime)
        return _finalize_run(
            service=service,
            runtime=fresh_runtime,
            username=username,
            user_message=user_message,
            analysis_flow_started=analysis_flow_started,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            charts=charts,
            tables=tables,
            allow_report_only=allow_report_only,
        )

    with session_scope(expire_on_commit=False) as session:
        return _operation(session)


def _fail_run_with_fresh_session(
    runtime: ConversationRunContext,
    error_message: str,
    preserve_existing_results: bool = False,
) -> None:
    def _operation(session):
        service, fresh_runtime = _open_fresh_persistence_context(session, runtime)
        service.fail_run(
            fresh_runtime.conversation.id,
            fresh_runtime.turn.id,
            error_message,
            preserve_existing_results=preserve_existing_results,
        )

    with session_scope(expire_on_commit=False) as session:
        _operation(session)


def _build_empty_response_error(
    runtime: ConversationRunContext,
    user_message: str,
    analysis_flow_started: bool,
) -> str:
    """按请求类型生成更准确的兜底错误。"""
    if analysis_flow_started:
        return '本轮分析已启动，但未生成可展示结果，请重试。'
    if _is_analysis_request_expected(runtime, user_message):
        return '本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。'
    return '模型本轮没有返回有效回复，请重试。'


def _ensure_analysis_result_ready(
    analysis_report: str,
    charts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
) -> None:
    """分析型请求必须真实产出结构化分析结果。"""
    if analysis_report and ((charts or []) or (tables or [])):
        return
    raise ValueError('本轮未生成完整分析产物：缺少结构化图表/表格结果或分析报告，请重新执行分析。')


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


def _load_turn_executions_fresh(runtime: ConversationRunContext) -> list[Any]:
    """Load execution history with a short-lived session to avoid stale long-running connections."""
    def _operation(session):
        return ConversationContextService(session).get_turn_executions(
            runtime.turn.id,
            started_at=runtime.turn.started_at,
        )

    try:
        with session_scope(expire_on_commit=False) as session:
            return _operation(session)
    except SQLAlchemyError:
        logger.warning(
            "读取轮次执行历史失败，跳过本次恢复提示上下文: conversation_id=%s turn_id=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            exc_info=True,
        )
        return []


def _build_failure_reason_reply(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    user_message: str,
) -> str:
    """当最后没有自然语言回复时，构造一条简短的失败原因说明。"""
    executions = _load_turn_executions_fresh(runtime)
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
    tables: list[dict[str, Any]] | None = None,
) -> bool:
    """
    判断当前轮次是否已经进入真实的数据分析执行链。

    这里不再额外请求模型做意图分流，只依据本轮是否出现过工具执行痕迹来判断。
    """
    if _has_latest_execution(runtime):
        return True
    if raw_tool_call_detected:
        return True
    if runtime.is_rerun:
        return False
    if analysis_report or (charts or []) or (tables or []):
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


def _clip_retry_text(text: Any, max_chars: int) -> str:
    value = str(text or '')
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    suffix = f'\n...[truncated {len(value) - max_chars} chars for retry context budget]'
    return value[:max(0, max_chars - len(suffix))] + suffix


def _is_chart_contract_error_text(text: str) -> bool:
    lowered = (text or '').lower()
    if any(marker in lowered for marker in (
        'chart_contract_error',
        'chart_semantic_error',
        'structured chart item',
        'chart_document',
        'chart_spec',
        'chart_kind',
        'unsupported chart_kind',
        'charts items',
        'xaxis',
        'yaxis',
    )):
        return True
    return 'series' in lowered and any(marker in lowered for marker in ('chart', 'echarts', 'axis'))


def _normalize_failure_for_summary(error_message: str) -> str:
    normalized = re.sub(r'\s+', ' ', (error_message or '').strip())
    return _clip_retry_text(normalized, 180)


def _build_failure_frequency_summary(executions: list[Any]) -> list[str]:
    errors = [
        _normalize_failure_for_summary(getattr(execution, 'error_message', '') or '')
        for execution in executions
        if getattr(execution, 'execution_status', '') != 'success'
        and (getattr(execution, 'error_message', '') or '').strip()
    ]
    counts = Counter(item for item in errors if item)
    return [
        f'{message} × {count}'
        for message, count in counts.most_common(5)
    ]


def _build_failed_pattern_hints(executions: list[Any]) -> list[str]:
    """从当前轮失败记录中提取“不要重复犯错”的提示。"""
    hints: list[str] = []
    seen: set[str] = set()

    for execution in executions:
        error_message = (getattr(execution, 'error_message', '') or '').strip()
        if not error_message:
            continue

        if _is_chart_contract_error_text(error_message):
            hint = (
                '当前失败属于图表产物契约错误：不要继续使用 matplotlib/base64 或手写 chart_spec；'
                '下一版必须改用 build_chart_result(...) 或 build_chart_suite(...) 生成 charts。'
            )
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)
            continue

        if _execution_has_time_series_empty_source_error(execution):
            hint = '已有时序数据源返回 unknown tag/no results：该数据源本轮视为无可分析数据，应从后续独立数据源分析中剔除；如果还有其他数据源，继续分析其他数据源。'
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)
            continue

        unresolved_columns = {
            match.group('field').strip()
            for pattern in UNRESOLVED_COLUMN_PATTERNS
            for match in pattern.finditer(error_message)
            if match.group('field').strip()
        }
        if unresolved_columns:
            fields = '、'.join(f'`{field}`' for field in sorted(unresolved_columns))
            hint = (
                f'当前真实数据源无法解析字段 {fields}。下一版必须从当前 Schema 的实际字段中重建所有引用这些字段的查询、计算和图表；'
                '不要重复该字段，也不要仅删除字段后把缺失含义静默当成 0。'
                '若当前代码无法从已知 Schema 确定可替代字段，下一步应先生成 execution_intent="probe"：'
                '对失败查询涉及的每个不确定数据源使用不含业务字段投影的轻量 SELECT * ... LIMIT 1，'
                '在 diagnostics 中返回实际 columns 和不可用字段，再基于该结果生成完整 analysis。'
            )
            if hint not in seen:
                seen.add(hint)
                hints.append(hint)
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

        normalized_error = _clip_retry_text(error_message, RETRY_HINT_MAX_CHARS)
        if normalized_error not in seen:
            seen.add(normalized_error)
            hints.append(f'避免再次触发已出现过的错误：{normalized_error}')

    return [_clip_retry_text(hint, RETRY_HINT_MAX_CHARS) for hint in hints[:RETRY_PATTERN_HINT_LIMIT]]


def _get_latest_failed_repair_target(executions: list[Any]) -> dict[str, Any]:
    """Return an immutable execution reference and bounded local repair context."""
    for execution in reversed(executions):
        if getattr(execution, 'execution_status', '') == 'success':
            continue
        generated_code = (getattr(execution, 'generated_code', '') or '').strip()
        if generated_code:
            payload = _extract_execution_payload(execution)
            code_error_context = payload.get('code_error_context')
            return {
                'base_execution_id': int(getattr(execution, 'id', 0) or 0),
                'source_sha256': hashlib.sha256(generated_code.encode('utf-8')).hexdigest(),
                'line_count': len(generated_code.splitlines()),
                'code_error_context': code_error_context if isinstance(code_error_context, dict) else {},
            }
    return {}


def _extract_execution_payload(execution: Any) -> dict[str, Any]:
    """解析执行记录中的结构化 payload。"""
    payload_text = (getattr(execution, 'result_payload_json', '') or '').strip()
    if not payload_text:
        return {}
    try:
        payload = json.loads(payload_text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_structured_execution_feedback(executions: list[Any]) -> list[dict[str, Any]]:
    """Carry bounded tool contracts into the next Runtime ReAct turn."""
    feedback: list[dict[str, Any]] = []
    for execution in reversed(executions):
        if getattr(execution, 'execution_status', '') == 'success':
            continue
        payload = _extract_execution_payload(execution)
        retry_type = str(
            payload.get('retry_type') or payload.get('error_type') or ''
        ).strip()
        diagnostics = payload.get('diagnostics')
        repair_instructions = payload.get('repair_instructions')
        if not retry_type and not isinstance(diagnostics, dict) and not isinstance(repair_instructions, list):
            continue
        item: dict[str, Any] = {}
        if retry_type:
            item['retry_type'] = retry_type
        message = str(payload.get('message') or getattr(execution, 'error_message', '') or '').strip()
        if message:
            item['message'] = _clip_retry_text(message, 700)
        if isinstance(diagnostics, dict) and diagnostics:
            item['diagnostics'] = diagnostics
        if isinstance(repair_instructions, list) and repair_instructions:
            item['repair_instructions'] = [
                _clip_retry_text(str(value), 500)
                for value in repair_instructions[:5]
                if str(value).strip()
            ]
        if item:
            feedback.append(item)
        if len(feedback) >= RETRY_LATEST_ERROR_LIMIT:
            break
    return list(reversed(feedback))


def _execution_has_time_series_empty_source_error(execution: Any) -> bool:
    error_message = (getattr(execution, 'error_message', '') or '').lower()
    payload = _extract_execution_payload(execution)
    payload_text = json.dumps(payload, ensure_ascii=False).lower() if payload else ''
    return any(
        marker in error_message or marker in payload_text
        for marker in TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS
    )


def _extract_execution_error_type(execution: Any) -> str:
    """从执行记录中提取结构化错误类型，旧数据回退到错误文本启发式识别。"""
    payload = _extract_execution_payload(execution)
    if payload:
        payload_text = json.dumps(payload, ensure_ascii=False).lower()
        if any(marker in payload_text for marker in TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS):
            return 'no_data_found'
        error_type = str(payload.get('error_type') or payload.get('retry_type') or '').strip()
        if error_type:
            return error_type
        error_signature = payload.get('error_signature')
        if isinstance(error_signature, dict):
            signature_type = str(
                error_signature.get('error_type') or error_signature.get('signature_type') or ''
            ).strip()
            if signature_type:
                return signature_type

    error_message = (getattr(execution, 'error_message', '') or '').strip()
    lowered = error_message.lower()
    if any(marker in lowered for marker in ('filenotfounderror', 'no such file', 'file not found')):
        return 'data_source_not_found'
    if any(marker in error_message for marker in ('文件不存在', '数据源不存在', '表不存在', '接口不可访问')):
        return 'data_source_not_found'
    if any(marker in lowered for marker in ('no_data_found', 'no data found')):
        return 'no_data_found'
    if any(marker in lowered for marker in TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS):
        return 'no_data_found'
    if any(marker in error_message for marker in ('未命中可分析数据', '筛选结果为空', '过滤后无数据', '关联后无数据')):
        return 'no_data_found'
    if _is_chart_contract_error_text(error_message):
        return 'chart_contract_error'
    return ''


def _latest_failed_execution_error_type(runtime: ConversationRunContext) -> str:
    """读取本轮最后一次失败执行的结构化错误类型。"""
    executions = _load_turn_executions_fresh(runtime)
    for execution in reversed(executions):
        if getattr(execution, 'execution_status', '') == 'success':
            continue
        error_type = _extract_execution_error_type(execution)
        if error_type:
            return error_type
    return ''


def _user_query_has_explicit_time_range(user_query: str) -> bool:
    text = str(user_query or '')
    return bool(
        ANALYSIS_EXPLICIT_TIME_PATTERN.search(text)
        or RELATIVE_SHORT_TIME_PATTERN.search(text)
    )


def _ensure_explicit_time_range_guard_memory(
    runtime: ConversationRunContext,
    *,
    source_error_type: str,
) -> None:
    if not _user_query_has_explicit_time_range(getattr(runtime.turn, 'user_query', '') or ''):
        return

    short_term_memory = getattr(runtime, 'short_term_memory', None)
    if not isinstance(short_term_memory, dict):
        short_term_memory = {}
        setattr(runtime, 'short_term_memory', short_term_memory)

    guard = short_term_memory.get('explicit_time_range_guard')
    if not isinstance(guard, dict):
        guard = {}
    guard.update({
        'active': True,
        'source_error_type': source_error_type,
        'user_query': getattr(runtime.turn, 'user_query', '') or '',
    })
    short_term_memory['explicit_time_range_guard'] = guard


def _update_short_term_memory_from_tool_payload(runtime: ConversationRunContext, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get('tool') not in ('', None, 'execute_python'):
        return

    error_type = str(
        payload.get('error_type')
        or payload.get('retry_type')
        or payload.get('retry_reason_code')
        or ''
    ).strip()
    if error_type in PROBE_NO_DATA_ERROR_TYPES:
        _ensure_explicit_time_range_guard_memory(runtime, source_error_type=error_type)
        short_term_memory = getattr(runtime, 'short_term_memory', None)
        if not isinstance(short_term_memory, dict):
            short_term_memory = {}
            setattr(runtime, 'short_term_memory', short_term_memory)
        probe_no_data_memory = short_term_memory.get('probe_no_data_retry')
        if not isinstance(probe_no_data_memory, dict):
            probe_no_data_memory = {
                'count': 0,
                'last_error_type': '',
                'limit': PROBE_NO_DATA_MEMORY_LIMIT,
            }
        probe_no_data_memory['count'] = int(probe_no_data_memory.get('count') or 0) + 1
        probe_no_data_memory['last_error_type'] = error_type
        probe_no_data_memory['limit'] = PROBE_NO_DATA_MEMORY_LIMIT
        probe_no_data_memory['limit_reached'] = probe_no_data_memory['count'] >= PROBE_NO_DATA_MEMORY_LIMIT
        short_term_memory['probe_no_data_retry'] = probe_no_data_memory


def _update_short_term_memory_from_messages(runtime: ConversationRunContext, messages: list[Any]) -> None:
    for message in messages or []:
        content = getattr(message, 'content', None)
        if not isinstance(content, str) or not content.strip().startswith('{'):
            continue
        try:
            payload = json.loads(content)
        except Exception:
            continue
        if isinstance(payload, dict):
            _update_short_term_memory_from_tool_payload(runtime, payload)


def _collect_probe_candidate_values(diagnostics: dict[str, Any]) -> list[tuple[str, str]]:
    """从 probe diagnostics 中提取候选值文本。"""
    if not isinstance(diagnostics, dict):
        return []

    collected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, value in diagnostics.items():
        lowered_key = str(key or '').lower()
        if 'candidate' not in lowered_key:
            continue
        if not isinstance(value, list):
            continue
        for item in value:
            candidate_text = ''
            if isinstance(item, str):
                candidate_text = item.strip()
            elif isinstance(item, dict):
                for candidate_field in ('value', 'name', 'material', 'candidate', 'text', 'label'):
                    candidate_text = str(item.get(candidate_field) or '').strip()
                    if candidate_text:
                        break
            if not candidate_text:
                continue
            signature = (str(key), candidate_text)
            if signature in seen:
                continue
            seen.add(signature)
            collected.append(signature)
    return collected


def _pick_exact_probe_candidate(user_query: str, diagnostics: dict[str, Any]) -> tuple[str, str]:
    """当 probe 已给出足够明确的候选值时，挑选最接近用户原意的单个真实值。"""
    candidates = _collect_probe_candidate_values(diagnostics)
    if not candidates:
        return '', ''
    if len(candidates) == 1:
        return candidates[0]

    query_text = str(user_query or '').strip()
    if not query_text:
        return '', ''

    matched = [
        (key, candidate)
        for key, candidate in candidates
        if candidate and candidate in query_text
    ]
    if len(matched) == 1:
        return matched[0]

    return '', ''


def _build_analysis_recovery_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    round_index: int,
) -> str:
    """为同一轮分析构造后续修复回合的运行时指令。"""
    executions = _load_turn_executions_fresh(runtime)
    failed_errors = [
        execution.error_message.strip()
        for execution in executions
        if execution.execution_status != 'success' and (execution.error_message or '').strip()
    ]
    latest_errors = [
        _clip_retry_text(item, RETRY_ERROR_MAX_CHARS)
        for item in failed_errors[-RETRY_LATEST_ERROR_LIMIT:]
    ]
    latest_error_types = [
        error_type
        for error_type in (_extract_execution_error_type(execution) for execution in executions)
        if error_type
    ][-3:]
    failed_pattern_hints = _build_failed_pattern_hints(executions)
    failure_summary = _build_failure_frequency_summary(executions)
    latest_repair_target = _get_latest_failed_repair_target(executions)
    latest_probe_feedback_payload = next(
        (
            payload
            for execution in reversed(executions)
            for payload in [_extract_execution_payload(execution)]
            if payload.get('error_type') == 'probe_feedback'
        ),
        {},
    )
    latest_probe_diagnostics = (
        latest_probe_feedback_payload.get('diagnostics')
        if isinstance(latest_probe_feedback_payload.get('diagnostics'), dict)
        else {}
    )
    structured_execution_feedback = _build_structured_execution_feedback(
        executions
    )
    exact_probe_candidate_key, exact_probe_candidate = _pick_exact_probe_candidate(
        runtime.turn.user_query,
        latest_probe_diagnostics,
    )

    lines = [
        '本轮已经进入数据分析执行阶段。',
        f'当前轮次累计执行了 {len(executions)} 次 Python 分析代码，但仍未成功生成完整分析产物。',
    ]
    if latest_repair_target:
        lines.extend([
            '最近失败代码已完整保存在不可变执行记录中；下面的引用和原始行号可用于理解失败位置。',
            '只有独立的局部 API 参数、单个图表字段映射或语法拼写错误，才适合令 code="" 并使用 base_execution_id + patches 修补。',
            '如果错误影响数据源字段、查询范围、JOIN、聚合粒度、计算变量、报告求值或最终产物结构，必须在当前完整洞察语义下重新提交完整 code，不能依赖局部 patch 保留旧数据流。',
            json.dumps(latest_repair_target, ensure_ascii=False),
        ])
    explicit_time_range_guard = getattr(runtime, 'short_term_memory', {}).get('explicit_time_range_guard', {})
    if isinstance(explicit_time_range_guard, dict) and explicit_time_range_guard.get('active'):
        lines.extend([
            '本轮短期记忆：用户问题里有明确时间范围，且本轮已经出现过无数据或探测反馈。',
            '后续所有 analysis 代码必须保持用户原始时间范围对应的 query_constraints.time_range；不能改成 diagnostics、探测 SQL 或数据库实际数据覆盖范围。',
            '如果目标时间范围内仍无数据，应返回无数据反馈或无数据结论，不要为了生成成功结果而改查其他时间范围。',
        ])
    probe_no_data_memory = getattr(runtime, 'short_term_memory', {}).get('probe_no_data_retry', {})
    if isinstance(probe_no_data_memory, dict) and probe_no_data_memory.get('limit_reached'):
        count = int(probe_no_data_memory.get('count') or 0)
        limit = int(probe_no_data_memory.get('limit') or PROBE_NO_DATA_MEMORY_LIMIT)
        lines.extend([
            f'本轮已经连续出现 {count} 次 probe_feedback/no_data_found 反馈，已达到上限 {limit} 次。',
            '下一步不要继续生成 probe 代码，不要继续猜测时序查询语法、字段名或表名，也不要扩大到其他数据源。',
            '请直接切换为 execution_intent = "analysis"，保持用户原始问题和原始时间范围，调用 save_analysis_result(...) 以“无数据或数据源暂不可分析”的结论收口；如已探测过多个数据源，应在表格中列出已尝试的数据源和失败原因。',
        ])
    if latest_errors:
        lines.append('最近的执行错误如下：')
        lines.extend(f'- {item}' for item in latest_errors)
    if structured_execution_feedback:
        lines.extend([
            '最近工具返回的结构化执行反馈如下。它是下一次修复的事实依据；必须消费其中的 diagnostics 和 repair_instructions，不能只根据错误摘要猜测。',
            json.dumps(structured_execution_feedback, ensure_ascii=False),
        ])
    if latest_error_types:
        lines.append(f'最近的结构化错误类型：{", ".join(latest_error_types)}。')

    latest_error_type = latest_error_types[-1] if latest_error_types else ''
    if latest_error_type in {'contract_error', 'missing_query_constraints', 'invalid_query_constraints'}:
        lines.extend([
            '最近失败发生在 execute_python 的静态契约检查阶段，上一版代码没有真正进入数据执行。',
            '下一步必须重新调用 execute_python，并提交一份完整的新代码；不能只用自然语言说明，也不能直接结束本轮。',
            '如果继续按分析执行：代码顶部必须声明 execution_intent = "analysis"，并一次性包含 query_constraints、validate_query_constraints(...)、数据加载、空数据检查、图表/表格构造和 result = save_analysis_result(...)。',
            '如果只是探测字段、样例、候选值或数据覆盖：代码顶部必须声明 execution_intent = "probe"，并以 result = request_retry(...) 返回结构化探测反馈。',
        ])

    if failure_summary:
        lines.append('本轮重复失败摘要如下，优先修复出现次数最多的问题，不要沿着同一错误反复试错：')
        lines.extend(f'- {_clip_retry_text(item, RETRY_ERROR_MAX_CHARS)}' for item in failure_summary)

    datasource_selection_hints = _build_possible_datasource_selection_hints(runtime, executions)
    if datasource_selection_hints:
        lines.append('本轮可能存在正式 analysis 数据源规划遗漏，请重新规划 used_datasource_ids：')
        lines.extend(f'- {item}' for item in datasource_selection_hints)

    if failed_pattern_hints:
        lines.append('本轮已经确认的失败写法如下，新的修复代码中不要再次出现：')
        lines.extend(f'- {item}' for item in failed_pattern_hints)

    lines.extend([
        '修复应保留已验证正确的业务口径和产物意图；但当错误影响数据流或结果表达时，应重建受影响的完整代码路径，不要用局部替换留下不一致的旧变量或占位文本。',
        '修复技术错误时必须保持当前用户问题的输出语言；不得翻译或切换上一版已经形成的报告、图表和表格语言。',
        '请直接修正完整 Python 代码并再次调用 execute_python。',
    ])
    if 'probe_feedback' in latest_error_types:
        lines.append(
            '最近一轮是 probe_feedback：探测阶段使用的 LIKE/contains 只用于确认候选值，下一版正式分析不能直接沿用这些模糊条件。'
        )
        lines.append(
            '如果用户问题里有明确时间范围，不能把正式分析的 query_constraints.time_range 改成 diagnostics 里的数据覆盖范围；数据覆盖范围只能用于解释为什么目标时间窗无数据。'
        )
        lines.append(
            '如果 probe 已确认用户明确时间范围内无数据，下一版应保持原始时间范围并调用 raise_no_data_error(...) 返回无数据反馈，不要为了命中数据改查其他时间范围。'
        )
        if exact_probe_candidate:
            field_hint = '目标字段'
            lowered_key = exact_probe_candidate_key.lower()
            if 'material' in lowered_key:
                field_hint = 'material'
            lines.extend([
                f'最近探测已给出足够明确的候选值：`{exact_probe_candidate}`。',
                f"下一版必须切换为 execution_intent = \"analysis\"，并对 {field_hint} 使用单个精确筛选条件（例如 `{field_hint} = '{exact_probe_candidate}'`）；禁止继续使用 LIKE、ILIKE、contains、str.contains(...) 或前缀匹配。",
            ])
        else:
            lines.append(
                '如果 diagnostics 中候选值已经足够明确，应从中选择单个真实值收敛成精确筛选条件；不要把 probe 中的 LIKE/contains 直接延续到正式分析。'
            )
    if latest_error_type == 'chart_contract_error' or any(_is_chart_contract_error_text(item) for item in failed_errors):
        lines.extend([
            '本轮已出现图表产物契约错误。下一版必须把修复重点放在 charts 构造，不要重新发散数据查询逻辑。',
            '禁止继续使用 matplotlib、base64 图片、手写 chart_spec 或自造 chart_type/chart_kind 结构。',
            '请使用 build_chart_result(...)、build_multi_metric_chart_result(...) 或 build_chart_suite(...) 生成 charts；异常值、分布和四分位数场景使用 chart_kind="boxplot"；如果无法生成图表，则至少用 tables 返回结构化汇总。',
        ])
    elif latest_error_type in {'data_source_not_found', 'data_source_unavailable'}:
        lines.extend([
            '最近失败属于数据源不可用或数据源标识错误，不是 no_data_found。',
            '不要调用 raise_no_data_error(...) 包装文件不存在、表不存在、路径不可访问或接口不可访问；raise_no_data_error 只用于数据源已成功加载后，过滤/关联/聚合结果为空。',
            '只能原样使用当前数据源上下文提供的文件路径、表名或接口地址；如果已原样使用仍不可访问，应调用 request_retry(retry_type="data_source_unavailable", message=..., diagnostics=..., repair_instructions=...) 返回结构化反馈，或让本轮失败结束，不要伪装成空数据分析。',
        ])
    elif latest_error_type == 'no_data_found':
        if any(_execution_has_time_series_empty_source_error(execution) for execution in executions):
            lines.append(
                '本轮已有时序数据源返回 unknown tag/no results：这些数据源视为当前没有可分析数据或尚未初始化。下一版应把这些数据源从独立分析范围中剔除，继续分析其他仍可查询的数据源；只有所有相关数据源都无数据时，才输出整体无数据结论。'
            )
        lines.append(
            '最近失败属于 no_data_found：请优先检查时间过滤是否命中数据、过滤后数据集是否为空、关联结果是否为空；如果探测后仍为空，应调用 raise_no_data_error(...) 把“无数据”返回给 execute_python，而不是继续误改字段名或图表参数。'
        )
        lines.append(
            '如果用户问题里有明确时间范围，必须继续保持该时间范围；不要把 query_constraints.time_range 改成探测到的数据覆盖范围来制造命中结果。'
        )
    else:
        lines.append(
            '只有在数据源已经成功加载后，过滤、关联或聚合结果为空时，才允许调用 raise_no_data_error(...)；文件、表或接口不可访问不属于无数据。'
        )
    lines.extend([
        '不要向用户解释工具错误，不要再次询问用户，也不要用自然语言提前结束本轮。',
        '只有成功生成完整的结构化图表结果和分析报告，这一轮分析才算完成。',
    ])

    if round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1:
        lines.append('这已经是本轮最后一次修复机会；如果仍无法完成，请让本轮以失败结束，而不是伪装成成功。')

    return _clip_retry_text('\n'.join(lines), RETRY_RECOVERY_INSTRUCTION_MAX_CHARS)


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
    with session_scope(expire_on_commit=False) as session:
        runtime = ConversationContextService(session).start_run(
            username=agent_request.username,
            namespace_id=agent_request.namespace_id,
            conversation_id=agent_request.conversation_id,
            user_message=agent_request.user_message,
        )
    service = None

    selected_model_token = bind_selected_model_id(agent_request.selected_llm_model_id)
    try:
        _resolve_runtime_context_mode(runtime, agent_request.user_message)
        assistant_message = ''
        analysis_report = ''
        charts: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        analysis_flow_started = False
        analysis_request_expected = _is_analysis_request_expected(runtime, agent_request.user_message)
        allow_report_only = False

        if _should_finish_without_datasource(runtime, agent_request.user_message):
            assistant_message = NO_DATASOURCE_ANALYSIS_REPLY
            allow_report_only = True
            return _finalize_run_with_fresh_session(
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

        runtime_config = _resolve_agent_runtime_config(agent_request)
        preparation = (
            _prepare_analysis_context(
                runtime=runtime,
                agent_request=agent_request,
                runtime_config=runtime_config,
            )
            if analysis_request_expected
            else None
        )

        agent = get_data_insight_agent(runtime_config)

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            _raise_if_analysis_task_cancelled(runtime)
            runtime_instruction = ''
            if analysis_flow_started:
                runtime_instruction = _build_analysis_recovery_instruction(
                    service=service,
                    runtime=runtime,
                    round_index=round_index,
                )
            elif runtime.is_rerun:
                runtime_instruction = _build_rerun_instruction(service, runtime)
            elif round_index == 0:
                runtime_instruction = _build_regenerate_instruction(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
                if not runtime_instruction:
                    runtime_instruction = _build_history_requirement_recovery_instruction(
                        service=service,
                        runtime=runtime,
                        user_message=agent_request.user_message,
                    )
                if not runtime_instruction:
                    runtime_instruction = _build_followup_hint_instruction(
                        agent_request.user_message,
                        context_mode=getattr(runtime, 'context_mode', None),
                        runtime=runtime,
                    )
            elif analysis_request_expected:
                runtime_instruction = _build_analysis_start_instruction(agent_request.user_message)

            agent_response = agent.invoke(
                _build_agent_input_with_runtime_instruction(
                    agent_request=agent_request,
                    runtime=runtime,
                    runtime_instruction=runtime_instruction,
                    preparation=preparation,
                ),
                context=_build_agent_context(
                    agent_request,
                    runtime,
                    preparation,
                ),
            )
            _raise_if_analysis_task_cancelled(runtime)
            _update_short_term_memory_from_messages(runtime, agent_response.get('messages', []))
            assistant_message, analysis_report, charts, tables, raw_tool_call_detected = _extract_invoke_result(
                agent_response.get('messages', [])
            )

            entered_analysis_flow = _did_enter_analysis_flow(
                service=service,
                runtime=runtime,
                raw_tool_call_detected=raw_tool_call_detected,
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow
            analysis_report, charts, tables = _resolve_analysis_outputs(
                service=service,
                runtime=runtime,
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
            )

            if raw_tool_call_detected and not (analysis_report or charts or tables):
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
                if analysis_report and (charts or tables):
                    break
                if analysis_report and _is_expected_time_no_data_report(agent_request.user_message, analysis_report):
                    allow_report_only = True
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                latest_failed_error_type = _latest_failed_execution_error_type(runtime)
                if latest_failed_error_type in {'contract_error', 'missing_query_constraints', 'invalid_query_constraints'}:
                    raise ValueError('生成的分析代码未通过静态契约检查，且模型未按反馈重新生成可执行代码，请重试。')
                _ensure_analysis_result_ready(analysis_report=analysis_report, charts=charts, tables=tables)
                break

            if _should_require_rerun_analysis_execution(service, runtime, analysis_flow_started):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('刷新分析未重新执行代码，已保留本轮原有分析结果，请重试。')

            if analysis_request_expected and not runtime.is_rerun:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

            break

        return _finalize_run_with_fresh_session(
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
    except AnalysisTaskCancelled as exc:
        _fail_run_with_fresh_session(
            runtime,
            CANCELLED_MESSAGE,
            preserve_existing_results=runtime.is_rerun,
        )
        raise RuntimeError(CANCELLED_MESSAGE) from exc
    except Exception as exc:
        user_error_message = get_user_facing_agent_error(exc)
        logger.error(
            "Agent 执行失败: conversation_id=%s turn_id=%s error=%s",
            runtime.conversation.id,
            runtime.turn.id,
            exc,
            exc_info=True,
        )
        _fail_run_with_fresh_session(
            runtime,
            user_error_message,
            preserve_existing_results=runtime.is_rerun,
        )
        raise RuntimeError(user_error_message) from exc
    finally:
        reset_selected_model_id(selected_model_token)


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
    analysis_request_expected = _is_analysis_request_expected(runtime, agent_request.user_message)
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
        if _should_finish_without_datasource(runtime, agent_request.user_message):
            assistant_message = NO_DATASOURCE_ANALYSIS_REPLY
            allow_report_only = True
            yield _build_progress_event(
                'assistant',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage='no_datasource',
                message=assistant_message,
            )
            _finalize_run_with_fresh_session(
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
            yield _build_progress_event(
                'done',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
            )
            return

        runtime_config = _resolve_agent_runtime_config(agent_request)
        preparation = (
            _prepare_analysis_context(
                runtime=runtime,
                agent_request=agent_request,
                runtime_config=runtime_config,
            )
            if analysis_request_expected
            else None
        )

        agent = get_data_insight_agent(runtime_config)

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            _raise_if_analysis_task_cancelled(runtime)
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
                runtime_instruction = _build_rerun_instruction(service, runtime)
            elif round_index == 0:
                runtime_instruction = _build_regenerate_instruction(
                    service=service,
                    runtime=runtime,
                    user_message=agent_request.user_message,
                )
                if not runtime_instruction:
                    runtime_instruction = _build_history_requirement_recovery_instruction(
                        service=service,
                        runtime=runtime,
                        user_message=agent_request.user_message,
                    )
                if not runtime_instruction:
                    runtime_instruction = _build_followup_hint_instruction(
                        agent_request.user_message,
                        context_mode=getattr(runtime, 'context_mode', None),
                        runtime=runtime,
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

            for stream_mode, chunk in agent.stream(
                _build_agent_input_with_runtime_instruction(
                    agent_request=agent_request,
                    runtime=runtime,
                    runtime_instruction=runtime_instruction,
                    preparation=preparation,
                ),
                context=_build_agent_context(
                    agent_request,
                    runtime,
                    preparation,
                ),
                stream_mode=["updates", "custom"],
            ):
                _raise_if_analysis_task_cancelled(runtime)
                if stream_mode == "updates":
                    for _, payload in chunk.items():
                        messages = payload.get("messages", [])
                        for message in messages:
                            tool_calls = getattr(message, 'tool_calls', None)
                            raw_tool_call = _extract_raw_tool_call(getattr(message, 'content', ''))
                            cleaned = _clean_message_content(getattr(message, 'content', ''))
                            if isinstance(cleaned, str) and cleaned.strip().startswith('{'):
                                try:
                                    tool_feedback_payload = json.loads(cleaned)
                                except Exception:
                                    tool_feedback_payload = {}
                                if isinstance(tool_feedback_payload, dict):
                                    _update_short_term_memory_from_tool_payload(runtime, tool_feedback_payload)

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
                            if execution_report:
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
                tables=tables,
            )
            analysis_flow_started = analysis_flow_started or entered_analysis_flow
            analysis_report, charts, tables = _resolve_analysis_outputs(
                service=service,
                runtime=runtime,
                analysis_report=analysis_report,
                charts=charts,
                tables=tables,
            )

            if raw_tool_call_detected and not (analysis_report or charts or tables):
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
                if analysis_report and (charts or tables):
                    break
                if analysis_report and _is_expected_time_no_data_report(agent_request.user_message, analysis_report):
                    allow_report_only = True
                    break
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                latest_failed_error_type = _latest_failed_execution_error_type(runtime)
                if latest_failed_error_type in {'contract_error', 'missing_query_constraints', 'invalid_query_constraints'}:
                    raise ValueError('生成的分析代码未通过静态契约检查，且模型未按反馈重新生成可执行代码，请重试。')
                _ensure_analysis_result_ready(analysis_report=analysis_report, charts=charts, tables=tables)
                break

            if _should_require_rerun_analysis_execution(service, runtime, analysis_flow_started):
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('刷新分析未重新执行代码，已保留本轮原有分析结果，请重试。')

            if analysis_request_expected and not runtime.is_rerun:
                if round_index < MAX_ANALYSIS_AGENT_ROUNDS - 1:
                    continue
                raise ValueError('本轮未进入分析执行阶段，未生成分析图表或分析报告，请重试。')

            break

        response = _finalize_run_with_fresh_session(
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
    except AnalysisTaskCancelled:
        _fail_run_with_fresh_session(
            runtime,
            CANCELLED_MESSAGE,
            preserve_existing_results=runtime.is_rerun,
        )
        yield _build_progress_event(
            'error',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='cancelled',
            level='warning',
            message=CANCELLED_MESSAGE,
        )
    except Exception as exc:
        user_error_message = get_user_facing_agent_error(exc)
        logger.error(
            "Agent 流式执行失败: conversation_id=%s turn_id=%s error=%s",
            runtime.conversation.id,
            runtime.turn.id,
            exc,
            exc_info=True,
        )
        _fail_run_with_fresh_session(
            runtime,
            user_error_message,
            preserve_existing_results=runtime.is_rerun,
        )
        yield _build_progress_event(
            'error',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='error',
            level='error',
            message=user_error_message,
        )
    finally:
        logger.reset_context(log_context_token)


def stream_invoke_agent(agent_request: AgentRequest) -> Iterator[dict[str, Any]]:
    """执行一次流式分析请求，并持续产出适合 SSE 的事件。"""
    with session_scope(expire_on_commit=False) as session:
        runtime = ConversationContextService(session).start_run(
            username=agent_request.username,
            namespace_id=agent_request.namespace_id,
            conversation_id=agent_request.conversation_id,
            user_message=agent_request.user_message,
        )
    service = None

    selected_model_token = bind_selected_model_id(agent_request.selected_llm_model_id)
    try:
        _resolve_runtime_context_mode(runtime, agent_request.user_message)
        yield from _stream_with_runtime(service, runtime, agent_request)
    finally:
        reset_selected_model_id(selected_model_token)


def _build_rerun_agent_request(
    username: str,
    runtime: ConversationRunContext,
    auth_token: str = '',
    selected_llm_model_id: str = '',
    database_conn_info: dict[str, Any] | None = None,
    database_context: dict[str, Any] | None = None,
) -> AgentRequest:
    """构造刷新分析复用的 AgentRequest，并透传用户数据库连接信息。"""
    database_payload = database_conn_info if database_conn_info is not None else database_context
    return AgentRequest(
        username=username,
        namespace_id=str(runtime.conversation.insight_namespace_id),
        conversation_id=str(runtime.conversation.id),
        user_message=runtime.turn.user_query,
        auth_token=auth_token or '',
        selected_llm_model_id=selected_llm_model_id or '',
        database_conn_info=dict(database_payload or {}),
    )


def stream_rerun_turn(
    username: str,
    conversation_id: Any,
    turn_id: Any,
    auth_token: str = '',
    selected_llm_model_id: str = '',
    database_conn_info: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """在同一轮次内重新执行一次分析，并把新结果回写到该轮。"""
    with session_scope(expire_on_commit=False) as session:
        runtime = ConversationContextService(session).start_rerun(
            username=username,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
    service = None

    if runtime is None:
        raise ValueError('轮次详情不存在')

    agent_request = _build_rerun_agent_request(
        username=username,
        runtime=runtime,
        auth_token=auth_token,
        selected_llm_model_id=selected_llm_model_id,
        database_conn_info=database_conn_info,
    )

    selected_model_token = bind_selected_model_id(agent_request.selected_llm_model_id)
    try:
        _resolve_runtime_context_mode(runtime, agent_request.user_message)
        yield from _stream_with_runtime(service, runtime, agent_request)
    finally:
        reset_selected_model_id(selected_model_token)


def stream_existing_turn_events(
    agent_request: AgentRequest,
    *,
    turn_id: Any,
    is_rerun: bool = False,
    analysis_task_id: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Execute an existing turn and stream events for the background task queue."""
    from model import InsightNsConversation, InsightNsTurn

    def _load_runtime(session):
        turn_id_int = int(turn_id or 0)
        conversation_id_int = int(agent_request.conversation_id or 0)
        conversation = session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id_int,
            InsightNsConversation.is_deleted == 0,
        ).first()
        turn = session.query(InsightNsTurn).filter(
            InsightNsTurn.id == turn_id_int,
            InsightNsTurn.conversation_id == conversation_id_int,
            InsightNsTurn.is_deleted == 0,
        ).first()
        if conversation is None or turn is None:
            raise ValueError('分析轮次不存在或已被删除')
        selected_datasource_ids = safe_json_loads(turn.selected_datasource_ids_json, [])
        selected_datasource_snapshot = safe_json_loads(turn.selected_datasource_snapshot_json, [])
        if selected_datasource_ids and not selected_datasource_snapshot:
            selected_datasource_snapshot = ConversationContextService(
                session
            )._build_turn_datasource_snapshot(
                conversation_id=conversation.id,
                datasource_ids=selected_datasource_ids,
            )
            logger.info(
                "rehydrated missing turn datasource snapshot: conversation_id=%s "
                "turn_id=%s datasource_count=%s",
                conversation.id,
                turn.id,
                len(selected_datasource_snapshot),
            )
        active_snapshot = {
            "namespace_id": conversation.insight_namespace_id,
            "conversation_id": conversation.id,
            "selected_datasource_ids": selected_datasource_ids,
            "selected_datasource_snapshot": selected_datasource_snapshot,
        }
        return ConversationRunContext(
            conversation=conversation,
            turn=turn,
            active_datasource_snapshot=active_snapshot,
            is_rerun=is_rerun,
            history_turn_limit=max(turn.turn_no - 1, 0) if is_rerun else None,
            analysis_task_id=analysis_task_id,
        )

    with session_scope(expire_on_commit=False) as session:
        runtime = _load_runtime(session)
    selected_model_token = bind_selected_model_id(agent_request.selected_llm_model_id)
    try:
        _resolve_runtime_context_mode(runtime, agent_request.user_message)
        yield from _stream_with_runtime(None, runtime, agent_request)
    finally:
        reset_selected_model_id(selected_model_token)
