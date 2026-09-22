import copy
import hashlib
import json
import re
import time
from calendar import monthrange
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, messages_from_dict, messages_to_dict
from sqlalchemy.exc import SQLAlchemyError

from agent import (
    CustomContext,
    InputMessage,
    get_data_insight_agent,
    get_general_conversation_agent,
    get_general_input,
    get_input,
)
from agent.analysis_preparation import (
    BusinessFactCoverageContract,
    BusinessFactRequirementContract,
    BusinessContextContract,
    BusinessQuestionContract,
    BusinessQuestionCoverageContract,
    DatasourceSelectionContract,
    IntentContract,
    PreparationResult,
    ScopeClarificationResult,
    infer_query_capabilities,
    prepare_analysis,
)
from agent.context_engineering_runtime import classify_analysis_context_mode, is_analysis_like_request
from agent.runtime_working_context import build_short_term_memory_catalog
from agent.runtime_driver_state import (
    RUNTIME_WORK_PACKET_MEMORY_KEY,
    RUNTIME_WORK_PACKET_PAYLOAD_KEY,
    new_runtime_work_packet,
    resume_runtime_work_packet,
    runtime_work_packet_from_dict,
)
from agent.runtime_schema_memory import merge_schema_sources
from agent.intent_classifier import (
    PENDING_SCOPE_MEMORY_TYPE,
    resolve_context_mode_with_llm,
)
from agent.invocation_policy import (
    normalize_clarification_mode,
    normalize_output_contract,
)
from config import Config
from config.database import session_scope
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from service.semantic_datasource_selection_service import DatasourceSelectionProviderRouter
from utils import logger
from agent.runtime_progress import tool_progress
from utils.datasource_utils import safe_json_loads
from utils.analysis_task_cancel import AnalysisTaskCancelled, CANCELLED_MESSAGE, is_analysis_task_cancelled
from utils.llm_error_utils import get_user_facing_agent_error
from utils.llm_model_factory import bind_selected_model_id, create_runtime_config, reset_selected_model_id
from utils.token_budget import estimate_messages_tokens

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
    r"(?P<unit>天|日|周(?!期)|星期|月|个月|年)"
)
EXPECTED_TIME_RANGE_CONTRACT_VERSION = 3
ANALYSIS_EXPLICIT_TIME_PATTERN = re.compile(
    r"(今天|昨天|前天|本周(?!期)|上周(?!期)|本月|上月|今年|去年|(?:近|最近|过去)\s*(?:\d+|[一二两三四五六七八九十]{1,3})\s*(?:天|日|周(?!期)|星期|月|个月|年)|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}日)"
)
# A normal bounded Runtime work chain may require: Schema detail lookup, Probe,
# a second targeted lookup, then a formal program submission. This ceiling is a
# circuit breaker for stalled work, not a three-attempt retry policy.
MAX_ANALYSIS_AGENT_ROUNDS = 6
RETRY_LATEST_ERROR_LIMIT = 3
RETRY_PATTERN_HINT_LIMIT = 5
RETRY_ERROR_MAX_CHARS = 700
RETRY_HINT_MAX_CHARS = 500


class RuntimeDatasourceUnavailableError(RuntimeError):
    """A required datasource could not be reached by any executed Probe path."""


def _raise_if_terminal_probe_unavailability(messages: list[BaseMessage]) -> None:
    """Stop the outer ReAct loop when Probe proved every required path unavailable."""
    for message in reversed(messages or []):
        if not isinstance(message, BaseMessage):
            continue
        try:
            payload = json.loads(str(getattr(message, 'content', '') or '{}'))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        diagnostics = payload.get('diagnostics')
        terminal = diagnostics.get('_terminal_probe_failure') if isinstance(diagnostics, dict) else None
        if isinstance(terminal, dict) and terminal.get('kind') == 'all_required_query_paths_unavailable':
            raise RuntimeDatasourceUnavailableError(
                '分析所需数据源当前不可访问，未生成分析结果。请确认数据源挂载、权限或联邦查询服务后重试。'
            )
RETRY_RECOVERY_INSTRUCTION_MAX_CHARS = 14000
RUNTIME_SCHEMA_DETAIL_MEMORY_KEY = 'runtime_schema_detail_workspace'
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


class NoDatasourceAvailableForAnalysis(RuntimeError):
    """Preparation could not establish any datasource scope for this analysis."""


@dataclass
class AgentRequest:
    """控制器层传入 Agent 层的标准化请求对象。"""

    username: str
    namespace_id: str
    conversation_id: str
    user_message: str
    platform_authorization: str = ''
    fedquery_authorization: str = ''
    selected_llm_model_id: str = ''
    database_conn_info: dict[str, Any] | None = None
    clarification_context: dict[str, Any] | None = None
    output_contract: dict[str, Any] | None = None
    clarification_mode: str = "same_turn"
    evidence_enabled: bool = True
    # Workflow callers provide the complete allowed UNS scope. Preparation may
    # validate and describe it, but must not retrieve additional sources.
    exact_datasource_scope: bool = False

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


def _apply_invocation_policy(
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
) -> None:
    runtime.output_contract = normalize_output_contract(
        getattr(agent_request, "output_contract", None)
    ).to_dict()
    runtime.clarification_mode = normalize_clarification_mode(
        getattr(agent_request, "clarification_mode", "same_turn")
    )
    runtime.evidence_enabled = bool(getattr(agent_request, "evidence_enabled", True))


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
            analysis_task_id=runtime.analysis_task_id,
            successful_only=True,
        )
        if latest_execution is None:
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
        and payload.get('status') in {'failed', 'observation'}
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
    end = today + timedelta(days=1)
    normalized_unit = str(unit or '').strip()
    if normalized_unit in {'天', '日'}:
        start = end - timedelta(days=amount)
    elif normalized_unit in {'周', '星期'}:
        start = end - timedelta(days=amount * 7)
    elif normalized_unit in {'月', '个月'}:
        start = _shift_month(end, -amount)
    elif normalized_unit == '年':
        try:
            start = end.replace(year=end.year - amount)
        except ValueError:
            start = end.replace(year=end.year - amount, day=28)
    else:
        return {}
    payload = _format_expected_time_range(start, end, label)
    payload.update({
        'relative_window': True,
        'amount': amount,
        'unit': normalized_unit,
        'alignment': 'calendar_day',
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

    if re.search(r'本周(?!期)', text):
        return _format_expected_time_range(current_week_start, current_week_start + timedelta(days=7), '本周')
    if re.search(r'上周(?!期)', text):
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


def _resolve_turn_expected_time_range(
    runtime: ConversationRunContext,
    user_query: str,
) -> dict[str, Any]:
    """Freeze one time-range contract for every stage in the current turn."""
    query = str(user_query or getattr(runtime.turn, 'user_query', '') or '').strip()
    turn_id = int(getattr(runtime.turn, 'id', 0) or 0)
    memory = runtime.short_term_memory
    cached = memory.get('expected_time_range_contract')
    if (
        isinstance(cached, dict)
        and cached.get('version') == EXPECTED_TIME_RANGE_CONTRACT_VERSION
        and int(cached.get('turn_id') or 0) == turn_id
        and str(cached.get('user_query') or '') == query
        and isinstance(cached.get('value'), dict)
    ):
        return dict(cached['value'])

    expected = _build_expected_time_range_from_user_query(query)
    memory['expected_time_range_contract'] = {
        'version': EXPECTED_TIME_RANGE_CONTRACT_VERSION,
        'turn_id': turn_id,
        'user_query': query,
        'value': dict(expected),
    }
    return expected


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
    expected_time_range = _resolve_turn_expected_time_range(
        runtime,
        user_message or getattr(runtime.turn, 'user_query', ''),
    )
    if not expected_time_range:
        return False

    with session_scope(expire_on_commit=False) as session:
        executions = ConversationContextService(session).get_turn_executions(
            runtime.turn.id,
            started_at=runtime.turn.started_at,
            analysis_task_id=runtime.analysis_task_id,
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
            analysis_task_id=runtime.analysis_task_id,
        )
        if executions:
            latest_error = (getattr(executions[-1], 'error_message', '') or '').strip()

    expected_time_range = _resolve_turn_expected_time_range(
        runtime,
        user_message or getattr(runtime.turn, 'user_query', ''),
    )
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
    short_term_memory = getattr(runtime, 'short_term_memory', None)
    if not isinstance(short_term_memory, dict):
        short_term_memory = {}
        runtime.short_term_memory = short_term_memory
    workspace_id = ':'.join(str(value or 0) for value in (
        getattr(runtime.conversation, 'id', 0),
        getattr(runtime.turn, 'id', 0),
        getattr(runtime, 'analysis_task_id', 0),
    ))
    runtime_work_packet = runtime_work_packet_from_dict(
        short_term_memory.get(RUNTIME_WORK_PACKET_MEMORY_KEY)
    )
    if runtime_work_packet is None or runtime_work_packet.workspace_id != workspace_id:
        runtime_work_packet = new_runtime_work_packet(
            workspace_id=workspace_id,
            focus=user_query,
        )
        short_term_memory[RUNTIME_WORK_PACKET_MEMORY_KEY] = runtime_work_packet.to_dict()
    return CustomContext(
        username=agent_request.username,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        user_query=user_query,
        expected_time_range=_resolve_turn_expected_time_range(runtime, user_query),
        platform_authorization=(
            getattr(agent_request, 'platform_authorization', '')
            or getattr(agent_request, 'auth_token', '')
            or ''
        ),
        fedquery_authorization=(
            getattr(agent_request, 'fedquery_authorization', '') or ''
        ),
        database_conn_info=dict(agent_request.database_conn_info or {}),
        active_datasource_snapshot=dict(
            preparation.runtime_prompt_snapshot()
            if preparation is not None
            else runtime.active_datasource_snapshot or {}
        ),
        execution_datasource_snapshot=dict(
            preparation.selected_snapshot
            if preparation is not None
            else runtime.active_datasource_snapshot or {}
        ),
        analysis_task_id=runtime.analysis_task_id,
        output_contract=dict(
            getattr(runtime, 'output_contract', None) or {"type": "standard"}
        ),
        context_mode=str(
            getattr(runtime, 'context_mode', 'fresh_analysis') or 'fresh_analysis'
        ),
        clarification_mode=str(
            getattr(runtime, 'clarification_mode', 'same_turn') or 'same_turn'
        ),
        evidence_enabled=bool(getattr(runtime, 'evidence_enabled', True)),
        working_memory_catalog=build_short_term_memory_catalog(
            getattr(runtime, 'short_term_memory', {})
        ),
        runtime_probe_fact_ledgers=copy.deepcopy(
            getattr(runtime, 'short_term_memory', {}).get('probe_fact_ledger', [])
            if isinstance(getattr(runtime, 'short_term_memory', {}), dict) else []
        ),
        runtime_schema_detail_workspace=copy.deepcopy(
            getattr(runtime, 'short_term_memory', {}).get(RUNTIME_SCHEMA_DETAIL_MEMORY_KEY, [])
            if isinstance(getattr(runtime, 'short_term_memory', {}), dict) else []
        ),
        runtime_work_packet=runtime_work_packet.to_dict(),
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
    prior_messages: list[BaseMessage] | None = None,
):
    """Build the first Runtime input or continue the same ReAct conversation."""
    resume = agent_request.clarification_context or {}
    if prior_messages is None and resume.get('react_messages'):
        prior_messages = messages_from_dict(resume['react_messages'])
    clarification_message = _build_runtime_clarification_answer_instruction(
        agent_request.clarification_context,
    )
    probe_observation_instruction = _build_resumed_probe_observation_instruction(
        agent_request.clarification_context
    )
    if resume.get('react_messages'):
        # The original tool observations are already in the restored transcript.
        probe_observation_instruction = ''
    if prior_messages:
        prior_content = [
            str(getattr(message, 'content', '') or '') for message in prior_messages
        ]
        if clarification_message and any(
            clarification_message == content for content in prior_content
        ):
            clarification_message = ''
        if probe_observation_instruction and any(
            '<resumed-probe-observations>' in content for content in prior_content
        ):
            probe_observation_instruction = ''
    extra_system_messages = [
        item for item in (probe_observation_instruction, runtime_instruction) if item
    ]
    if prior_messages:
        continued_messages = list(prior_messages)
        if extra_system_messages:
            continued_messages.append(SystemMessage("\n\n".join(extra_system_messages)))
        if clarification_message:
            continued_messages.append(HumanMessage(clarification_message))
        return InputMessage(messages=continued_messages)
    agent_input = get_input(
        agent_request.user_message,
        namespace_id=runtime.conversation.insight_namespace_id,
        conversation_id=runtime.conversation.id,
        extra_system_messages=extra_system_messages or None,
        history_turn_limit=runtime.history_turn_limit,
        datasource_snapshot_override=(
            preparation.runtime_prompt_snapshot()
            if preparation is not None
            else runtime.active_datasource_snapshot
        ),
        context_mode=getattr(runtime, 'context_mode', None),
        output_contract=runtime.output_contract,
        clarification_mode=runtime.clarification_mode,
    )
    if clarification_message:
        agent_input.messages.append(HumanMessage(clarification_message))
    return agent_input


def _get_runtime_agent(
    runtime_config,
    *,
    analysis_request_expected: bool,
    clarification_mode: str = "same_turn",
):
    if analysis_request_expected:
        return get_data_insight_agent(
            runtime_config,
            clarification_mode=clarification_mode,
        )
    return get_general_conversation_agent(runtime_config)


def _build_runtime_agent_input(
    *,
    agent_request: AgentRequest,
    runtime: ConversationRunContext,
    analysis_request_expected: bool,
    runtime_instruction: str = '',
    preparation: PreparationResult | None = None,
    prior_messages: list[BaseMessage] | None = None,
):
    if not analysis_request_expected:
        return get_general_input(
            agent_request.user_message,
            namespace_id=runtime.conversation.insight_namespace_id,
            conversation_id=runtime.conversation.id,
            history_turn_limit=runtime.history_turn_limit,
            datasource_snapshot_override=runtime.active_datasource_snapshot,
        )
    return _build_agent_input_with_runtime_instruction(
        agent_request=agent_request,
        runtime=runtime,
        runtime_instruction=runtime_instruction,
        preparation=preparation,
        prior_messages=prior_messages,
    )


def _build_runtime_clarification_answer_instruction(
    clarification_context: dict[str, Any] | None,
) -> str:
    context = dict(clarification_context or {})
    facts = _canonical_runtime_clarification_facts(
        context.get("user_clarifications")
    )
    if not facts:
        return ''
    return (
        '<user-clarification-answers>\n'
        '以下是用户对本轮澄清问题的直接回答，是当前分析的权威业务事实。'
        '请在同一个 Runtime 洞察语义中继续原问题，保留每条回答的完整含义并映射到完整 Schema。'
        '仍可由数据核实的技术未知合并到一次有界 Probe；不要增加独立的指标计划、报告模板或语义审核回合。\n'
        + json.dumps(facts, ensure_ascii=False, separators=(',', ':'))
        + '\n</user-clarification-answers>'
    )


def _canonical_runtime_clarification_facts(
    clarifications: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Preserve user-confirmed facts without compiling them into analysis semantics."""
    facts: list[dict[str, Any]] = []
    for item in clarifications or []:
        if not isinstance(item, dict):
            continue
        answer_text = str(item.get('answer_text') or '').strip()
        resolved_value = str(item.get('resolved_value') or answer_text).strip()
        if not resolved_value:
            continue
        semantic_scope = item.get('semantic_scope')
        if not isinstance(semantic_scope, dict):
            semantic_scope = {}
        fact = {
            'clarification_id': str(item.get('clarification_id') or '').strip(),
            'item_id': str(
                item.get('clarification_item_id') or item.get('item_id') or ''
            ).strip(),
            'question': str(item.get('question') or '').strip(),
            'answer_text': answer_text,
            'resolved_value': resolved_value,
            'option_id': str(item.get('option_id') or '').strip(),
            'semantic_scope': dict(semantic_scope),
        }
        facts.append(fact)
    return facts


def _build_resumed_probe_observation_instruction(
    clarification_context: dict[str, Any] | None,
) -> str:
    """Restore bounded tool observations when a clarification resumes one task."""
    context = dict(clarification_context or {})
    observations = [
        item for item in context.get('probe_observations') or []
        if isinstance(item, dict)
    ]
    if not observations:
        return ''
    return (
        '<resumed-probe-observations>\n'
        '以下是发起本轮澄清前已经由 probe_analysis_data 得到的结构化观察。'
        '它们只用于恢复真实枚举、格式、覆盖范围和查询可用性，不替代用户回答的业务定义。\n'
        + json.dumps(observations, ensure_ascii=False, separators=(',', ':'))
        + '\n</resumed-probe-observations>'
    )


def _clarification_fact_key(item: dict[str, Any]) -> str:
    item_id = str(
        item.get('clarification_item_id') or item.get('item_id') or ''
    ).strip()
    if item_id:
        return f'item:{item_id}'
    clarification_id = str(item.get('clarification_id') or '').strip()
    question = str(item.get('question') or '').strip()
    if clarification_id and question:
        return f'question:{clarification_id}:{question}'
    return ''


def _merge_runtime_clarification_facts(
    persisted: list[dict[str, Any]] | None,
    current: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge only the same clarification item, preserving complementary answers."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, item in enumerate([*(persisted or []), *(current or [])]):
        if not isinstance(item, dict):
            continue
        answer_text = str(item.get('resolved_value') or item.get('answer_text') or '').strip()
        key = _clarification_fact_key(item)
        if not answer_text:
            continue
        if not key:
            key = f'append:{index}'
        if key in merged:
            order.remove(key)
        merged[key] = dict(item)
        order.append(key)
    return [merged[key] for key in order]


def _hydrate_persisted_runtime_clarifications(
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
) -> None:
    """Restore same-turn business facts when a completed or interrupted turn is rerun."""
    if runtime.short_term_memory.get('persisted_clarifications_loaded'):
        return
    # A new analysis or an in-place clarification resume already carries the
    # current answers in AgentRequest. Only an explicit rerun of an existing
    # turn needs to recover older answers from message history. Keeping this
    # lookup off the normal Preparation path avoids an extra database roundtrip
    # for every analysis.
    if not bool(getattr(runtime, 'is_rerun', False)):
        runtime.short_term_memory['persisted_clarifications_loaded'] = True
        return
    with session_scope(expire_on_commit=False) as session:
        persisted = ConversationContextService(session).get_turn_clarification_answers(
            runtime.turn.id
        )
    context = dict(agent_request.clarification_context or {})
    merged = _merge_runtime_clarification_facts(
        persisted,
        [
            item for item in context.get('user_clarifications') or []
            if isinstance(item, dict)
        ],
    )
    if merged:
        context['user_clarifications'] = merged
        agent_request.clarification_context = context
    logger.info(
        'restored persisted runtime clarifications: conversation_id=%s turn_id=%s count=%s',
        runtime.conversation.id,
        runtime.turn.id,
        len(merged),
    )
    runtime.short_term_memory['persisted_clarifications_loaded'] = True


def _resolve_agent_runtime_config(agent_request: AgentRequest):
    runtime_config = create_runtime_config(
        selected_model_id=agent_request.selected_llm_model_id
    )
    logger.info(
        "analysis LLM runtime resolved provider=%s model=%s base_url=%s",
        runtime_config.provider,
        runtime_config.model_id,
        runtime_config.base_url,
    )
    return runtime_config


def _prepare_analysis_context(
    *,
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
    runtime_config,
    stage_callback: Callable[[str], None] | None = None,
) -> PreparationResult | ScopeClarificationResult:
    """Select and enrich datasource Schema without owning insight semantics."""
    _hydrate_persisted_runtime_clarifications(runtime, agent_request)
    cached = runtime.short_term_memory.get("preparation_result")
    if isinstance(cached, PreparationResult):
        _clear_consumed_scope_clarification(runtime)
        return cached

    clarification_context = dict(
        getattr(agent_request, "clarification_context", None) or {}
    )
    restored_packet = runtime_work_packet_from_dict(
        clarification_context.get(RUNTIME_WORK_PACKET_MEMORY_KEY)
    )
    if restored_packet is not None:
        runtime.short_term_memory[RUNTIME_WORK_PACKET_MEMORY_KEY] = (
            resume_runtime_work_packet(
                restored_packet,
                focus=(
                    getattr(runtime.turn, "user_query", "")
                    or agent_request.user_message
                ),
            ).to_dict()
        )
    resumed = PreparationResult.from_resume_payload(
        clarification_context.get("preparation")
    )
    if resumed is not None:
        runtime.active_datasource_snapshot = resumed.selected_snapshot
        runtime.short_term_memory["preparation_result"] = resumed
        return resumed

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
    expected_time_range = _resolve_turn_expected_time_range(runtime, user_query)
    try:
        result = prepare_analysis(
            user_query=user_query,
            context_mode=getattr(runtime, "context_mode", "") or "fresh_analysis",
            expected_time_range=expected_time_range,
            datasource_snapshot=source_snapshot,
            conversation_context=conversation_context,
            runtime_config=runtime_config,
            execution_context={
                "token": (
                    getattr(agent_request, "platform_authorization", "")
                    or getattr(agent_request, "auth_token", "")
                    or ""
                ),
                "fedquery_authorization": (
                    getattr(agent_request, "fedquery_authorization", "") or ""
                ),
                "database_conn_info": dict(agent_request.database_conn_info or {}),
                "namespace_id": int(runtime.conversation.insight_namespace_id or 0),
                "conversation_id": int(runtime.conversation.id or 0),
                "turn_id": int(runtime.turn.id or 0),
                "exact_datasource_scope": bool(
                    getattr(agent_request, "exact_datasource_scope", False)
                ),
            },
            cancelled=lambda: _raise_if_analysis_task_cancelled(runtime),
            stage_callback=lambda stage: _notify_preparation_stage(
                runtime=runtime,
                stage=stage,
                stage_callback=stage_callback,
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
        if not selected_ids:
            raise NoDatasourceAvailableForAnalysis(
                "no datasource could be established for analysis"
            ) from exc
        result = PreparationResult(
            intent=IntentContract(
                business_goal=user_query or "完成本轮数据洞察分析",
                business_questions=[BusinessQuestionContract(
                    question_id="question_1",
                    business_question=user_query or "完成本轮数据洞察分析",
                    subject_scope="question_defined_scope",
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
    if isinstance(result, ScopeClarificationResult):
        return result
    _clear_consumed_scope_clarification(runtime)
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


PREPARATION_STAGE_MESSAGES = {
    "intent_analysis": "正在理解业务问题，明确本轮需要分析的业务范围。",
    "scope_capacity": "正在结合当前数据源规模确认合理的分析范围。",
    "semantic_selection": "正在从语义中心检索与业务问题相关的数据源。",
    "select_datasources": "正在核对候选数据源，确认其对业务问题的覆盖情况。",
    "business_context": "正在梳理已选数据源之间的业务关系与可分析事实。",
}


def _notify_preparation_stage(
    *,
    runtime: ConversationRunContext,
    stage: str,
    stage_callback: Callable[[str], None] | None,
) -> None:
    logger.info(
        "analysis preparation stage started: conversation_id=%s turn_id=%s stage=%s",
        runtime.conversation.id,
        runtime.turn.id,
        stage,
    )
    if stage_callback is not None:
        stage_callback(stage)


def _stream_analysis_preparation(
    *,
    runtime: ConversationRunContext,
    agent_request: AgentRequest,
    runtime_config,
) -> Iterator[dict[str, Any]]:
    """Run Preparation off the stream thread while forwarding stage progress."""
    stage_queue: Queue[str] = Queue()
    emitted_stages: set[str] = set()

    yield _build_progress_event(
        'status',
        conversation_id=runtime.conversation.id,
        turn_id=runtime.turn.id,
        stage='preparation',
        level='info',
        message='正在准备本轮分析所需的业务与数据上下文。',
    )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='analysis-preparation') as executor:
        future = executor.submit(
            _prepare_analysis_context,
            runtime=runtime,
            agent_request=agent_request,
            runtime_config=runtime_config,
            stage_callback=stage_queue.put,
        )
        while not future.done():
            _raise_if_analysis_task_cancelled(runtime)
            try:
                stage = stage_queue.get(timeout=0.25)
            except Empty:
                continue
            if stage in emitted_stages:
                continue
            emitted_stages.add(stage)
            yield _build_progress_event(
                'status',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage=f'preparation:{stage}',
                level='info',
                message=PREPARATION_STAGE_MESSAGES.get(
                    stage,
                    '正在完善本轮分析所需的数据上下文。',
                ),
            )

        while not stage_queue.empty():
            stage = stage_queue.get_nowait()
            if stage in emitted_stages:
                continue
            emitted_stages.add(stage)
            yield _build_progress_event(
                'status',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage=f'preparation:{stage}',
                level='info',
                message=PREPARATION_STAGE_MESSAGES.get(
                    stage,
                    '正在完善本轮分析所需的数据上下文。',
                ),
            )
        result = future.result()

    if isinstance(result, ScopeClarificationResult):
        yield _build_progress_event(
            'status',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='preparation:scope_clarification',
            level='info',
            message='当前全范围分析规模较大，需要补充更具体的分析范围。',
        )
    else:
        yield _build_progress_event(
            'status',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='preparation:complete',
            level='success',
            message='分析范围和数据关系已准备完成，开始执行洞察分析。',
        )
    return result


def _resolve_runtime_context_mode(runtime: ConversationRunContext, user_message: str) -> str:
    classification = resolve_context_mode_with_llm(runtime, user_message)
    runtime.context_mode = classification.mode
    clarification_relation = getattr(
        classification,
        'clarification_relation',
        'none',
    )
    runtime.short_term_memory['scope_clarification_relation'] = clarification_relation
    if clarification_relation in {'new_request', 'cancel'}:
        _clear_pending_scope_clarification(runtime)
    logger.info(
        "Resolved analysis context mode: conversation_id=%s turn_id=%s mode=%s source=%s confidence=%s clarification_relation=%s reason=%s",
        getattr(runtime.conversation, 'id', ''),
        getattr(runtime.turn, 'id', ''),
        classification.mode,
        classification.source,
        classification.confidence,
        clarification_relation,
        classification.reason,
    )
    return classification.mode


def _clear_pending_scope_clarification(runtime: ConversationRunContext) -> None:
    with session_scope(expire_on_commit=False) as session:
        ConversationContextService(session).clear_memory(
            runtime.conversation.id,
            PENDING_SCOPE_MEMORY_TYPE,
        )
        session.commit()


def _clear_consumed_scope_clarification(runtime: ConversationRunContext) -> None:
    if runtime.short_term_memory.get('scope_clarification_relation') != 'answer':
        return
    _clear_pending_scope_clarification(runtime)
    runtime.short_term_memory['scope_clarification_relation'] = 'none'


def _persist_scope_clarification(
    runtime: ConversationRunContext,
    clarification: ScopeClarificationResult,
) -> None:
    payload = clarification.memory_payload()
    payload.update({
        'conversation_id': int(runtime.conversation.id or 0),
        'turn_id': int(runtime.turn.id or 0),
        'turn_no': int(runtime.turn.turn_no or 0),
        'selected_datasource_ids': list(
            (runtime.active_datasource_snapshot or {}).get(
                'selected_datasource_ids',
                [],
            )
        ),
    })
    with session_scope(expire_on_commit=False) as session:
        ConversationContextService(session).set_memory(
            runtime.conversation.id,
            PENDING_SCOPE_MEMORY_TYPE,
            payload,
        )
        session.commit()


def _is_analysis_request_expected(runtime: ConversationRunContext, user_message: str) -> bool:
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
        '不要复述历史结论，必须重新调用 submit_analysis_program 基于当前会话数据源重新完成分析。'
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
        query_access = row.get('query_access') if isinstance(row.get('query_access'), dict) else {}
        relation_expression = str(query_access.get('relation_expression') or '').strip()
        if not datasource_id or not datasource_name:
            continue
        if relation_expression:
            lines.append(
                f'- datasource_id={datasource_id}，datasource_name="{datasource_name}"，'
                f'relation_expression="{relation_expression}"'
            )
        elif datasource_identifier:
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
        (item.get('query_access') or {}).get('uns_path')
        if isinstance(item.get('query_access'), dict) else None,
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
        query_access = item.get('query_access') if isinstance(item.get('query_access'), dict) else {}
        relation_expression = str(query_access.get('relation_expression') or '').strip()
        if relation_expression:
            hint += f' SQL 访问必须使用 relation_expression="{relation_expression}"。'
        elif datasource_identifier:
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
        '如果恢复出的目标需要数据分析、统计、联表、图表或报告，必须调用 submit_analysis_program 重新执行，而不是复述历史结论。',
        '访问表型数据源时，datasource_name 只用于语义匹配；真正写 SQL 必须遵守当前数据源 query_access/relation_expression。',
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
        query_access = item.get('query_access') if isinstance(item.get('query_access'), dict) else {}
        relation_expression = str(query_access.get('relation_expression') or '').strip()
        if relation_expression:
            lines.append(
                f'- datasource_id={datasource_id}, datasource_name="{datasource_name}", '
                f'relation_expression="{relation_expression}"'
            )
        elif datasource_identifier:
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
            '- 对于表型数据源，SQL 必须遵守 query_access/relation_expression；不要根据其它 selected 数据源名称自行猜 catalog、schema 或表名。',
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
    if getattr(runtime, 'context_mode', '') == 'general':
        return False
    if analysis_flow_started:
        return True
    return _rerun_has_prior_analysis_result(service, runtime)


def _build_rerun_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    *,
    analysis_request_expected: bool,
) -> str:
    """
    为“分析刷新/原轮次重跑”补一条首轮运行时指令。

    这里不改变原问题文本，只明确告诉模型：
    当前不是普通续问，也不是让它复述历史答案，而是要基于同一轮问题重新执行分析。
    """
    if not runtime.is_rerun:
        return ''

    if not analysis_request_expected:
        return (
            '当前操作是“刷新回复”，需要基于本轮原始问题和相关会话上下文重新回答。'
            '本轮已分流为普通问答，请直接用自然语言回答。'
        )

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
                '必须重新调用 submit_analysis_program，基于当前会话数据源重新完成该分析目标并生成新的分析结果。'
            )

    lines = [
        '当前操作是“刷新分析”，需要在同一轮内重新执行这条分析请求。',
        '刷新分析不是用户发起的新问题，也不是新的普通追问；不要重新判断成全新分析目标。',
        '本轮分析目标只能来自本轮原始问题；历史上下文只能用于理解背景、数据口径和歧义消解，',
        '不得把历史轮次的问题、结论、图表或表格追加、合并成本轮新的分析目标。',
        '请基于本轮原始问题和当前会话数据源重新完成分析，',
        '不要直接复述之前的结论，必须重新调用 submit_analysis_program 生成新的分析结果。',
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
    if not _is_analysis_request_expected(runtime, user_message):
        return False
    if _has_bound_datasource(runtime):
        return False
    return not DatasourceSelectionProviderRouter.can_discover_without_bound_datasource(
        runtime.active_datasource_snapshot
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
        evidence_enabled=runtime.evidence_enabled,
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
            analysis_task_id=runtime.analysis_task_id,
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
        output_contract=dict(runtime.output_contract or {"type": "standard"}),
        clarification_mode=runtime.clarification_mode,
        evidence_enabled=runtime.evidence_enabled,
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
    output_format: str = "standard",
) -> None:
    """分析型请求必须真实产出结构化分析结果。"""
    if output_format in {"text", "json"} and analysis_report:
        return
    if analysis_report and ((charts or []) or (tables or [])):
        return
    raise ValueError('本轮未生成完整分析产物：缺少结构化图表/表格结果或分析报告，请重新执行分析。')


def _build_analysis_start_instruction(
    user_message: str,
    output_format: str = "standard",
) -> str:
    """当模型没有进入分析流时，补一条最小运行时纠偏指令。"""
    result_instruction = (
        '按本轮 analysis-output-contract 生成并保存纯文本或 JSON 结果；'
        if output_format in {"text", "json"}
        else '生成图表、表格和分析报告；'
    )
    return (
        '当前用户输入是明确的数据分析任务，不能按普通问答结束。'
        f'请围绕这条请求真正启动分析执行：{user_message}。'
        '必须调用 submit_analysis_program 读取当前会话已绑定的数据源，'
        + result_instruction +
        '不要只给自然语言说明，也不要提前结束本轮。'
    )


def _should_use_report_only_fallback(
    *,
    analysis_request_expected: bool,
    round_index: int,
    has_any_artifact: bool,
    assistant_message: str,
    analysis_flow_started: bool = False,
) -> bool:
    """判断是否允许把最后一条可见回复直接当作报告产物收口。"""
    if not analysis_request_expected:
        return False
    if analysis_flow_started:
        return False
    if has_any_artifact:
        return False
    if not (assistant_message or '').strip():
        return False
    return round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1


def _promote_assistant_message_to_report(assistant_message: str, analysis_report: str) -> str:
    """在 report-only 兜底时，把可见回复提升为分析报告内容。"""
    return (analysis_report or '').strip() or (assistant_message or '').strip()


def _get_selected_datasource_names(
    runtime: ConversationRunContext,
    datasource_ids: set[int] | None = None,
) -> list[str]:
    """提取当前会话已绑定数据源名称，供失败原因说明使用。"""
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = snapshot.get('selected_datasource_snapshot') or []
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if datasource_ids is not None:
            try:
                datasource_id = int(row.get('datasource_id') or row.get('id') or 0)
            except (TypeError, ValueError):
                continue
            if datasource_id not in datasource_ids:
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
            analysis_task_id=runtime.analysis_task_id,
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
    if not executions:
        # A fallback reply should still explain the error when the short-lived
        # read session is temporarily unavailable. The caller owns the active
        # request session and is a useful best-effort secondary source.
        try:
            executions = service.get_turn_executions(
                runtime.turn.id,
                started_at=runtime.turn.started_at,
                analysis_task_id=runtime.analysis_task_id,
            )
        except Exception:
            executions = []
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


def _build_datasource_unavailable_report(
    runtime: ConversationRunContext,
    fallback_message: str = '',
) -> str:
    """Build a visible terminal result for a proven infrastructure failure."""
    executions = _load_turn_executions_fresh(runtime)
    latest_execution = next((
        execution for execution in reversed(executions)
        if str(getattr(execution, 'error_message', '') or '').strip()
    ), None)
    latest_error = (
        str(getattr(latest_execution, 'error_message', '') or '').strip()
        if latest_execution is not None else ''
    )
    required_datasource_ids: set[int] = set()
    if latest_execution is not None:
        try:
            payload = json.loads(str(getattr(latest_execution, 'result_payload_json', '') or '{}'))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        diagnostics = payload.get('diagnostics') if isinstance(payload, dict) else None
        terminal = diagnostics.get('_terminal_probe_failure') if isinstance(diagnostics, dict) else None
        for datasource_id in (terminal or {}).get('required_datasource_ids') or []:
            try:
                normalized_id = int(datasource_id)
            except (TypeError, ValueError):
                continue
            if normalized_id > 0:
                required_datasource_ids.add(normalized_id)
    latest_error = latest_error or fallback_message
    latest_error = re.split(r'\n\s*(?:错误定位|Error location)\s*[:：]', latest_error, maxsplit=1)[0]
    latest_error = re.sub(r'\s+', ' ', latest_error).strip()
    if len(latest_error) > 700:
        latest_error = latest_error[:697].rstrip() + '...'
    datasource_names = _get_selected_datasource_names(
        runtime,
        required_datasource_ids or None,
    )

    lines = [
        '## 数据源不可用说明',
        '',
        '### 当前结论',
        '',
        '本轮未生成统计结果、图表或业务结论，因为分析所需的数据源当前无法通过查询服务访问。'
        '这属于数据源连接、挂载或查询能力异常，不能解释为业务数据为空。',
    ]
    if datasource_names:
        lines.extend([
            '',
            '### 受影响的数据源',
            '',
            *[f'- {name}' for name in datasource_names],
        ])
    if latest_error:
        lines.extend([
            '',
            '### 已确认的技术原因',
            '',
            f'- {latest_error}',
        ])
    lines.extend([
        '',
        '### 建议处理',
        '',
        '1. 检查相关 UNS 节点是否已在联邦查询服务中正确注册或挂载。',
        '2. 检查查询连接器、scanner metadata 能力以及底层数据库连接状态。',
        '3. 环境恢复后重新执行本轮分析，才能得到可信的分析结果。',
    ])
    return '\n'.join(lines).strip()


def _requires_strict_json_result(runtime: ConversationRunContext) -> bool:
    output_contract = runtime.output_contract if isinstance(runtime.output_contract, dict) else {}
    return str(output_contract.get('type') or 'standard') == 'json'


def _should_use_failure_reason_fallback(
    *,
    analysis_request_expected: bool,
    round_index: int,
    has_any_artifact: bool,
    assistant_message: str,
    analysis_flow_started: bool = False,
) -> bool:
    """当最后没有任何自然语言回复时，补一条失败原因说明产物。"""
    if not analysis_request_expected:
        return False
    if analysis_flow_started:
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

    if tool_name in {'execute_python', 'submit_analysis_program'}:
        title = args.get('title') or '数据分析任务'
        return f"已生成分析代码，准备执行：{title}"

    if tool_name == 'probe_analysis_data':
        title = args.get('title') or '数据探测'
        return f"已生成探测代码，准备核实：{title}"

    if tool_name == 'read_runtime_reference':
        return "正在查阅分析工具的用法说明。"

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
    """Return a patch target only when the tool explicitly proved patchability."""
    for execution in reversed(executions):
        if getattr(execution, 'execution_status', '') == 'success':
            continue
        generated_code = (getattr(execution, 'generated_code', '') or '').strip()
        if not generated_code:
            continue
        payload = _extract_execution_payload(execution)
        repair_target = payload.get('repair_target')
        if not isinstance(repair_target, dict):
            continue
        base_execution_id = int(repair_target.get('base_execution_id') or 0)
        if base_execution_id != int(getattr(execution, 'id', 0) or 0):
            continue
        return dict(repair_target)
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


def _build_structured_execution_feedback(
    executions: list[Any],
    *,
    include_probe_ledger: bool = True,
) -> list[dict[str, Any]]:
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
        item: dict[str, Any] = {
            'execution_id': getattr(execution, 'id', None),
            'feedback_kind': 'observation' if retry_type == 'probe_feedback' else 'execution_failure',
        }
        if retry_type:
            item['retry_type'] = retry_type
        message = str(payload.get('message') or getattr(execution, 'error_message', '') or '').strip()
        if message:
            item['message'] = _clip_retry_text(message, 700)
        if retry_type == 'probe_feedback' and include_probe_ledger:
            probe_ledger = _extract_probe_fact_ledger(payload)
            if probe_ledger:
                item['probe_fact_ledger'] = probe_ledger
        elif isinstance(diagnostics, dict) and diagnostics:
            item['diagnostics'] = _compact_probe_diagnostic_facts(diagnostics)
        if retry_type != 'probe_feedback':
            failed_program = str(payload.get('current_failed_code') or '').strip()
            if failed_program:
                item['failed_program'] = _clip_retry_text(
                    failed_program, TOOL_ERROR_CURRENT_CODE_MAX_CHARS
                )
                item['failed_program_truncated'] = bool(
                    payload.get('current_failed_code_truncated')
                )
        if isinstance(repair_instructions, list) and repair_instructions:
            item['repair_instructions'] = [str(value) for value in repair_instructions if str(value).strip()]
        if item:
            feedback.append(item)
        if len(feedback) >= RETRY_LATEST_ERROR_LIMIT:
            break
    return list(reversed(feedback))


def _extract_probe_fact_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a canonical, bounded record of facts learned by Probe.

    The next Runtime step needs executed query facts, not an accumulating replay
    of stdout, SQL text and earlier speculative plans.  This adapter also
    accepts historical Probe payloads that predate ``probe_fact_ledger``.
    """
    diagnostics = payload.get('diagnostics')
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    ledger = diagnostics.get('probe_fact_ledger')
    if isinstance(ledger, dict):
        return ledger

    observations: list[dict[str, Any]] = []
    for fallback_key, snapshot in (payload.get('query_snapshots') or {}).items():
        if not isinstance(snapshot, dict):
            continue
        execution = snapshot.get('execution')
        implementation = snapshot.get('implementation')
        execution = execution if isinstance(execution, dict) else {}
        implementation = implementation if isinstance(implementation, dict) else {}
        observation = {
            'query_key': str(snapshot.get('query_key') or fallback_key),
            'datasource_identifiers': (
                implementation.get('datasource_identifiers')
                or ([implementation.get('datasource_identifier')]
                    if implementation.get('datasource_identifier') else [])
            ),
            'query_constraints': snapshot.get('query_constraints') or {},
            'query_scope': snapshot.get('query_scope') or {},
            'status': execution.get('status'),
            'row_count': execution.get('row_count'),
            'effective_no_data': execution.get('effective_no_data'),
            'columns': execution.get('columns') or [],
            'error_type': execution.get('error_type'),
        }
        observations.append({
            key: value for key, value in observation.items()
            if value not in (None, '', [], {})
        })
    diagnostic_facts = _compact_probe_diagnostic_facts(diagnostics)
    confirmed_facts = (
        diagnostics.get('confirmed_facts')
        if isinstance(diagnostics.get('confirmed_facts'), list) else []
    )
    unresolved_facts = (
        diagnostics.get('unresolved_facts')
        if isinstance(diagnostics.get('unresolved_facts'), list) else []
    )
    if not (observations or confirmed_facts or unresolved_facts or diagnostic_facts):
        return {}
    return {
        'schema_version': 1,
        'observations': observations,
        'confirmed_facts': confirmed_facts,
        'unresolved_facts': unresolved_facts,
        'diagnostic_facts': diagnostic_facts,
    }


def _compact_probe_diagnostic_facts(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Keep small Probe observations that predate the canonical fact ledger.

    Historical Probe feedback frequently stores useful facts such as a status
    enumeration directly in ``diagnostics``.  Dropping those fields makes a
    clarification resume forget why it was asked.  Keep only JSON-shaped,
    bounded values and deliberately exclude payloads that replay source code,
    snapshots, or the ledger itself.
    """
    ignored_keys = {
        'probe_fact_ledger', 'confirmed_facts', 'unresolved_facts',
        'current_failed_code', 'code_error_context', 'query_snapshots',
        'repair_target', 'repair_instructions',
    }
    facts: dict[str, Any] = {}
    for key, value in diagnostics.items():
        if key in ignored_keys or len(facts) >= 16:
            continue
        compact = _compact_probe_diagnostic_value(value)
        if compact is not None:
            facts[str(key)] = compact
    return facts


def _compact_probe_diagnostic_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe bounded diagnostic value, or ``None`` when unsafe."""
    if depth > 2:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip_retry_text(value, 300)
    if isinstance(value, list):
        items = [
            compact for item in value[:20]
            if (compact := _compact_probe_diagnostic_value(item, depth=depth + 1)) is not None
        ]
        return items
    if isinstance(value, dict):
        compact_items: dict[str, Any] = {}
        for key, item in list(value.items())[:12]:
            compact = _compact_probe_diagnostic_value(item, depth=depth + 1)
            if compact is not None:
                compact_items[str(key)] = compact
        return compact_items
    return None


def _iter_stream_update_payloads(chunk: Any) -> Iterator[dict[str, Any]]:
    """Yield message-bearing node updates, ignoring LangGraph control events.

    With ``interrupt_after`` enabled, LangGraph can emit a tuple for an
    interrupted node alongside ordinary update dictionaries. That tuple is a
    graph-control envelope rather than a node state payload.
    """
    if not isinstance(chunk, dict):
        return

    for payload in chunk.values():
        if isinstance(payload, dict):
            yield payload
            continue
        if isinstance(payload, tuple):
            for item in reversed(payload):
                if isinstance(item, dict):
                    yield item
                    break


def _message_identity(message: BaseMessage) -> tuple[Any, ...]:
    message_id = getattr(message, 'id', None)
    if message_id:
        return ('id', message_id)
    return (
        type(message).__name__,
        getattr(message, 'tool_call_id', None),
        json.dumps(getattr(message, 'tool_calls', None), ensure_ascii=False, default=str),
        str(getattr(message, 'content', '') or ''),
    )


def _merge_agent_round_messages(
    input_messages: list[BaseMessage],
    output_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Preserve one coherent ReAct transcript across outer recovery rounds."""
    merged = list(input_messages)
    known = {_message_identity(message) for message in merged}
    for message in output_messages:
        identity = _message_identity(message)
        if identity in known:
            continue
        known.add(identity)
        merged.append(message)
    return merged


def _new_agent_round_messages(
    input_messages: list[BaseMessage],
    output_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Return only messages created in the current graph invocation."""
    known = {_message_identity(message) for message in input_messages}
    current: list[BaseMessage] = []
    current_positions: dict[tuple[Any, ...], int] = {}
    for message in output_messages:
        identity = _message_identity(message)
        if identity in known:
            continue
        message_id = getattr(message, 'id', None)
        stable_identity = ('id', message_id) if message_id else identity
        if stable_identity in current_positions:
            current[current_positions[stable_identity]] = message
            continue
        current_positions[stable_identity] = len(current)
        current.append(message)
    return current


def _runtime_message_stats(messages: list[BaseMessage]) -> dict[str, Any]:
    """Summarize message volume without logging user or analysis content."""
    by_type: dict[str, int] = {}
    content_chars = 0
    tool_call_count = 0
    for message in messages:
        message_type = str(
            getattr(message, "type", "") or type(message).__name__
        )
        by_type[message_type] = by_type.get(message_type, 0) + 1
        content = getattr(message, "content", "")
        if isinstance(content, str):
            content_chars += len(content)
        else:
            content_chars += len(json.dumps(content, ensure_ascii=False, default=str))
        tool_call_count += len(getattr(message, "tool_calls", None) or [])
    return {
        "message_count": len(messages),
        "message_types": by_type,
        "content_chars": content_chars,
        "estimated_tokens": estimate_messages_tokens(messages),
        "tool_call_count": tool_call_count,
    }


def _runtime_model_response_stats(messages: list[BaseMessage]) -> dict[str, Any]:
    """Collect standard model usage and finish metadata from one graph round."""
    finish_reasons: list[str] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    usage_observed = False
    latest_messages: dict[tuple[Any, ...], BaseMessage] = {}
    for index, message in enumerate(messages):
        message_id = getattr(message, "id", None)
        identity = ("id", message_id) if message_id else ("index", index)
        latest_messages[identity] = message
    for message in latest_messages.values():
        response_metadata = getattr(message, "response_metadata", None)
        response_token_usage: dict[str, Any] = {}
        if isinstance(response_metadata, dict):
            finish_reason = str(
                response_metadata.get("finish_reason")
                or response_metadata.get("stop_reason")
                or ""
            ).strip()
            if finish_reason and finish_reason not in finish_reasons:
                finish_reasons.append(finish_reason)
            token_usage = response_metadata.get("token_usage")
            if isinstance(token_usage, dict):
                response_token_usage = token_usage
        usage_metadata = getattr(message, "usage_metadata", None)
        if isinstance(usage_metadata, dict):
            input_tokens += int(usage_metadata.get("input_tokens") or 0)
            output_tokens += int(usage_metadata.get("output_tokens") or 0)
            total_tokens += int(usage_metadata.get("total_tokens") or 0)
            usage_observed = True
        elif response_token_usage:
            input_tokens += int(
                response_token_usage.get("input_tokens")
                or response_token_usage.get("prompt_tokens")
                or 0
            )
            output_tokens += int(
                response_token_usage.get("output_tokens")
                or response_token_usage.get("completion_tokens")
                or 0
            )
            total_tokens += int(response_token_usage.get("total_tokens") or 0)
            usage_observed = True
    if usage_observed and total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    truncated_reasons = {
        "length",
        "max_tokens",
        "max_output_tokens",
        "model_length",
    }
    return {
        "finish_reasons": finish_reasons,
        "truncated": any(reason.lower() in truncated_reasons for reason in finish_reasons),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        } if usage_observed else {},
    }


def _log_runtime_round_input(
    *,
    runtime: ConversationRunContext,
    round_index: int,
    mode: str,
    messages: list[BaseMessage],
) -> None:
    logger.info(
        "Runtime模型轮次开始 conversation_id=%s turn_id=%s round=%s mode=%s input=%s",
        runtime.conversation.id,
        runtime.turn.id,
        round_index + 1,
        mode,
        _runtime_message_stats(messages),
    )


def _log_runtime_round_output(
    *,
    runtime: ConversationRunContext,
    round_index: int,
    mode: str,
    messages: list[BaseMessage],
    transcript: list[BaseMessage],
    duration_seconds: float,
) -> None:
    model_stats = _runtime_model_response_stats(messages)
    tool_calls: list[dict[str, Any]] = []
    for message in messages:
        for tool_call in getattr(message, 'tool_calls', None) or []:
            args = tool_call.get('args') if isinstance(tool_call, dict) else None
            args = args if isinstance(args, dict) else {}
            code = args.get('code')
            patches = args.get('patches')
            tool_calls.append({
                'name': str(tool_call.get('name') or ''),
                'arg_keys': sorted(str(key) for key in args),
                'code_chars': len(code) if isinstance(code, str) else 0,
                'base_execution_id': int(args.get('base_execution_id') or 0),
                'patch_count': len(patches) if isinstance(patches, list) else 0,
            })
    payload = {
        "duration_seconds": round(duration_seconds, 3),
        "output": _runtime_message_stats(messages),
        "model": model_stats,
        "transcript": _runtime_message_stats(transcript),
        "tool_calls": tool_calls,
    }
    log_method = logger.warning if model_stats["truncated"] else logger.info
    log_method(
        "Runtime模型轮次结束 conversation_id=%s turn_id=%s round=%s mode=%s stats=%s",
        runtime.conversation.id,
        runtime.turn.id,
        round_index + 1,
        mode,
        payload,
    )


def _agent_input_messages(agent_input: Any) -> list[BaseMessage]:
    messages = getattr(agent_input, 'messages', None)
    if messages is None and isinstance(agent_input, dict):
        messages = agent_input.get('messages')
    return list(messages or [])


def _extract_runtime_clarification(
    messages: list[BaseMessage],
) -> dict[str, Any] | None:
    """Read the terminal clarification tool payload from one Runtime graph run."""
    for message in reversed(messages):
        content = getattr(message, 'content', '')
        if not isinstance(content, str) or not content.lstrip().startswith('{'):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if (
            payload.get('tool') != 'request_analysis_clarification'
            or payload.get('status') != 'clarification_required'
        ):
            continue
        clarification = payload.get('clarification')
        return dict(clarification) if isinstance(clarification, dict) else None
    return None


def _build_clarification_resume_context(
    *,
    preparation: PreparationResult | None,
    runtime: ConversationRunContext,
    transcript: list[BaseMessage] | None = None,
) -> dict[str, Any]:
    if transcript:
        _update_short_term_memory_from_messages(runtime, transcript)
    memory = getattr(runtime, 'short_term_memory', None)
    memory = memory if isinstance(memory, dict) else {}
    probe_observations = [
        item
        for item in _build_structured_execution_feedback(
            _load_turn_executions_fresh(runtime)
        )
        if str(item.get('retry_type') or '') == 'probe_feedback'
    ]
    return {
        "preparation": preparation.resume_payload() if preparation is not None else None,
        "context_mode": getattr(runtime, "context_mode", "") or "fresh_analysis",
        "probe_observations": probe_observations,
        **(
            {RUNTIME_WORK_PACKET_MEMORY_KEY: copy.deepcopy(memory[RUNTIME_WORK_PACKET_MEMORY_KEY])}
            if isinstance(memory.get(RUNTIME_WORK_PACKET_MEMORY_KEY), dict) else {}
        ),
        **({'react_messages': messages_to_dict(transcript)} if transcript else {}),
    }


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
    packet = runtime_work_packet_from_dict(payload.get(RUNTIME_WORK_PACKET_PAYLOAD_KEY))
    if packet is not None:
        memory = getattr(runtime, 'short_term_memory', None)
        if not isinstance(memory, dict):
            memory = {}
            setattr(runtime, 'short_term_memory', memory)
        memory[RUNTIME_WORK_PACKET_MEMORY_KEY] = packet.to_dict()
    tool_name = str(payload.get('tool') or '').strip()
    if tool_name == 'read_runtime_datasource_context':
        _update_runtime_schema_detail_workspace(runtime, payload)
        return
    if tool_name not in ('', 'execute_python'):
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
        task_id = int(getattr(runtime, 'analysis_task_id', 0) or 0)
        if short_term_memory.get('execution_feedback_task_id') != task_id:
            short_term_memory['execution_feedback_task_id'] = task_id
            short_term_memory['seen_execution_feedback'] = []
            short_term_memory.pop('probe_no_data_retry', None)
            short_term_memory['probe_feedback_count'] = 0
            short_term_memory['probe_fact_ledger'] = []
        execution_id = payload.get('execution_id')
        if execution_id:
            key = f'{execution_id}:{error_type}'
            seen = short_term_memory.setdefault('seen_execution_feedback', [])
            if key in seen:
                return
            seen.append(key)
        if error_type == 'probe_feedback':
            short_term_memory['probe_feedback_count'] = (
                int(short_term_memory.get('probe_feedback_count') or 0) + 1
            )
            probe_ledger = _extract_probe_fact_ledger(payload)
            if probe_ledger:
                ledgers = short_term_memory.setdefault('probe_fact_ledger', [])
                ledgers.append(probe_ledger)
        else:
            diagnostics = payload.get('diagnostics') or {}
            if isinstance(diagnostics, dict) and 'probe_no_data_retry_count' in diagnostics:
                short_term_memory['probe_no_data_retry'] = {
                    'count': diagnostics['probe_no_data_retry_count'],
                    'limit': diagnostics.get('probe_no_data_retry_limit'),
                    'limit_reached': diagnostics.get('probe_no_data_limit_reached', False),
                    'last_error_type': error_type,
                }


def _update_runtime_schema_detail_workspace(
    runtime: ConversationRunContext,
    payload: dict[str, Any],
) -> None:
    """Remember only exact Schema details explicitly opened by Runtime.

    The outer ReAct loop deliberately rebuilds its messages after each tool
    batch, rather than replaying raw ToolMessages. Schema reads therefore need
    a compact canonical workspace entry so the next model turn can use the
    fields it just requested. This does not broaden datasource scope or retain
    query output.
    """
    raw_sources = payload.get('datasources')
    if not isinstance(raw_sources, list):
        return
    memory = getattr(runtime, 'short_term_memory', None)
    if not isinstance(memory, dict):
        memory = {}
        setattr(runtime, 'short_term_memory', memory)

    memory[RUNTIME_SCHEMA_DETAIL_MEMORY_KEY] = merge_schema_sources(
        memory.get(RUNTIME_SCHEMA_DETAIL_MEMORY_KEY), raw_sources,
    )


def _update_short_term_memory_from_messages(runtime: ConversationRunContext, messages: list[Any]) -> None:
    if not isinstance(getattr(runtime, 'short_term_memory', None), dict):
        runtime.short_term_memory = {}
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


def _build_analysis_recovery_instruction(
    service: ConversationContextService,
    runtime: ConversationRunContext,
    round_index: int,
) -> str:
    """Recover exact execution feedback without introducing new business rules."""
    executions = _load_turn_executions_fresh(runtime)
    feedback = _build_structured_execution_feedback(
        executions,
        include_probe_ledger=False,
    )
    lines = [_build_continued_react_recovery_instruction()]
    output = getattr(runtime, 'output_contract', None) or {'type': 'standard'}
    lines.append('继续遵循当前输出协议：' + json.dumps(output, ensure_ascii=False))
    if getattr(runtime, 'clarification_mode', 'same_turn') == 'same_turn':
        lines.append('只有业务歧义无法由现有信息或 Probe 消除时，才调用 request_analysis_clarification；技术错误不交给用户澄清。')
    else:
        lines.append('当前调用不支持用户澄清；对无法确定的业务含义说明限制，不编造规则或指标值。')
    latest_repair_target = _get_latest_failed_repair_target(executions)
    if latest_repair_target:
        lines.extend([
            'repair_target 表示后端已把本次技术错误限定到可验证的 Python 语句范围；'
            '每个 patch 必须完整替换一个允许的语句或代码块，并保留其余已执行程序。'
            '查询范围、字段归属、JOIN、聚合、指标计算或报告语义发生变化时，不得借局部 patch 改变分析设计。',
            json.dumps(latest_repair_target, ensure_ascii=False),
        ])
    if getattr(runtime, 'short_term_memory', {}).get('probe_fact_ledger'):
        lines.append('已执行 Probe 的事实账本由当前 Runtime 工作视图统一投影；不要从旧恢复消息重建或重复粘贴它。')
    if getattr(runtime, 'short_term_memory', {}).get(RUNTIME_SCHEMA_DETAIL_MEMORY_KEY):
        lines.append('已按需读取的精确 Schema 由当前 Runtime 工作视图统一投影；直接使用该工作视图，不重复读取同一明细。')
    if feedback:
        lines.extend([
            '以下是本次任务真实执行的反馈；Probe 观察不是执行失败，也不自动确定业务筛选条件：',
            json.dumps(feedback, ensure_ascii=False),
        ])
    if round_index >= MAX_ANALYSIS_AGENT_ROUNDS - 1:
        lines.append('本次任务的执行预算即将用尽；未完成分析时如实说明，不能把预算耗尽当作无数据证明。')
    return '\n'.join(lines)


def _build_continued_react_recovery_instruction() -> str:
    """State the bounded recovery boundary for a fresh Runtime model turn."""
    return (
        '继续完成同一分析任务。上方提供的是后端归纳的真实 Probe 事实和执行反馈，而不是完整工具转录；'
        '请消费事实账本、diagnostics、repair_instructions、失败程序和源码位置，在保持原问题、数据范围及产物意图的前提下修复；'
        '已有 Probe 事实只能复用其已证明的范围和观察，不重复探测同一事实；'
        '当已有观察足以实现分析时直接调用 submit_analysis_program，不能再提交局部或占位程序；'
        '若仍有影响答案的技术未知，可用 probe_analysis_data 核实，不重复已确认的观察。'
    )


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
    _apply_invocation_policy(runtime, agent_request)
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
        try:
            preparation = (
                _prepare_analysis_context(
                    runtime=runtime,
                    agent_request=agent_request,
                    runtime_config=runtime_config,
                )
                if analysis_request_expected
                else None
            )
        except NoDatasourceAvailableForAnalysis:
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

        if isinstance(preparation, ScopeClarificationResult):
            if runtime.clarification_mode == "disabled":
                raise ValueError(
                    "当前调用的分析范围仍存在关键歧义，且该调用模式不支持用户澄清："
                    + preparation.assistant_message()
                )
            _persist_scope_clarification(runtime, preparation)
            assistant_message = preparation.assistant_message()
            allow_report_only = True
            return _finalize_run_with_fresh_session(
                runtime=runtime,
                username=agent_request.username,
                user_message=agent_request.user_message,
                analysis_flow_started=False,
                assistant_message=assistant_message,
                analysis_report='',
                charts=[],
                tables=[],
                allow_report_only=allow_report_only,
            )

        agent_round_messages: list[BaseMessage] | None = None

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            _raise_if_analysis_task_cancelled(runtime)
            agent = _get_runtime_agent(
                runtime_config,
                analysis_request_expected=analysis_request_expected,
                clarification_mode=runtime.clarification_mode,
            )
            runtime_instruction = ''
            if analysis_flow_started:
                runtime_instruction = (
                    _build_continued_react_recovery_instruction()
                    if agent_round_messages
                    else _build_analysis_recovery_instruction(
                        service=service,
                        runtime=runtime,
                        round_index=round_index,
                    )
                )
            elif runtime.is_rerun:
                runtime_instruction = _build_rerun_instruction(
                    service, runtime, analysis_request_expected=analysis_request_expected,
                )
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
                runtime_instruction = _build_analysis_start_instruction(
                    agent_request.user_message,
                    str(runtime.output_contract.get("type") or "standard"),
                )

            agent_input = _build_runtime_agent_input(
                agent_request=agent_request,
                runtime=runtime,
                analysis_request_expected=analysis_request_expected,
                runtime_instruction=runtime_instruction,
                preparation=preparation,
                prior_messages=agent_round_messages,
            )
            round_input_messages = _agent_input_messages(agent_input)
            _log_runtime_round_input(
                runtime=runtime,
                round_index=round_index,
                mode="invoke",
                messages=round_input_messages,
            )
            round_started_at = time.monotonic()
            agent_response = agent.invoke(
                agent_input,
                context=_build_agent_context(
                    agent_request,
                    runtime,
                    preparation,
                ),
            )
            _raise_if_analysis_task_cancelled(runtime)
            response_messages = list(agent_response.get('messages', []))
            round_output_messages = _new_agent_round_messages(
                round_input_messages,
                response_messages,
            )
            round_transcript = _merge_agent_round_messages(
                round_input_messages,
                response_messages,
            )
            _log_runtime_round_output(
                runtime=runtime,
                round_index=round_index,
                mode="invoke",
                messages=round_output_messages,
                transcript=round_transcript,
                duration_seconds=time.monotonic() - round_started_at,
            )
            # The outer recovery loop is still one Runtime work session. Keep
            # its lossless transcript so the request-view middleware can
            # project current code, observations and failures on the next
            # focused model call instead of reconstructing a fresh prompt.
            agent_round_messages = round_transcript
            clarification = _extract_runtime_clarification(response_messages)
            if clarification is not None:
                raise ValueError(
                    "当前分析需要用户补充业务定义，请通过异步分析接口完成同轮次澄清。"
                )
            # Persist from the lossless round, not merely the returned suffix.
            # Tool results need their paired calls to drive the same explicit
            # workspace reducers used by the SSE path.
            _update_short_term_memory_from_messages(runtime, round_transcript)
            _raise_if_terminal_probe_unavailability(round_transcript)
            assistant_message, analysis_report, charts, tables, raw_tool_call_detected = _extract_invoke_result(
                response_messages
            )

            # A general answer has no execution or artifact contract. In
            # particular, stale executions from an earlier failed run of the
            # same turn must not pull a rerun into the analysis repair loop.
            if not analysis_request_expected:
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
                analysis_flow_started=analysis_flow_started,
            ):
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if _should_use_failure_reason_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
                analysis_flow_started=analysis_flow_started,
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
                _ensure_analysis_result_ready(
                    analysis_report=analysis_report,
                    charts=charts,
                    tables=tables,
                    output_format=str(runtime.output_contract.get("type") or "standard"),
                )
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
    except RuntimeDatasourceUnavailableError as exc:
        if _requires_strict_json_result(runtime):
            user_error_message = str(exc)
            _fail_run_with_fresh_session(
                runtime,
                user_error_message,
                preserve_existing_results=runtime.is_rerun,
            )
            raise RuntimeError(user_error_message) from exc
        analysis_report = _build_datasource_unavailable_report(runtime, str(exc))
        assistant_message = '分析所需数据源当前不可访问，已生成问题说明。'
        runtime.evidence_enabled = False
        return _finalize_run_with_fresh_session(
            runtime=runtime,
            username=agent_request.username,
            user_message=agent_request.user_message,
            analysis_flow_started=True,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            charts=[],
            tables=[],
            allow_report_only=True,
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
    # Keep this entry point self-contained: callers that resume or directly invoke
    # the runtime must receive the same policy defaults as controller-driven calls.
    _apply_invocation_policy(runtime, agent_request)
    active_snapshot = runtime.active_datasource_snapshot
    if isinstance(active_snapshot, dict):
        rows = active_snapshot.get('selected_datasource_snapshot') or []
        if any(
            isinstance(item, dict)
            and item.get('datasource_type') == 'table'
            and not isinstance(item.get('query_access'), dict)
            for item in rows
        ):
            with session_scope(expire_on_commit=False) as session:
                hydrated_rows = ConversationContextService(
                    session
                ).hydrate_datasource_snapshot_query_access(rows)
            runtime.active_datasource_snapshot = {
                **active_snapshot,
                'selected_datasource_snapshot': hydrated_rows,
            }
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
            message=(
                '正在重新执行本轮分析。'
                if analysis_request_expected
                else '正在重新生成本轮回复。'
            ),
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
        try:
            preparation = None
            if analysis_request_expected:
                preparation = yield from _stream_analysis_preparation(
                    runtime=runtime,
                    agent_request=agent_request,
                    runtime_config=runtime_config,
                )
        except NoDatasourceAvailableForAnalysis:
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

        if isinstance(preparation, ScopeClarificationResult):
            if runtime.clarification_mode == "disabled":
                raise ValueError(
                    "当前调用的分析范围仍存在关键歧义，且该调用模式不支持用户澄清："
                    + preparation.assistant_message()
                )
            _persist_scope_clarification(runtime, preparation)
            assistant_message = preparation.assistant_message()
            allow_report_only = True
            yield _build_progress_event(
                'assistant',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage='scope_clarification',
                message=assistant_message,
            )
            _finalize_run_with_fresh_session(
                runtime=runtime,
                username=agent_request.username,
                user_message=agent_request.user_message,
                analysis_flow_started=False,
                assistant_message=assistant_message,
                analysis_report='',
                charts=[],
                tables=[],
                allow_report_only=allow_report_only,
            )
            yield _build_progress_event(
                'done',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
            )
            return

        agent_round_messages: list[BaseMessage] | None = None

        for round_index in range(MAX_ANALYSIS_AGENT_ROUNDS):
            _raise_if_analysis_task_cancelled(runtime)
            agent = _get_runtime_agent(
                runtime_config,
                analysis_request_expected=analysis_request_expected,
                clarification_mode=runtime.clarification_mode,
            )
            raw_tool_call_detected = False
            runtime_instruction = ''
            tool_result_ready = False
            if analysis_flow_started:
                runtime_instruction = (
                    _build_continued_react_recovery_instruction()
                    if agent_round_messages
                    else _build_analysis_recovery_instruction(
                        service=service,
                        runtime=runtime,
                        round_index=round_index,
                    )
                )
                yield _build_progress_event(
                    'status',
                    conversation_id=runtime.conversation.id,
                    turn_id=runtime.turn.id,
                    stage='analysis_continue',
                    level='info',
                    message='正在基于已确认的 Schema 与执行观察继续完成分析。',
                )
            elif runtime.is_rerun:
                runtime_instruction = _build_rerun_instruction(
                    service, runtime, analysis_request_expected=analysis_request_expected,
                )
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
                runtime_instruction = _build_analysis_start_instruction(
                    agent_request.user_message,
                    str(runtime.output_contract.get("type") or "standard"),
                )
                yield _build_progress_event(
                    'status',
                    conversation_id=runtime.conversation.id,
                    turn_id=runtime.turn.id,
                    stage='analysis_retry',
                    level='warning',
                    message='正在重新引导模型进入分析执行阶段。',
                )

            agent_input = _build_runtime_agent_input(
                agent_request=agent_request,
                runtime=runtime,
                analysis_request_expected=analysis_request_expected,
                runtime_instruction=runtime_instruction,
                preparation=preparation,
                prior_messages=agent_round_messages,
            )
            round_input_messages = _agent_input_messages(agent_input)
            _log_runtime_round_input(
                runtime=runtime,
                round_index=round_index,
                mode="stream",
                messages=round_input_messages,
            )
            round_started_at = time.monotonic()
            streamed_messages: list[BaseMessage] = []
            deferred_assistant_messages: list[str] = []
            progress_calls = {}
            active_progress_call_ids: set[str] = set()
            for stream_mode, chunk in agent.stream(
                agent_input,
                context=_build_agent_context(
                    agent_request,
                    runtime,
                    preparation,
                ),
                stream_mode=["updates", "custom"],
            ):
                _raise_if_analysis_task_cancelled(runtime)
                if stream_mode == "updates":
                    for payload in _iter_stream_update_payloads(chunk):
                        messages = payload.get("messages", [])
                        for message in messages:
                            streamed_messages.append(message)
                            tool_calls = getattr(message, 'tool_calls', None)
                            raw_tool_call = _extract_raw_tool_call(getattr(message, 'content', ''))
                            cleaned = _clean_message_content(getattr(message, 'content', ''))
                            result_call_id = getattr(message, 'tool_call_id', None)
                            if result_call_id in active_progress_call_ids:
                                progress = tool_progress(progress_calls[result_call_id], getattr(message, 'content', ''))
                                if progress:
                                    yield {**progress, 'conversation_id': runtime.conversation.id, 'turn_id': runtime.turn.id}
                                active_progress_call_ids.discard(result_call_id)
                            if isinstance(cleaned, str) and cleaned.strip().startswith('{'):
                                try:
                                    tool_feedback_payload = json.loads(cleaned)
                                except Exception:
                                    tool_feedback_payload = {}
                                if isinstance(tool_feedback_payload, dict):
                                    _update_short_term_memory_from_tool_payload(runtime, tool_feedback_payload)

                            if tool_calls:
                                for tool_call in tool_calls:
                                    progress_calls[tool_call['id']] = tool_call
                                    progress = tool_progress(tool_call)
                                    if progress:
                                        active_progress_call_ids.add(tool_call['id'])
                                        yield {**progress, 'conversation_id': runtime.conversation.id, 'turn_id': runtime.turn.id}
                                        continue
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
                            deferred_assistant_messages.append(cleaned)

                elif stream_mode == "custom":
                    if isinstance(chunk, dict):
                        chunk.setdefault('conversation_id', runtime.conversation.id)
                        chunk.setdefault('turn_id', runtime.turn.id)
                        yield chunk
                        terminal_stage = str(chunk.get('stage') or '')
                        if terminal_stage in {'tool_finished', 'tool_result', 'tool_error'}:
                            execution_call_id = next((
                                call_id
                                for call_id, call in reversed(list(progress_calls.items()))
                                if call_id in active_progress_call_ids
                                and call.get('name') in {'submit_analysis_program', 'execute_python'}
                            ), None)
                            if execution_call_id:
                                terminal_status = (
                                    'error' if terminal_stage == 'tool_error' else 'success'
                                )
                                progress = tool_progress(
                                    progress_calls[execution_call_id],
                                    {'status': terminal_status},
                                )
                                active_progress_call_ids.discard(execution_call_id)
                                if progress:
                                    yield {
                                        **progress,
                                        'conversation_id': runtime.conversation.id,
                                        'turn_id': runtime.turn.id,
                                    }
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

            round_transcript = _merge_agent_round_messages(
                round_input_messages,
                streamed_messages,
            )
            round_output_messages = _new_agent_round_messages(
                round_input_messages,
                streamed_messages,
            )
            _log_runtime_round_output(
                runtime=runtime,
                round_index=round_index,
                mode="stream",
                messages=round_output_messages,
                transcript=round_transcript,
                duration_seconds=time.monotonic() - round_started_at,
            )
            # See the non-streaming path above. Streaming retries must retain
            # the same work memory and tool-call identities as well.
            agent_round_messages = round_transcript
            _update_short_term_memory_from_messages(runtime, round_transcript)
            _raise_if_terminal_probe_unavailability(round_transcript)

            clarification = _extract_runtime_clarification(streamed_messages)
            if clarification is not None:
                yield {
                    "type": "clarification_required",
                    "stage": "runtime_clarification",
                    "level": "info",
                    "conversation_id": runtime.conversation.id,
                    "turn_id": runtime.turn.id,
                    "clarification": clarification,
                    "_resume_context": _build_clarification_resume_context(
                        preparation=preparation,
                        runtime=runtime,
                        transcript=round_transcript,
                    ),
                }
                return

            # Tool-oriented Runtime prose is private execution scaffolding.
            # Structured lifecycle/tool progress already describes that work
            # without leaking chain-of-thought or language-inconsistent text.
            # Only a tool-free answer is eligible for the assistant stream.
            if deferred_assistant_messages and not any(
                getattr(message, 'tool_calls', None) for message in streamed_messages
            ):
                for message_text in deferred_assistant_messages:
                    yield _build_progress_event(
                        'assistant',
                        conversation_id=runtime.conversation.id,
                        turn_id=runtime.turn.id,
                        stage='planning',
                        message=message_text,
                    )

            if tool_result_ready:
                analysis_flow_started = True
                break

            # A general answer has no execution or artifact contract. In
            # particular, stale executions from an earlier failed run of the
            # same turn must not pull a rerun into the analysis repair loop.
            if not analysis_request_expected:
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
                analysis_flow_started=analysis_flow_started,
            ):
                analysis_report = _promote_assistant_message_to_report(assistant_message, analysis_report)
                allow_report_only = True
                break

            if _should_use_failure_reason_fallback(
                analysis_request_expected=analysis_request_expected,
                round_index=round_index,
                has_any_artifact=bool((analysis_report or '').strip() or charts or tables),
                assistant_message=assistant_message,
                analysis_flow_started=analysis_flow_started,
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
                _ensure_analysis_result_ready(
                    analysis_report=analysis_report,
                    charts=charts,
                    tables=tables,
                    output_format=str(runtime.output_contract.get("type") or "standard"),
                )
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
    except RuntimeDatasourceUnavailableError as exc:
        if _requires_strict_json_result(runtime):
            user_error_message = str(exc)
            _fail_run_with_fresh_session(
                runtime,
                user_error_message,
                preserve_existing_results=runtime.is_rerun,
            )
            yield _build_progress_event(
                'error',
                conversation_id=runtime.conversation.id,
                turn_id=runtime.turn.id,
                stage='data_source_unavailable',
                level='error',
                message=user_error_message,
            )
            return
        analysis_report = _build_datasource_unavailable_report(runtime, str(exc))
        assistant_message = '分析所需数据源当前不可访问，已生成问题说明。'
        runtime.evidence_enabled = False
        response = _finalize_run_with_fresh_session(
            runtime=runtime,
            username=agent_request.username,
            user_message=agent_request.user_message,
            analysis_flow_started=True,
            assistant_message=assistant_message,
            analysis_report=analysis_report,
            charts=[],
            tables=[],
            allow_report_only=True,
        )
        yield _build_progress_event(
            'result',
            conversation_id=runtime.conversation.id,
            turn_id=runtime.turn.id,
            stage='data_source_unavailable',
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
    _apply_invocation_policy(runtime, agent_request)
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
    platform_authorization: str = '',
    fedquery_authorization: str = '',
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
        platform_authorization=platform_authorization or '',
        fedquery_authorization=fedquery_authorization or '',
        selected_llm_model_id=selected_llm_model_id or '',
        database_conn_info=dict(database_payload or {}),
    )


def stream_rerun_turn(
    username: str,
    conversation_id: Any,
    turn_id: Any,
    platform_authorization: str = '',
    fedquery_authorization: str = '',
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
        platform_authorization=platform_authorization,
        fedquery_authorization=fedquery_authorization,
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
            output_contract=normalize_output_contract(
                agent_request.output_contract
            ).to_dict(),
            clarification_mode=normalize_clarification_mode(
                agent_request.clarification_mode
            ),
            evidence_enabled=bool(agent_request.evidence_enabled),
        )

    with session_scope(expire_on_commit=False) as session:
        runtime = _load_runtime(session)
    _apply_invocation_policy(runtime, agent_request)
    selected_model_token = bind_selected_model_id(agent_request.selected_llm_model_id)
    try:
        clarification_context = dict(agent_request.clarification_context or {})
        if clarification_context:
            runtime.context_mode = str(
                clarification_context.get("context_mode") or "fresh_analysis"
            )
        else:
            _resolve_runtime_context_mode(runtime, agent_request.user_message)
        yield from _stream_with_runtime(None, runtime, agent_request)
    finally:
        reset_selected_model_id(selected_model_token)
