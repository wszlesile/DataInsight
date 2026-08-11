import ast
import hashlib
import json
import math
import multiprocessing as mp
import os
from queue import Empty
import re
import tempfile
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time as datetime_time, timedelta
from difflib import SequenceMatcher
from io import StringIO
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent.context_engineering_runtime import CustomContext
from api import supos_kernel_api
from dto import DatabaseConnInfo
from utils import (
    build_chart_document,
    build_chart_result,
    build_chart_suite,
    build_multi_metric_chart_result,
    logger,
    normalize_chart_result_item,
    normalize_ts_query_source,
)
from utils.analysis_task_cancel import AnalysisTaskCancelled, CANCELLED_MESSAGE, is_analysis_task_cancelled

CURRENT_USERNAME = ContextVar('current_username', default='anonymous')
CURRENT_EXPECTED_TIME_RANGE = ContextVar('current_expected_time_range', default={})
CURRENT_TIME_RANGE_GUARD_ACTIVE = ContextVar('current_time_range_guard_active', default=False)
CURRENT_ANALYSIS_PARAMETERS_OVERRIDE = ContextVar('current_analysis_parameters_override', default={})
CURRENT_QUERY_SNAPSHOTS = ContextVar('current_query_snapshots', default={})
CURRENT_LAST_QUERY_KEY = ContextVar('current_last_query_key', default='')
CURRENT_DATASOURCE_NUMERIC_FIELDS = ContextVar('current_datasource_numeric_fields', default=[])
CURRENT_EXECUTION_INTENT = ContextVar('current_execution_intent', default='')
CURRENT_CONFIRMED_NO_DATA_RESULT = ContextVar('current_confirmed_no_data_result', default=False)

TOOL_ERROR_CURRENT_CODE_MAX_CHARS = int(os.environ.get('TOOL_ERROR_CURRENT_CODE_MAX_CHARS', '3500'))
TOOL_ERROR_TEXT_MAX_CHARS = int(os.environ.get('TOOL_ERROR_TEXT_MAX_CHARS', '1800'))
TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS = int(os.environ.get('TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS', '700'))
TOOL_ERROR_INSTRUCTION_MAX_CHARS = int(os.environ.get('TOOL_ERROR_INSTRUCTION_MAX_CHARS', '700'))
TOOL_ERROR_STDIO_MAX_CHARS = int(os.environ.get('TOOL_ERROR_STDIO_MAX_CHARS', '800'))
TOOL_ERROR_CODE_CONTEXT_MAX_CHARS = int(os.environ.get('TOOL_ERROR_CODE_CONTEXT_MAX_CHARS', '1800'))
PROBE_NO_DATA_RETRY_LIMIT = max(int(os.environ.get('PROBE_NO_DATA_RETRY_LIMIT', '3') or 3), 1)
PROBE_NO_DATA_RETRY_TYPES = {'probe_feedback', 'no_data_found'}
GENERATED_CODE_FILENAME = '<generated_analysis_code>'
GENERATED_CODE_CONTEXT_RADIUS = 8
FRONTEND_TOOL_RETRY_MESSAGE = '分析代码规范需要调整，正在反馈给大模型重新生成。'
FRONTEND_TOOL_ERROR_MESSAGE = '分析代码逻辑需要调整，正在反馈给大模型重新生成。'
FRONTEND_TOOL_TIMEOUT_MESSAGE = '分析代码执行时间较长，正在反馈给大模型优化后重试。'
QUERY_SNAPSHOT_SQL_MAX_CHARS = int(os.environ.get('QUERY_SNAPSHOT_SQL_MAX_CHARS', '8000'))
QUERY_SNAPSHOT_CONTEXT_MAX_CHARS = int(os.environ.get('QUERY_SNAPSHOT_CONTEXT_MAX_CHARS', '12000'))
MAX_SQL_RESULT_ROWS = 10000


class ChartArtifactContractError(ValueError):
    def __init__(self, *, chart_index: int, chart_title: str, cause: ValueError):
        self.chart_index = chart_index
        self.chart_title = chart_title
        self.cause = cause
        super().__init__(f"chart[{chart_index}] title={chart_title!r}: {cause}")

CHART_CONTRACT_ERROR_MARKERS = (
    'structured chart item',
    'chart_document',
    'chart_spec',
    'chart_kind',
    'unsupported chart_kind',
    'charts items',
    'charts item',
    'xaxis',
    'yaxis',
    'series',
)
TIME_SERIES_UTC_Z_LITERAL_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b"
)
TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS = (
    'unknown tag',
    'no results',
)
REPORT_CHART_DUMP_OBJECT_MARKERS = (
    "{'version':",
    '{"version":',
    "{'chart_kind':",
    '{"chart_kind":',
    "{'chart_document':",
    '{"chart_document":',
    "{'chart_spec':",
    '{"chart_spec":',
)
REPORT_CHART_DUMP_FIELD_MARKERS = (
    'chart_kind',
    'chart_document',
    'chart_spec',
    'dataset',
    'columns',
    'rows',
    'encoding',
    'category_field',
    'value_field',
    'x_field',
    'y_field',
    'transform',
    'presentation',
    'xaxis',
    'yaxis',
    'series',
)


def _is_time_series_empty_source_error(error_message: str) -> bool:
    lowered = (error_message or '').lower()
    return any(marker in lowered for marker in TIME_SERIES_EMPTY_SOURCE_ERROR_MARKERS)
REPORT_CHART_DUMP_SCAN_MAX_CHARS = 20000
HELPER_CHART_FUNCTIONS = {
    'build_chart_document',
    'build_chart_result',
    'build_chart_suite',
    'build_multi_metric_chart_result',
}
HELPER_SUPPORTED_CHART_KINDS = {'bar', 'line', 'pie', 'scatter', 'boxplot'}
SUPPORTED_PYECHARTS_CHART_CLASSES = {
    'Bar': 'bar',
    'Line': 'line',
    'Pie': 'pie',
    'Scatter': 'scatter',
    'Boxplot': 'boxplot',
    'BoxPlot': 'boxplot',
}
DATAFRAME_FIELD_EXPR_PATTERN = r"(?:[A-Za-z_]\w*\s*\[\s*['\"][^'\"]+['\"]\s*\])|(?:[A-Za-z_]\w*\.[A-Za-z_]\w*)"
SQL_EXACT_FILTER_PATTERN = re.compile(
    r"(?P<field>(?:[\w]+\.)?[\w\"`]+)\s*=\s*'(?P<literal>[^']+)'",
    re.IGNORECASE,
)
SQL_FUZZY_FILTER_PATTERN = re.compile(
    r"(?P<field>(?:[\w]+\.)?[\w\"`]+)\s+(?P<operator>like|ilike)\s+'(?P<literal>[^']+)'",
    re.IGNORECASE,
)
PANDAS_EXACT_FILTER_PATTERNS = (
    re.compile(
        rf"(?P<field>{DATAFRAME_FIELD_EXPR_PATTERN})\s*==\s*(['\"])(?P<literal>.+?)\2",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<field>{DATAFRAME_FIELD_EXPR_PATTERN})\s*\.eq\(\s*(['\"])(?P<literal>.+?)\2",
        re.IGNORECASE,
    ),
)
PANDAS_FUZZY_FILTER_PATTERNS = (
    re.compile(
        rf"(?P<field>{DATAFRAME_FIELD_EXPR_PATTERN})\s*\.str\.contains\(\s*(['\"])(?P<literal>.+?)\2",
        re.IGNORECASE,
    ),
    re.compile(
        rf"contains\(\s*(?P<field>{DATAFRAME_FIELD_EXPR_PATTERN})\s*,\s*(['\"])(?P<literal>.+?)\2",
        re.IGNORECASE,
    ),
)
QUERY_CONSTRAINT_MATCH_MODES = {'exact', 'fuzzy'}
QUERY_CONSTRAINT_GRAIN_PATTERNS = {
    'month': (
        re.compile(r"date_trunc\(\s*['\"]month['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*month", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*month", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*mo", re.IGNORECASE),
        re.compile(r"strftime\(\s*['\"]%Y-%m['\"]", re.IGNORECASE),
        re.compile(r"to_char\([^)]*['\"]YYYY-MM['\"]", re.IGNORECASE),
        re.compile(r"substr\([^)]*,\s*1\s*,\s*7\)", re.IGNORECASE),
        re.compile(r"left\([^)]*,\s*7\)", re.IGNORECASE),
        re.compile(r"\.dt\.to_period\(\s*['\"]M['\"]\s*\)", re.IGNORECASE),
        re.compile(r"\.dt\.month\b", re.IGNORECASE),
        re.compile(r"\bmonth\b", re.IGNORECASE),
        re.compile(r"年月"),
        re.compile(r"月度"),
    ),
    'day': (
        re.compile(r"date_trunc\(\s*['\"]day['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*day", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*day", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*d", re.IGNORECASE),
        re.compile(r"strftime\(\s*['\"]%Y-%m-%d['\"]", re.IGNORECASE),
        re.compile(r"to_char\([^)]*['\"]YYYY-MM-DD['\"]", re.IGNORECASE),
        re.compile(r"\.dt\.date\b", re.IGNORECASE),
        re.compile(r"\bday\b", re.IGNORECASE),
        re.compile(r"日期"),
        re.compile(r"天"),
    ),
    'year': (
        re.compile(r"date_trunc\(\s*['\"]year['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*year", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*year", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*y", re.IGNORECASE),
        re.compile(r"strftime\(\s*['\"]%Y['\"]", re.IGNORECASE),
        re.compile(r"to_char\([^)]*['\"]YYYY['\"]", re.IGNORECASE),
        re.compile(r"\.dt\.year\b", re.IGNORECASE),
        re.compile(r"\byear\b", re.IGNORECASE),
        re.compile(r"年份"),
        re.compile(r"年度"),
    ),
    'week': (
        re.compile(r"date_trunc\(\s*['\"]week['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*week", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*week", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*w", re.IGNORECASE),
        re.compile(r"\.dt\.isocalendar\(\)\.week", re.IGNORECASE),
        re.compile(r"\bweek\b", re.IGNORECASE),
        re.compile(r"周"),
    ),
    'hour': (
        re.compile(r"date_trunc\(\s*['\"]hour['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*hour", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*hour", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*h", re.IGNORECASE),
        re.compile(r"\bhour\b", re.IGNORECASE),
        re.compile(r"小时"),
    ),
    'minute': (
        re.compile(r"date_trunc\(\s*['\"]minute['\"]", re.IGNORECASE),
        re.compile(r"time_bucket\(\s*['\"][^'\"]*min", re.IGNORECASE),
        re.compile(r"time_bucket_gapfill\(\s*['\"][^'\"]*min", re.IGNORECASE),
        re.compile(r"interval\(\s*['\"][^'\"]*m", re.IGNORECASE),
        re.compile(r"\bminute\b", re.IGNORECASE),
        re.compile(r"分钟"),
    ),
}
def _clip_retry_context_text(text: Any, max_chars: int) -> str:
    """Bound retry feedback so repeated tool failures do not exhaust model context."""
    value = str(text or '')
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    suffix = f'\n...[truncated {len(value) - max_chars} chars for retry context budget]'
    return value[:max(0, max_chars - len(suffix))] + suffix


def _clip_retry_context_list(items: list[Any] | None, max_items: int, max_chars: int) -> list[str]:
    return [
        _clip_retry_context_text(item, max_chars)
        for item in (items or [])[-max_items:]
        if str(item or '').strip()
    ]


def _is_chart_contract_error_message(error_message: str) -> bool:
    lowered = (error_message or '').lower()
    if any(marker in lowered for marker in (
        'structured chart item',
        'chart_document',
        'chart_spec',
        'chart_kind',
        'unsupported chart_kind',
        'charts items',
        'charts item',
        'xaxis',
        'yaxis',
    )):
        return True
    return 'series' in lowered and any(marker in lowered for marker in ('chart', 'echarts', 'axis'))


def _build_generated_code_error_context(
    code: str,
    error: Exception,
    traceback_text: str,
) -> dict[str, Any]:
    """Extract the generated-code line that caused a Python runtime error."""
    line_no: int | None = None
    column_no: int | None = None

    if isinstance(error, SyntaxError):
        if isinstance(error.lineno, int):
            line_no = error.lineno
        if isinstance(error.offset, int):
            column_no = error.offset

    traceback_obj = getattr(error, '__traceback__', None)
    if traceback_obj is not None:
        for frame in traceback.extract_tb(traceback_obj):
            if frame.filename in {GENERATED_CODE_FILENAME, '<string>'}:
                line_no = frame.lineno

    code_lines = str(code or '').splitlines()
    context_lines: list[dict[str, Any]] = []
    error_line = ''
    if line_no is not None and 1 <= line_no <= len(code_lines):
        error_line = code_lines[line_no - 1]
        start = max(1, line_no - GENERATED_CODE_CONTEXT_RADIUS)
        end = min(len(code_lines), line_no + GENERATED_CODE_CONTEXT_RADIUS)
        for current_line_no in range(start, end + 1):
            context_lines.append({
                'line_no': current_line_no,
                'code': code_lines[current_line_no - 1],
                'is_error_line': current_line_no == line_no,
            })

    related_context_lines: list[dict[str, Any]] = []
    chart_title = str(getattr(error, 'chart_title', '') or '')
    if chart_title:
        try:
            tree = ast.parse(code or '')
        except SyntaxError:
            tree = None
        if tree is not None:
            related_call = next(
                (
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and (
                        (isinstance(node.func, ast.Name) and node.func.id in {
                            'build_chart_result', 'build_multi_metric_chart_result', 'build_chart_suite',
                        })
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr in {
                                'build_chart_result', 'build_multi_metric_chart_result', 'build_chart_suite',
                            }
                        )
                    )
                    and any(
                        keyword.arg == 'title'
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == chart_title
                        for keyword in node.keywords
                    )
                ),
                None,
            )
            if related_call is not None:
                related_start = max(1, int(getattr(related_call, 'lineno', 1) or 1) - 4)
                related_end = min(
                    len(code_lines),
                    int(getattr(related_call, 'end_lineno', related_start) or related_start) + 2,
                )
                related_context_lines = [
                    {
                        'line_no': current_line_no,
                        'code': code_lines[current_line_no - 1],
                    }
                    for current_line_no in range(related_start, related_end + 1)
                ]

    statement_ranges = _source_statement_patch_ranges(code, line_no)
    if line_no and any(
        marker in str(error or '').lower()
        for marker in ('query result exceeds', 'rows, please update query conditions')
    ):
        statement_ranges = _extend_query_dependency_ranges(code, line_no, statement_ranges)

    return {
        'filename': GENERATED_CODE_FILENAME,
        'line_no': line_no,
        'column_no': column_no,
        'error_line': error_line,
        'context_lines': context_lines,
        'related_context_lines': related_context_lines,
        'suggested_patch_ranges': statement_ranges,
        'traceback_excerpt': _clip_retry_context_text(traceback_text, TOOL_ERROR_CODE_CONTEXT_MAX_CHARS),
    }


def _source_statement_patch_ranges(
    code: str,
    line_no: int | None,
) -> list[dict[str, Any]]:
    """Return the smallest complete Python statement containing an error line."""
    if not line_no:
        return []
    try:
        tree = ast.parse(code or '')
    except SyntaxError:
        return []
    source_lines = str(code or '').splitlines()
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and int(getattr(node, 'lineno', 0) or 0) <= line_no
        <= int(getattr(node, 'end_lineno', 0) or 0)
    ]
    if not candidates:
        return []
    node = min(
        candidates,
        key=lambda item: (
            int(getattr(item, 'end_lineno', line_no) or line_no)
            - int(getattr(item, 'lineno', line_no) or line_no),
            -int(getattr(item, 'lineno', 0) or 0),
        ),
    )
    start_line = int(getattr(node, 'lineno', line_no) or line_no)
    end_line = int(getattr(node, 'end_lineno', line_no) or line_no)
    return [{
        'start_line': start_line,
        'end_line': end_line,
        'kind': type(node).__name__,
        'source': '\n'.join(source_lines[start_line - 1:end_line]),
        'role': 'failing_statement',
    }]


def _extend_query_dependency_ranges(
    code: str,
    line_no: int,
    ranges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add the complete query assignment used by a failing load statement."""
    try:
        tree = ast.parse(code or '')
    except SyntaxError:
        return ranges
    source_lines = str(code or '').splitlines()
    containing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and int(getattr(node, 'lineno', 0) or 0) <= line_no
        <= int(getattr(node, 'end_lineno', 0) or 0)
    ]
    if not containing:
        return ranges
    failing_statement = min(
        containing,
        key=lambda item: int(getattr(item, 'end_lineno', line_no) or line_no)
        - int(getattr(item, 'lineno', line_no) or line_no),
    )
    referenced_names = {
        node.id
        for node in ast.walk(failing_statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assignments: list[tuple[int, ast.stmt, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if int(getattr(node, 'lineno', 0) or 0) >= line_no:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {
            item.id
            for target in targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name)
        }
        for name in names & referenced_names:
            assignments.append((int(getattr(node, 'lineno', 0) or 0), node, name))
    if not assignments:
        return ranges
    _, node, name = max(assignments, key=lambda item: item[0])
    start_line = int(getattr(node, 'lineno', 0) or 0)
    end_line = int(getattr(node, 'end_lineno', start_line) or start_line)
    dependency = {
        'start_line': start_line,
        'end_line': end_line,
        'kind': type(node).__name__,
        'source': '\n'.join(source_lines[start_line - 1:end_line]),
        'role': 'query_definition',
        'name': name,
    }
    return [dependency, *ranges]


def _format_error_message_with_code_location(
    error_message: str,
    code_error_context: dict[str, Any] | None,
) -> str:
    line_no = (code_error_context or {}).get('line_no')
    error_line = str((code_error_context or {}).get('error_line') or '').strip()
    if not line_no:
        return error_message

    location = f'错误定位：生成代码第 {line_no} 行'
    if error_line:
        location = f'{location}：{error_line}'
    return f'{error_message}\n{location}'


def _normalize_filter_text(text: str) -> str:
    normalized = str(text or '').strip().lower()
    normalized = normalized.replace('%', '').replace('_', '')
    normalized = re.sub(r'[\s\'"`]+', '', normalized)
    return normalized


def _extract_code_filter_literals(code: str) -> list[dict[str, str]]:
    text = str(code or '')
    matches: list[dict[str, str]] = []

    for match in SQL_EXACT_FILTER_PATTERN.finditer(text):
        literal = str(match.group('literal') or '').strip()
        if literal:
            matches.append({
                'kind': 'sql_exact',
                'field': str(match.group('field') or '').strip(),
                'literal': literal,
            })

    for match in SQL_FUZZY_FILTER_PATTERN.finditer(text):
        literal = str(match.group('literal') or '').strip()
        if literal:
            matches.append({
                'kind': 'sql_fuzzy',
                'field': str(match.group('field') or '').strip(),
                'literal': literal,
            })

    for pattern in PANDAS_EXACT_FILTER_PATTERNS:
        for match in pattern.finditer(text):
            literal = str(match.group('literal') or '').strip()
            if literal:
                matches.append({
                    'kind': 'pandas_exact',
                    'field': str(match.group('field') or '').strip(),
                    'literal': literal,
                })

    for pattern in PANDAS_FUZZY_FILTER_PATTERNS:
        for match in pattern.finditer(text):
            literal = str(match.group('literal') or '').strip()
            if literal:
                matches.append({
                    'kind': 'pandas_fuzzy',
                    'field': str(match.group('field') or '').strip(),
                    'literal': literal,
                })
    return matches


def _validate_query_constraints_schema(query_constraints: Any) -> list[str]:
    if not isinstance(query_constraints, dict):
        return ['query_constraints 必须是 dict。']

    errors: list[str] = []
    if not any(key in query_constraints for key in ('time_range', 'target_filters', 'aggregation')):
        errors.append('query_constraints 至少包含 time_range、target_filters、aggregation 中的一项。')

    time_range = query_constraints.get('time_range')
    if time_range is not None:
        if not isinstance(time_range, dict):
            errors.append('time_range 必须是 dict。')
        else:
            if time_range.get('start') is not None and not isinstance(time_range.get('start'), str):
                errors.append('time_range.start 必须是字符串。')
            if time_range.get('end') is not None and not isinstance(time_range.get('end'), str):
                errors.append('time_range.end 必须是字符串。')
            if 'end_exclusive' in time_range and not isinstance(time_range.get('end_exclusive'), bool):
                errors.append('time_range.end_exclusive 必须是 bool。')

    target_filters = query_constraints.get('target_filters')
    if target_filters is not None:
        if not isinstance(target_filters, list):
            errors.append('target_filters 必须是 list。')
        else:
            for index, item in enumerate(target_filters):
                if not isinstance(item, dict):
                    errors.append(f'target_filters[{index}] 必须是 dict。')
                    continue
                if not isinstance(item.get('semantic_role'), str) or not str(item.get('semantic_role') or '').strip():
                    errors.append(f'target_filters[{index}].semantic_role 必须是非空字符串。')
                filter_value = item.get('value')
                valid_scalar_value = (
                    isinstance(filter_value, str)
                    and bool(filter_value.strip())
                )
                valid_multi_value = (
                    isinstance(filter_value, list)
                    and bool(filter_value)
                    and all(
                        isinstance(value, str) and bool(value.strip())
                        for value in filter_value
                    )
                )
                if not (valid_scalar_value or valid_multi_value):
                    errors.append(
                        f'target_filters[{index}].value 必须是非空字符串或非空字符串列表。'
                    )
                if str(item.get('match_mode') or '').strip().lower() not in QUERY_CONSTRAINT_MATCH_MODES:
                    errors.append(f'target_filters[{index}].match_mode 必须是 exact 或 fuzzy。')
                if item.get('field_hint') is not None and not isinstance(item.get('field_hint'), str):
                    errors.append(f'target_filters[{index}].field_hint 必须是字符串。')

    aggregation = query_constraints.get('aggregation')
    if aggregation is not None:
        if not isinstance(aggregation, dict):
            errors.append('aggregation 必须是 dict。')
        else:
            if aggregation.get('grain') is not None and not isinstance(aggregation.get('grain'), str):
                errors.append('aggregation.grain 必须是字符串。')
            if aggregation.get('metric') is not None and not isinstance(aggregation.get('metric'), str):
                errors.append('aggregation.metric 必须是字符串。')

    if query_constraints.get('notes') is not None and not isinstance(query_constraints.get('notes'), str):
        errors.append('notes 必须是字符串。')
    return errors


def _build_query_constraint_error(
    error_type: str,
    error_message: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'error_type': error_type,
        'error_message': error_message,
        'diagnostics': diagnostics or {},
    }


def _check_target_filter_constraints(query_constraints: dict[str, Any], code: str) -> dict[str, Any] | None:
    filters = _extract_code_filter_literals(code)
    for item in query_constraints.get('target_filters') or []:
        if not isinstance(item, dict):
            continue
        if str(item.get('match_mode') or '').strip().lower() != 'exact':
            continue

        raw_values = item.get('value', '')
        expected_values = raw_values if isinstance(raw_values, list) else [raw_values]
        for raw_value in expected_values:
            expected_value = _normalize_filter_text(raw_value)
            if len(expected_value) < 2:
                continue

            exact_matches = [
                filter_item for filter_item in filters
                if filter_item.get('kind') in {'sql_exact', 'pandas_exact'}
                and _normalize_filter_text(filter_item.get('literal', '')) == expected_value
            ]
            fuzzy_matches = [
                filter_item for filter_item in filters
                if filter_item.get('kind') in {'sql_fuzzy', 'pandas_fuzzy'}
                and _normalize_filter_text(filter_item.get('literal', '')) == expected_value
            ]
            if fuzzy_matches and not exact_matches:
                return _build_query_constraint_error(
                    'query_constraint_violation',
                    f'query_constraints 要求 `{item.get("semantic_role", "target_filter")}` 使用精确匹配，但当前代码把 `{raw_value}` 实现成了模糊匹配。',
                    diagnostics={
                        'violated_constraint': 'target_filters',
                        'constraint_item': item,
                        'violated_value': raw_value,
                        'matched_fuzzy_filters': fuzzy_matches[:5],
                    },
                )
    return None


def _check_time_range_constraint(query_constraints: dict[str, Any], code: str) -> dict[str, Any] | None:
    time_range = query_constraints.get('time_range')
    if not isinstance(time_range, dict):
        return None

    start = str(time_range.get('start') or '').strip()
    end = str(time_range.get('end') or '').strip()
    if _month_grain_filter_satisfies_time_range(start, end, time_range, code):
        return None
    if _year_grain_filter_satisfies_time_range(start, end, time_range, code):
        return None
    if _named_time_parameters_satisfy_time_range(code):
        return None

    missing_parts = []
    if start and not _time_range_part_present(code, start):
        missing_parts.append('start')
    if end and not _time_range_part_present(code, end):
        missing_parts.append('end')
    if not missing_parts:
        return None

    observed_date_literals = sorted(set(re.findall(
        r'(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)',
        str(code or ''),
    )))
    expected_boundaries = {
        'start': start or None,
        'end': end or None,
        'end_exclusive': bool(time_range.get('end_exclusive')),
    }

    return _build_query_constraint_error(
        'query_constraint_violation',
        f'query_constraints 中声明的时间范围没有完整体现在当前代码里，缺失部分：{", ".join(missing_parts)}。',
        diagnostics={
            'violated_constraint': 'time_range',
            'time_range': time_range,
            'missing_parts': missing_parts,
            'expected_boundaries': expected_boundaries,
            'observed_date_literals': observed_date_literals,
            'repair_strategy': {
                'preserve_declared_constraint': True,
                'align_sql_and_query_params_to': expected_boundaries,
                'boundary_rule': (
                    'When end_exclusive is true, SQL/filter/query parameters must use the declared '
                    'exclusive end instead of the inclusive display date.'
                ),
                'derived_grain_rule': (
                    'Year/month predicates are equivalent only when they preserve the exact start '
                    'and exclusive-end boundary.'
                ),
            },
        },
    )


def _check_portable_time_bucket_expression(
    query_constraints: dict[str, Any],
    sql: str,
) -> dict[str, Any] | None:
    """Reject SQL operators that can silently turn a temporal bucket into a boolean."""
    aggregation = query_constraints.get('aggregation')
    if not isinstance(aggregation, dict):
        return None
    grain = str(aggregation.get('grain') or '').strip().lower()
    if grain not in {'hour', 'day', 'week', 'month', 'quarter', 'year'}:
        return None
    if '||' not in str(sql or ''):
        return None
    return _build_query_constraint_error(
        'query_constraint_violation',
        '当前时间分桶使用了 `||` 拼接；该运算符在联邦关系查询中可能被解释为布尔运算，导致所有周期塌缩为同一个值。',
        diagnostics={
            'violated_constraint': 'nonportable_time_bucket_concat',
            'aggregation_grain': grain,
        },
    )


def _named_time_parameters_satisfy_time_range(code: str) -> bool:
    """Recognize bound SQL ranges whose parameter names carry start/end roles."""
    text = str(code or "")
    parameter_names = {
        item.lower()
        for item in re.findall(
            r":([A-Za-z_][A-Za-z0-9_]*)|%\(([A-Za-z_][A-Za-z0-9_]*)\)s",
            text,
        )
        for item in item
        if item
    }
    if not parameter_names:
        return False
    start_names = {
        item for item in parameter_names
        if "start" in item or item in {"from", "begin", "since"}
    }
    end_names = {
        item for item in parameter_names
        if "end" in item or item in {"to", "until"}
    }
    return bool(start_names and end_names)


def _year_grain_filter_satisfies_time_range(
    start: str,
    end: str,
    time_range: dict[str, Any],
    code: str,
) -> bool:
    """Recognize an exact year-column predicate as a full-year date range."""
    if not start or not end or not bool(time_range.get('end_exclusive')):
        return False
    try:
        start_date = datetime.strptime(start, '%Y-%m-%d').date()
        end_date = datetime.strptime(end, '%Y-%m-%d').date()
    except ValueError:
        return False
    if not (
        start_date.month == 1
        and start_date.day == 1
        and end_date.year == start_date.year + 1
        and end_date.month == 1
        and end_date.day == 1
    ):
        return False

    def _is_year_field(value: str) -> bool:
        normalized = re.sub(r'[^a-z0-9]+', '_', str(value or '').lower()).strip('_')
        return normalized == 'year' or normalized.endswith('_year')

    try:
        from sqlglot import exp, parse

        for statement in parse(code):
            for comparison in statement.find_all(exp.EQ):
                sides = ((comparison.left, comparison.right), (comparison.right, comparison.left))
                for field_node, value_node in sides:
                    if not isinstance(field_node, exp.Column) or not isinstance(value_node, exp.Literal):
                        continue
                    if _is_year_field(field_node.name) and str(value_node.this) == str(start_date.year):
                        return True
    except Exception:
        pass

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for comparison in (node for node in ast.walk(tree) if isinstance(node, ast.Compare)):
        if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
            continue
        if len(comparison.comparators) != 1:
            continue
        sides = ((comparison.left, comparison.comparators[0]), (comparison.comparators[0], comparison.left))
        for field_node, value_node in sides:
            field_name = ''
            if isinstance(field_node, ast.Subscript):
                try:
                    field_name = str(ast.literal_eval(field_node.slice))
                except Exception:
                    field_name = ''
            elif isinstance(field_node, ast.Attribute):
                field_name = field_node.attr
            if not _is_year_field(field_name):
                continue
            if isinstance(value_node, ast.Constant) and value_node.value == start_date.year:
                return True
    return False


def _month_grain_filter_satisfies_time_range(
    start: str,
    end: str,
    time_range: dict[str, Any],
    code: str,
) -> bool:
    if not start or not end or not bool(time_range.get('end_exclusive')):
        return False
    try:
        start_date = datetime.strptime(start, '%Y-%m-%d').date()
        end_date = datetime.strptime(end, '%Y-%m-%d').date()
    except ValueError:
        return False

    if start_date.day != 1 or end_date.day != 1:
        return False
    start_month = start_date.strftime('%Y-%m')
    end_month = end_date.strftime('%Y-%m')

    if start_date.year == end_date.year and end_date.month > start_date.month:
        year_month_pattern = re.compile(
            rf"(?P<prefix>[A-Za-z_][A-Za-z0-9_]*)_YEAR\s*=\s*{start_date.year}"
            rf"[\s\S]*?(?P=prefix)_MONTH\s+BETWEEN\s+{start_date.month}\s+AND\s+{end_date.month - 1}",
            re.IGNORECASE,
        )
        if year_month_pattern.search(code):
            return True

    if (
        start_date.month == 1
        and end_date.year == start_date.year + 1
        and end_date.month == 1
        and re.search(rf"\.str\.startswith\(\s*['\"]{start_date.year}['\"]\s*\)", code)
    ):
        return True

    if (
        re.search(rf">=\s*['\"]{re.escape(start_month)}['\"]", code) is not None
        and re.search(rf"<\s*['\"]{re.escape(end_month)}['\"]", code) is not None
    ):
        return True

    next_month_year = start_date.year + (1 if start_date.month == 12 else 0)
    next_month = 1 if start_date.month == 12 else start_date.month + 1
    if end_date.year != next_month_year or end_date.month != next_month or end_date.day != 1:
        return False

    if re.search(rf"['\"]{re.escape(start_month)}['\"]", code):
        if re.search(rf"==\s*['\"]{re.escape(start_month)}['\"]", code) or re.search(
            rf"=\s*['\"]{re.escape(start_month)}['\"]",
            code,
        ):
            return True
    return False


def _check_aggregation_constraint(query_constraints: dict[str, Any], code: str) -> dict[str, Any] | None:
    aggregation = query_constraints.get('aggregation')
    if not isinstance(aggregation, dict):
        return None

    grain = str(aggregation.get('grain') or '').strip().lower()
    if not grain:
        return None

    patterns = QUERY_CONSTRAINT_GRAIN_PATTERNS.get(grain)
    if not patterns:
        return None
    if any(pattern.search(code) for pattern in patterns):
        return None

    return _build_query_constraint_error(
        'query_constraint_violation',
        f'query_constraints 要求按 `{grain}` 粒度统计，但当前代码里没有识别到对应的聚合粒度实现。',
        diagnostics={
            'violated_constraint': 'aggregation',
            'aggregation': aggregation,
        },
    )


def _sql_context_fragments(sql_context: Any) -> list[dict[str, Any]]:
    if not isinstance(sql_context, dict):
        return []
    fragments = sql_context.get('fragments')
    if isinstance(fragments, list):
        return [item for item in fragments if isinstance(item, dict)]

    if str(sql_context.get('inner_sql') or '').strip():
        return [{
            'kind': 'time_series_sql',
            'name': sql_context.get('name') or 'time_series',
            'ts_query_source': sql_context.get('ts_query_source'),
            'inner_sql': sql_context.get('inner_sql'),
            'datasource_identifier': sql_context.get('datasource_identifier'),
            'time_field': sql_context.get('time_field'),
            'quality_field': sql_context.get('quality_field'),
            'value_fields': sql_context.get('value_fields'),
        }]
    return []


def _fragment_text(fragment: dict[str, Any]) -> str:
    return '\n'.join(
        str(fragment.get(key) or '').strip()
        for key in ('sql', 'inner_sql', 'filter_expression')
        if str(fragment.get(key) or '').strip()
    )


def _validate_relational_fragment(query_constraints: dict[str, Any], fragment: dict[str, Any]) -> list[dict[str, Any]]:
    code = _fragment_text(fragment)
    issues: list[dict[str, Any]] = []
    for checker in (_check_target_filter_constraints,):
        issue = checker(query_constraints, code)
        if issue is not None:
            issue.setdefault('diagnostics', {})['fragment'] = {
                'kind': fragment.get('kind') or 'relational_sql',
                'name': fragment.get('name') or '',
            }
            issues.append(issue)
    return issues


def _quote_sql_identifier(identifier: str) -> str:
    return '"' + str(identifier or '').replace('"', '""') + '"'


def _identifier_requires_quotes(identifier: str) -> bool:
    text = str(identifier or '').strip()
    if not text:
        return False
    return not bool(re.fullmatch(r'[a-z_][a-z0-9_]*', text))


def _validate_timescale_identifier_quoting(fragment: dict[str, Any], code: str) -> dict[str, Any] | None:
    datasource_identifier = str(fragment.get('datasource_identifier') or '').strip()
    if not datasource_identifier or not _identifier_requires_quotes(datasource_identifier):
        return None
    quoted_identifier = _quote_sql_identifier(datasource_identifier)
    if quoted_identifier in code:
        return None
    return _build_query_constraint_error(
        'query_constraint_violation',
        'timescale 内部 SQL 中的 datasource_identifier 包含大写字母、$ 或其他特殊字符时，必须使用双引号按原样引用。',
        diagnostics={
            'violated_constraint': 'timescale_identifier_quoting',
            'ts_query_source': 'timescale',
            'datasource_identifier': datasource_identifier,
            'expected_identifier': quoted_identifier,
        },
    )


def _validate_timescale_fragment(query_constraints: dict[str, Any], fragment: dict[str, Any]) -> list[dict[str, Any]]:
    code = _fragment_text(fragment)
    issues = _validate_relational_fragment(query_constraints, {**fragment, 'sql': code})
    identifier_issue = _validate_timescale_identifier_quoting(fragment, code)
    if identifier_issue is not None:
        issues.append(identifier_issue)
    return issues


def _extract_vxbase_inner_where_forbidden_fields(code: str) -> list[str]:
    match = re.search(
        r"\bwhere\b(?P<where>.*?)(?:\blimit\b|\binterval\s*\(|\binterpolation\s*\(|\bgroup\s+by\b|\border\s+by\b|$)",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []

    where_clause = re.sub(r"'[^']*'|\"[^\"]*\"", " ", match.group('where') or '')
    fields: list[str] = []
    seen: set[str] = set()
    patterns = [
        r'"(?P<field>[^"]+)"\s*(?:=|<>|!=|>=|<=|>|<|\bin\b|\blike\b|\bis\b)',
        r'\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\b\s*(?:=|<>|!=|>=|<=|>|<|\bin\b|\blike\b|\bis\b)',
    ]
    ignored = {'ts', 'and', 'or', 'not'}
    for pattern in patterns:
        for item in re.finditer(pattern, where_clause, re.IGNORECASE):
            field = str(item.group('field') or '').strip()
            normalized = field.lower()
            if not field or normalized in ignored or normalized in seen:
                continue
            seen.add(normalized)
            fields.append(field)
    return fields


def _extract_vxbase_select_projection(code: str) -> str | None:
    match = re.search(
        r"\bselect\b(?P<projection>.*?)\bfrom\b",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    projection = " ".join(str(match.group('projection') or '').split())
    if not projection or projection == '*':
        return None
    if re.search(r"\b(max|min|first|last|sum|avg|var|count)\s*\(", projection, re.IGNORECASE):
        return None
    return projection


def _extract_vxbase_limit_issue(code: str) -> str | None:
    comma_limit = re.search(r"\blimit\s+\d+\s*,\s*\d+\b", code, re.IGNORECASE)
    if comma_limit:
        return comma_limit.group(0)
    offset_limit = re.search(r"\blimit\s+\d+\s+offset\s+\d+\b", code, re.IGNORECASE)
    if offset_limit:
        return offset_limit.group(0)
    return None


def _validate_vxbase_fragment(query_constraints: dict[str, Any], fragment: dict[str, Any]) -> list[dict[str, Any]]:
    code = _fragment_text(fragment)
    issues: list[dict[str, Any]] = []

    projection = _extract_vxbase_select_projection(code)
    if projection:
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            'vxbase 原始数据、趋势图和探测查询的内部 SQL 应使用 SELECT *，不要显式指定返回列。',
            diagnostics={
                'violated_constraint': 'vxbase_inner_select_projection',
                'ts_query_source': 'vxbase',
                'projection': projection,
            },
        ))

    limit_issue = _extract_vxbase_limit_issue(code)
    if limit_issue:
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            'vxbase 内部 SQL 限制条数时只使用 limit N，不要使用 limit offset,count 或 LIMIT ... OFFSET ...。',
            diagnostics={
                'violated_constraint': 'vxbase_inner_limit_syntax',
                'ts_query_source': 'vxbase',
                'limit_syntax': limit_issue,
            },
        ))

    forbidden_fields = _extract_vxbase_inner_where_forbidden_fields(code)
    if forbidden_fields:
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            'vxbase 内部 SQL 的 WHERE 只支持 ts 时间过滤；其他字段过滤需要放到 ts_query(...) 外层 SQL。',
            diagnostics={
                'violated_constraint': 'vxbase_inner_where_filter',
                'ts_query_source': 'vxbase',
                'forbidden_fields': forbidden_fields,
            },
        ))

    if re.search(r"\b(max|min|first|last|sum|avg|var|count)\s*\(", code, re.IGNORECASE) and re.search(
        r"\binterpolation\s*\(",
        code,
        re.IGNORECASE,
    ):
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            'vxbase 统计聚合查询不要同时使用 interpolation(...)。',
            diagnostics={
                'violated_constraint': 'time_series_sql',
                'ts_query_source': 'vxbase',
            },
        ))
    return issues


def _time_range_literal_candidates(value: Any) -> set[str]:
    text = str(value or '').strip()
    if not text:
        return set()

    candidates = {text}
    if ' ' in text and 'T' not in text:
        candidates.add(text.replace(' ', 'T'))
        if re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', text):
            candidates.add(text.replace(' ', 'T') + '+08:00')
    if text.endswith('+08:00'):
        candidates.add(text[:-6])
    if text.endswith('Z'):
        candidates.add(text[:-1])
    return {item for item in candidates if item}


def _time_range_part_present(code: str, value: Any) -> bool:
    candidates = _time_range_literal_candidates(value)
    if not candidates:
        return True
    return any(candidate in code for candidate in candidates)


def _check_time_series_inner_time_range(query_constraints: dict[str, Any], code: str, source: str) -> dict[str, Any] | None:
    time_range = query_constraints.get('time_range')
    if not isinstance(time_range, dict):
        return None

    start = time_range.get('start')
    end = time_range.get('end')
    missing_parts: list[str] = []
    if start and not _time_range_part_present(code, start):
        missing_parts.append('start')
    if end and not _time_range_part_present(code, end):
        missing_parts.append('end')
    if not missing_parts:
        return None

    return _build_query_constraint_error(
        'query_constraint_violation',
        '时序查询必须在 ts_query(...) 的内部 SQL 中体现 query_constraints.time_range，不能先全量读取时序表再用 pandas 过滤时间。',
        diagnostics={
            'violated_constraint': 'time_series_inner_time_range',
            'ts_query_source': source,
            'time_range': time_range,
            'missing_parts': missing_parts,
        },
    )


def _validate_time_series_fragment(query_constraints: dict[str, Any], fragment: dict[str, Any]) -> list[dict[str, Any]]:
    source = normalize_ts_query_source(fragment.get('ts_query_source'))
    code = _fragment_text(fragment)
    issues: list[dict[str, Any]] = []
    if not source:
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            '时序 SQL 片段缺少 ts_query_source，无法判断应按 timescale 还是 vxbase 规则校验。',
            diagnostics={'fragment': fragment},
        ))
        return issues
    if source == 'timescale':
        issues.extend(_validate_timescale_fragment(query_constraints, fragment))
    elif source == 'vxbase':
        issues.extend(_validate_vxbase_fragment(query_constraints, fragment))

    time_range_issue = _check_time_series_inner_time_range(query_constraints, code, source)
    if time_range_issue is not None:
        issues.append(time_range_issue)

    if TIME_SERIES_UTC_Z_LITERAL_PATTERN.search(code):
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            '时序 SQL 的时间字面量需要带业务时区偏移，例如 +08:00；不要使用以 Z 结尾的 UTC 时间字面量。',
            diagnostics={
                'violated_constraint': 'time_series_timezone',
                'ts_query_source': source,
                'timezone': 'Asia/Shanghai',
                'expected_offset': '+08:00',
            },
        ))

    datasource_identifier = str(fragment.get('datasource_identifier') or '').strip()
    if datasource_identifier and datasource_identifier not in code:
        issues.append(_build_query_constraint_error(
            'query_constraint_violation',
            '时序 SQL 内部查询没有使用当前数据源的 datasource_identifier。',
            diagnostics={
                'violated_constraint': 'time_series_datasource_identifier',
                'datasource_identifier': datasource_identifier,
                'ts_query_source': source,
            },
        ))
    return issues


def _find_malformed_ts_query_literal(sql: str) -> dict[str, Any] | None:
    text = str(sql or '')
    for match in re.finditer(r"\bts_query\s*\(\s*'(?P<source>[^']+)'\s*,\s*'", text, re.IGNORECASE):
        index = match.end()
        while index < len(text):
            char = text[index]
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                tail = text[index + 1:].lstrip()
                if tail.startswith(')'):
                    break
                return {
                    'ts_query_source': match.group('source'),
                    'position': index,
                    'near': text[index:index + 120],
                }
            index += 1
    return None


def _validate_sql_context_fragments(
    query_constraints: dict[str, Any],
    sql: str,
    sql_context: Any,
) -> tuple[list[dict[str, Any]], str, str]:
    fragments = _sql_context_fragments(sql_context)
    if not fragments:
        return [], '', ''

    filter_fragments = [str(sql or '').strip()]
    aggregation_fragments = [str(sql or '').strip()]
    issues: list[dict[str, Any]] = []
    has_relational = False
    has_time_series = False

    for fragment in fragments:
        kind = str(fragment.get('kind') or '').strip().lower()
        text = _fragment_text(fragment)
        if text:
            filter_fragments.append(text)
            aggregation_fragments.append(text)
        if kind == 'time_series_sql':
            has_time_series = True
            issues.extend(_validate_time_series_fragment(query_constraints, fragment))
        elif kind == 'relational_sql':
            has_relational = True
            issues.extend(_validate_relational_fragment(query_constraints, fragment))

    if has_relational and has_time_series:
        outer_sql = str(sql or '')
        if not re.search(r"\bjoin\b", outer_sql, re.IGNORECASE) or not re.search(r"\bon\b", outer_sql, re.IGNORECASE):
            issues.append(_build_query_constraint_error(
                'query_constraint_violation',
                '混合关系型与时序查询时，外层 SQL 需要包含明确的 JOIN 和 ON 条件。',
                diagnostics={'violated_constraint': 'mixed_join'},
            ))

    return (
        issues,
        '\n'.join(fragment for fragment in filter_fragments if fragment),
        '\n'.join(fragment for fragment in aggregation_fragments if fragment),
    )


def _load_data_with_fed_query(sql: str,params: Optional[list[Any]] = None):
    return supos_kernel_api.query_dataframe(sql=sql, params=params)

def _ensure_local_file_exists(file_path: str) -> None:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")


def load_local_file(file_path: str, sheet_name: Optional[str] = None):
    """
    加载本地 CSV 或 Excel 文件，并返回 pandas DataFrame。

    这个辅助函数属于 `prompts/runtime/sys_prompt.md` 中约定的 Python 执行工具契约。
    """
    import pandas as pd

    _ensure_local_file_exists(file_path)
    lower_file_path = file_path.lower()

    if lower_file_path.endswith('.csv'):
        loader_name = 'read_csv'
    elif lower_file_path.endswith(('.xlsx', '.xls', '.xlsm')):
        loader_name = 'read_excel'
    else:
        raise ValueError(f"不支持的文件格式: {file_path}")

    _begin_loader_snapshot_execution(
        loader='load_local_file',
        datasource_identifier=file_path,
        options={'sheet_name': sheet_name},
    )
    start_time = time.time()
    try:
        data = (
            pd.read_csv(file_path)
            if loader_name == 'read_csv'
            else pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)
        )
    except Exception:
        _mark_latest_query_snapshot_execution(status='failed', duration_ms=int((time.time() - start_time) * 1000))
        raise
    _mark_latest_query_snapshot_execution(
        status='success',
        duration_ms=int((time.time() - start_time) * 1000),
        data=data,
    )
    attrs = getattr(data, 'attrs', None)
    if isinstance(attrs, dict):
        attrs['data_insight_query_key'] = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
    return data


def _parse_excel_usecols(
    usecols: Optional[list[str] | list[int] | str],
    column_names: list[str],
) -> list[int]:
    if usecols is None:
        return list(range(len(column_names)))

    def _deduplicate(items: list[int]) -> list[int]:
        seen: set[int] = set()
        result: list[int] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    if isinstance(usecols, list):
        if all(isinstance(item, int) for item in usecols):
            resolved = [int(item) for item in usecols if 0 <= int(item) < len(column_names)]
            if not resolved:
                raise ValueError('低内存读取时没有匹配到任何 Excel 列，请检查 usecols 配置。')
            return _deduplicate(resolved)
        requested_names = {str(item).strip() for item in usecols if str(item).strip()}
        resolved = [index for index, name in enumerate(column_names) if name in requested_names]
        if not resolved:
            raise ValueError('低内存读取时没有匹配到任何 Excel 列，请检查 usecols 配置。')
        return _deduplicate(resolved)

    if isinstance(usecols, str):
        normalized = usecols.strip()
        if not normalized:
            return list(range(len(column_names)))

        try:
            from openpyxl.utils.cell import column_index_from_string
        except ImportError:
            column_index_from_string = None

        if column_index_from_string and ':' in normalized:
            start_token, end_token = [part.strip() for part in normalized.split(':', 1)]
            if start_token.isalpha() and end_token.isalpha():
                start_index = column_index_from_string(start_token.upper()) - 1
                end_index = column_index_from_string(end_token.upper()) - 1
                return list(range(max(start_index, 0), min(end_index, len(column_names) - 1) + 1))

        tokens = [token.strip() for token in normalized.split(',') if token.strip()]
        if tokens and all(token.isdigit() for token in tokens):
            resolved = [int(token) for token in tokens if 0 <= int(token) < len(column_names)]
            if not resolved:
                raise ValueError('低内存读取时没有匹配到任何 Excel 列，请检查 usecols 配置。')
            return _deduplicate(resolved)
        if column_index_from_string and tokens and all(token.isalpha() for token in tokens):
            resolved = [column_index_from_string(token.upper()) - 1 for token in tokens]
            resolved = [index for index in resolved if 0 <= index < len(column_names)]
            if not resolved:
                raise ValueError('低内存读取时没有匹配到任何 Excel 列，请检查 usecols 配置。')
            return _deduplicate(resolved)

        requested_names = set(tokens)
        resolved = [index for index, name in enumerate(column_names) if name in requested_names]
        if not resolved:
            raise ValueError('低内存读取时没有匹配到任何 Excel 列，请检查 usecols 配置。')
        return _deduplicate(resolved)

    raise ValueError('低内存读取时暂不支持当前 usecols 类型，请改用列名列表、列序号列表或逗号分隔字符串。')


def _apply_batch_transforms(
    dataframe,
    dtype: Optional[dict[str, Any] | str] = None,
    parse_dates: Optional[list[str] | bool] = None,
):
    import pandas as pd

    if parse_dates is True:
        for column in dataframe.columns:
            dataframe[column] = pd.to_datetime(dataframe[column], errors='ignore')
    elif isinstance(parse_dates, list):
        for column in parse_dates:
            if column in dataframe.columns:
                dataframe[column] = pd.to_datetime(dataframe[column], errors='coerce')

    if dtype is None:
        return dataframe

    if isinstance(dtype, dict):
        matched_dtype = {column: value for column, value in dtype.items() if column in dataframe.columns}
        if matched_dtype:
            return dataframe.astype(matched_dtype)
        return dataframe

    return dataframe.astype(dtype)


def _iter_excel_rows_in_batches(
    file_path: str,
    sheet_name: Optional[str] = None,
    chunk_size: int = 50000,
    usecols: Optional[list[str] | list[int] | str] = None,
    dtype: Optional[dict[str, Any] | str] = None,
    parse_dates: Optional[list[str] | bool] = None,
):
    import pandas as pd

    normalized_chunk_size = max(int(chunk_size or 50000), 1)
    lower_file_path = file_path.lower()

    if lower_file_path.endswith(('.xlsx', '.xlsm')):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError('当前环境缺少 openpyxl，无法对 .xlsx/.xlsm 文件执行低内存分批读取。') from exc

        workbook = load_workbook(file_path, read_only=True, data_only=True)
        try:
            worksheet = workbook[sheet_name] if sheet_name else workbook.worksheets[0]
            row_iterator = worksheet.iter_rows(values_only=True)
            headers = next(row_iterator, None)
            if headers is None:
                return

            column_names = [str(value).strip() if value is not None else '' for value in headers]
            selected_indexes = _parse_excel_usecols(usecols, column_names)
            selected_columns = [column_names[index] or f'column_{index + 1}' for index in selected_indexes]
            batch_rows: list[list[Any]] = []

            for row in row_iterator:
                current_row = row or ()
                batch_rows.append([
                    current_row[index] if index < len(current_row) else None
                    for index in selected_indexes
                ])
                if len(batch_rows) >= normalized_chunk_size:
                    batch_frame = pd.DataFrame(batch_rows, columns=selected_columns)
                    yield _apply_batch_transforms(batch_frame, dtype=dtype, parse_dates=parse_dates)
                    batch_rows = []

            if batch_rows:
                batch_frame = pd.DataFrame(batch_rows, columns=selected_columns)
                yield _apply_batch_transforms(batch_frame, dtype=dtype, parse_dates=parse_dates)
        finally:
            workbook.close()
        return

    if lower_file_path.endswith('.xls'):
        try:
            import xlrd
        except ImportError as exc:
            raise ValueError('当前环境缺少 xlrd，无法对 .xls 文件执行低内存分批读取；必要时请提示用户转换为 .xlsx 或 CSV。') from exc

        workbook = xlrd.open_workbook(file_path, on_demand=True)
        try:
            worksheet = workbook.sheet_by_name(sheet_name) if sheet_name else workbook.sheet_by_index(0)
            if worksheet.nrows == 0:
                return

            column_names = [str(value).strip() if value is not None else '' for value in worksheet.row_values(0)]
            selected_indexes = _parse_excel_usecols(usecols, column_names)
            selected_columns = [column_names[index] or f'column_{index + 1}' for index in selected_indexes]

            for start_row in range(1, worksheet.nrows, normalized_chunk_size):
                end_row = min(start_row + normalized_chunk_size, worksheet.nrows)
                batch_rows = []
                for row_index in range(start_row, end_row):
                    row_values = worksheet.row_values(row_index)
                    batch_rows.append([
                        row_values[index] if index < len(row_values) else None
                        for index in selected_indexes
                    ])
                if batch_rows:
                    batch_frame = pd.DataFrame(batch_rows, columns=selected_columns)
                    yield _apply_batch_transforms(batch_frame, dtype=dtype, parse_dates=parse_dates)
        finally:
            workbook.release_resources()
        return

    raise ValueError(f'不支持的 Excel 文件格式: {file_path}')


def load_local_file_low_memory(
    file_path: str,
    sheet_name: Optional[str] = None,
    chunk_size: int = 50000,
    usecols: Optional[list[str] | list[int] | str] = None,
    dtype: Optional[dict[str, Any] | str] = None,
    parse_dates: Optional[list[str] | bool] = None,
    low_memory: bool = True,
):
    """按批次低内存读取本地 CSV 或 Excel，适合 OOM 后的修复性重试场景。"""
    import pandas as pd

    _ensure_local_file_exists(file_path)
    lower_file_path = file_path.lower()
    normalized_chunk_size = max(int(chunk_size or 50000), 1)

    if lower_file_path.endswith('.csv'):
        iterator = pd.read_csv(
            file_path,
            chunksize=normalized_chunk_size,
            usecols=usecols,
            dtype=dtype,
            parse_dates=parse_dates,
            low_memory=low_memory,
        )
    elif lower_file_path.endswith(('.xlsx', '.xls', '.xlsm')):
        iterator = _iter_excel_rows_in_batches(
            file_path=file_path,
            sheet_name=sheet_name,
            chunk_size=normalized_chunk_size,
            usecols=usecols,
            dtype=dtype,
            parse_dates=parse_dates,
        )
    else:
        raise ValueError(f"不支持的文件格式: {file_path}")

    _begin_loader_snapshot_execution(
        loader='load_local_file_low_memory',
        datasource_identifier=file_path,
        options={
            'sheet_name': sheet_name,
            'chunk_size': normalized_chunk_size,
            'usecols': usecols,
            'parse_dates': parse_dates,
        },
    )
    query_key = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
    start_time = time.time()

    def _tracked_iterator():
        row_count = 0
        columns: list[str] = []
        try:
            for chunk in iterator:
                row_count += int(len(chunk))
                columns = [str(column) for column in getattr(chunk, 'columns', [])]
                attrs = getattr(chunk, 'attrs', None)
                if isinstance(attrs, dict):
                    attrs['data_insight_query_key'] = query_key
                yield chunk
        except Exception:
            _mark_latest_query_snapshot_execution(
                status='failed',
                duration_ms=int((time.time() - start_time) * 1000),
                query_key=query_key,
            )
            raise
        _mark_latest_query_snapshot_execution(
            status='success',
            duration_ms=int((time.time() - start_time) * 1000),
            query_key=query_key,
            row_count=row_count,
            columns=columns,
        )

    return _tracked_iterator()


def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None):
    """预留给后续 MinIO 文件加载场景的辅助函数。"""
    pass


def load_data_with_sql(
    sql: str,
    params: Optional[list[Any]] = None,
    numeric_columns: Optional[list[str]] = None,
    text_columns: Optional[list[str]] = None,
):
    _begin_query_snapshot_execution(sql=sql, params=params)
    start_time = time.time()
    try:
        data = _load_data_with_fed_query(sql=sql, params=params)
        data = _restore_sql_projection_column_names(sql, data)
        explicit_numeric_columns = list(dict.fromkeys(
            str(item) for item in (numeric_columns or []) if str(item)
        ))
        explicit_text_columns = list(dict.fromkeys(
            str(item) for item in (text_columns or []) if str(item)
        ))
        conflicting_columns = sorted(
            set(explicit_numeric_columns) & set(explicit_text_columns)
        )
        if conflicting_columns:
            raise ValueError(
                "columns cannot be both numeric_columns and text_columns: "
                + ", ".join(conflicting_columns)
            )
        missing_explicit_columns = [
            column for column in explicit_numeric_columns
            if column not in data.columns
        ]
        missing_text_columns = [
            column for column in explicit_text_columns
            if column not in data.columns
        ]
        if missing_explicit_columns:
            raise ValueError(
                "numeric_columns references unavailable query result columns: "
                + ", ".join(missing_explicit_columns)
            )
        if missing_text_columns:
            raise ValueError(
                "text_columns references unavailable query result columns: "
                + ", ".join(missing_text_columns)
            )
        inferred_numeric_columns = [
            column
            for column in [
                *_infer_sql_aggregate_numeric_columns(sql),
                *_infer_schema_numeric_result_columns(sql, list(data.columns)),
            ]
            if column in data.columns and column not in explicit_text_columns
        ]
        effective_numeric_columns = list(dict.fromkeys([
            *explicit_numeric_columns,
            *inferred_numeric_columns,
        ]))
        if effective_numeric_columns:
            import pandas as pd
            for column in effective_numeric_columns:
                data[column] = pd.to_numeric(data[column], errors="raise")
        for column in explicit_text_columns:
            data[column] = data[column].map(
                lambda value: "" if value is None else str(value).strip()
            )
        population_diagnostics = _inspect_zero_population_aggregate(sql, data)
    except Exception:
        _mark_latest_query_snapshot_execution(status='failed', duration_ms=int((time.time() - start_time) * 1000))
        raise
    _mark_latest_query_snapshot_execution(
        status='success',
        duration_ms=int((time.time() - start_time) * 1000),
        data=data,
        diagnostics=population_diagnostics,
    )
    attrs = getattr(data, 'attrs', None)
    if isinstance(attrs, dict):
        attrs['data_insight_query_key'] = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
    return data


def probe_data_with_sql(
    sql: str,
    params: Optional[list[Any]] = None,
    numeric_columns: Optional[list[str]] = None,
    text_columns: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Execute one diagnostic SQL query without hiding its outcome.

    Probe programs often need to inspect several independent candidate queries.
    This wrapper keeps query failures as structured observations so one invalid
    candidate does not force generated code to invent broad exception handling.
    Formal analysis code must continue to use ``load_data_with_sql`` directly.
    """
    if CURRENT_EXECUTION_INTENT.get() != "probe":
        raise RuntimeError("probe_data_with_sql(...) is only available to probe code")
    try:
        data = load_data_with_sql(
            sql=sql,
            params=params,
            numeric_columns=numeric_columns,
            text_columns=text_columns,
        )
    except Exception as exc:
        error_message = str(exc)
        error_type = _classify_execution_error(exc, error_message)
        return {
            "status": "no_data" if error_type == "no_data_found" else "query_error",
            "data": None,
            "row_count": 0,
            "error_type": error_type,
            "error_message": error_message,
        }
    return {
        "status": "success",
        "data": data,
        "row_count": int(len(data)),
        "error_type": "",
        "error_message": "",
    }


def _restore_sql_projection_column_names(sql: str, data: Any) -> Any:
    """Align DataFrame labels with unambiguous names declared by SELECT."""
    columns = [str(item) for item in getattr(data, "columns", [])]
    if not columns:
        return data
    try:
        from sqlglot import exp, parse_one

        statement = parse_one(str(sql or ""))
        select = statement.find(exp.Select)
    except Exception:
        return data
    if select is None:
        return data

    declared_names: list[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            continue
        if isinstance(projection, exp.Alias):
            declared = str(projection.alias or "").strip()
        elif isinstance(projection, exp.Column):
            declared = str(projection.name or "").strip()
        else:
            declared = ""
        if declared:
            declared_names.append(declared)

    actual_by_folded: dict[str, list[str]] = {}
    for column in columns:
        actual_by_folded.setdefault(column.casefold(), []).append(column)
    rename_map: dict[str, str] = {}
    claimed_targets = set(columns)
    for declared in declared_names:
        if declared in claimed_targets:
            continue
        matches = actual_by_folded.get(declared.casefold(), [])
        if len(matches) != 1:
            continue
        actual = matches[0]
        if actual in rename_map or declared in claimed_targets:
            continue
        rename_map[actual] = declared
        claimed_targets.add(declared)
    return data.rename(columns=rename_map) if rename_map else data


def _inspect_zero_population_aggregate(sql: str, data: Any) -> dict[str, Any]:
    """Describe a one-row aggregate whose unconditional population counts are all zero."""
    if CURRENT_EXECUTION_INTENT.get() != 'analysis':
        return {}
    if data is None or len(data) != 1:
        return {}
    try:
        from sqlglot import exp, parse_one

        statement = parse_one(str(sql or ''))
        select = statement.find(exp.Select)
    except Exception:
        return {}
    if select is None or select.args.get('group') is not None:
        return {}

    count_columns: list[str] = []
    for projection in select.expressions:
        source = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(source, exp.Count):
            continue
        if any(isinstance(node, (exp.Case, exp.If)) for node in source.walk()):
            continue
        alias = str(projection.alias_or_name or '').strip()
        if alias and alias in data.columns:
            count_columns.append(alias)
    if not count_columns:
        return {}

    values: list[float] = []
    for column in count_columns:
        try:
            values.append(float(data.iloc[0][column] or 0))
        except (TypeError, ValueError):
            return {}
    if values and all(value <= 0 for value in values):
        return {
            'effective_no_data': True,
            'population_count_columns': count_columns,
            'population_count_values': values,
        }
    return {}


def _normalize_sql_driver_placeholders(sql: str) -> str:
    """Make DB-API placeholders parseable without changing SQL semantics."""
    return re.sub(
        r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s",
        "'__query_parameter__'",
        str(sql or ""),
    )


def _infer_schema_numeric_result_columns(sql: str, result_columns: list[Any]) -> list[str]:
    """Map direct SQL projections back to numeric fields in the selected datasource schemas."""
    profiles = CURRENT_DATASOURCE_NUMERIC_FIELDS.get() or []
    sql_text = str(sql or "")
    matched_fields: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        identifiers = [str(item or "").strip() for item in profile.get("identifiers") or []]
        if identifiers and not any(identifier in sql_text for identifier in identifiers):
            continue
        matched_fields.update(str(item) for item in profile.get("fields") or [] if str(item))
    if not matched_fields:
        return []

    output_names = {str(item) for item in result_columns}
    inferred = [field for field in matched_fields if field in output_names]
    try:
        from sqlglot import exp, parse_one

        statement = parse_one(_normalize_sql_driver_placeholders(sql_text))
        select = statement.find(exp.Select)
        for projection in select.expressions if select is not None else []:
            source = projection.this if isinstance(projection, exp.Alias) else projection
            output_name = str(projection.alias_or_name or getattr(source, "name", ""))
            if output_name not in output_names:
                continue
            if isinstance(source, exp.Column) and source.name in matched_fields:
                inferred.append(output_name)
                continue
            aggregate_nodes = list(source.find_all(exp.AggFunc))
            referenced_fields = {
                str(column.name or "").strip()
                for column in source.find_all(exp.Column)
                if str(column.name or "").strip()
            }
            if aggregate_nodes and referenced_fields & matched_fields:
                inferred.append(output_name)
    except Exception:
        pass
    return list(dict.fromkeys(inferred))


def _infer_sql_aggregate_numeric_columns(sql: str) -> list[str]:
    """Infer unambiguously numeric aggregate and date-part projection aliases."""
    try:
        from sqlglot import exp, parse
    except Exception:
        return []

    result: list[str] = []
    try:
        statements = parse(_normalize_sql_driver_placeholders(sql))
    except Exception:
        return []
    for statement in statements:
        selects = list(statement.find_all(exp.Select))
        if isinstance(statement, exp.Select):
            selects.insert(0, statement)
        for select in selects:
            for projection in select.expressions:
                aggregate_nodes = list(projection.find_all(exp.AggFunc))
                numeric_aggregate = bool(aggregate_nodes) and not all(
                    isinstance(node, (exp.Min, exp.Max)) for node in aggregate_nodes
                )
                date_part_projection = bool(list(projection.find_all(exp.Extract)))
                if not date_part_projection:
                    date_part_projection = any(
                        isinstance(node, exp.Anonymous)
                        and str(node.name or "").strip().upper()
                        in {"YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"}
                        for node in projection.walk()
                    )
                if not date_part_projection:
                    direct_expression = projection.this if isinstance(projection, exp.Alias) else projection
                    date_part_projection = (
                        isinstance(direct_expression, exp.Column)
                        and bool(re.search(
                            r"(?i)^WM_EXTRA_TS_.+_(?:YEAR|MONTH|DAY|HOUR|MINUTE|SECOND)$",
                            str(direct_expression.name or ""),
                        ))
                    )
                if not numeric_aggregate and not date_part_projection:
                    continue
                alias = str(projection.alias_or_name or "").strip()
                if alias and alias not in result:
                    result.append(alias)
    return result

def load_local_data_with_sql(sql: str, params: Optional[list[Any]] = None):
    """通过 SQL 从当前配置的数据源中加载表格数据。"""
    import pandas as pd

    from config.database import engine

    with engine.connect() as connection:
        return pd.read_sql(sql, connection, params=params if params else None)


def _json_default(value: Any) -> Any:
    item = getattr(value, 'item', None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    isoformat = getattr(value, 'isoformat', None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _copy_jsonable(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    except Exception:
        return fallback


def _clip_snapshot_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or '')
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _clip_snapshot_json(value: Any, max_chars: int) -> tuple[Any, bool]:
    jsonable = _copy_jsonable(value, {})
    text = json.dumps(jsonable, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return jsonable, False
    return {"truncated_preview": text[:max_chars]}, True


def _extract_sql_projection_semantics(sql: str) -> list[dict[str, Any]]:
    """Persist compact projection lineage before the SQL preview is truncated."""
    try:
        from sqlglot import exp, parse_one

        statement = parse_one(str(sql or ''))
    except Exception:
        return []
    select = statement.find(exp.Select)
    if select is None:
        return []
    group = select.args.get('group')
    group_expressions = list(group.expressions) if isinstance(group, exp.Group) else []
    group_keys = {
        value
        for expression in group_expressions
        for value in (
            expression.sql(pretty=False).strip(),
            str(expression.alias_or_name or '').strip(),
        )
        if value
    }
    projections: list[dict[str, Any]] = []
    for projection in select.expressions:
        alias = str(projection.alias or projection.alias_or_name or '').strip()
        if not alias:
            continue
        calculation = projection.this if isinstance(projection, exp.Alias) else projection
        if calculation.find(exp.AggFunc) is not None:
            semantic_kind = 'aggregation'
        elif calculation.find(exp.Window) is not None:
            semantic_kind = 'ranking'
        elif calculation.find(exp.Binary) is not None:
            semantic_kind = 'formula'
        else:
            semantic_kind = 'projection'
        expression_source = calculation.sql(pretty=False)
        projection_role = 'measure'
        if semantic_kind == 'projection':
            projection_role = (
                'dimension'
                if expression_source.strip() in group_keys or alias in group_keys
                else 'unknown'
            )
        projections.append({
            'alias': alias,
            'semantic_kind': semantic_kind,
            'projection_role': projection_role,
            'expression': expression_source[:2000],
        })
    return projections


def _normalize_query_key(query_key: str | None = None) -> str:
    normalized = re.sub(r'[^A-Za-z0-9_-]+', '_', str(query_key or '').strip()).strip('_')
    if normalized:
        return normalized[:80]
    snapshots = CURRENT_QUERY_SNAPSHOTS.get() or {}
    return f"query_{len(snapshots) + 1}"


def _analysis_scope_from_query_constraints(query_constraints: Any) -> dict[str, Any]:
    """Keep user-level filters separate from one physical query's metric grain."""
    constraints = query_constraints if isinstance(query_constraints, dict) else {}
    return {
        "time_range": _copy_jsonable(constraints.get("time_range"), None),
        "target_filters": _copy_jsonable(constraints.get("target_filters") or [], []),
    }


def _normalize_query_scope(
    *,
    query_key: str,
    query_constraints: Any,
    query_scope: Any = None,
) -> dict[str, Any]:
    """Normalize query-local semantics without making them a second analysis plan."""
    scope = _copy_jsonable(query_scope, {}) if isinstance(query_scope, dict) else {}
    scope["scope_status"] = "explicit"
    scope["query_key"] = query_key
    if "aggregation" not in scope:
        constraints = query_constraints if isinstance(query_constraints, dict) else {}
        scope["aggregation"] = _copy_jsonable(constraints.get("aggregation"), None)
    for key in ("fact_ids", "datasource_ids"):
        value = scope.get(key)
        if isinstance(value, list):
            scope[key] = list(dict.fromkeys(value))
    return scope


def _register_query_snapshot(
    *,
    query_key: str,
    query_constraints: Any,
    sql: str = '',
    query_params: Any = None,
    filter_expressions: Optional[list[str]] = None,
    aggregation_hints: Optional[list[str]] = None,
    sql_context: Optional[dict[str, Any]] = None,
    query_scope: Optional[dict[str, Any]] = None,
) -> "RetryResult | None":
    snapshots = dict(CURRENT_QUERY_SNAPSHOTS.get() or {})
    sql_text, sql_truncated = _clip_snapshot_text(sql, QUERY_SNAPSHOT_SQL_MAX_CHARS)
    sql_context_json, sql_context_truncated = _clip_snapshot_json(sql_context or {}, QUERY_SNAPSHOT_CONTEXT_MAX_CHARS)
    snapshot = {
        "query_key": query_key,
        "query_constraints": _copy_jsonable(query_constraints, {}),
        "analysis_scope": _analysis_scope_from_query_constraints(query_constraints),
        "query_scope": _normalize_query_scope(
            query_key=query_key,
            query_constraints=query_constraints,
            query_scope=query_scope,
        ),
        "implementation": {
            "sql": sql_text,
            "sql_hash": hashlib.sha256(str(sql or '').encode('utf-8')).hexdigest() if str(sql or '').strip() else '',
            "sql_truncated": sql_truncated,
            "projection_semantics": _extract_sql_projection_semantics(sql),
            "query_params": _copy_jsonable(query_params, None),
            "filter_expressions": _copy_jsonable(filter_expressions or [], []),
            "aggregation_hints": _copy_jsonable(aggregation_hints or [], []),
            "sql_context": sql_context_json,
            "sql_context_truncated": sql_context_truncated,
        },
        "execution": {
            "status": "validated",
        },
    }
    existing = snapshots.get(query_key)
    if isinstance(existing, dict):
        comparable_existing = {
            "query_constraints": existing.get("query_constraints") or {},
            "analysis_scope": existing.get("analysis_scope") or {},
            "query_scope": existing.get("query_scope") or {},
            "implementation": existing.get("implementation") or {},
        }
        comparable_new = {
            "query_constraints": snapshot["query_constraints"],
            "analysis_scope": snapshot["analysis_scope"],
            "query_scope": snapshot["query_scope"],
            "implementation": snapshot["implementation"],
        }
        if comparable_existing != comparable_new:
            return request_retry(
                retry_type="invalid_query_constraints",
                message=f"query_key={query_key} 被重复用于不同查询，请为不同查询使用不同 query_key。",
                diagnostics={"query_key": query_key},
                repair_instructions=[
                    "同一个 query_key 只能表示同一条逻辑查询。",
                    "如果存在多条 SQL 或过滤逻辑，请分别使用不同的 query_key。",
                ],
            )
        snapshot["execution"] = existing.get("execution") or snapshot["execution"]
    snapshots[query_key] = snapshot
    CURRENT_QUERY_SNAPSHOTS.set(snapshots)
    CURRENT_LAST_QUERY_KEY.set(query_key)
    return None


def _begin_query_snapshot_execution(*, sql: str, params: Any = None) -> None:
    """Bind one physical SQL execution to its own immutable query snapshot."""
    sql_text = str(sql or '')
    if not sql_text.strip():
        return
    snapshots = dict(CURRENT_QUERY_SNAPSHOTS.get() or {})
    sql_hash = hashlib.sha256(sql_text.encode('utf-8')).hexdigest()
    matching_key = next(
        (
            str(key)
            for key, value in snapshots.items()
            if isinstance(value, dict)
            and isinstance(value.get('implementation'), dict)
            and value['implementation'].get('sql_hash') == sql_hash
        ),
        '',
    )
    if matching_key:
        matching_snapshot = dict(snapshots[matching_key])
        matching_implementation = dict(matching_snapshot.get('implementation') or {})
        matching_implementation['physical_execution'] = True
        matching_snapshot['implementation'] = matching_implementation
        snapshots[matching_key] = matching_snapshot
        CURRENT_QUERY_SNAPSHOTS.set(snapshots)
        CURRENT_LAST_QUERY_KEY.set(matching_key)
        return

    parent_key = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
    parent = snapshots.get(parent_key) if parent_key else None
    parent = parent if isinstance(parent, dict) else {}
    parent_implementation = (
        parent.get('implementation')
        if isinstance(parent.get('implementation'), dict)
        else {}
    )
    scope_source_key = str(parent_implementation.get('scope_source_query_key') or '').strip()
    if scope_source_key and isinstance(snapshots.get(scope_source_key), dict):
        parent_key = scope_source_key
        parent = snapshots[scope_source_key]
        parent_implementation = (
            parent.get('implementation')
            if isinstance(parent.get('implementation'), dict)
            else {}
        )
    family_sql = re.sub(
        r'(?i)\b(LIMIT|OFFSET)\s+\d+\b',
        lambda match: f'{match.group(1).upper()} __page_value__',
        sql_text,
    )
    family_hash = hashlib.sha256(family_sql.encode('utf-8')).hexdigest()
    query_key = _normalize_query_key(f'query_auto_{family_hash[:12]}')
    existing_family = snapshots.get(query_key)
    physical_executions: list[dict[str, Any]] = []
    if isinstance(existing_family, dict):
        existing_implementation = (
            existing_family.get('implementation')
            if isinstance(existing_family.get('implementation'), dict)
            else {}
        )
        physical_executions = list(
            existing_implementation.get('physical_executions') or []
        )
        previous_sql = str(existing_implementation.get('sql') or '')
        previous_execution = (
            existing_family.get('execution')
            if isinstance(existing_family.get('execution'), dict)
            else {}
        )
        if previous_sql and previous_execution.get('status') != 'running':
            physical_executions.append({
                'sql': previous_sql,
                'sql_hash': str(existing_implementation.get('sql_hash') or ''),
                'query_params': _copy_jsonable(
                    existing_implementation.get('query_params'), None
                ),
                'execution': _copy_jsonable(previous_execution, {}),
            })
    sql_snapshot, sql_truncated = _clip_snapshot_text(sql_text, QUERY_SNAPSHOT_SQL_MAX_CHARS)
    snapshots[query_key] = {
        'query_key': query_key,
        # A helper query may share the user's time/filter envelope with the
        # previous query, but it must not inherit that query's metric, grain or
        # business-fact boundary.  Its own SQL remains the only reliable source
        # of physical lineage until the program declares an explicit scope.
        'query_constraints': {},
        'analysis_scope': _copy_jsonable(
            parent.get('analysis_scope')
            if isinstance(parent.get('analysis_scope'), dict)
            else _analysis_scope_from_query_constraints(parent.get('query_constraints')),
            {},
        ),
        'query_scope': {
            'scope_status': 'implicit',
            'query_key': query_key,
            'logical_query_family': query_key,
        },
        'implementation': {
            'sql': sql_snapshot,
            'sql_hash': sql_hash,
            'sql_truncated': sql_truncated,
            'projection_semantics': _extract_sql_projection_semantics(sql_text),
            'query_params': _copy_jsonable(params, None),
            'filter_expressions': [],
            'aggregation_hints': [],
            'sql_context': {},
            'sql_context_truncated': False,
            'logical_query_family': query_key,
            'sql_family_hash': family_hash,
            'physical_executions': physical_executions,
            'physical_execution': True,
        },
        'execution': {'status': 'running'},
    }
    CURRENT_QUERY_SNAPSHOTS.set(snapshots)
    CURRENT_LAST_QUERY_KEY.set(query_key)


def _begin_loader_snapshot_execution(
    *,
    loader: str,
    datasource_identifier: str,
    options: dict[str, Any] | None = None,
) -> None:
    identifier = str(datasource_identifier or '').strip()
    if not identifier:
        return
    snapshots = dict(CURRENT_QUERY_SNAPSHOTS.get() or {})
    identity = json.dumps(
        {"loader": loader, "datasource_identifier": identifier, "options": options or {}},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    access_hash = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    matching_key = next(
        (
            str(key)
            for key, value in snapshots.items()
            if isinstance(value, dict)
            and isinstance(value.get('implementation'), dict)
            and value['implementation'].get('access_hash') == access_hash
        ),
        '',
    )
    if matching_key:
        CURRENT_LAST_QUERY_KEY.set(matching_key)
        return

    parent_key = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
    parent = snapshots.get(parent_key) if parent_key else None
    parent = parent if isinstance(parent, dict) else {}
    parent_implementation = parent.get('implementation') if isinstance(parent.get('implementation'), dict) else {}
    scope_source_key = str(parent_implementation.get('scope_source_query_key') or '').strip()
    if scope_source_key and isinstance(snapshots.get(scope_source_key), dict):
        parent_key = scope_source_key
        parent = snapshots[scope_source_key]
    query_key = _normalize_query_key(f'{parent_key or "query"}__load_{access_hash[:10]}')
    snapshots[query_key] = {
        'query_key': query_key,
        'query_constraints': {},
        'analysis_scope': _copy_jsonable(
            parent.get('analysis_scope')
            if isinstance(parent.get('analysis_scope'), dict)
            else _analysis_scope_from_query_constraints(parent.get('query_constraints')),
            {},
        ),
        'query_scope': {
            'scope_status': 'implicit',
            'query_key': query_key,
            'scope_source_query_key': parent_key,
        },
        'implementation': {
            'loader': loader,
            'datasource_identifier': identifier,
            'loader_options': _copy_jsonable(options or {}, {}),
            'access_hash': access_hash,
            'scope_source_query_key': parent_key,
            'physical_execution': True,
        },
        'execution': {'status': 'running'},
    }
    CURRENT_QUERY_SNAPSHOTS.set(snapshots)
    CURRENT_LAST_QUERY_KEY.set(query_key)


def _mark_latest_query_snapshot_execution(
    *,
    status: str,
    duration_ms: int = 0,
    data: Any = None,
    query_key: str = '',
    row_count: int | None = None,
    columns: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    query_key = str(query_key or CURRENT_LAST_QUERY_KEY.get() or '').strip()
    if not query_key:
        return
    snapshots = dict(CURRENT_QUERY_SNAPSHOTS.get() or {})
    snapshot = snapshots.get(query_key)
    if not isinstance(snapshot, dict):
        return
    execution = dict(snapshot.get("execution") or {})
    execution["status"] = status
    if duration_ms >= 0:
        execution["duration_ms"] = int(duration_ms)
    if data is not None:
        try:
            execution["row_count"] = int(len(data))
        except Exception:
            pass
        columns = getattr(data, "columns", None)
        if columns is not None:
            try:
                execution["columns"] = [str(column) for column in list(columns)]
            except Exception:
                pass
    if row_count is not None:
        execution["row_count"] = max(int(row_count), 0)
    if columns is not None:
        execution["columns"] = [str(column) for column in columns]
    if diagnostics:
        execution.update(_copy_jsonable(diagnostics, {}))
    implementation = (
        snapshot.get("implementation")
        if isinstance(snapshot.get("implementation"), dict)
        else {}
    )
    physical_executions = implementation.get("physical_executions") or []
    if implementation.get("logical_query_family"):
        prior_rows = sum(
            max(int((item.get("execution") or {}).get("row_count") or 0), 0)
            for item in physical_executions
            if isinstance(item, dict)
        )
        execution["physical_execution_count"] = len(physical_executions) + 1
        execution["family_row_count"] = prior_rows + max(
            int(execution.get("row_count") or 0), 0
        )
    execution["executed_at"] = datetime.now().isoformat()
    snapshot["execution"] = execution
    snapshots[query_key] = snapshot
    CURRENT_QUERY_SNAPSHOTS.set(snapshots)

def load_data_with_api(
    endpoint: str,
    method: str = "GET",
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, Any]] = None,
    timeout: int = 30,
):
    """通过 HTTP API 加载表格数据，并返回 pandas DataFrame。"""
    from io import StringIO as CsvBuffer

    import pandas as pd
    import requests

    response = requests.request(
        method=method.upper(),
        url=endpoint,
        params=params if method.upper() == "GET" else None,
        json=params if method.upper() == "POST" else None,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()

    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        json_data = response.json()
        if isinstance(json_data, list):
            return pd.DataFrame(json_data)
        if isinstance(json_data, dict):
            if 'data' in json_data and isinstance(json_data['data'], list):
                return pd.DataFrame(json_data['data'])
            return pd.DataFrame([json_data])
        raise ValueError("不支持的 JSON 数据格式")

    if 'text/csv' in content_type or endpoint.endswith('.csv'):
        return pd.read_csv(CsvBuffer(response.text))

    raise ValueError(f"不支持的响应格式: {content_type}")


def get_day_range(days_ago: int = 0, timezone_name: str = 'Asia/Shanghai') -> tuple[datetime, datetime]:
    """
    返回某个自然日的起止时间。

    `days_ago=0` 表示今天，`1` 表示昨天，`2` 表示前天。
    返回值始终带时区信息，便于与带时区的时间列直接比较。
    """
    tz = ZoneInfo(timezone_name)
    target_date = (datetime.now(tz) - timedelta(days=days_ago)).date()
    start_at = datetime.combine(target_date, datetime_time.min, tzinfo=tz)
    end_at = datetime.combine(target_date, datetime_time.max, tzinfo=tz)
    return start_at, end_at


def describe_time_coverage(dataframe, column_name: str) -> dict[str, Any]:
    """
    描述某个时间列的数据覆盖范围。

    适合在“最近 / 上个月 / 近 N 天 / 根因关联”这类时间敏感分析中，
    先判断目标时间窗口是否命中数据，再决定后续过滤、关联和空结果处理策略。
    """
    import pandas as pd

    if dataframe is None:
        return {
            'column_name': column_name,
            'row_count': 0,
            'non_null_count': 0,
            'min_time': None,
            'max_time': None,
        }

    if not isinstance(dataframe, pd.DataFrame):
        dataframe = pd.DataFrame(dataframe)

    if column_name not in dataframe.columns:
        raise KeyError(column_name)

    series = pd.to_datetime(dataframe[column_name], errors='coerce')
    valid_series = series.dropna()
    if valid_series.empty:
        return {
            'column_name': column_name,
            'row_count': int(len(dataframe)),
            'non_null_count': 0,
            'min_time': None,
            'max_time': None,
        }

    return {
        'column_name': column_name,
        'row_count': int(len(dataframe)),
        'non_null_count': int(valid_series.shape[0]),
        'min_time': valid_series.min().isoformat(),
        'max_time': valid_series.max().isoformat(),
    }


def probe_distinct_values(
    dataframe,
    column_name: str,
    top_n: int = 20,
    dropna: bool = True,
) -> list[dict[str, Any]]:
    """
    返回某个字段的高频取值分布，适合在“无数据重试”时先做轻量探测。

    常见用途：
    - 看筛选字段是否真的存在预期取值
    - 看枚举值是否存在空格、大小写、前后缀差异
    - 给后续条件纠偏提供候选值，而不是盲目放宽语义
    """
    import pandas as pd

    frame = _ensure_probe_dataframe(dataframe)
    if column_name not in frame.columns:
        raise KeyError(column_name)

    normalized_top_n = max(int(top_n or 20), 1)
    series = frame[column_name]
    counts = series.value_counts(dropna=dropna).head(normalized_top_n)

    return [
        {
            'value': _serialize_probe_value(index),
            'count': int(count),
            'normalized_value': _normalize_probe_text(index),
        }
        for index, count in counts.items()
    ]


def probe_text_candidates(
    dataframe,
    column_name: str,
    keyword: str,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """
    基于字符串规范化和相似度，为目标关键词返回候选值。

    适合在无数据重试时，用于判断：
    - 用户输入值是否存在轻微格式差异
    - 是否应该把精确匹配纠偏成 contains / LIKE
    - 是否只是空格、大小写、连接符、中英文括号等表达差异
    """
    import pandas as pd

    frame = _ensure_probe_dataframe(dataframe)
    if column_name not in frame.columns:
        raise KeyError(column_name)

    normalized_keyword = _normalize_probe_text(keyword)
    if not normalized_keyword:
        return []

    counts = frame[column_name].value_counts(dropna=True)
    candidates: list[dict[str, Any]] = []
    for raw_value, count in counts.items():
        normalized_value = _normalize_probe_text(raw_value)
        if not normalized_value:
            continue
        similarity = SequenceMatcher(None, normalized_keyword, normalized_value).ratio()
        exact_match = normalized_value == normalized_keyword
        contains_match = normalized_keyword in normalized_value or normalized_value in normalized_keyword
        if not exact_match and not contains_match and similarity < 0.45:
            continue

        if exact_match:
            match_type = 'exact'
            score = 1.0
        elif contains_match:
            match_type = 'contains'
            score = max(similarity, 0.8)
        else:
            match_type = 'similar'
            score = similarity

        candidates.append({
            'value': _serialize_probe_value(raw_value),
            'count': int(count),
            'normalized_value': normalized_value,
            'match_type': match_type,
            'similarity': round(float(score), 4),
        })

    candidates.sort(
        key=lambda item: (
            {'exact': 3, 'contains': 2, 'similar': 1}.get(item['match_type'], 0),
            item['similarity'],
            item['count'],
        ),
        reverse=True,
    )
    return candidates[:max(int(top_n or 10), 1)]


def _ensure_probe_dataframe(dataframe):
    import pandas as pd

    if dataframe is None:
        return pd.DataFrame()
    if isinstance(dataframe, pd.DataFrame):
        return dataframe
    return pd.DataFrame(dataframe)


def _normalize_probe_text(value: Any) -> str:
    if value is None:
        return ''
    text = str(value).strip().lower()
    if not text or text == 'nan':
        return ''
    text = text.replace('（', '(').replace('）', ')')
    text = text.replace('【', '[').replace('】', ']')
    text = text.replace('－', '-').replace('—', '-').replace('_', '-')
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'[-/]+', '-', text)
    return text


def _serialize_probe_value(value: Any) -> Any:
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, datetime):
        return value.isoformat()
    return value.item() if hasattr(value, 'item') else value


def build_markdown_table(dataframe, columns: Optional[list[Any]] = None, max_rows: int = 10) -> str:
    """
    把 DataFrame 转成 Markdown 表格文本。

    这个辅助函数适合在“需要输出 Markdown 表格”的场景中直接复用，
    用来减少手写表格拼接时的引号、换行和格式错误。
    如果当前分析任务并不需要表格展示，则不必使用它。
    columns 可以传字符串列表，也可以传 {"key": ..., "label": ...} 列表；
    当 DataFrame 使用底层字段名时，应使用后一种形式提供用户可读表头。
    """
    import pandas as pd

    if dataframe is None:
        return ''

    if not isinstance(dataframe, pd.DataFrame):
        dataframe = pd.DataFrame(dataframe)

    header_labels: dict[str, str] = {}
    if columns:
        available_columns = []
        for column in columns:
            if isinstance(column, dict):
                key = str(column.get('key') or column.get('field') or '').strip()
                label = str(column.get('label') or column.get('title') or key).strip()
            else:
                key = str(column).strip()
                label = key
            if key and key in dataframe.columns:
                available_columns.append(key)
                header_labels[key] = label
        table_df = dataframe[available_columns].copy()
    else:
        table_df = dataframe.copy()

    if max_rows > 0:
        table_df = table_df.head(max_rows)

    if table_df.empty:
        return ''

    table_df = table_df.fillna('')

    def _stringify(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:,.2f}".rstrip('0').rstrip('.')
        return re.sub(r'\s+', ' ', str(value)).strip()

    headers = [header_labels.get(str(column), str(column)) for column in table_df.columns]
    header_line = "| " + " | ".join(headers) + " |"
    separator_line = "| " + " | ".join(['---'] * len(headers)) + " |"

    body_lines = []
    for _, row in table_df.iterrows():
        values = [_stringify(row[column]).replace('|', '&#124;') for column in table_df.columns]
        body_lines.append("| " + " | ".join(values) + " |")

    return "\n".join([header_line, separator_line, *body_lines])


class StructuredResult(BaseModel):
    """当前 Agent 运行时消费的标准工具返回结构。"""

    analysis_report: str = Field(description="分析报告内容")
    charts: list[dict[str, Any]] = Field(default_factory=list, description="图表产物列表")
    tables: list[dict[str, Any]] = Field(default_factory=list, description="表格产物列表")
    analysis_parameters_schema: dict[str, Any] = Field(default_factory=dict, description="Reusable parameter schema")
    analysis_parameters: dict[str, Any] = Field(default_factory=dict, description="Actual parameter values")
    query_snapshots: dict[str, Any] = Field(default_factory=dict, description="Runtime query snapshots collected by backend")


class RetryResult(BaseModel):
    """Structured feedback that asks the agent to generate another code attempt."""

    retry_type: Literal[
        "probe_feedback",
        "data_source_unavailable",
        "missing_structured_artifact",
        "missing_report",
        "incomplete_result",
        "contract_error",
        "result_contract_error",
        "chart_contract_error",
        "missing_query_constraints",
        "invalid_query_constraints",
        "query_constraint_violation",
    ] = Field(description="Why the current execution needs another attempt.")
    message: str = Field(description="Human-readable retry reason.")
    diagnostics: dict[str, Any] = Field(default_factory=dict, description="Structured diagnostic facts.")
    repair_instructions: list[str] = Field(default_factory=list, description="Targeted next-step instructions.")
    analysis_report: str = Field(default="", description="Optional diagnostic report; not a final answer.")
    code_error_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Bounded generated-code location for a local repair.",
    )


class NoDataFoundError(Exception):
    """用于把“当前代码筛选后无数据”的情况显式上抛给 execute_python。"""

    def __init__(self, reason: str, detail_lines: Optional[list[str]] = None):
        normalized_reason = (reason or '当前条件下未命中可分析数据。').strip() or '当前条件下未命中可分析数据。'
        normalized_details = [
            str(item).strip()
            for item in (detail_lines or [])
            if str(item).strip()
        ]
        self.reason = normalized_reason
        self.detail_lines = normalized_details
        super().__init__(normalized_reason)


PROBE_TIME_RANGE_DRIFT_PATTERNS = (
    re.compile(r'真实数据覆盖范围.*重新生成分析代码'),
    re.compile(r'数据覆盖范围.*重新生成分析代码'),
    re.compile(r'调整时间范围'),
    re.compile(r'改查.*时间范围'),
    re.compile(r'查询可用.*时间范围'),
    re.compile(r'使用.*数据覆盖范围.*分析'),
    re.compile(r'(?:基于|按照|使用).*?(?:实际)?时间覆盖范围.*?(?:设置|调整|更新).*?time_range', re.I),
    re.compile(r'(?:使用|采用).*?(?:实际)?(?:expiration_time|时间).*?范围.*?(?:查询|分析)', re.I),
)

PROBE_TIME_RANGE_GUARD_INSTRUCTIONS = [
    'diagnostics 是探测证据，不是正式分析口径；数据覆盖范围只能用于解释无数据原因。',
    '如果用户问题里有明确时间范围，不能替换用户明确时间范围，也不能把 diagnostics 里的数据覆盖范围写回 query_constraints.time_range。',
    '如果探测确认用户明确时间范围内无数据，下一版应保持原始时间范围并返回无数据反馈或无数据结论，不要为了命中数据改查其他时间范围。',
]


def _normalize_probe_feedback_repair_instructions(instructions: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for instruction in instructions:
        if any(pattern.search(instruction) for pattern in PROBE_TIME_RANGE_DRIFT_PATTERNS):
            continue
        if instruction in seen:
            continue
        seen.add(instruction)
        normalized.append(instruction)

    for guard_instruction in PROBE_TIME_RANGE_GUARD_INSTRUCTIONS:
        if guard_instruction in seen:
            continue
        seen.add(guard_instruction)
        normalized.append(guard_instruction)

    return normalized


_PROBE_POPULATION_COUNT_FIELDS = {
    "row_count",
    "total_count",
    "population_count",
    "record_count",
    "distinct_count",
    "batch_count",
    "sample_count",
    "event_count",
}


def _extract_probe_population_counts(observation: dict[str, Any]) -> dict[str, int]:
    """Read explicit population aggregates without confusing them with result rows."""
    data = observation.get("data")
    records = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    counts: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for raw_key, raw_value in record.items():
            key = str(raw_key or "").strip().lower()
            if key not in _PROBE_POPULATION_COUNT_FIELDS:
                continue
            try:
                value = int(float(raw_value))
            except (TypeError, ValueError):
                continue
            if value < 0:
                continue
            counts[key] = max(counts.get(key, 0), value)
    return counts


def raise_no_data_error(reason: str, detail_lines: Optional[list[str]] = None) -> None:
    """
    在数据加载、过滤、关联或聚合后发现结果为空时，显式通知上层重试。

    生成代码遇到这类情况时，不应继续生成空图表并调用 save_analysis_result() 收口，
    而应调用本函数把“本轮未命中数据”的信息返回给 execute_python，再由上层决定是否继续重试。
    """
    raise NoDataFoundError(reason=reason, detail_lines=detail_lines)


def request_retry(
    retry_type: str,
    message: str,
    diagnostics: Optional[dict[str, Any]] = None,
    repair_instructions: Optional[list[str]] = None,
    analysis_report: str = "",
    code_error_context: Optional[dict[str, Any]] = None,
) -> RetryResult:
    """Return structured retry feedback from generated probe code."""
    allowed_types = {
        "probe_feedback",
        "data_source_unavailable",
        "missing_structured_artifact",
        "missing_report",
        "incomplete_result",
        "contract_error",
        "result_contract_error",
        "chart_contract_error",
        "missing_query_constraints",
        "invalid_query_constraints",
        "query_constraint_violation",
    }
    normalized_retry_type = str(retry_type or "").strip() or "contract_error"
    if normalized_retry_type not in allowed_types:
        normalized_retry_type = "contract_error"

    normalized_message = str(message or "").strip() or "当前代码执行完成，但仍需要重新生成代码继续分析。"
    normalized_instructions = [
        str(item).strip()
        for item in (repair_instructions or [])
        if str(item).strip()
    ]
    if normalized_retry_type == "probe_feedback":
        normalized_instructions = _normalize_probe_feedback_repair_instructions(normalized_instructions)
    normalized_diagnostics = _copy_jsonable(diagnostics or {}, {})
    if normalized_retry_type == "probe_feedback":
        query_observations = normalized_diagnostics.get("query_observations")
        if isinstance(query_observations, list):
            outcome_groups = {
                "successful": [],
                "no_data": [],
                "unavailable": [],
            }
            capacity_risks: list[dict[str, Any]] = []
            for observation in query_observations:
                if not isinstance(observation, dict):
                    continue
                try:
                    datasource_id = int(observation.get("datasource_id") or 0)
                except (TypeError, ValueError):
                    datasource_id = 0
                population_counts = _extract_probe_population_counts(observation)
                item = {
                    "datasource_id": datasource_id,
                    "datasource_name": str(observation.get("datasource_name") or ""),
                    "error_type": str(observation.get("error_type") or ""),
                    "row_count": int(observation.get("row_count") or 0),
                }
                if population_counts:
                    item["observed_population_counts"] = population_counts
                status = str(observation.get("status") or "")
                row_count = int(observation.get("row_count") or 0)
                if status == "success" and row_count > 0:
                    outcome_groups["successful"].append(item)
                    max_population = max(
                        [row_count, *population_counts.values()],
                        default=row_count,
                    )
                    if max_population > MAX_SQL_RESULT_ROWS:
                        capacity_risks.append({
                            "datasource_id": datasource_id,
                            "datasource_name": item["datasource_name"],
                            "observed_row_count": row_count,
                            "observed_population_count": max_population,
                            "observed_population_counts": population_counts,
                            "max_rows_per_physical_query": MAX_SQL_RESULT_ROWS,
                            "required_strategy": (
                                "query_side_final_aggregation_or_stable_pagination"
                            ),
                        })
                elif status == "no_data" or (status == "success" and row_count == 0):
                    outcome_groups["no_data"].append(item)
                else:
                    outcome_groups["unavailable"].append(item)
            normalized_diagnostics["_probe_outcome_summary"] = outcome_groups
            normalized_diagnostics["_query_capacity_risks"] = capacity_risks
        expected_time_range = CURRENT_EXPECTED_TIME_RANGE.get()
        normalized_diagnostics["_probe_contract"] = {
            "time_coverage_role": "diagnostic_only",
            "path_scope_role": "observed_paths_only",
            "query_execution": {
                "max_rows_per_physical_query": MAX_SQL_RESULT_ROWS,
                "capacity_risks_are_formal_design_inputs": True,
            },
            "explicit_user_time_range": (
                dict(expected_time_range)
                if isinstance(expected_time_range, dict) and expected_time_range
                else None
            ),
            "formal_scope_policy": (
                "Probe outcomes classify only the query paths present in query_observations. "
                "They do not remove unprobed selected datasources from the formal analysis "
                "scope. Keep an explicit user range unchanged; without one, do not use "
                "observed coverage to window an overall baseline."
            ),
        }
    return RetryResult(
        retry_type=normalized_retry_type,  # type: ignore[arg-type]
        message=normalized_message,
        diagnostics=normalized_diagnostics,
        repair_instructions=normalized_instructions,
        analysis_report=str(analysis_report or "").strip(),
        code_error_context=_copy_jsonable(code_error_context or {}, {}),
    )


def _extract_static_string_assignment(tree: ast.AST, name: str) -> str:
    for node in getattr(tree, 'body', []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value.strip()
    return ''


def _has_static_assignment(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return True
    return False


def _extract_static_assignment_literal(tree: ast.AST, name: str) -> Any:
    for node in getattr(tree, 'body', []):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        try:
            return ast.literal_eval(node.value)
        except Exception:
            return None
    return None


def _extract_validate_query_constraints_literal(tree: ast.AST) -> Any:
    """Return an inline literal passed as validate_query_constraints(query_constraints={...})."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != 'validate_query_constraints':
            continue
        for keyword in node.keywords or []:
            if keyword.arg != 'query_constraints':
                continue
            try:
                return ast.literal_eval(keyword.value)
            except Exception:
                return None
    return None


def _has_validate_query_constraints_argument(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != 'validate_query_constraints':
            continue
        for keyword in node.keywords or []:
            if keyword.arg == 'query_constraints':
                return True
    return False


def _extract_query_constraints_literal(tree: ast.AST) -> Any:
    assigned = _extract_static_assignment_literal(tree, 'query_constraints')
    if isinstance(assigned, dict):
        return assigned
    return _extract_validate_query_constraints_literal(tree)


def _called_function_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _call_line_numbers(tree: ast.AST, function_name: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        matched = False
        if isinstance(func, ast.Name) and func.id == function_name:
            matched = True
        elif isinstance(func, ast.Attribute) and func.attr == function_name:
            matched = True
        if matched:
            line_no = getattr(node, 'lineno', None)
            if isinstance(line_no, int):
                lines.append(line_no)
    return sorted(lines)


def _validate_analysis_sql_validation_order(tree: ast.AST) -> RetryResult | None:
    validate_lines = _call_line_numbers(tree, 'validate_query_constraints')
    load_lines = _call_line_numbers(tree, 'load_data_with_sql')
    if not validate_lines or not load_lines:
        return None
    first_validate_line = min(validate_lines)
    first_load_line = min(load_lines)
    if first_validate_line < first_load_line:
        return None
    return request_retry(
        retry_type="contract_error",
        message="正式 analysis 代码必须先调用 validate_query_constraints(...) 校验 SQL，再调用 load_data_with_sql(...) 执行查询。",
        diagnostics={
            'violated_contract': 'validate_query_constraints_before_sql_load',
            'first_validate_query_constraints_line': first_validate_line,
            'first_load_data_with_sql_line': first_load_line,
        },
        repair_instructions=[
            "先完整构造 query_constraints、SQL 和 sql_context.fragments，再调用 validate_query_constraints(...)。",
            "只有 validate_query_constraints(...) 返回 None 时，才可以进入 load_data_with_sql(...) 数据加载。",
            "不要先执行 SQL 再补做 validate_query_constraints(...)；这样会让错误 SQL 先进入执行链路。",
        ],
    )


def _validate_query_mapping_default_contract(tree: ast.AST) -> RetryResult | None:
    """Reject silent zero defaults for keys missing from query-derived mappings."""
    mapping_names: set[str] = set()
    count_mapping_names: set[str] = set()
    count_field_pattern = re.compile(
        r"(?:count|cnt|quantity|number|数量|个数|次数|记录数)$",
        re.IGNORECASE,
    )
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "to_dict"
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target_names = {
            target.id for target in targets if isinstance(target, ast.Name)
        }
        mapping_names.update(target_names)
        projected_fields = {
            str(item.value).strip()
            for item in ast.walk(value.func.value)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if any(count_field_pattern.search(field) for field in projected_fields):
            count_mapping_names.update(target_names)
        if any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "value_counts"
            for item in ast.walk(value.func.value)
        ):
            count_mapping_names.update(target_names)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id in mapping_names
        ):
            continue
        default = node.args[1]
        if not (
            isinstance(default, ast.Constant)
            and isinstance(default.value, (int, float))
            and not isinstance(default.value, bool)
            and default.value == 0
        ):
            continue
        # A fixed category absent from a count distribution has a well-defined
        # count of zero. Keep rejecting variable-key lookups and non-count
        # measures, where a type mismatch or missing period must remain distinct
        # from a measured zero.
        key = node.args[0]
        if (
            func.value.id in count_mapping_names
            and isinstance(key, ast.Constant)
            and isinstance(key.value, (str, int))
            and not isinstance(key.value, bool)
        ):
            continue
        return request_retry(
            retry_type="contract_error",
            message=(
                "查询结果按维度转成字典后，不能用 mapping.get(key, 0) "
                "把键缺失静默解释为真实数值 0。"
            ),
            diagnostics={
                "violated_contract": "query_dimension_missing_is_not_zero",
                "mapping_name": func.value.id,
                "line_no": getattr(node, "lineno", None),
            },
            repair_instructions=[
                "先统一查询维度和分析参数的类型，例如把年份两侧都规范为字符串，再显式判断 key 是否存在。",
                "键不存在表示口径、类型或数据覆盖缺失，不等于测量值为 0；应区分无记录与真实零值。",
                "修复完整代码中的同类查找，并让报告、图表和同比计算消费同一份已对齐结果。",
            ],
        )
    return None


def _validate_report_extrema_selection_contract(
    tree: ast.AST,
    code: str,
) -> RetryResult | None:
    """Require explicit extrema calculations for superlative report claims.

    A positional row can represent the latest period, the first category, or an
    arbitrary SQL order. It is not proof of a maximum/minimum unless the metric
    extremum was calculated explicitly. Restrict this check to report templates
    that make a superlative claim; ordinary positional presentation remains valid.
    """

    superlative_markers = (
        "最高", "最低", "最多", "最少", "峰值", "高峰", "为主", "主导",
        "highest", "lowest", "largest", "smallest", "peak", "dominant", "most ",
    )
    def sorted_frame_orders() -> dict[str, set[tuple[str, bool]]]:
        """Collect explicit DataFrame sort orders assigned back to a frame."""
        orders: dict[str, set[tuple[str, bool]]] = {}
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            target_names = {
                target.id for target in targets if isinstance(target, ast.Name)
            }
            if not target_names:
                continue
            for call in ast.walk(assignment.value):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "sort_values"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    continue
                ascending = True
                for keyword in call.keywords:
                    if (
                        keyword.arg == "ascending"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, bool)
                    ):
                        ascending = keyword.value.value
                for target_name in target_names:
                    orders.setdefault(target_name, set()).add((
                        str(call.args[0].value), ascending,
                    ))
        return orders

    frame_orders = sorted_frame_orders()

    def unwrap_display_call(value: ast.AST) -> ast.AST:
        current = value
        while (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id in {"int", "float", "str", "round"}
            and current.args
        ):
            current = current.args[0]
        return current

    def positional_row_signature(value: ast.AST) -> tuple[str, int] | None:
        """Return the DataFrame and row position for ``frame.iloc[n]['field']``."""
        value = unwrap_display_call(value)
        if not isinstance(value, ast.Subscript):
            return None
        if not (
            isinstance(value.slice, ast.Constant)
            and isinstance(value.slice.value, str)
        ):
            return None
        row = value.value
        if not (
            isinstance(row, ast.Subscript)
            and isinstance(row.value, ast.Attribute)
            and row.value.attr == "iloc"
            and isinstance(row.value.value, (ast.Name, ast.Call))
        ):
            return None
        if isinstance(row.slice, ast.Constant) and isinstance(row.slice.value, int):
            row_index = row.slice.value
        elif (
            isinstance(row.slice, ast.UnaryOp)
            and isinstance(row.slice.op, ast.USub)
            and isinstance(row.slice.operand, ast.Constant)
            and isinstance(row.slice.operand.value, int)
        ):
            row_index = -row.slice.operand.value
        else:
            return None
        if row_index not in {0, -1}:
            return None
        frame_expression = row.value.value
        frame_key = (
            frame_expression.id
            if isinstance(frame_expression, ast.Name)
            else ast.dump(frame_expression, include_attributes=False)
        )
        return frame_key, row_index

    def ordered_extremum_is_proven(value: ast.AST, claim_text: str) -> bool:
        """Accept a positional row only when its stated metric is sorted."""
        value = unwrap_display_call(value)
        if not isinstance(value, ast.Subscript):
            return False
        if not (
            isinstance(value.slice, ast.Constant)
            and isinstance(value.slice.value, str)
        ):
            return False
        metric_field = str(value.slice.value)
        row_signature = positional_row_signature(value)
        if row_signature is None:
            return False
        frame_name, row_index = row_signature
        lower = claim_text.lower()
        highest_markers = (
            "highest", "largest", "peak", "dominant", "most ",
            "\u6700\u9ad8", "\u6700\u5927", "\u6700\u591a", "\u5cf0\u503c", "\u4e3b\u5bfc", "\u4e3a\u4e3b",
        )
        lowest_markers = (
            "lowest", "smallest", "\u6700\u4f4e", "\u6700\u5c0f",
        )
        claims_highest = any(marker in lower for marker in highest_markers)
        claims_lowest = any(marker in lower for marker in lowest_markers)
        if claims_highest == claims_lowest:
            return False
        required_ascending = row_index == -1 if claims_highest else row_index == 0
        row = value.value
        frame_expression = row.value.value
        orders = set(frame_orders.get(frame_name, set()))
        if (
            isinstance(frame_expression, ast.Call)
            and isinstance(frame_expression.func, ast.Attribute)
            and frame_expression.func.attr == "sort_values"
            and frame_expression.args
            and isinstance(frame_expression.args[0], ast.Constant)
            and isinstance(frame_expression.args[0].value, str)
        ):
            ascending = True
            for keyword in frame_expression.keywords:
                if (
                    keyword.arg == "ascending"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, bool)
                ):
                    ascending = keyword.value.value
            orders.add((str(frame_expression.args[0].value), ascending))
        return (metric_field, required_ascending) in orders

    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal_text = "".join(
            str(item.value)
            for item in node.values
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        normalized_text = literal_text.lower()
        if not any(marker.lower() in normalized_text for marker in superlative_markers):
            continue
        positional_values: list[ast.AST] = []
        for formatted in node.values:
            if not isinstance(formatted, ast.FormattedValue):
                continue
            if any(
                isinstance(item, ast.Attribute) and item.attr == "iloc"
                for item in ast.walk(formatted.value)
            ):
                positional_values.append(formatted.value)
        proven_rows = {
            signature
            for value in positional_values
            if ordered_extremum_is_proven(value, literal_text)
            for signature in [positional_row_signature(value)]
            if signature is not None
        }
        positional_expressions = [
            ast.get_source_segment(code, value) or ast.unparse(value)
            for value in positional_values
            if positional_row_signature(value) not in proven_rows
        ]
        if positional_expressions:
            findings.append({
                "line_no": int(getattr(node, "lineno", 0) or 0),
                "claim_preview": literal_text[:240],
                "positional_expressions": positional_expressions,
            })
    if not findings:
        return None
    return request_retry(
        retry_type="contract_error",
        message=(
            "报告使用 DataFrame 位置行直接支撑最高、最低、最多、峰值或主导项判断；"
            "位置顺序不能证明业务极值。"
        ),
        diagnostics={
            "violated_contract": "report_superlative_requires_explicit_extrema",
            "findings": findings[:12],
        },
        repair_instructions=[
            "保留现有查询、指标、图表和报告结构，只修正极值/主导项的计算变量及其引用。",
            "按报告声称的业务度量显式使用 idxmax()/idxmin()，或先按该度量明确排序后选行；不要用未证明顺序的 iloc[0]/iloc[-1] 直接声称最高、最低、最多、峰值或主导。",
            "维度值与对应度量必须来自同一条已选中的极值记录；同步修正报告中复用该记录的结论和建议。",
        ],
    )


def _validate_bounded_interval_direction_contract(
    tree: ast.AST,
    code: str,
) -> RetryResult | None:
    """Keep nearest-boundary margins distinct from outward deviations.

    For a value inside ``[lower, upper]``, ``min(value - lower, upper - value)``
    decreases as the value approaches either limit.  Treating that direct
    margin as a higher-is-riskier deviation reverses the calculation semantics.
    This check follows the calculation AST and only uses the accompanying
    direction statement to disambiguate intent; it does not inspect rendered
    report numbers.
    """

    def static_string(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                str(item.value)
                for item in node.values
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
        return ""

    def dataframe_field_target(node: ast.AST) -> tuple[str, str] | None:
        if not isinstance(node, ast.Subscript):
            return None
        if (
            isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return node.value.id, str(node.slice.value)
        if not (
            isinstance(node.value, ast.Attribute)
            and node.value.attr in {"at", "loc"}
            and isinstance(node.value.value, ast.Name)
            and isinstance(node.slice, ast.Tuple)
            and len(node.slice.elts) >= 2
            and isinstance(node.slice.elts[1], ast.Constant)
            and isinstance(node.slice.elts[1].value, str)
        ):
            return None
        return node.value.value.id, str(node.slice.elts[1].value)

    def direct_nearest_boundary_ratio(node: ast.AST) -> bool:
        current = node
        while (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id in {"float", "round"}
            and current.args
        ):
            current = current.args[0]
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
            numerator = current.left
        elif isinstance(current, ast.Call):
            numerator = current
        else:
            return False
        return any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "min"
            and len(item.args) >= 2
            and all(isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Sub) for arg in item.args[:2])
            for item in ast.walk(numerator)
        )

    proximity_fields: dict[tuple[str, str], int] = {}
    for assignment in ast.walk(tree):
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
            continue
        value = getattr(assignment, "value", None)
        if value is None or not direct_nearest_boundary_ratio(value):
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            signature = dataframe_field_target(target)
            if signature is not None:
                proximity_fields[signature] = int(getattr(assignment, "lineno", 0) or 0)
    if not proximity_fields:
        return None

    reversed_direction_lines: list[int] = []
    direction_pattern = re.compile(
        r"(?:越高|越大|higher|larger).{0,32}(?:接近|贴近|nearer|closer).{0,48}(?:限值|边界|规格|控制限|limit|boundary)",
        re.IGNORECASE,
    )
    for node in ast.walk(tree):
        literal = static_string(node)
        if literal and direction_pattern.search(literal):
            reversed_direction_lines.append(int(getattr(node, "lineno", 0) or 0))
    if not reversed_direction_lines:
        return None

    descending_sorts: list[dict[str, Any]] = []
    proximity_names = {field for _, field in proximity_fields}
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "sort_values"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
            and str(call.args[0].value) in proximity_names
        ):
            continue
        ascending = True
        for keyword in call.keywords:
            if (
                keyword.arg == "ascending"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, bool)
            ):
                ascending = keyword.value.value
        if not ascending:
            descending_sorts.append({
                "line_no": int(getattr(call, "lineno", 0) or 0),
                "field": str(call.args[0].value),
            })

    first_line = min(
        [line for line in proximity_fields.values() if line > 0]
        + [line for line in reversed_direction_lines if line > 0]
        + [1]
    )
    return request_retry(
        retry_type="contract_error",
        message=(
            "区间内到最近边界的距离被解释成了数值越高越接近限值，"
            "计算方向与业务判断相反。"
        ),
        diagnostics={
            "violated_contract": "bounded_interval_metric_direction_mismatch",
            "proximity_fields": [
                {"frame": frame, "field": field, "line_no": line}
                for (frame, field), line in sorted(proximity_fields.items())
            ],
            "reversed_direction_lines": sorted(set(reversed_direction_lines)),
            "descending_sorts": descending_sorts,
        },
        repair_instructions=[
            "保留现有查询、分析范围和产物结构，只统一该区间指标的定义、排序方向和报告判断。",
            "若指标是区间内到最近上下限的距离，则值越小越贴近边界，应按升序识别高风险项。",
            "若需要值越大风险越高的统一指标，请显式转换成同方向风险分数；不要把区间内安全余量与区间外超限幅度混在同一字段中排序。",
            "同步修正使用该指标的图表说明、极值选择和报告结论。",
        ],
        code_error_context={"line_no": first_line},
    )


def _validate_report_bounded_time_window_contract(
    tree: ast.AST,
    code: str,
) -> RetryResult | None:
    """Reject one-sided date filters presented as a bounded report period.

    The check follows AST assignments from the numeric slot next to an explicit
    bounded-period phrase. It does not scan rendered Markdown numbers or attempt
    open-ended natural-language validation. SQL-backed metrics and calculations
    whose date predicate cannot be proven statically remain untouched.
    """

    bounded_period = re.compile(
        r"(?:未来|接下来|随后)\s*(?:半(?:年|个月)|[一二三四五六七八九十百\d]+(?:个)?(?:天|周|月|季度|年))"
        r"|\bnext\s+(?:half\s+year|\d+\s+(?:day|week|month|quarter|year)s?)\b",
        re.IGNORECASE,
    )
    date_markers = re.compile(
        r"date|time|day|week|month|quarter|year|日期|时间|日|周|月|季度|年|到期|签约|创建",
        re.IGNORECASE,
    )
    assignments: dict[str, list[ast.AST]] = {}

    def assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
        raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [
            item.id
            for target in raw_targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        ]

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = getattr(node, "value", None)
        if value is None:
            continue
        for target_name in assignment_target_names(node):
            assignments.setdefault(target_name, []).append(value)

    def dependency_nodes(expression: ast.AST) -> list[ast.AST]:
        collected: list[ast.AST] = []
        pending = [expression]
        visited_names: set[str] = set()
        while pending:
            current = pending.pop()
            collected.append(current)
            for loaded in (
                item.id for item in ast.walk(current)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
            ):
                if loaded in visited_names:
                    continue
                visited_names.add(loaded)
                pending.extend(assignments.get(loaded) or [])
        return collected

    def is_numeric_metric_expression(formatted: ast.FormattedValue) -> bool:
        if formatted.format_spec is not None:
            return True
        direct_nodes = [formatted.value]
        if isinstance(formatted.value, ast.Name):
            direct_nodes.extend(assignments.get(formatted.value.id) or [])
        numeric_calls = {"abs", "count", "float", "int", "len", "max", "mean", "min", "nunique", "round", "size", "sum"}
        for direct in direct_nodes:
            for item in ast.walk(direct):
                if isinstance(item, ast.BinOp) and isinstance(
                    item.op,
                    (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow),
                ):
                    return True
                if isinstance(item, ast.Call):
                    function_name = (
                        item.func.id if isinstance(item.func, ast.Name)
                        else item.func.attr if isinstance(item.func, ast.Attribute)
                        else ""
                    )
                    if function_name in numeric_calls:
                        return True
        return False

    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        values = list(node.values)
        for index, formatted in enumerate(values):
            if not isinstance(formatted, ast.FormattedValue):
                continue
            prefix = (
                str(values[index - 1].value)
                if index > 0
                and isinstance(values[index - 1], ast.Constant)
                and isinstance(values[index - 1].value, str)
                else ""
            )
            claim = bounded_period.search(prefix[-120:])
            if claim is None or not is_numeric_metric_expression(formatted):
                continue
            related_nodes = dependency_nodes(formatted.value)
            date_comparison_count = 0
            has_between = False
            for related in related_nodes:
                for item in ast.walk(related):
                    if isinstance(item, ast.Compare):
                        comparison_source = ast.get_source_segment(code, item) or ast.unparse(item)
                        if date_markers.search(comparison_source):
                            date_comparison_count += max(1, len(item.ops))
                    elif (
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "between"
                    ):
                        call_source = ast.get_source_segment(code, item) or ast.unparse(item)
                        if date_markers.search(call_source):
                            has_between = True
            # No statically visible date predicate means there is no safe basis
            # for a rejection (the boundary may live in SQL or a helper API).
            if has_between or date_comparison_count == 0 or date_comparison_count >= 2:
                continue
            findings.append({
                "line_no": int(getattr(formatted, "lineno", 0) or 0),
                "period_claim": claim.group(0),
                "metric_expression": (
                    ast.get_source_segment(code, formatted.value)
                    or ast.unparse(formatted.value)
                ),
                "date_comparison_count": date_comparison_count,
            })
    if not findings:
        return None
    return request_retry(
        retry_type="contract_error",
        message=(
            "报告把只有单侧日期边界的计算表述为固定长度的未来时间窗口，"
            "该计算会把窗口之外的数据一并计入。"
        ),
        diagnostics={
            "violated_contract": "bounded_report_period_requires_two_date_boundaries",
            "findings": findings[:12],
        },
        repair_instructions=[
            "保留现有查询、产物和报告结构，只修正该时间窗口指标的日期范围计算。",
            "固定长度的未来窗口必须同时包含开始边界和结束边界；可使用 between，或显式组合 >= 开始时间与 < 结束时间。",
            "窗口结束时间必须由报告声明的天、周、月、季度或年长度计算，不要把所有未来记录表述成有限窗口。",
            "图表、报告结论及建议必须复用修正后的同一窗口数据。",
        ],
    )


def _validate_report_paired_metric_population_contract(
    tree: ast.AST,
    code: str,
) -> RetryResult | None:
    """Keep an explicitly paired percentage and count on one population.

    This is intentionally narrower than natural-language report validation. It
    only handles a structural report form such as ``80% (120 items)`` where the
    count is presented as the concrete population behind the percentage. The
    check follows Python assignments and derived DataFrame columns; it never
    scans rendered Markdown for arbitrary numbers.
    """
    assignments: dict[str, list[tuple[int, ast.AST]]] = {}
    column_assignments: dict[tuple[str, str], list[tuple[int, ast.AST]]] = {}

    def subscript_field(node: ast.AST) -> tuple[str, str] | None:
        if not (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return None
        return node.value.id, str(node.slice.value)

    for node in sorted(
        (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda item: int(getattr(item, "lineno", 0) or 0),
    ):
        value = getattr(node, "value", None)
        if value is None:
            continue
        raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        line_no = int(getattr(node, "lineno", 0) or 0)
        for target in raw_targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(target.id, []).append((line_no, value))
            field = subscript_field(target)
            if field is not None:
                column_assignments.setdefault(field, []).append((line_no, value))

    def latest_definition(
        definitions: list[tuple[int, ast.AST]],
        before_line: int,
    ) -> tuple[int, ast.AST] | None:
        eligible = [item for item in definitions if item[0] <= before_line]
        return max(eligible, key=lambda item: item[0], default=None)

    def dependency_nodes(expression: ast.AST) -> list[ast.AST]:
        pending = [(expression, int(getattr(expression, "lineno", 0) or 10**9))]
        collected: list[ast.AST] = []
        visited_names: set[tuple[str, int]] = set()
        visited_fields: set[tuple[str, str, int]] = set()
        while pending:
            current, before_line = pending.pop()
            collected.append(current)
            for item in ast.walk(current):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                    definition = latest_definition(assignments.get(item.id) or [], before_line)
                    marker = (item.id, definition[0] if definition else 0)
                    if definition is not None and marker not in visited_names:
                        visited_names.add(marker)
                        pending.append((definition[1], definition[0]))
                field = subscript_field(item)
                if field is not None:
                    definition = latest_definition(column_assignments.get(field) or [], before_line)
                    marker = (*field, definition[0] if definition else 0)
                    if definition is not None and marker not in visited_fields:
                        visited_fields.add(marker)
                        pending.append((definition[1], definition[0]))
        return collected

    def root_fields(expression: ast.AST) -> set[str]:
        fields: set[str] = set()
        for related in dependency_nodes(expression):
            for item in ast.walk(related):
                field = subscript_field(item)
                if field is None:
                    continue
                before_line = int(getattr(item, "lineno", 0) or 10**9)
                if latest_definition(column_assignments.get(field) or [], before_line) is not None:
                    continue
                fields.add(field[1])
        return fields

    def has_population_selector(expression: ast.AST) -> bool:
        selector_calls = {"groupby", "value_counts", "nunique", "count"}
        return any(
            isinstance(item, ast.Compare)
            or (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in selector_calls
            )
            for related in dependency_nodes(expression)
            for item in ast.walk(related)
        )

    def format_spec_text(node: ast.FormattedValue) -> str:
        if not isinstance(node.format_spec, ast.JoinedStr):
            return ""
        return "".join(
            str(item.value)
            for item in node.format_spec.values
            if isinstance(item, ast.Constant)
        )

    count_unit = re.compile(
        r"^\s*(?:万元|亿元|公斤|千克|个|份|台|条|笔|项|家|人|次|件|套|辆|座|吨|元)"
        r"(?=\s|[）)\]】,，。；;]|$)"
    )
    findings: list[dict[str, Any]] = []
    for joined in ast.walk(tree):
        if not isinstance(joined, ast.JoinedStr):
            continue
        values = list(joined.values)
        formatted_positions = [
            index for index, item in enumerate(values)
            if isinstance(item, ast.FormattedValue)
        ]
        for pair_index in range(len(formatted_positions) - 1):
            percent_pos = formatted_positions[pair_index]
            count_pos = formatted_positions[pair_index + 1]
            percent_expr = values[percent_pos]
            count_expr = values[count_pos]
            if not isinstance(percent_expr, ast.FormattedValue) or not isinstance(count_expr, ast.FormattedValue):
                continue
            between = "".join(
                str(item.value)
                for item in values[percent_pos + 1:count_pos]
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            suffix = (
                str(values[count_pos + 1].value)
                if count_pos + 1 < len(values)
                and isinstance(values[count_pos + 1], ast.Constant)
                and isinstance(values[count_pos + 1].value, str)
                else ""
            )
            is_percentage = (
                "%" in format_spec_text(percent_expr)
                or bool(re.match(r"^\s*%", between))
            )
            is_parenthesized_count = (
                (between.count("（") + between.count("("))
                > (between.count("）") + between.count(")"))
                and bool(count_unit.match(suffix))
            )
            if not is_percentage or not is_parenthesized_count:
                continue
            percent_fields = root_fields(percent_expr.value)
            count_fields = root_fields(count_expr.value)
            if (
                not percent_fields
                or not count_fields
                or not percent_fields.isdisjoint(count_fields)
                or not has_population_selector(percent_expr.value)
                or not has_population_selector(count_expr.value)
            ):
                continue
            findings.append({
                "line_no": int(getattr(joined, "lineno", 0) or 0),
                "percentage_expression": (
                    ast.get_source_segment(code, percent_expr.value)
                    or ast.unparse(percent_expr.value)
                ),
                "count_expression": (
                    ast.get_source_segment(code, count_expr.value)
                    or ast.unparse(count_expr.value)
                ),
                "percentage_population_fields": sorted(percent_fields),
                "count_population_fields": sorted(count_fields),
            })
    if not findings:
        return None
    return request_retry(
        retry_type="contract_error",
        message=(
            "报告把占比与括号内数量表达为同一业务群体，但两者实际来自不同的筛选或分组总体。"
        ),
        diagnostics={
            "violated_contract": "paired_report_metrics_require_shared_population",
            "findings": findings[:12],
        },
        repair_instructions=[
            "保留现有洞察结构，只修正成对展示的占比和数量口径。",
            "先为该业务群体构造一份明确的布尔筛选、分组行或数量变量；括号内数量直接复用该变量，占比分子也必须复用同一变量或同一筛选结果。",
            "如果两个指标本来描述不同业务群体，不要放在占比（数量）的成对表达中，应分别陈述并明确各自口径。",
        ],
    )


def _validate_analysis_relationship_contract(
    context: CustomContext,
    code: str,
) -> RetryResult | None:
    """Keep the finalized Preparation relationship graph authoritative at runtime."""
    snapshot = getattr(context, "active_datasource_snapshot", {}) or {}
    semantic_context = snapshot.get("analysis_semantic_context") or {}
    relationships = semantic_context.get("relationships") or []
    governed_relationships = [
        item
        for item in relationships
        if isinstance(item, dict)
        and item.get("relationship_type") in {
            "same_entity",
            "parent_child",
            "event_of_entity",
            "dimension_enrichment",
            "bridge",
        }
    ]
    unresolved_pairs = [
        item
        for item in governed_relationships
        if item.get("joinability") != "joinable"
    ]
    fanout_pairs = [
        item
        for item in governed_relationships
        if item.get("joinability") == "joinable"
        and any(
            isinstance(policy, dict)
            and policy.get("status") == "unsafe_repeated_by_join"
            for policy in (item.get("aggregation_safety") or {}).values()
        )
    ]
    key_domain_pairs = [
        item
        for item in governed_relationships
        if item.get("joinability") == "joinable"
        and isinstance(
            (item.get("aggregation_safety") or {}).get("key_domain_coverage"),
            dict,
        )
    ]
    normalization_pairs = [
        item
        for item in governed_relationships
        if item.get("joinability") == "joinable"
        and item.get("key_normalization") == "integral_numeric_text"
    ]
    if (
        not unresolved_pairs
        and not fanout_pairs
        and not key_domain_pairs
        and not normalization_pairs
    ):
        return None
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None
    identifiers = {
        int(item.get("datasource_id") or 0): str(
            item.get("datasource_identifier") or ""
        ).strip()
        for item in snapshot.get("selected_datasource_snapshot") or []
        if isinstance(item, dict) and int(item.get("datasource_id") or 0) > 0
    }

    normalized_code = re.sub(r'["`\[\]\s]+', "", code or "").lower()
    referenced_source_ids = {
        datasource_id
        for datasource_id, identifier in identifiers.items()
        if identifier and re.sub(r'["`\[\]\s]+', "", identifier).lower()
        in normalized_code
    }
    called_functions = _called_function_names(tree)
    declared_text_columns = {
        str(item.value)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == "load_data_with_sql")
            or (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "load_data_with_sql"
            )
        )
        for keyword in call.keywords
        if keyword.arg == "text_columns" and isinstance(keyword.value, (ast.List, ast.Tuple))
        for item in keyword.value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    for relationship in normalization_pairs:
        relationship_source_ids = {
            int(relationship.get("left_datasource_id") or 0),
            int(relationship.get("right_datasource_id") or 0),
        }
        if not relationship_source_ids.issubset(referenced_source_ids):
            continue
        relationship_fields = {
            str(field)
            for field in [
                *(relationship.get("left_fields") or []),
                *(relationship.get("right_fields") or []),
            ]
            if str(field)
        }
        if (
            "normalize_relationship_key" in called_functions
            and relationship_fields.issubset(declared_text_columns)
        ):
            continue
        return request_retry(
            retry_type="query_constraint_violation",
            message=(
                "当前代码使用了需要数值标识规范化的已确认关系，但没有通过 "
                "Runtime 标准能力保真加载并统一关联键。"
            ),
            diagnostics={
                "violated_contract": "relationship_key_normalization_required",
                "relationship_id": relationship.get("relationship_id"),
                "required_text_columns": sorted(relationship_fields),
                "declared_text_columns": sorted(declared_text_columns),
                "required_helper": "normalize_relationship_key",
            },
            repair_instructions=[
                "两侧关系键所在查询都通过 load_data_with_sql(..., text_columns=[...]) 保留原始标识精度。",
                "分别调用 normalize_relationship_key(dataframe[key_field]) 生成规范键，再按规范键 JOIN 或 merge。",
                "不要使用 str()、astype(str)、float 或自行 int(float(...)) 转换大整数标识。",
            ],
        )

    def _relationship_retry(relationship: dict[str, Any]) -> RetryResult:
        left_id = int(relationship.get("left_datasource_id") or 0)
        right_id = int(relationship.get("right_datasource_id") or 0)
        return request_retry(
            retry_type="query_constraint_violation",
            message=(
                "当前代码使用了 Business Context 未确认可直接关联的跨数据源路径；"
                "unknown/alignable/not_joinable 关系不能直接作为 JOIN 或 DataFrame merge。"
            ),
            diagnostics={
                "violated_contract": "unresolved_business_lineage_join",
                "relationship_id": relationship.get("relationship_id"),
                "left_datasource_id": left_id,
                "right_datasource_id": right_id,
                "joinability": relationship.get("joinability"),
            },
            repair_instructions=[
                "业务血缘已经由 Business Context 定稿；不要在正式运行阶段再次探测或改写关系结论。",
                "若关系为 alignable，可先分别聚合到共同且不会放大量值的业务粒度后再比较；否则分别分析并如实说明事实边界。",
                "不要因字段同名、描述相近或能够执行就把关系视为已确认。",
            ],
        )

    def _normalize_identifier(value: str) -> str:
        return re.sub(r'["`\[\]\s]+', "", value or "").lower()

    def _source_ids_in_text(value: str) -> set[int]:
        normalized = _normalize_identifier(value)
        return {
            datasource_id
            for datasource_id, identifier in identifiers.items()
            if identifier and _normalize_identifier(identifier) in normalized
        }

    sql_sources_by_name: dict[str, set[int]] = {}
    dataframe_sources_by_name: dict[str, set[int]] = {}
    sql_text_by_name: dict[str, str] = {}
    dataframe_sql_text_by_name: dict[str, str] = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for assignment in assignments:
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            continue
        target_name = assignment.targets[0].id
        source_text = ast.get_source_segment(code, assignment.value) or ""
        source_ids = _source_ids_in_text(source_text)
        if source_ids:
            sql_sources_by_name[target_name] = source_ids
        if isinstance(assignment.value, ast.Constant) and isinstance(
            assignment.value.value,
            str,
        ):
            sql_text_by_name[target_name] = assignment.value.value
        if not isinstance(assignment.value, ast.Call):
            continue
        call = assignment.value
        function_name = (
            call.func.id
            if isinstance(call.func, ast.Name)
            else call.func.attr
            if isinstance(call.func, ast.Attribute)
            else ""
        )
        if function_name != "load_data_with_sql":
            continue
        sql_node = call.args[0] if call.args else next(
            (item.value for item in call.keywords if item.arg == "sql"),
            None,
        )
        if isinstance(sql_node, ast.Name):
            dataframe_sources_by_name[target_name] = set(
                sql_sources_by_name.get(sql_node.id, set())
            )
            dataframe_sql_text_by_name[target_name] = sql_text_by_name.get(
                sql_node.id,
                "",
            )
        elif sql_node is not None:
            inline_sql = (
                sql_node.value
                if isinstance(sql_node, ast.Constant)
                and isinstance(sql_node.value, str)
                else ast.get_source_segment(code, sql_node) or ""
            )
            dataframe_sources_by_name[target_name] = _source_ids_in_text(inline_sql)
            dataframe_sql_text_by_name[target_name] = inline_sql

    for _ in range(3):
        for assignment in assignments:
            if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
                continue
            target_name = assignment.targets[0].id
            if target_name in dataframe_sources_by_name:
                continue
            inherited = {
                datasource_id
                for node in ast.walk(assignment.value)
                if isinstance(node, ast.Name)
                for datasource_id in dataframe_sources_by_name.get(node.id, set())
            }
            if inherited:
                dataframe_sources_by_name[target_name] = inherited

    def _sources_for_expression(node: ast.AST | None) -> set[int]:
        if node is None:
            return set()
        return {
            datasource_id
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
            for datasource_id in dataframe_sources_by_name.get(child.id, set())
        }

    coverage_name_pattern = re.compile(
        r"(?:^|_)(?:cover(?:age|ed)?|match(?:ed)?|link(?:ed)?|associate(?:d)?|"
        r"bound|binding|with|without)(?:_|$)",
        re.IGNORECASE,
    )
    ratio_name_pattern = re.compile(
        r"(?:^|_)(?:rate|ratio|share|pct|percentage)(?:_|$)",
        re.IGNORECASE,
    )

    def _cross_source_coverage_retry(
        relationship: dict[str, Any],
        node: ast.AST,
        names: list[str],
    ) -> RetryResult:
        return request_retry(
            retry_type="query_constraint_violation",
            message=(
                "当前代码用两个数据源各自独立统计的键数量计算覆盖或匹配关系，"
                "辅助数据源中的孤立键可能使覆盖数量超过主体总体。"
            ),
            diagnostics={
                "violated_contract": "cross_source_key_domain_coverage",
                "relationship_id": relationship.get("relationship_id"),
                "left_datasource_id": relationship.get("left_datasource_id"),
                "right_datasource_id": relationship.get("right_datasource_id"),
                "coverage_variables": names,
                "line_no": getattr(node, "lineno", None),
                "key_domain_coverage": (
                    relationship.get("aggregation_safety") or {}
                ).get("key_domain_coverage") or {},
            },
            repair_instructions=[
                "覆盖分子必须从主体键与辅助键的交集、EXISTS/SEMI JOIN，或按已确认关系键合并后的去重主体集合计算。",
                "覆盖分母必须使用同一筛选口径下的主体键去重数；辅助表独有键不能进入分子。",
                "如果只想披露辅助表自身涉及的键数量，应将其作为独立事实说明，不得命名或解释为主体覆盖数量、覆盖率或缺失量。",
            ],
        )

    def _expression_aligns_key_domains(node: ast.AST) -> bool:
        return any(
            (
                isinstance(item, ast.BinOp)
                and isinstance(item.op, ast.BitAnd)
            )
            or (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"intersection", "isin", "merge", "join"}
            )
            or (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "merge"
            )
            for item in ast.walk(node)
        )

    for relationship in key_domain_pairs:
        pair = {
            int(relationship.get("left_datasource_id") or 0),
            int(relationship.get("right_datasource_id") or 0),
        }
        if len(pair) != 2:
            continue

        relationship_fields = {
            int(relationship.get("left_datasource_id") or 0): [
                str(item) for item in relationship.get("left_fields") or []
                if str(item).strip()
            ],
            int(relationship.get("right_datasource_id") or 0): [
                str(item) for item in relationship.get("right_fields") or []
                if str(item).strip()
            ],
        }

        def _distinct_relationship_key_aliases(
            dataframe_name: str,
            datasource_id: int,
        ) -> set[str]:
            sql_text = dataframe_sql_text_by_name.get(dataframe_name, "")
            aliases: set[str] = set()
            for field_name in relationship_fields.get(datasource_id, []):
                field_pattern = re.escape(field_name)
                aliases.update(
                    match.group("alias")
                    for match in re.finditer(
                        rf"count\s*\(\s*distinct\s+(?:[\w\"`]+\.)?[\"`]?{field_pattern}[\"`]?\s*\)\s+(?:as\s+)?[\"`]?(?P<alias>[A-Za-z_]\w*)[\"`]?",
                        sql_text,
                        re.IGNORECASE,
                    )
                )
            return aliases

        key_count_variables: dict[str, set[int]] = {}
        for assignment in assignments:
            if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
                continue
            target_name = assignment.targets[0].id
            for item in ast.walk(assignment.value):
                if not isinstance(item, ast.Subscript) or not isinstance(item.value, ast.Name):
                    continue
                dataframe_name = item.value.id
                column_name = (
                    item.slice.value
                    if isinstance(item.slice, ast.Constant)
                    and isinstance(item.slice.value, str)
                    else ""
                )
                if not column_name:
                    continue
                for datasource_id in dataframe_sources_by_name.get(dataframe_name, set()):
                    if datasource_id not in pair:
                        continue
                    if column_name in _distinct_relationship_key_aliases(
                        dataframe_name,
                        datasource_id,
                    ):
                        key_count_variables.setdefault(target_name, set()).add(
                            datasource_id
                        )

        for assignment in assignments:
            if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
                continue
            target_name = assignment.targets[0].id
            if not ratio_name_pattern.search(target_name):
                continue
            for item in ast.walk(assignment.value):
                if not isinstance(item, ast.BinOp) or not isinstance(item.op, ast.Div):
                    continue
                numerator_sources = {
                    datasource_id
                    for child in ast.walk(item.left)
                    if isinstance(child, ast.Name)
                    for datasource_id in key_count_variables.get(child.id, set())
                }
                denominator_sources = {
                    datasource_id
                    for child in ast.walk(item.right)
                    if isinstance(child, ast.Name)
                    for datasource_id in key_count_variables.get(child.id, set())
                }
                if (
                    numerator_sources
                    and denominator_sources
                    and numerator_sources.isdisjoint(denominator_sources)
                    and pair.issubset(numerator_sources | denominator_sources)
                ):
                    return _cross_source_coverage_retry(
                        relationship,
                        item,
                        [
                            target_name,
                            *[
                                child.id
                                for child in ast.walk(item)
                                if isinstance(child, ast.Name)
                            ],
                        ],
                    )

        coverage_nodes: list[tuple[ast.AST, list[str]]] = []
        for assignment in assignments:
            if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
                continue
            target_name = assignment.targets[0].id
            referenced_names = [
                item.id for item in ast.walk(assignment.value) if isinstance(item, ast.Name)
            ]
            if coverage_name_pattern.search(target_name):
                left_datasource_id = int(
                    relationship.get("left_datasource_id") or 0
                )
                right_datasource_id = int(
                    relationship.get("right_datasource_id") or 0
                )
                counted_key_sources = key_count_variables.get(target_name, set())
                reads_auxiliary_distinct_count = any(
                    right_datasource_id in dataframe_sources_by_name.get(name, set())
                    and re.search(
                        r"count\s*\(\s*distinct\b",
                        dataframe_sql_text_by_name.get(name, ""),
                        re.IGNORECASE,
                    )
                    for name in referenced_names
                )
                reads_aligned_domain = any(
                    pair.issubset(dataframe_sources_by_name.get(name, set()))
                    for name in referenced_names
                )
                if (
                    (
                        right_datasource_id in counted_key_sources
                        or reads_auxiliary_distinct_count
                    )
                    and left_datasource_id not in counted_key_sources
                    and not reads_aligned_domain
                ):
                    return _cross_source_coverage_retry(
                        relationship,
                        assignment.value,
                        [target_name, *referenced_names],
                    )
                coverage_nodes.append((assignment.value, referenced_names))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            referenced_names = [
                item.id for item in ast.walk(node) if isinstance(item, ast.Name)
            ]
            if any(coverage_name_pattern.search(name) for name in referenced_names):
                coverage_nodes.append((node, referenced_names))

        for node, referenced_names in coverage_nodes:
            source_ids = _sources_for_expression(node)
            if not pair.issubset(source_ids):
                continue
            if _expression_aligns_key_domains(node):
                continue
            has_aligned_domain = any(
                pair.issubset(dataframe_sources_by_name.get(name, set()))
                for name in referenced_names
            )
            if has_aligned_domain:
                continue
            return _cross_source_coverage_retry(
                relationship,
                node,
                list(dict.fromkeys(referenced_names)),
            )

    query_constraints = _extract_query_constraints_literal(tree)
    aggregation = (
        query_constraints.get("aggregation")
        if isinstance(query_constraints, dict)
        else None
    )
    aggregation_grain = str(
        aggregation.get("grain") if isinstance(aggregation, dict) else ""
    ).strip().lower()

    if fanout_pairs and aggregation_grain in {"overall", "snapshot"}:
        def _is_raw_fanout_query(sql_text: str) -> bool:
            try:
                from sqlglot import exp, parse_one

                statement = parse_one(str(sql_text or ""))
                # SQL aggregation has already established a result grain. Unsafe
                # aggregate expressions on a repeated side are checked above.
                return statement.find(exp.Group) is None and not any(
                    isinstance(item, exp.AggFunc)
                    for item in statement.walk()
                )
            except Exception:
                return True

        fanout_frame_names: set[str] = {
            frame_name
            for frame_name, source_ids in dataframe_sources_by_name.items()
            if frame_name in dataframe_sql_text_by_name
            and _is_raw_fanout_query(dataframe_sql_text_by_name[frame_name])
            and any({
                int(relationship.get("left_datasource_id") or 0),
                int(relationship.get("right_datasource_id") or 0),
            }.issubset(source_ids) for relationship in fanout_pairs)
        }

        def _establishes_explicit_grouped_grain(node: ast.AST) -> bool:
            aggregation_calls = {
                item.func.attr
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"agg", "aggregate"}
            }
            if not aggregation_calls:
                return False
            return any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"groupby", "pivot_table"}
                for item in ast.walk(node)
            )

        # Propagate raw-row expansion risk, not the much weaker fact that an
        # expression mentions both domains. Deduplication and explicit key-domain
        # intersections restore a safe grain.
        changed = True
        while changed:
            changed = False
            for assignment in assignments:
                if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
                    continue
                target_name = assignment.targets[0].id
                if target_name in fanout_frame_names:
                    continue
                source_names = {
                    item.id
                    for item in ast.walk(assignment.value)
                    if isinstance(item, ast.Name)
                }
                if not (source_names & fanout_frame_names):
                    continue
                if _expression_aligns_key_domains(assignment.value):
                    continue
                if _establishes_explicit_grouped_grain(assignment.value):
                    continue
                if (
                    isinstance(assignment.value, ast.Call)
                    and isinstance(assignment.value.func, ast.Attribute)
                    and assignment.value.func.attr == "drop_duplicates"
                ):
                    continue
                fanout_frame_names.add(target_name)
                changed = True

        deduplicated_names = {
            assignment.targets[0].id
            for assignment in assignments
            if len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name)
            and isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Attribute)
            and assignment.value.func.attr == "drop_duplicates"
            and bool(_sources_for_expression(assignment.value.func.value) & {
                source_id
                for relationship in fanout_pairs
                for source_id in (
                    int(relationship.get("left_datasource_id") or 0),
                    int(relationship.get("right_datasource_id") or 0),
                )
            })
        }

        def _root_frame_name(node: ast.AST | None) -> str:
            current = node
            while current is not None:
                if isinstance(current, ast.Name):
                    return current.id
                if isinstance(current, ast.Subscript):
                    current = current.value
                    continue
                if isinstance(current, ast.Attribute):
                    current = current.value
                    continue
                if isinstance(current, ast.Call):
                    current = (
                        current.func.value
                        if isinstance(current.func, ast.Attribute)
                        else None
                    )
                    continue
                return ""
            return ""

        unsafe_reductions: list[dict[str, Any]] = []
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id == "len" and call.args:
                frame_name = _root_frame_name(call.args[0])
                operation = "len"
            elif (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"value_counts", "size", "count"}
            ):
                frame_name = _root_frame_name(call.func.value)
                operation = call.func.attr
            else:
                continue
            if frame_name not in fanout_frame_names or frame_name in deduplicated_names:
                continue
            unsafe_reductions.append({
                "dataframe": frame_name,
                "operation": operation,
                "line_no": getattr(call, "lineno", None),
            })

        if unsafe_reductions:
            relationship = fanout_pairs[0]
            return request_retry(
                retry_type="query_constraint_violation",
                message=(
                    "当前总体分析在一对多关联展开结果上直接按行计数或分组计数，"
                    "会把辅助明细行误当成业务主体。"
                ),
                diagnostics={
                    "violated_contract": "relationship_fanout_population_grain",
                    "relationship_id": relationship.get("relationship_id"),
                    "cardinality": relationship.get("cardinality"),
                    "unsafe_reductions": unsafe_reductions[:12],
                    "left_fields": relationship.get("left_fields") or [],
                    "right_fields": relationship.get("right_fields") or [],
                },
                repair_instructions=[
                    "先从关联结果按业务主体键 drop_duplicates(...) 形成 population_df，再用 population_df 计算主体总数、状态构成、占比和主体级图表。",
                    "辅助明细的行数、授权数或合同编号数保留为独立明细口径，不能替代主体数量；如只需主体分析，可取消不必要的一对多 JOIN。",
                    "报告、图表和表格必须消费同一份主体粒度结果；主体分组数量之和不得超过主体总数。",
                ],
            )

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"merge", "join"}:
            continue
        if isinstance(call.func.value, ast.Name) and call.func.value.id == "pd":
            left_node = call.args[0] if call.args else None
            right_node = call.args[1] if len(call.args) > 1 else None
        else:
            left_node = call.func.value
            right_node = call.args[0] if call.args else next(
                (item.value for item in call.keywords if item.arg == "right"),
                None,
            )
        merged_source_ids = (
            _sources_for_expression(left_node) | _sources_for_expression(right_node)
        )
        for relationship in unresolved_pairs:
            pair = {
                int(relationship.get("left_datasource_id") or 0),
                int(relationship.get("right_datasource_id") or 0),
            }
            if pair.issubset(merged_source_ids):
                return _relationship_retry(relationship)

    sql_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"\bselect\b", node.value, re.IGNORECASE)
        and re.search(r"\bjoin\b", node.value, re.IGNORECASE)
    ]
    if not sql_literals:
        return None

    try:
        from sqlglot import exp, parse
    except Exception:
        return None

    def _sql_for_contract_parsing(sql: str) -> str:
        """Replace driver placeholders without changing the query structure."""
        return re.sub(
            r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s",
            "'__query_parameter__'",
            sql,
        )

    for sql in sql_literals:
        try:
            statements = parse(_sql_for_contract_parsing(sql))
        except Exception:
            continue
        table_names: set[str] = set()
        for statement in statements:
            for table in statement.find_all(exp.Table):
                qualified_name = ".".join(
                    part
                    for part in (table.catalog, table.db, table.name)
                    if part
                )
                table_names.add(_normalize_identifier(qualified_name))
        for relationship in unresolved_pairs:
            left_id = int(relationship.get("left_datasource_id") or 0)
            right_id = int(relationship.get("right_datasource_id") or 0)
            left_identifier = _normalize_identifier(identifiers.get(left_id, ""))
            right_identifier = _normalize_identifier(identifiers.get(right_id, ""))
            if not left_identifier or not right_identifier:
                continue
            if left_identifier not in table_names or right_identifier not in table_names:
                continue
            return _relationship_retry(relationship)

        if not fanout_pairs:
            continue
        try:
            from sqlglot.optimizer.scope import traverse_scope
        except Exception:
            continue
        for statement in statements:
            for scope in traverse_scope(statement):
                alias_source_ids: dict[str, int] = {}
                for alias, source in scope.sources.items():
                    if not isinstance(source, exp.Table):
                        continue
                    qualified_name = ".".join(
                        part
                        for part in (source.catalog, source.db, source.name)
                        if part
                    )
                    normalized_name = _normalize_identifier(qualified_name)
                    for datasource_id, identifier in identifiers.items():
                        if normalized_name == _normalize_identifier(identifier):
                            alias_source_ids[str(alias)] = datasource_id
                            break

                scope_source_ids = set(alias_source_ids.values())
                for relationship in fanout_pairs:
                    left_id = int(relationship.get("left_datasource_id") or 0)
                    right_id = int(relationship.get("right_datasource_id") or 0)
                    if not {left_id, right_id}.issubset(scope_source_ids):
                        continue
                    safety = relationship.get("aggregation_safety") or {}
                    unsafe_source_ids: set[int] = set()
                    if (safety.get("left_measure_after_join") or {}).get("status") == "unsafe_repeated_by_join":
                        unsafe_source_ids.add(left_id)
                    if (safety.get("right_measure_after_join") or {}).get("status") == "unsafe_repeated_by_join":
                        unsafe_source_ids.add(right_id)
                    for aggregate in scope.expression.find_all(exp.AggFunc):
                        if isinstance(aggregate, (exp.Min, exp.Max)):
                            continue
                        if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
                            continue
                        unsafe_columns = [
                            column
                            for column in aggregate.find_all(exp.Column)
                            if alias_source_ids.get(str(column.table)) in unsafe_source_ids
                        ]
                        if not unsafe_columns:
                            continue
                        return request_retry(
                            retry_type="query_constraint_violation",
                            message=(
                                "当前查询在一对多或多对一连接后直接聚合了会被重复展开的一侧度量，"
                                "会造成指标放大或加权失真。"
                            ),
                            diagnostics={
                                "violated_contract": "relationship_fanout_aggregation",
                                "relationship_id": relationship.get("relationship_id"),
                                "cardinality": relationship.get("cardinality"),
                                "unsafe_datasource_ids": sorted(unsafe_source_ids),
                                "aggregate_expression": aggregate.sql(),
                            },
                            repair_instructions=[
                                "整体度量在其原始事实粒度独立聚合，不要经一对多明细连接后再求和、计数或平均。",
                                "若要按另一侧维度拆分，改用该维度侧自身可加度量；只有存在明确分摊规则或已确认函数依赖时，才可分配父侧度量。",
                                "图表、表格和报告应消费修正后的同一计算结果，并如实说明不同事实口径不能直接相加。",
                            ],
                        )
    return None


def _node_calls_function(node: ast.AST, function_name: str) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == function_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == function_name:
            return True
    return False


def _handler_is_broad_exception(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {'Exception', 'BaseException'}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {'Exception', 'BaseException'}
            for item in handler.type.elts
        )
    return False


def _handler_references_exception(handler: ast.ExceptHandler) -> bool:
    exception_name = str(handler.name or '').strip()
    if not exception_name:
        return False
    for child in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(child, ast.Name) and child.id == exception_name:
            return True
    return False


def _handler_assigns_none(handler: ast.ExceptHandler) -> bool:
    for child in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(child, ast.Assign) and isinstance(child.value, ast.Constant) and child.value.value is None:
            return True
        if isinstance(child, ast.AnnAssign) and isinstance(child.value, ast.Constant) and child.value.value is None:
            return True
    return False


def _handler_has_pass_only(handler: ast.ExceptHandler) -> bool:
    meaningful_nodes = [
        node
        for node in handler.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    ]
    return not meaningful_nodes or all(isinstance(node, ast.Pass) for node in meaningful_nodes)


def _handler_records_query_failure(handler: ast.ExceptHandler) -> bool:
    for child in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in {'append', 'extend', 'update'}:
            return True
        if isinstance(child, ast.Assign):
            if isinstance(child.targets[0], (ast.Subscript, ast.Attribute)):
                return True
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, (ast.Subscript, ast.Attribute)):
            return True
    return False


def _validate_sql_load_error_handling_contract(tree: ast.AST) -> RetryResult | None:
    execution_intent = _extract_static_string_assignment(tree, 'execution_intent')
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        try_body = ast.Module(body=node.body, type_ignores=[])
        if not _node_calls_function(try_body, 'load_data_with_sql'):
            continue
        for handler in node.handlers:
            if not _handler_is_broad_exception(handler):
                continue
            handler_module = ast.Module(body=handler.body, type_ignores=[])
            if _node_calls_function(handler_module, 'request_retry') or any(isinstance(child, ast.Raise) for child in ast.walk(handler_module)):
                continue
            if execution_intent == 'probe':
                return request_retry(
                    retry_type="contract_error",
                    message=(
                        "多查询 Probe 使用了宽泛 try/except 包裹 load_data_with_sql(...)，"
                        "无法稳定保留每条候选路径的成功、空集和查询失败状态。"
                    ),
                    diagnostics={
                        'violated_contract': 'probe_sql_outcomes_require_structured_helper',
                        'try_line_no': getattr(node, 'lineno', None),
                        'except_line_no': getattr(handler, 'lineno', None),
                    },
                    repair_instructions=[
                        "保持当前 Probe 的完整数据源和 SQL 范围，不要缩成单个数据源逐轮试错。",
                        "删除包裹 load_data_with_sql(...) 的 try/except；对循环中的每条 SQL 调用 probe_data_with_sql(...)。",
                        "按返回的 status 区分 success、成功空集和 query_error，把每条路径的 datasource_id、datasource_name、row_count、error_type、error_message 汇总进同一次 request_retry(..., diagnostics=...)。",
                        "query_error 是已观测的数据源不可用状态，不是无数据；后续正式分析只计算 Probe 成功且非空的路径，并在报告中披露空集和不可用路径。",
                    ],
                )
            records_query_failure = _handler_records_query_failure(handler)
            if (
                _handler_has_pass_only(handler)
                or _handler_assigns_none(handler)
                or not _handler_references_exception(handler)
                or not records_query_failure
            ):
                return request_retry(
                    retry_type="contract_error",
                    message=(
                        "当前代码在执行前因 SQL 加载异常处理契约被拒绝；"
                        "尚未执行任何查询，因此不能据此判定某个数据源无数据或不可用。"
                    ),
                    diagnostics={
                        'violated_contract': 'sql_load_error_handling',
                        'try_line_no': getattr(node, 'lineno', None),
                        'except_line_no': getattr(handler, 'lineno', None),
                        'failure_phase': 'static_contract_validation',
                        'queries_executed': False,
                        'scope_change_authorized': False,
                        'recommended_next_execution_intent': execution_intent or 'analysis',
                    },
                    repair_instructions=[
                        "这是技术实现错误，不是数据源可用性结论；必须保留当前的全部数据源、查询路径、计算范围和分析产物。",
                        "只修正包裹 load_data_with_sql(...) 的宽泛 try/except：删除默认空结果降级，让原异常直接向上反馈。",
                        "不要删除或跳过任何数据源，也不要把完整分析改成部分报告；只有后续真实执行返回的结构化诊断才能改变可用路径范围。",
                    ],
                )
            return request_retry(
                retry_type="contract_error",
                message="SQL 加载异常是阻断性错误。load_data_with_sql(...) 执行失败时必须立即反馈明确的 SQL 错误并停止当前分析流程，要求修正 SQL 后重试。",
                diagnostics={
                    'violated_contract': 'sql_load_exception_must_stop',
                    'try_line_no': getattr(node, 'lineno', None),
                    'except_line_no': getattr(handler, 'lineno', None),
                },
                repair_instructions=[
                    "禁止将 SQL 加载异常降级为无数据、空数据、部分失败、可跳过数据源或普通查询失败后继续分析。",
                    "SQL 语法错误、字段错误、表标识错误、连接错误等执行异常必须直接 raise，或在 except 中立即 result = request_retry(retry_type=\"data_source_unavailable\" 或 \"runtime_error\", ...) 并结束当前尝试。",
                    "多数据源独立分析中，只有 load_data_with_sql(...) 成功返回空 DataFrame 的数据源，才可以按无数据剔除；SQL 执行失败的数据源必须先修正 SQL，不能出现在部分成功报告里。",
                ],
            )
    return None


def _string_constants(tree: ast.AST) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if value:
                values.add(value)
    return values


def _joined_string_text(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append('{}')
    return ''.join(parts)


def _is_quote_identifier_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == 'quote_identifier'
    if isinstance(func, ast.Attribute):
        return func.attr == 'quote_identifier'
    return False


def _quote_identifier_assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_quote_identifier_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_quote_identifier_call(node.value):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _is_safe_timescale_from_expr(node: ast.AST, quoted_names: set[str]) -> bool:
    if _is_quote_identifier_call(node):
        return True
    if isinstance(node, ast.Name) and node.id in quoted_names:
        return True
    return False


def _find_unsafe_dynamic_timescale_from(tree: ast.AST) -> dict[str, Any] | None:
    quoted_names = _quote_identifier_assigned_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        prior_text = ''
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                prior_text += value.value
                continue
            if not isinstance(value, ast.FormattedValue):
                continue
            if re.search(r'\bfrom\s*$', prior_text, re.IGNORECASE) and not _is_safe_timescale_from_expr(value.value, quoted_names):
                return {
                    'line_no': getattr(value, 'lineno', getattr(node, 'lineno', None)),
                    'formatted_expression': ast.unparse(value.value) if hasattr(ast, 'unparse') else '',
                    'safe_quoted_identifier_names': sorted(quoted_names),
                }
            prior_text += '{}'
    return None


def _looks_like_datasource_identifier_constant(value: str) -> bool:
    text = str(value or '').strip()
    if len(text) < 3:
        return False
    if any(char.isspace() for char in text):
        return False
    if any(char in text for char in ('"', "'", '(', ')', '<', '>', '=')):
        return False
    if ':' in text:
        return False
    return bool(re.search(r'[A-Za-z_$]', text))


def _validate_dynamic_timescale_identifier_contract(tree: ast.AST, code: str) -> RetryResult | None:
    if "ts_query('timescale'" not in code and 'ts_query("timescale"' not in code:
        return None

    quoted_constants = {
        value.strip('"')
        for value in _string_constants(tree)
        if len(value) >= 2 and value.startswith('"') and value.endswith('"')
    }
    unquoted_identifiers = sorted(
        value
        for value in _string_constants(tree)
        if _identifier_requires_quotes(value)
        and _looks_like_datasource_identifier_constant(value)
        and value not in quoted_constants
        and not (value.startswith('"') and value.endswith('"'))
        and not value.startswith('SELECT ')
        and not value.startswith('select ')
    )
    if not unquoted_identifiers:
        return None

    unsafe_dynamic_from = _find_unsafe_dynamic_timescale_from(tree)
    if unsafe_dynamic_from is None:
        return None

    return request_retry(
        retry_type="contract_error",
        message="timescale 内部 SQL 动态拼接 datasource_identifier 时，必须先用双引号引用需要保留大小写或特殊字符的表标识。",
        diagnostics={
            'violated_contract': 'timescale_dynamic_identifier_quoting',
            'identifiers_requiring_quotes': unquoted_identifiers[:20],
            **unsafe_dynamic_from,
        },
        repair_instructions=[
            '定义 quote_identifier(identifier) 辅助函数：返回 "\"" + identifier.replace("\"", "\"\"") + "\""; 内部 SQL 使用 FROM {quote_identifier(identifier)}。',
            '不要在 timescale 内部 SQL 中直接写 FROM {identifier}，否则 __Do_xxx、_0_I_xxx、$xxx 等标识会被错误解析。',
            'validate_query_constraints(...) 的 sql_context.fragments 必须覆盖本轮实际查询的所有时序数据源，不能只校验第一个样例 SQL。',
        ],
    )


def _extract_static_string_constant(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    return ''


def _extract_dict_string_key(key: ast.AST | None) -> str:
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value.strip()
    return ''


def _collect_supported_chart_spec_kinds(tree: ast.AST) -> set[str]:
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        items = {
            _extract_dict_string_key(key): value
            for key, value in zip(node.keys, node.values)
            if _extract_dict_string_key(key)
        }
        series_value = items.get('series')
        if not isinstance(series_value, ast.List):
            continue
        for series_node in series_value.elts:
            if not isinstance(series_node, ast.Dict):
                continue
            series_items = {
                _extract_dict_string_key(key): value
                for key, value in zip(series_node.keys, series_node.values)
                if _extract_dict_string_key(key)
            }
            chart_kind = _extract_static_string_constant(series_items.get('type')).lower()
            if chart_kind in HELPER_SUPPORTED_CHART_KINDS:
                kinds.add(chart_kind)
    return kinds


def _collect_supported_pyecharts_chart_apis(tree: ast.AST, calls: set[str]) -> list[str]:
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and str(node.module or '').startswith('pyecharts.charts'):
            imported_names.update(alias.asname or alias.name for alias in node.names)

    blocked_names = {
        name
        for name in set(calls) | imported_names
        if name in SUPPORTED_PYECHARTS_CHART_CLASSES
        and SUPPORTED_PYECHARTS_CHART_CLASSES[name] in HELPER_SUPPORTED_CHART_KINDS
    }
    return sorted(blocked_names)


def _validate_helper_chart_usage_contract(tree: ast.AST, calls: set[str]) -> RetryResult | None:
    if HELPER_CHART_FUNCTIONS & calls:
        return None

    blocked_chart_apis = _collect_supported_pyecharts_chart_apis(tree, calls)
    supported_chart_spec_kinds = _collect_supported_chart_spec_kinds(tree)
    uses_low_level_dump = bool({'dump_options', 'dump_options_with_quotes'} & calls)
    has_chart_spec_assignment = _has_static_assignment(tree, 'chart_spec')

    if not blocked_chart_apis and not supported_chart_spec_kinds:
        return None

    diagnostics = {
        'blocked_chart_apis': blocked_chart_apis,
        'supported_chart_spec_kinds': sorted(supported_chart_spec_kinds),
        'uses_low_level_dump': uses_low_level_dump,
        'has_chart_spec_assignment': has_chart_spec_assignment,
        'helper_functions': sorted(HELPER_CHART_FUNCTIONS),
        'helper_supported_chart_kinds': sorted(HELPER_SUPPORTED_CHART_KINDS),
    }
    return request_retry(
        retry_type="chart_contract_error",
        message=(
            "当前 analysis 代码绕过了已支持的后端图表 helper，使用了 pyecharts 或手写 chart_spec "
            "生成 helper 已覆盖的图表类型。"
        ),
        diagnostics=diagnostics,
        repair_instructions=[
            '当图表类型属于 bar、line、pie、scatter、boxplot 时，必须使用 build_chart_result(...)、build_multi_metric_chart_result(...) 或 build_chart_suite(...) 生成 charts。',
            '异常值、分布和四分位数场景请用 build_chart_result(chart_kind="boxplot", data=..., category_field=..., value_field=...)。',
            '只有 helper 无法表达的图表类型，才可以退回 pyecharts 或手写 chart_spec；不要为了已支持类型调用 dump_options_with_quotes()。',
        ],
    )


def _date_text(value: Any) -> str:
    text = str(value or '').strip()
    match = re.search(r'\d{4}-\d{1,2}-\d{1,2}', text)
    if not match:
        return ''
    try:
        return datetime.strptime(match.group(0), '%Y-%m-%d').date().isoformat()
    except ValueError:
        return ''


def _actual_time_range_matches_expected(actual_time_range: Any, expected_time_range: Any) -> bool:
    if not isinstance(actual_time_range, dict) or not isinstance(expected_time_range, dict):
        return False

    expected_start = _date_text(expected_time_range.get('start'))
    expected_end = _date_text(expected_time_range.get('end'))
    expected_display_end = _date_text(expected_time_range.get('display_end'))
    actual_start = _date_text(actual_time_range.get('start'))
    actual_end = _date_text(actual_time_range.get('end'))

    if not expected_start or not actual_start or actual_start != expected_start:
        return False

    accepted_ends = {item for item in (expected_end, expected_display_end) if item}
    if accepted_ends and actual_end not in accepted_ends:
        return False
    return True


def _build_explicit_time_range_drift_retry(
    expected_time_range: dict[str, Any],
    actual_time_range: Any,
) -> RetryResult:
    return request_retry(
        retry_type="query_constraint_violation",
        message=(
            "当前 analysis 代码把用户问题中的明确时间范围改成了其他时间范围；"
            "在本轮已出现无数据或探测反馈后，不能用数据覆盖范围替换用户原始时间范围。"
        ),
        diagnostics={
            'expected_time_range': expected_time_range,
            'actual_time_range': actual_time_range if isinstance(actual_time_range, dict) else {},
        },
        repair_instructions=[
            "重新生成完整 analysis 代码，query_constraints.time_range 必须保持 expected_time_range。",
            "不要使用探测到的数据覆盖范围、最接近一周、历史月份或代理时间段来替换用户问题中的时间范围。",
            "如果 expected_time_range 内确实无数据，请生成无数据结论和建议报告；可以调用 save_analysis_result(...) 保存自然语言说明，但不要扩大时间范围继续分析。",
        ],
    )


def _validate_explicit_time_range_guard(
    code: str,
    expected_time_range: dict[str, Any],
    time_range_guard_active: bool,
) -> RetryResult | None:
    if not time_range_guard_active or not isinstance(expected_time_range, dict) or not expected_time_range:
        return None

    try:
        tree = ast.parse(code or '')
    except SyntaxError:
        return None

    execution_intent = _extract_static_string_assignment(tree, 'execution_intent')
    if execution_intent != 'analysis':
        return None

    query_constraints = _extract_query_constraints_literal(tree)
    if not isinstance(query_constraints, dict):
        return None

    actual_time_range = query_constraints.get('time_range')
    if _actual_time_range_matches_expected(actual_time_range, expected_time_range):
        return None

    return _build_explicit_time_range_drift_retry(expected_time_range, actual_time_range)


def _validate_probe_observation_contract(
    tree: ast.AST,
    calls: set[str],
) -> RetryResult | None:
    """Ensure a Probe returns observed facts rather than a plan to inspect data."""

    data_access_calls = {
        "load_data_with_sql",
        "probe_data_with_sql",
        "load_local_data_with_sql",
        "load_local_file",
        "load_local_file_low_memory",
        "load_minio_file",
        "load_data_with_api",
    }
    used_access_calls = sorted(calls & data_access_calls)
    if not used_access_calls:
        return request_retry(
            retry_type="contract_error",
            message="Probe 代码没有执行任何数据读取，无法产生可供正式分析使用的观测事实。",
            diagnostics={
                "violated_contract": "probe_requires_observed_data",
                "called_functions": sorted(calls),
                "supported_data_access_calls": sorted(data_access_calls),
            },
            repair_instructions=[
                "保留当前需要核实的字段和范围，使用对应数据源的只读 loader 执行一次轻量查询或采样读取。",
                "基于实际返回的 DataFrame 调用 probe_distinct_values(...)、probe_text_candidates(...)、describe_time_coverage(...)，或计算行数、非空数、min/max 等有界摘要。",
                "把这些实际观测结果放进 request_retry(..., diagnostics=...)；不要只返回 SQL 字符串、检查计划或预期值。",
            ],
        )

    def call_name(node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    def target_names(target: ast.AST) -> set[str]:
        names = {
            item.id
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        }
        if isinstance(target, (ast.Subscript, ast.Attribute)):
            names.update(
                item.id
                for item in ast.walk(target)
                if isinstance(item, ast.Name)
            )
        return names

    def loaded_names(node: ast.AST | None) -> set[str]:
        if node is None:
            return set()
        return {
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
        }

    tainted_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = getattr(node, "value", None)
            if value is None or not any(
                isinstance(item, ast.Call) and call_name(item) in data_access_calls
                for item in ast.walk(value)
            ):
                continue
            raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in raw_targets:
                tainted_names.update(target_names(target))
        elif isinstance(node, (ast.For, ast.AsyncFor)) and any(
            isinstance(item, ast.Call) and call_name(item) in data_access_calls
            for item in ast.walk(node.iter)
        ):
            tainted_names.update(target_names(node.target))

    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    ]
    changed = True
    while changed:
        changed = False
        for loop in (
            item for item in ast.walk(tree)
            if isinstance(item, (ast.For, ast.AsyncFor))
        ):
            if not loaded_names(loop.iter).intersection(tainted_names):
                continue
            new_targets = target_names(loop.target) - tainted_names
            if new_targets:
                tainted_names.update(new_targets)
                changed = True
        for node in assignments:
            value = getattr(node, "value", None)
            if value is None:
                continue
            if not loaded_names(value).intersection(tainted_names):
                continue
            raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            derived_targets = set().union(*(target_names(target) for target in raw_targets))
            new_targets = derived_targets - tainted_names
            if new_targets:
                tainted_names.update(new_targets)
                changed = True
        for call in (
            item for item in ast.walk(tree)
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr in {"append", "extend", "insert", "update", "setdefault", "add"}
        ):
            container_names = loaded_names(call.func.value)
            value_names = set().union(
                *(loaded_names(item) for item in [*call.args, *(keyword.value for keyword in call.keywords)])
            )
            if not value_names.intersection(tainted_names):
                continue
            new_targets = container_names - tainted_names
            if new_targets:
                tainted_names.update(new_targets)
                changed = True

    diagnostics_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or call_name(node) != "request_retry":
            continue
        diagnostics_nodes.extend(
            keyword.value for keyword in node.keywords if keyword.arg == "diagnostics"
        )
    observed_names = sorted({
        item.id
        for diagnostics in diagnostics_nodes
        for item in ast.walk(diagnostics)
        if isinstance(item, ast.Name)
        and isinstance(item.ctx, ast.Load)
        and item.id in tainted_names
    })
    if observed_names:
        return None
    return request_retry(
        retry_type="contract_error",
        message="Probe 虽然读取了数据，但 diagnostics 没有消费任何由读取结果派生的观测值。",
        diagnostics={
            "violated_contract": "probe_diagnostics_require_observed_values",
            "data_access_calls": used_access_calls,
            "observed_value_candidates": sorted(tainted_names),
        },
        repair_instructions=[
            "从已加载结果生成有界摘要，例如字段枚举及频次、总行数、非空数、时间 min/max 或候选值匹配结果。",
            "将摘要变量直接写入 request_retry(..., diagnostics=...)，让下一版正式分析获得真实观测，而不是操作说明。",
            "Probe 不生成正式图表、表格或报告；完成观测后仍以 retry_type=\"probe_feedback\" 返回。",
        ],
    )


def _static_node_code_context(code: str, node: ast.AST) -> dict[str, Any]:
    source_lines = str(code or "").splitlines()
    line_no = int(getattr(node, "lineno", 0) or 0)
    end_line_no = int(getattr(node, "end_lineno", line_no) or line_no)
    if not (1 <= line_no <= len(source_lines)):
        return {}
    start = max(1, line_no - GENERATED_CODE_CONTEXT_RADIUS)
    end = min(len(source_lines), end_line_no + GENERATED_CODE_CONTEXT_RADIUS)
    statement_ranges = _source_statement_patch_ranges(code, line_no)
    return {
        "filename": GENERATED_CODE_FILENAME,
        "line_no": line_no,
        "column_no": int(getattr(node, "col_offset", 0) or 0) + 1,
        "error_line": source_lines[line_no - 1],
        "context_lines": [
            {
                "line_no": current_line_no,
                "code": source_lines[current_line_no - 1],
                "is_error_line": line_no <= current_line_no <= end_line_no,
            }
            for current_line_no in range(start, end + 1)
        ],
        "related_context_lines": [],
        "suggested_patch_ranges": statement_ranges,
        "traceback_excerpt": "",
    }


def _static_string_values(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return {
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return set()


_UNFINISHED_REPORT_SENTINELS = {
    "placeholder",
    "report placeholder",
    "todo",
    "tbd",
    "占位",
    "占位符",
    "待补充",
    "待生成",
    "待完善",
}


def _is_unfinished_report_sentinel(report_text: Any) -> bool:
    """Return whether the entire report is an explicit unfinished sentinel."""
    normalized = re.sub(
        r"[\s#*_`~:：;；,.，。!！?？\-]+",
        " ",
        str(report_text or "").strip().lower(),
    ).strip()
    return normalized in _UNFINISHED_REPORT_SENTINELS


def _find_static_unfinished_report_call(tree: ast.AST) -> tuple[ast.Call, str] | None:
    """Find a direct sentinel literal passed to save_analysis_result(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        function_name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if function_name != "save_analysis_result":
            continue
        report_node: ast.AST | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "analysis_report":
                report_node = keyword.value
                break
        if (
            isinstance(report_node, ast.Constant)
            and isinstance(report_node.value, str)
            and _is_unfinished_report_sentinel(report_node.value)
        ):
            return node, report_node.value
    return None


def _validate_grouped_visible_dimension_contract(
    tree: ast.AST,
    code: str,
) -> RetryResult | None:
    """Reject unproven first/last values exposed as grouped dimensions."""
    visible_dimension_fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            mapping = {
                key.value: value.value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            }
            if "key" in mapping:
                visible_dimension_fields.add(mapping["key"])
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg == "category_field"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    visible_dimension_fields.add(keyword.value.value)
    if not visible_dimension_fields:
        return None

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"agg", "aggregate"}:
            continue
        group_call = call.func.value
        if (
            not isinstance(group_call, ast.Call)
            or not isinstance(group_call.func, ast.Attribute)
            or group_call.func.attr != "groupby"
        ):
            continue
        group_fields = _static_string_values(group_call.args[0] if group_call.args else None)
        risky_dimensions: list[dict[str, str]] = []
        for keyword in call.keywords:
            if keyword.arg not in visible_dimension_fields:
                continue
            if not isinstance(keyword.value, (ast.Tuple, ast.List)) or len(keyword.value.elts) < 2:
                continue
            source_fields = _static_string_values(keyword.value.elts[0])
            reducers = _static_string_values(keyword.value.elts[1])
            if not source_fields or not (reducers & {"first", "last"}):
                continue
            source_field = next(iter(source_fields))
            if source_field in group_fields:
                continue
            risky_dimensions.append({
                "output_field": str(keyword.arg),
                "source_field": source_field,
                "reducer": next(iter(reducers & {"first", "last"})),
            })
        if call.args and isinstance(call.args[0], ast.Dict):
            for key, value in zip(call.args[0].keys, call.args[0].values):
                source_fields = _static_string_values(key)
                reducers = _static_string_values(value)
                if not source_fields or not (reducers & {"first", "last"}):
                    continue
                source_field = next(iter(source_fields))
                if source_field not in visible_dimension_fields or source_field in group_fields:
                    continue
                risky_dimensions.append({
                    "output_field": source_field,
                    "source_field": source_field,
                    "reducer": next(iter(reducers & {"first", "last"})),
                })
        if not risky_dimensions:
            continue
        code_error_context = _static_node_code_context(code, call)
        return request_retry(
            retry_type="query_constraint_violation",
            message=_format_error_message_with_code_location(
                "分组结果把未证明唯一的首值或末值作为用户可见维度，可能给汇总指标附上错误标签。",
                code_error_context,
            ),
            diagnostics={
                "violated_contract": "grouped_visible_dimension_functional_dependency",
                "group_fields": sorted(group_fields),
                "risky_dimensions": risky_dimensions,
                "code_error_context": code_error_context,
            },
            repair_instructions=[
                "保持查询、指标和洞察目标不变；若该字段属于结果粒度，将它加入 groupby 分组键。",
                "若分析确实要跨该字段汇总，从用户可见结果中移除该标签，不要用 first/last 冒充汇总维度。",
                "不要仅替换首值文本；同步让图表、表格和报告消费修正后的同一粒度结果。",
            ],
            code_error_context=code_error_context,
        )
    return None


def _merge_static_contract_retries(
    retries: list[RetryResult | None],
) -> RetryResult | None:
    """Return one complete static review instead of one violation per retry."""
    findings = [retry for retry in retries if retry is not None]
    if not findings:
        return None
    if len(findings) == 1:
        return findings[0]

    violations: list[dict[str, Any]] = []
    repair_instructions: list[str] = []
    seen_instructions: set[str] = set()
    related_contexts: list[dict[str, Any]] = []
    suggested_patch_ranges: list[dict[str, Any]] = []
    for retry in findings:
        diagnostics = dict(retry.diagnostics or {})
        violations.append({
            'retry_type': retry.retry_type,
            'message': retry.message,
            'diagnostics': diagnostics,
        })
        for instruction in retry.repair_instructions or []:
            text = str(instruction or '').strip()
            if text and text not in seen_instructions:
                seen_instructions.add(text)
                repair_instructions.append(text)
        context = dict(retry.code_error_context or {})
        if not context and isinstance(diagnostics.get('code_error_context'), dict):
            context = dict(diagnostics['code_error_context'])
        if context:
            related_contexts.append(context)
            for item in context.get('suggested_patch_ranges') or []:
                if isinstance(item, dict) and item not in suggested_patch_ranges:
                    suggested_patch_ranges.append(item)

    code_error_context: dict[str, Any] = {
        'related_contexts': related_contexts,
        'suggested_patch_ranges': suggested_patch_ranges,
    }
    return request_retry(
        retry_type='contract_error',
        message=(
            f'The generated analysis code has {len(findings)} independent static contract violations. '
            'Fix every item in diagnostics.violations in the same repair; do not submit one-at-a-time rewrites.'
        ),
        diagnostics={
            'violated_contract': 'multiple_static_contract_violations',
            'violation_count': len(findings),
            'violations': violations,
        },
        repair_instructions=[
            'Preserve the complete query, calculation, chart, table, report and datasource scope; repair only the listed static defects.',
            *repair_instructions,
        ],
        code_error_context=code_error_context,
    )


def _validate_generated_code_contract(code: str) -> RetryResult | None:
    """Validate the static tool contract of generated Python code before execution."""
    try:
        tree = ast.parse(code or '')
    except SyntaxError as exc:
        code_error_context = _build_generated_code_error_context(code, exc, '')
        return request_retry(
            retry_type="contract_error",
            message=_format_error_message_with_code_location(
                f"生成的 Python 代码存在语法错误，无法执行：{exc.msg}",
                code_error_context,
            ),
            diagnostics={
                "violated_contract": "invalid_python_syntax",
                "line_no": exc.lineno,
                "offset": exc.offset,
                "code_error_context": code_error_context,
            },
            repair_instructions=[
                "根据 code_error_context 中的错误行和相邻代码，只修正该处 Python 引号、括号、逗号或缩进语法；不要原样重复同一错误。",
                "保留已经完成的查询、计算、图表、表格、报告语言和洞察内容，不要因为局部语法错误重写整个分析。",
                "修复后仍须提交一份顶部声明 execution_intent 的完整可执行代码。",
            ],
        )

    execution_intent = _extract_static_string_assignment(tree, 'execution_intent')
    has_query_constraints = (
        _has_static_assignment(tree, 'query_constraints')
        or _has_validate_query_constraints_argument(tree)
    )
    calls = _called_function_names(tree)

    if execution_intent not in {'analysis', 'probe'}:
        return request_retry(
            retry_type="contract_error",
            message='生成代码必须在顶部声明 execution_intent = "analysis" 或 execution_intent = "probe"。',
            diagnostics={
                'execution_intent': execution_intent,
                'has_query_constraints': has_query_constraints,
                'called_functions': sorted(calls),
            },
            repair_instructions=[
                '不要只补 execution_intent；请根据当前代码目的重写成完整 probe 或完整 analysis 代码。',
                '如果当前代码只是查看字段、打印样例、探测候选值、确认时间覆盖或数据分布，应改成完整 probe：execution_intent = "probe"，最后 result = request_retry(...)。',
                '如果当前代码要正式回答用户问题，应改成完整 analysis：execution_intent、query_constraints、SQL/filter_expressions、validate_query_constraints(...)、数据加载、空数据检查、图表/表格和 result = save_analysis_result(...) 必须一次性齐全。',
            ],
        )

    sql_load_error_retry = _validate_sql_load_error_handling_contract(tree)
    if sql_load_error_retry is not None:
        return sql_load_error_retry

    dynamic_timescale_identifier_retry = _validate_dynamic_timescale_identifier_contract(tree, code or '')
    if dynamic_timescale_identifier_retry is not None:
        return dynamic_timescale_identifier_retry

    if execution_intent == 'analysis':
        missing_items: list[str] = []
        if not has_query_constraints:
            missing_items.append('query_constraints')
        if 'validate_query_constraints' not in calls:
            missing_items.append('validate_query_constraints(...)')
        if 'save_analysis_result' not in calls:
            missing_items.append('save_analysis_result(...)')
        if missing_items:
            if missing_items == ['save_analysis_result(...)']:
                return request_retry(
                    retry_type="contract_error",
                    message=(
                        "当前 analysis 代码只完成了数据加载或中间分析，没有形成可保存的完整分析产物。"
                    ),
                    diagnostics={
                        'violated_contract': 'incomplete_analysis_program',
                        'execution_intent': execution_intent,
                        'has_query_constraints': has_query_constraints,
                        'called_functions': sorted(calls),
                        'missing_items': missing_items,
                    },
                    repair_instructions=[
                        '每次 execute_python 都是独立执行单元，后续调用不会继承当前代码中的 DataFrame、变量或中间结果。',
                        '如果已有查询口径足以回答问题，请在同一份完整代码中继续完成计算、空数据判断、图表、结构化表格、洞察报告，并以 result = save_analysis_result(...) 收口。',
                        '如果必须先观察真实枚举、格式、时间覆盖或数据分布，请改写为 execution_intent = "probe"，将有界观察写入 diagnostics，并以 result = request_retry(retry_type="probe_feedback", ...) 收口。',
                        '不要再次提交只打印“数据加载完成”“准备继续分析”或只生成中间 DataFrame 的 analysis 代码。',
                    ],
                )
            return request_retry(
                retry_type="missing_query_constraints",
                message=f'正式 analysis 代码缺少必要契约：{", ".join(missing_items)}。',
                diagnostics={
                    'execution_intent': execution_intent,
                    'has_query_constraints': has_query_constraints,
                    'called_functions': sorted(calls),
                    'missing_items': missing_items,
                },
                repair_instructions=[
                    '不要只在半成品代码上逐项补字段；请重写为完整 analysis 模板后再执行。',
                    'analysis 代码必须先声明 query_constraints，再生成 SQL 或 pandas/DataFrame 过滤表达式。',
                    'analysis 代码必须调用 validate_query_constraints(...)；只有返回 None 时才能继续加载数据、聚合、构造图表和调用 save_analysis_result(...)。',
                    'analysis 代码最终必须构造 analysis_report、charts 或 tables，并将 save_analysis_result(...) 的返回值赋给 result。',
                    '如果 validate_query_constraints(...) 返回 RetryResult，必须将其赋给 result 并结束当前尝试。',
                ],
            )

        unfinished_report = _find_static_unfinished_report_call(tree)
        if unfinished_report is not None:
            report_call, report_value = unfinished_report
            code_error_context = _static_node_code_context(code or "", report_call)
            return request_retry(
                retry_type="missing_report",
                message=_format_error_message_with_code_location(
                    "正式分析代码仍使用未完成的报告占位值，不能作为成功结果保存。",
                    code_error_context,
                ),
                diagnostics={
                    "violated_contract": "unfinished_report_sentinel",
                    "report_value": report_value,
                    "code_error_context": code_error_context,
                },
                repair_instructions=[
                    "保留已经正确完成的查询、计算、图表和表格，不要缩减分析范围。",
                    "使用已有计算结果构造面向用户的 Markdown 洞察报告，再传给 save_analysis_result(...)。",
                    "对于数据不足或无法确认的分析问题，在报告中如实说明限制和建议，不要用占位文本代替最终报告。",
                ],
                code_error_context=code_error_context,
            )

        validation_order_retry = _validate_analysis_sql_validation_order(tree)
        if validation_order_retry is not None:
            return validation_order_retry

        mapping_default_retry = _validate_query_mapping_default_contract(tree)
        if mapping_default_retry is not None:
            return mapping_default_retry

        static_semantic_retry = _merge_static_contract_retries([
            _validate_report_extrema_selection_contract(tree, code or ''),
            _validate_bounded_interval_direction_contract(tree, code or ''),
            _validate_report_bounded_time_window_contract(tree, code or ''),
            _validate_report_paired_metric_population_contract(tree, code or ''),
            _validate_grouped_visible_dimension_contract(tree, code or ''),
        ])
        if static_semantic_retry is not None:
            return static_semantic_retry

        helper_chart_retry = _validate_helper_chart_usage_contract(tree, calls)
        if helper_chart_retry is not None:
            return helper_chart_retry

    if execution_intent == 'probe':
        violations: list[str] = []
        if 'request_retry' not in calls:
            violations.append('probe 代码必须调用 request_retry(...)')
        if 'save_analysis_result' in calls:
            violations.append('probe 代码不要调用 save_analysis_result(...)')
        if violations:
            return request_retry(
                retry_type="contract_error",
                message=f'探测 probe 代码不符合契约：{"；".join(violations)}。',
                diagnostics={
                    'execution_intent': execution_intent,
                    'has_query_constraints': has_query_constraints,
                    'called_functions': sorted(calls),
                    'violations': violations,
                },
                repair_instructions=[
                    'probe 代码只做轻量探测，并调用 request_retry(retry_type="probe_feedback", ...) 返回结构化反馈。',
                    'probe 代码不要调用 save_analysis_result(...)；query_constraints 不是必需项，但已有的只读查询校验不会改变正式分析口径。',
                ],
            )

        probe_observation_retry = _validate_probe_observation_contract(tree, calls)
        if probe_observation_retry is not None:
            return probe_observation_retry

    return None


def validate_query_constraints(
    query_constraints: Any,
    query_key: str = '',
    sql: str = '',
    query_params: Any = None,
    sql_params: Any = None,
    filter_expressions: Optional[list[str]] = None,
    aggregation_hints: Optional[list[str]] = None,
    sql_context: Optional[dict[str, Any]] = None,
) -> RetryResult | None:
    """Validate analysis query constraints and register the physical query."""
    if query_params is not None and sql_params is not None and query_params != sql_params:
        return request_retry(
            retry_type="invalid_query_constraints",
            message="validate_query_constraints(...) 同时收到不一致的 query_params 与 sql_params。",
            diagnostics={"violated_constraint": "conflicting_query_parameter_aliases"},
            repair_instructions=[
                "query_params 与 sql_params 是同一参数语义的别名；只传一个，且与 load_data_with_sql(..., params=...) 使用同一组值。",
            ],
        )
    effective_query_params = query_params if query_params is not None else sql_params
    schema_errors = _validate_query_constraints_schema(query_constraints)
    if schema_errors:
        return request_retry(
            retry_type="invalid_query_constraints",
            message='query_constraints 结构不合法，请先修正查询约束声明。',
            diagnostics={'schema_errors': schema_errors},
            repair_instructions=[
                'query_constraints 必须是可求值的 dict，并包含与当前问题相关的 time_range、target_filters、aggregation。',
                'target_filters 中每个对象至少要包含 semantic_role、value、match_mode；match_mode 只能是 exact 或 fuzzy。',
            ],
        )

    expected_time_range = CURRENT_EXPECTED_TIME_RANGE.get()
    if (
        CURRENT_TIME_RANGE_GUARD_ACTIVE.get()
        and isinstance(expected_time_range, dict)
        and expected_time_range
        and not _actual_time_range_matches_expected(query_constraints.get('time_range'), expected_time_range)
    ):
        return _build_explicit_time_range_drift_retry(
            expected_time_range=expected_time_range,
            actual_time_range=query_constraints.get('time_range'),
        )

    aggregation = query_constraints.get('aggregation')
    aggregation_grain = str(
        aggregation.get('grain') if isinstance(aggregation, dict) else ''
    ).strip().lower()
    fragments = _sql_context_fragments(sql_context)
    is_time_series_query = bool(re.search(r'\bts_query\s*\(', str(sql or ''), re.I)) or any(
        str(item.get('kind') or '').strip().lower() == 'time_series_sql'
        for item in fragments
    )
    runtime_overrides = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.get()
    has_runtime_time_override = bool(
        isinstance(runtime_overrides, dict)
        and isinstance(runtime_overrides.get('time_range'), dict)
        and runtime_overrides.get('time_range')
    )
    if (
        CURRENT_EXECUTION_INTENT.get() == 'analysis'
        and not (isinstance(expected_time_range, dict) and expected_time_range)
        and not has_runtime_time_override
        and aggregation_grain in {'overall', 'detail', 'snapshot'}
        and isinstance(query_constraints.get('time_range'), dict)
        and query_constraints.get('time_range')
        and not is_time_series_query
    ):
        return request_retry(
            retry_type='query_constraint_violation',
            message='用户未指定时间范围，但当前总体或明细基线被一个推断时间窗截断。',
            diagnostics={
                'violated_constraint': 'inferred_overall_time_window',
                'time_range': query_constraints.get('time_range'),
                'aggregation_grain': aggregation_grain,
            },
            repair_instructions=[
                '开放式概览的总体规模、状态构成和基础明细查询不要设置任意时间范围，query_constraints.time_range 应为 None。',
                '如趋势观察确实需要时间窗，请保留不受该时间窗截断的总体基线，并为趋势使用独立、明确标注的观察窗口；不要用趋势窗口替代总体范围。',
                'Probe 返回的数据覆盖范围只用于诊断字段格式和查询口径，不能直接成为正式总体分析的时间范围。',
            ],
        )

    malformed_ts_query = _find_malformed_ts_query_literal(sql)
    if malformed_ts_query:
        return request_retry(
            retry_type="query_constraint_violation",
            message="ts_query(...) 外层 SQL 中的内部 SQL 单引号未正确转义。",
            diagnostics={
                'violated_constraint': 'ts_query_inner_sql_escape',
                **malformed_ts_query,
            },
            repair_instructions=[
                '先构造 inner_sql，再使用 escaped_inner_sql = inner_sql.replace("\'", "\'\'")。',
                '外层 SQL 写成 SELECT * FROM ts_query(source, escaped_inner_sql)，不要把包含单引号的 inner_sql 原样拼进去。',
                'SQL 加载异常是阻断性错误；load_data_with_sql(...) 执行失败时必须立即反馈 SQL 错误并停止当前分析流程，不能降级为无数据或可跳过数据源。',
            ],
        )

    portable_bucket_issue = _check_portable_time_bucket_expression(
        query_constraints,
        sql,
    )
    if portable_bucket_issue is not None:
        return request_retry(
            retry_type=str(portable_bucket_issue.get('error_type') or 'query_constraint_violation'),
            message=str(portable_bucket_issue.get('error_message') or '当前时间分桶表达式不具备可移植语义。'),
            diagnostics=portable_bucket_issue.get('diagnostics') or {},
            repair_instructions=[
                '优先分别返回数值 year/month/day 字段，再在 pandas 中构造展示标签。',
                '如必须在 SQL 中拼接，使用当前关系查询引擎明确支持的 CONCAT(...)，不要使用 `||`。',
            ],
        )

    fragment_issues, context_filter_code, context_aggregation_code = _validate_sql_context_fragments(
        query_constraints=query_constraints,
        sql=sql,
        sql_context=sql_context,
    )
    if fragment_issues:
        issue = fragment_issues[0]
        return request_retry(
            retry_type=str(issue.get('error_type') or 'query_constraint_violation'),
            message=str(issue.get('error_message') or '当前 SQL 片段没有满足 query_constraints 声明的查询约束。'),
            diagnostics=issue.get('diagnostics') if isinstance(issue.get('diagnostics'), dict) else {},
            repair_instructions=[
                '保持 query_constraints 不变，按数据源片段分别修正关系型 SQL 与时序 inner_sql。',
                '时序 SQL 应把 inner_sql 放入 sql_context.fragments，并带上 ts_query_source、datasource_identifier、time_field、quality_field 和 value_fields。',
                '混合关系型与时序查询时，外层 SQL 应保留明确 JOIN 和 ON 条件。',
            ],
        )

    parameter_code = json.dumps(effective_query_params, ensure_ascii=False, default=str) \
        if effective_query_params is not None else ""
    filter_fragments = [
        str(sql or '').strip(),
        parameter_code,
        context_filter_code,
        *[str(item or '').strip() for item in (filter_expressions or [])],
    ]
    filter_code = '\n'.join(fragment for fragment in filter_fragments if fragment)
    aggregation_fragments = [
        filter_code,
        context_aggregation_code,
        *[str(item or '').strip() for item in (aggregation_hints or [])],
    ]
    aggregation_code = '\n'.join(fragment for fragment in aggregation_fragments if fragment)
    if not aggregation_code:
        return request_retry(
            retry_type="query_constraint_violation",
            message='validate_query_constraints(...) 没有收到可校验的 SQL、过滤表达式或聚合提示。',
            diagnostics={'query_constraints': query_constraints},
            repair_instructions=[
                '先生成 SQL 字符串或 pandas/DataFrame 过滤表达式，再调用 validate_query_constraints(...)。',
                'analysis 场景下不要跳过查询约束校验。',
            ],
        )

    requires_filter_code = (
        isinstance(query_constraints.get('time_range'), dict)
        or bool(query_constraints.get('target_filters') or [])
    )
    if requires_filter_code and not filter_code:
        return request_retry(
            retry_type="query_constraint_violation",
            message='validate_query_constraints(...) 声明了时间范围或目标筛选约束，但没有收到可校验的 SQL 或过滤表达式。',
            diagnostics={
                'query_constraints': query_constraints,
                'missing_inputs': ['sql', 'filter_expressions'],
            },
            repair_instructions=[
                '先生成 SQL 字符串或 pandas/DataFrame 过滤表达式，再调用 validate_query_constraints(query_constraints=query_constraints, sql=sql, ...)。',
                'aggregation_hints 只能用于辅助校验聚合粒度，不能替代 SQL 或过滤表达式来证明 time_range / target_filters 已落实。',
            ],
        )

    checker_inputs = (
        (_check_target_filter_constraints, filter_code),
        (_check_time_range_constraint, filter_code),
        (_check_aggregation_constraint, aggregation_code),
    )
    for checker, code_to_check in checker_inputs:
        if not code_to_check:
            continue
        issue = checker(query_constraints, code_to_check)
        if issue is not None:
            return request_retry(
                retry_type=str(issue.get('error_type') or 'query_constraint_violation'),
                message=str(issue.get('error_message') or '当前实现没有满足 query_constraints 声明的查询约束。'),
                diagnostics=issue.get('diagnostics') if isinstance(issue.get('diagnostics'), dict) else {},
                repair_instructions=[
                    '先保持 query_constraints 不变，再重写 SQL / pandas / 文件过滤实现，使其符合时间范围、目标筛选和聚合粒度约束。',
                    '时间范围优先直接作用于原始时间字段：field >= start 且 field < end；除非完整保持日期边界，不要用 YEAR/MONTH 派生条件替代原始范围。',
                    '如果 target_filters 里的 match_mode = "exact"，正式 analysis 代码不要把同一个值实现成 LIKE、ILIKE、contains 或 str.contains(...)。',
                    '如果需要先探测候选值，请改成 execution_intent = "probe"，不要在 analysis 阶段继续放宽口径。',
                ],
            )
    normalized_query_key = _normalize_query_key(query_key)
    return _register_query_snapshot(
        query_key=normalized_query_key,
        query_constraints=query_constraints,
        sql=sql,
        query_params=effective_query_params,
        filter_expressions=filter_expressions,
        aggregation_hints=aggregation_hints,
        sql_context=sql_context,
    )


def save_empty_analysis_result(
    title: str,
    reason: str,
    detail_lines: Optional[list[str]] = None,
) -> StructuredResult:
    """
    在目标时间范围或关联结果为空时，生成一份可直接展示的空结果分析。

    这个函数保留给兼容场景使用。

    对于当前主链路，如果只是本轮 SQL、时间窗、筛选条件或关联条件没有命中数据，
    应优先调用 raise_no_data_error(...) 把“无数据”返回给上层重试，而不是直接在 Python 层收口。
    """
    import html

    safe_title = (title or '分析结果').strip() or '分析结果'
    safe_reason = (reason or '当前条件下未命中可分析数据。').strip() or '当前条件下未命中可分析数据。'
    lines = [str(item).strip() for item in (detail_lines or []) if str(item).strip()]

    report_sections = [
        f"## {safe_title}",
        "### 结果说明",
        f"- {safe_reason}",
    ]
    if lines:
        report_sections.extend([
            "### 补充信息",
            *[f"- {item}" for item in lines],
        ])

    analysis_report = '\n\n'.join(section for section in report_sections if section)
    return save_analysis_result(
        analysis_report=analysis_report,
        charts=[],
        tables=[],
    )


def _normalize_chart_items(
    charts: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
    """统一图表结果结构。"""
    normalized_items: list[dict[str, Any]] = []

    flattened: list[Any] = []
    pending = list(charts or [])
    while pending:
        chart = pending.pop(0)
        if isinstance(chart, (list, tuple)):
            pending[0:0] = list(chart)
            continue
        flattened.append(chart)

    for index, chart in enumerate(flattened, start=1):
        try:
            normalized_items.append(normalize_chart_result_item(chart, index=index))
        except ValueError as exc:
            title = str(chart.get('title') or '') if isinstance(chart, dict) else ''
            raise ChartArtifactContractError(
                chart_index=index,
                chart_title=title,
                cause=exc,
            ) from exc

    return normalized_items


def _normalize_table_items(tables: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """统一表格结果结构。"""
    normalized_items: list[dict[str, Any]] = []
    for index, table in enumerate(tables or [], start=1):
        if not isinstance(table, dict):
            raise ValueError('tables 中的每一项都必须是 dict。')
        wrapped_table = table.get('table')
        if isinstance(wrapped_table, dict):
            # build_table_result returns a {"table": ...} envelope, while generated
            # analysis code may put presentation metadata on that envelope. Treat both
            # forms as one table contract instead of dropping the outer metadata.
            table = {
                **wrapped_table,
                **{
                    key: table[key]
                    for key in ('title', 'description')
                    if str(table.get(key) or '').strip()
                },
            }

        title = str(table.get('title') or f'表格 {index}').strip()
        description = str(table.get('description') or '').strip()
        columns = table.get('columns') or []
        rows = table.get('rows') or []
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError('表格产物必须包含 columns(list) 与 rows(list)。')

        normalized_items.append({
            "title": title,
            "description": description,
            "columns": columns,
            "rows": rows,
        })
    return normalized_items


def build_table_result(
    title: str,
    description: str = '',
    columns: Optional[list[dict[str, Any]]] = None,
    rows: Optional[list[dict[str, Any]]] = None,
    data: Any = None,
    value_precision: Optional[int] = None,
) -> dict[str, Any]:
    """Build a normalized table artifact."""
    if value_precision is not None:
        if isinstance(value_precision, bool) or not isinstance(value_precision, int):
            raise ValueError('build_table_result.value_precision 必须是 0 到 10 的整数。')
        if value_precision < 0 or value_precision > 10:
            raise ValueError('build_table_result.value_precision 必须是 0 到 10 的整数。')
    if data is not None and rows is not None:
        raise ValueError('build_table_result 的 data 与 rows 不能同时传入。')
    if data is not None:
        if hasattr(data, 'to_dict') and hasattr(data, 'columns'):
            rows = data.to_dict(orient='records')
            if columns is None:
                columns = [
                    {'key': str(column), 'label': str(column)}
                    for column in list(data.columns)
                ]
        elif isinstance(data, dict) and isinstance(data.get('rows'), list):
            rows = data.get('rows') or []
            if columns is None:
                columns = data.get('columns')
        elif isinstance(data, list):
            rows = data
            if columns is None and rows and isinstance(rows[0], dict):
                columns = [
                    {'key': str(key), 'label': str(key)}
                    for key in rows[0].keys()
                ]
        else:
            raise ValueError('build_table_result.data 必须是 DataFrame、records 列表或包含 rows 的 dict。')

    clean_columns: list[dict[str, Any]] = []
    for index, column in enumerate(columns or [], start=1):
        if not isinstance(column, dict):
            raise ValueError('build_table_result.columns 中的每一项都必须是 dict。')
        key = str(
            column.get('key')
            or column.get('field')
            or column.get('dataIndex')
            or column.get('label')
            or f'col_{index}'
        ).strip()
        label = str(column.get('label') or column.get('title') or key).strip()
        clean_columns.append({"key": key, "label": label})

    clean_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            raise ValueError('build_table_result.rows 中的每一项都必须是 dict。')
        clean_row = dict(row)
        if value_precision is not None:
            for key, value in clean_row.items():
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    and not isinstance(value, int)
                ):
                    clean_row[key] = round(float(value), value_precision)
        clean_rows.append(clean_row)

    metric_discriminator_keys = {
        column["key"]
        for column in clean_columns
        if re.search(r"(?:^|_)(?:metric|measure)(?:$|_)|指标|度量", column["key"], re.IGNORECASE)
        or re.search(r"指标|度量|metric|measure", column["label"], re.IGNORECASE)
    }
    for discriminator_key in metric_discriminator_keys:
        metric_labels = {
            str(row.get(discriminator_key) or '').strip()
            for row in clean_rows
            if str(row.get(discriminator_key) or '').strip()
        }
        if len(metric_labels) < 2:
            continue
        mixed_value_columns = []
        for column in clean_columns:
            key = column["key"]
            if key == discriminator_key:
                continue
            labels_with_numeric_values = {
                str(row.get(discriminator_key) or '').strip()
                for row in clean_rows
                if isinstance(row.get(key), (int, float))
                and not isinstance(row.get(key), bool)
                and math.isfinite(float(row.get(key)))
            }
            if len(labels_with_numeric_values) > 1:
                mixed_value_columns.append(key)
        if mixed_value_columns:
            raise ValueError(
                'table_semantic_error: 同一数值列包含多个由指标行区分的计算口径，'
                f'无法形成稳定的列级指标语义：{mixed_value_columns[:8]}。'
                '请使用长表结构，将部门、月份等作为维度列，并把每个业务指标分别放在独立数值列中。'
            )

    return {"table": {
        "title": str(title or '表格').strip() or '表格',
        "description": str(description or '').strip(),
        "columns": clean_columns,
        "rows": clean_rows,
    }}


def get_analysis_parameters(defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return generated-code defaults merged with local rerun overrides."""
    base = dict(defaults or {})
    override = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.get() or {}
    if isinstance(override, dict):
        base.update(override)
    return base


def save_analysis_result(
    analysis_report: str = '',
    charts: Optional[list[dict[str, Any]]] = None,
    tables: Optional[list[dict[str, Any]]] = None,
    analysis_parameters_schema: Optional[dict[str, Any]] = None,
    analysis_parameters: Optional[dict[str, Any]] = None,
) -> StructuredResult:
    """
    结束一次由生成代码驱动的分析任务，并返回标准结构化结果。

    当前主契约是：
    - analysis_report: Markdown 报告
    - charts: 图表数组
    - tables: 表格数组

    """
    report_text = _strip_structural_empty_report_lines(analysis_report)
    if not report_text:
        raise ValueError('analysis_report 不能为空，必须传入完整的 Markdown 分析报告。')

    _raise_if_all_query_snapshots_no_data()

    normalized_charts = _copy_jsonable(
        _normalize_chart_items(charts=_copy_jsonable(charts or [], [])),
        [],
    )
    # A conditional analytic branch may legitimately produce no rows even when
    # other branches have data. It is not a user-facing chart until it has a
    # renderable point; retaining its shell creates an empty panel and gives a
    # false impression that the branch itself was visualized.
    normalized_charts = [
        chart for chart in normalized_charts
        if _chart_has_renderable_data(chart)
    ]
    normalized_tables = _copy_jsonable(
        _normalize_table_items(tables=_copy_jsonable(tables or [], [])),
        [],
    )
    return StructuredResult(
        analysis_report=report_text,
        charts=normalized_charts,
        tables=normalized_tables,
        analysis_parameters_schema=_copy_jsonable(analysis_parameters_schema or {}, {}),
        analysis_parameters=_copy_jsonable(analysis_parameters or {}, {}),
        query_snapshots=_copy_jsonable(CURRENT_QUERY_SNAPSHOTS.get() or {}, {}),
    )


def _strip_structural_empty_report_lines(report_text: Any) -> str:
    """Remove Markdown list shells that carry no user-visible content."""
    lines = str(report_text or '').splitlines()
    non_empty_list_marker = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s*$")
    return "\n".join(
        line for line in lines
        if not non_empty_list_marker.fullmatch(line)
    ).strip()


def _chart_has_renderable_data(chart: Any) -> bool:
    """Return whether a normalized chart contains at least one drawable point."""
    if not isinstance(chart, dict):
        return False
    if _chart_point_count(chart) > 0:
        return True
    document = chart.get("chart_document")
    dataset = document.get("dataset") if isinstance(document, dict) else None
    return bool(isinstance(dataset, dict) and dataset.get("rows"))


class SourceCodePatch(BaseModel):
    """A line-addressed edit against one immutable failed execution."""

    start_line: int = Field(description="被替换区间的起始行号（1-based，包含）。")
    end_line: int = Field(description="被替换区间的结束行号（1-based，包含）。")
    replacement: str = Field(description="替换一个完整 Python 语句或代码块的可解析源码。")


class ExePythonCodeInput(BaseModel):
    code: str = Field(
        default='',
        description=(
            "一份独立、完整、可执行的 Python 代码。execution_intent='analysis' 时必须在同一次调用内完成"
            "查询、计算、图表/表格、报告并以 result = save_analysis_result(...) 收口；"
            "execution_intent='probe' 时必须以 result = request_retry(...) 返回结构化探测反馈。"
            "不同 execute_python 调用之间不会保留 DataFrame、变量或中间状态。首次生成或整体调整时填写；"
            "对上一执行中的局部技术错误做修复时，改用 base_execution_id + patches，避免重写整份代码。"
        )
    )
    title: str = Field(default='', description="本次代码执行任务的简短标题；省略时使用默认任务标题。")
    description: str = Field(default='', description="本次代码执行任务的简要说明；局部修复时可以省略。")
    used_datasource_ids: list[int] = Field(
        default_factory=list,
        description="本次代码实际读取或查询的数据源 datasource_id 列表。analysis 和 probe 都必须先根据当前问题、历史上下文和数据源语义判断相关数据源后准确填写，只能从当前选中的候选数据源 ID 中选择，不要把所有候选数据源都填入。最终 analysis_report、charts、tables 中声称已分析、已覆盖或展示状态的数据源必须与这里声明的实际访问范围一致。",
    )
    base_execution_id: int = Field(
        default=0,
        description=(
            "局部修复所基于的失败 execution_id。只修复错误定位附近代码时填写，并让 code 保持为空；"
            "后端会从该不可变执行记录恢复完整代码和原 used_datasource_ids。"
        ),
    )
    patches: list[SourceCodePatch] = Field(
        default_factory=list,
        description=(
            "针对 base_execution_id 完整源码的非重叠语句替换。start_line/end_line 必须完整覆盖"
            "错误反馈 repair_target.allowed_ranges 中的一个 Python 语句或代码块；"
            "后端合成并验证完整代码，未覆盖区域保持原样。"
        ),
    )


def _serialize_exec_result(exec_result: Any) -> dict[str, Any]:
    if isinstance(exec_result, StructuredResult):
        return {
            'result_kind': 'structured',
            'payload': exec_result.model_dump(),
        }
    if isinstance(exec_result, RetryResult):
        return {
            'result_kind': 'retry',
            'payload': exec_result.model_dump(),
        }
    return {
        'result_kind': 'raw',
        'payload': None,
    }


def _deserialize_exec_result(payload: dict[str, Any]) -> Any:
    if payload.get('result_kind') == 'structured' and isinstance(payload.get('payload'), dict):
        return StructuredResult(**payload['payload'])
    if payload.get('result_kind') == 'retry' and isinstance(payload.get('payload'), dict):
        return RetryResult(**payload['payload'])
    return None


def _build_no_data_feedback(
    reason: str,
    detail_lines: Optional[list[str]] = None,
) -> dict[str, Any]:
    normalized_reason = (reason or '当前条件下未命中可分析数据。').strip() or '当前条件下未命中可分析数据。'
    normalized_details = [
        str(item).strip()
        for item in (detail_lines or [])
        if str(item).strip()
    ]
    error_message = normalized_reason
    if normalized_details:
        error_message = f"{normalized_reason} 细节：{'；'.join(normalized_details[:5])}"
    executed_query_contexts: list[dict[str, Any]] = []
    for query_key, raw_snapshot in (CURRENT_QUERY_SNAPSHOTS.get() or {}).items():
        if not isinstance(raw_snapshot, dict):
            continue
        implementation = raw_snapshot.get('implementation') or {}
        execution = raw_snapshot.get('execution') or {}
        if not isinstance(implementation, dict) or not isinstance(execution, dict):
            continue
        if not implementation.get('physical_execution') and 'row_count' not in execution:
            continue
        query_constraints = raw_snapshot.get('query_constraints') or {}
        query_constraints = query_constraints if isinstance(query_constraints, dict) else {}
        analysis_scope = raw_snapshot.get('analysis_scope') or {}
        analysis_scope = (
            analysis_scope
            if isinstance(analysis_scope, dict) and analysis_scope
            else _analysis_scope_from_query_constraints(query_constraints)
        )
        query_scope = raw_snapshot.get('query_scope') or {}
        query_scope = query_scope if isinstance(query_scope, dict) else {}
        row_count = execution.get('row_count')
        empty_observation = (
            str(execution.get('status') or '').lower() == 'failed'
            or row_count == 0
            or bool(execution.get('effective_no_data'))
        )
        executed_query_contexts.append({
            'query_key': str(query_key),
            'applied_constraints': {
                'time_range': _copy_jsonable(analysis_scope.get('time_range'), None),
                'target_filters': _copy_jsonable(analysis_scope.get('target_filters') or [], []),
                'aggregation': _copy_jsonable(
                    query_scope.get('aggregation') or query_constraints.get('aggregation') or {},
                    {},
                ),
            },
            'query_scope': _copy_jsonable(query_scope, {}),
            'filter_expressions': list(implementation.get('filter_expressions') or [])[:40],
            'sql': str(implementation.get('sql') or '')[:2000],
            'loader': str(implementation.get('loader') or ''),
            'datasource_identifier': str(implementation.get('datasource_identifier') or ''),
            'execution_status': str(execution.get('status') or ''),
            'row_count': row_count,
            'empty_observation': empty_observation,
            'columns': list(execution.get('columns') or [])[:80],
        })
    executed_query_contexts = executed_query_contexts[-5:]
    probe_targets = [
        item for item in executed_query_contexts if item.get('empty_observation')
    ] or executed_query_contexts[-1:]
    diagnostics = {
        'executed_query_contexts': executed_query_contexts,
        'probe_targets': probe_targets,
        'probe_scope': (
            'Probe only the query implementations and applied filter fields listed in probe_targets.'
        ),
    }
    return {
        'error_type': 'no_data_found',
        'error_message': error_message,
        'error_signature': {
            'error_type': 'no_data_found',
            'signature_type': 'no_data_found',
            'reason': normalized_reason,
            'detail_lines': normalized_details[:5],
            'recommended_next_execution_intent': 'probe',
            **diagnostics,
        },
        'diagnostics': diagnostics,
        'repair_instructions': _build_repair_instructions('no_data_found'),
    }


def _build_retry_error_signature(retry_result: RetryResult) -> dict[str, Any]:
    return {
        'error_type': retry_result.retry_type,
        'signature_type': retry_result.retry_type,
        'message': retry_result.message,
        'diagnostics': retry_result.diagnostics,
    }


def _is_invalid_chart_data_value(value: Any) -> bool:
    """Return True when a chart data point cannot be rendered by ECharts."""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if hasattr(value, "item"):
        try:
            return _is_invalid_chart_data_value(value.item())
        except Exception:
            return False
    if isinstance(value, dict):
        return any(_is_invalid_chart_data_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_invalid_chart_data_value(item) for item in value)
    return False


def _find_invalid_chart_data_points(charts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    invalid_points: list[dict[str, Any]] = []
    for chart_index, chart in enumerate(charts or []):
        if not isinstance(chart, dict):
            continue
        chart_spec = chart.get("chart_spec") or {}
        if not isinstance(chart_spec, dict):
            continue
        series_items = chart_spec.get("series") or []
        if isinstance(series_items, dict):
            series_items = [series_items]
        if not isinstance(series_items, list):
            continue
        for series_index, series in enumerate(series_items):
            if not isinstance(series, dict):
                continue
            data_items = series.get("data") or []
            if not isinstance(data_items, list):
                continue
            series_type = str(series.get("type") or "").strip().lower()
            sparse_series_has_renderable_point = series_type in {"line", "bar"} and any(
                value is not None and not _is_invalid_chart_data_value(value)
                for value in data_items
            )
            for data_index, value in enumerate(data_items):
                if sparse_series_has_renderable_point and value is None:
                    continue
                if _is_invalid_chart_data_value(value):
                    invalid_points.append({
                        "chart_index": chart_index,
                        "chart_title": str(chart.get("title") or ""),
                        "series_index": series_index,
                        "series_name": str(series.get("name") or ""),
                        "data_index": data_index,
                        "value_repr": repr(value),
                    })
                    if len(invalid_points) >= 10:
                        return invalid_points
    return invalid_points


_FLAT_TOTAL_CATEGORY_PATTERN = re.compile(
    r"(?:^|[\s(/\uff08_\-])(?:grand\s+total|sub\s*total|total|overall|\u603b\u8ba1|\u5408\u8ba1|\u5c0f\u8ba1|\u6c47\u603b|\u603b\u4f53)(?:$|[\s)\uff09/_\-])",
    re.IGNORECASE,
)


def _find_overlapping_total_component_charts(
    charts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Find flat chart categories that mix an additive total with its parts."""
    findings: list[dict[str, Any]] = []
    for chart_index, chart in enumerate(charts or []):
        if not isinstance(chart, dict):
            continue
        document = (
            chart.get("chart_document")
            if isinstance(chart.get("chart_document"), dict)
            else {}
        )
        dataset = (
            document.get("dataset")
            if isinstance(document.get("dataset"), dict)
            else {}
        )
        rows = dataset.get("rows") if isinstance(dataset.get("rows"), list) else []
        encoding = (
            document.get("encoding")
            if isinstance(document.get("encoding"), dict)
            else {}
        )
        category_field = str(
            encoding.get("category_field") or encoding.get("x_field") or ""
        ).strip()
        value_fields = [
            str(field).strip()
            for field in (
                encoding.get("value_fields")
                if isinstance(encoding.get("value_fields"), list)
                else [encoding.get("value_field") or encoding.get("y_field")]
            )
            if str(field or "").strip()
        ]
        if not rows or not category_field or not value_fields:
            continue
        for value_field in value_fields:
            total_rows: list[tuple[str, float]] = []
            component_values: list[float] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                label = str(row.get(category_field) or "").strip()
                value = row.get(value_field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric_value = float(value)
                if not math.isfinite(numeric_value):
                    continue
                if _FLAT_TOTAL_CATEGORY_PATTERN.search(label):
                    total_rows.append((label, numeric_value))
                else:
                    component_values.append(numeric_value)
            if len(total_rows) != 1 or len(component_values) < 2:
                continue
            total_label, total_value = total_rows[0]
            component_sum = sum(component_values)
            tolerance = max(1e-9, abs(total_value) * 1e-9)
            if abs(total_value - component_sum) > tolerance:
                continue
            findings.append({
                "chart_index": chart_index,
                "chart_title": str(chart.get("title") or ""),
                "category_field": category_field,
                "value_field": value_field,
                "total_label": total_label,
                "total_value": total_value,
                "component_count": len(component_values),
                "component_sum": component_sum,
            })
    return findings


def _split_report_scan_blocks(report_text: str) -> list[str]:
    text = (report_text or '')[:REPORT_CHART_DUMP_SCAN_MAX_CHARS]
    blocks = [block.strip() for block in re.split(r'\n\s*\n|```', text) if block.strip()]
    return blocks or ([text.strip()] if text.strip() else [])


def _find_chart_structure_dump_in_report(report_text: str) -> dict[str, Any] | None:
    for block in _split_report_scan_blocks(report_text):
        lowered = block.lower()
        object_like = any(marker in lowered for marker in REPORT_CHART_DUMP_OBJECT_MARKERS)
        if not object_like:
            continue

        matched_markers = [
            marker
            for marker in REPORT_CHART_DUMP_FIELD_MARKERS
            if marker in lowered
        ]
        if len(matched_markers) < 3:
            continue

        return {
            "matched_markers": matched_markers[:10],
            "dump_preview": block[:800],
        }
    return None


def _is_expected_time_no_data_report(report_text: str, expected_time_range: dict[str, Any]) -> bool:
    if not isinstance(expected_time_range, dict) or not expected_time_range:
        return False
    normalized_report = str(report_text or '').strip()
    if not normalized_report:
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
    if not any(marker in normalized_report for marker in no_data_markers):
        return False

    if expected_time_range.get('relative_window'):
        return True

    expected_start = _date_text(expected_time_range.get('start'))
    expected_end = _date_text(expected_time_range.get('end'))
    expected_display_end = _date_text(expected_time_range.get('display_end'))
    accepted_ends = {item for item in (expected_end, expected_display_end) if item}
    return bool(expected_start and expected_start in normalized_report and any(end in normalized_report for end in accepted_ends))


NO_DATA_RESULT_MARKERS = (
    '无数据',
    '暂无',
    '没有数据',
    '未命中',
    '未返回',
    'unknown tag',
    'no results',
)
NO_DATA_STATUS_CHART_MARKERS = (
    '无数据',
    '暂无数据',
    'unknown tag',
    'no results',
    '数据状态',
    '查询状态',
    '失败原因',
)


def _result_to_scan_text(exec_result: StructuredResult) -> str:
    payload = {
        'analysis_report': exec_result.analysis_report or '',
        'charts': exec_result.charts or [],
        'tables': exec_result.tables or [],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _extract_sql_limit_clauses(code: str) -> list[str]:
    clauses: list[str] = []
    for match in re.finditer(
        r'\blimit\s+\d+(?:\s*,\s*\d+|\s+offset\s+\d+)?',
        code or '',
        re.IGNORECASE,
    ):
        clause = re.sub(r'\s+', ' ', match.group(0)).strip()
        if clause and clause.lower() not in {item.lower() for item in clauses}:
            clauses.append(clause)
        if len(clauses) >= 10:
            break
    return clauses


def _extract_dataframe_reduction_clauses(code: str) -> list[str]:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []

    parent_by_id: dict[int, ast.AST] = {}
    loaded_names: dict[str, int] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_by_id[id(child)] = parent
        if isinstance(parent, ast.Name) and isinstance(parent.ctx, ast.Load):
            loaded_names[parent.id] = loaded_names.get(parent.id, 0) + 1

    def assigned_names(node: ast.AST) -> list[str]:
        current = node
        while id(current) in parent_by_id:
            current = parent_by_id[id(current)]
            if isinstance(current, (ast.Assign, ast.AnnAssign)):
                targets = (
                    current.targets
                    if isinstance(current, ast.Assign)
                    else [current.target]
                )
                return [
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name)
                ]
            if isinstance(current, (ast.Expr, ast.Return, ast.Call)):
                return []
        return []

    reduction_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        is_reducer_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"head", "tail", "sample"}
        )
        is_iloc_slice = (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "iloc"
            and any(
                isinstance(slice_node, ast.Slice)
                for slice_node in ast.walk(node.slice)
            )
        )
        if not (is_reducer_call or is_iloc_slice):
            continue
        targets = assigned_names(node)
        if targets and not any(loaded_names.get(name, 0) > 0 for name in targets):
            continue
        reduction_nodes.append(node)

    clauses: list[str] = []
    for node in reduction_nodes:
        clause = ast.get_source_segment(code, node) or ""
        clause = re.sub(r"\s+", " ", clause).strip()
        if clause and clause.lower() not in {item.lower() for item in clauses}:
            clauses.append(clause)
        if len(clauses) >= 10:
            break
    return clauses


def _text_discloses_limit_usage(text: str) -> bool:
    scan_text = str(text or '').lower()
    # A trailing ``\b`` does not match ``Top5客户`` because Python treats
    # CJK characters as word characters.  Only guard the ASCII identifier side
    # so Top-N disclosures remain detectable in natural Chinese labels.
    if re.search(r'(?<![a-z0-9_])top\s*\d+', scan_text, re.IGNORECASE):
        return True
    disclosure_markers = (
        'limit',
        '限制',
        '限量',
        '截取',
        '采样',
        '抽样',
        '最多',
        '仅展示',
        '只展示',
        '实际展示',
        '实际覆盖',
        '实际查询',
        '返回上限',
        '条数上限',
        'topn',
        'top n',
    )
    return any(marker in scan_text for marker in disclosure_markers)


def _text_discloses_relative_window(text: str) -> bool:
    scan_text = str(text or "")
    return bool(
        re.search(
            r"(?:近|最近)\s*\d+\s*(?:个)?(?:小时|天|周|月|季度|年|条|项|个)",
            scan_text,
        )
        or re.search(
            r"\b(?:latest|recent)\s+\d+\s+(?:hours?|days?|weeks?|months?|quarters?|years?|rows?|items?)\b",
            scan_text,
            re.IGNORECASE,
        )
    )


def _raise_if_all_query_snapshots_no_data() -> None:
    """Keep partial-data analysis valid, but reject an ordinary result built on no population."""
    if CURRENT_EXECUTION_INTENT.get() != 'analysis':
        return
    snapshots = CURRENT_QUERY_SNAPSHOTS.get() or {}
    executions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for snapshot in snapshots.values():
        if not isinstance(snapshot, dict):
            continue
        implementation = snapshot.get('implementation')
        execution = snapshot.get('execution')
        if not isinstance(implementation, dict) or not implementation.get('physical_execution'):
            continue
        if not isinstance(execution, dict) or execution.get('status') != 'success':
            continue
        executions.append((implementation, execution))
    if not executions:
        return

    def _is_no_data(execution: dict[str, Any]) -> bool:
        if execution.get('effective_no_data') is True:
            return True
        row_count = execution.get('row_count')
        return isinstance(row_count, int) and row_count == 0

    # A generated analysis may execute several independent physical queries
    # after one declared query envelope. A positive later path is still real
    # analyzable data and must not be hidden merely because it was registered
    # as an implicit/helper snapshot.
    no_data_executions = [execution for _, execution in executions]
    if all(_is_no_data(execution) for execution in no_data_executions):
        if CURRENT_CONFIRMED_NO_DATA_RESULT.get():
            return
        raise NoDataFoundError(
            reason='本轮所有实际查询均未命中可分析数据。',
            detail_lines=[
                f'已检查查询数：{len(no_data_executions)}',
                '请先探测筛选枚举、时间边界或关联口径；确认仍无数据后再生成无数据原因与建议报告。',
            ],
        )


def _result_visible_text(exec_result: StructuredResult) -> str:
    visible_parts = [str(exec_result.analysis_report or "")]
    for artifact in [*(exec_result.charts or []), *(exec_result.tables or [])]:
        if not isinstance(artifact, dict):
            continue
        visible_parts.extend([
            str(artifact.get("title") or ""),
            str(artifact.get("description") or ""),
        ])
    return "\n".join(part for part in visible_parts if part)


def _find_empty_markdown_leaf_sections(report_text: str) -> list[dict[str, Any]]:
    """Find headings whose own leaf section has no user-visible content.

    Parent headings are allowed to contain only child sections.  This check is
    intentionally structural: it does not parse numbers or judge whether the
    prose is insightful, but it prevents a conditional report builder from
    publishing named sections that rendered to nothing.
    """
    lines = str(report_text or '').splitlines()
    headings: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = re.match(r'^\s*(#{2,6})\s+(.+?)\s*$', line)
        if not match:
            continue
        headings.append({
            'line_index': index,
            'line_no': index + 1,
            'level': len(match.group(1)),
            'heading': match.group(2).strip(),
        })

    empty: list[dict[str, Any]] = []
    for heading_index, heading in enumerate(headings):
        if heading['level'] < 3:
            continue
        next_heading = headings[heading_index + 1] if heading_index + 1 < len(headings) else None
        if next_heading is not None and next_heading['level'] > heading['level']:
            continue
        end_index = next_heading['line_index'] if next_heading is not None else len(lines)
        body_lines = lines[heading['line_index'] + 1:end_index]
        if any(line.strip() for line in body_lines):
            continue
        empty.append({
            'line_no': heading['line_no'],
            'level': heading['level'],
            'heading': heading['heading'],
        })
    return empty


def _result_discloses_limit_usage(exec_result: StructuredResult) -> bool:
    return _text_discloses_limit_usage(_result_visible_text(exec_result))


def _result_has_proven_population_reduction(
    exec_result: StructuredResult,
) -> bool:
    """Return only runtime-proven reduction, never a syntax-only guess.

    ``head(10)`` over three rows is not a reduction. Generated source alone
    cannot distinguish that case from a truncated population, so a blocking
    repair requires an execution observation that records the actual boundary.
    """
    for snapshot in (exec_result.query_snapshots or {}).values():
        if not isinstance(snapshot, dict):
            continue
        execution = snapshot.get("execution")
        if not isinstance(execution, dict):
            continue
        if execution.get("population_truncated") is True:
            return True
        before = execution.get("rows_before_reduction")
        after = execution.get("rows_after_reduction")
        if (
            isinstance(before, int)
            and isinstance(after, int)
            and before > after >= 0
        ):
            return True
    return False


def _report_discloses_limit_usage(exec_result: StructuredResult) -> bool:
    return _text_discloses_limit_usage(exec_result.analysis_report or '')


def _is_no_data_like_report(report_text: str) -> bool:
    normalized = str(report_text or '').lower()
    return any(marker.lower() in normalized for marker in NO_DATA_RESULT_MARKERS) or bool(
        re.search(r"(?:无|没有|未)[^。；;\n]{0,16}(?:数据|记录|结果)", normalized)
    )


def _chart_point_count(chart: dict[str, Any]) -> int:
    if not isinstance(chart, dict):
        return 0
    chart_spec = chart.get('chart_spec') or {}
    if not isinstance(chart_spec, dict):
        return 0
    series_items = chart_spec.get('series') or []
    if isinstance(series_items, dict):
        series_items = [series_items]
    if not isinstance(series_items, list):
        return 0
    total = 0
    for series in series_items:
        if not isinstance(series, dict):
            continue
        data_items = series.get('data') or []
        if isinstance(data_items, list):
            total += len(data_items)
    return total


def _has_materialized_structured_values(exec_result: StructuredResult) -> bool:
    """Return whether a returned chart/table carries at least one real data value.

    This is deliberately an output-shape check, not a judgement about the report's
    prose quality.  An analysis result cannot satisfy the structured-artifact
    contract by returning a schema-only table or an empty chart after successful
    calculation of a non-empty population.
    """
    for table in exec_result.tables or []:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows")
        if not isinstance(rows, list):
            rows = table.get("data")
        if isinstance(rows, list) and rows:
            return True
    for chart in exec_result.charts or []:
        if not isinstance(chart, dict):
            continue
        if _chart_point_count(chart) > 0:
            return True
        document = chart.get("chart_document")
        dataset = document.get("dataset") if isinstance(document, dict) else None
        if isinstance(dataset, dict) and isinstance(dataset.get("rows"), list) and dataset["rows"]:
            return True
    return False


def _find_no_data_status_charts(charts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    suspicious: list[dict[str, Any]] = []
    for index, chart in enumerate(charts or []):
        if not isinstance(chart, dict):
            continue
        chart_text = json.dumps(chart, ensure_ascii=False, default=str).lower()
        matched_markers = [
            marker for marker in NO_DATA_STATUS_CHART_MARKERS
            if marker.lower() in chart_text
        ]
        if not matched_markers:
            continue
        suspicious.append({
            'chart_index': index,
            'chart_title': str(chart.get('title') or ''),
            'chart_type': str(chart.get('chart_type') or ''),
            'matched_markers': matched_markers[:5],
            'point_count': _chart_point_count(chart),
        })
        if len(suspicious) >= 5:
            break
    return suspicious


def _get_runtime_selected_datasource_snapshot(context: CustomContext) -> list[dict[str, Any]]:
    snapshot = getattr(context, 'active_datasource_snapshot', {}) or {}
    if not isinstance(snapshot, dict):
        return []
    rows = snapshot.get('selected_datasource_snapshot') or []
    if rows:
        return [item for item in rows if isinstance(item, dict)]

    selected_ids = _normalize_used_datasource_ids(snapshot.get('selected_datasource_ids'))
    conversation_id = int(getattr(context, 'conversation_id', 0) or 0)
    if not selected_ids or conversation_id <= 0:
        return []

    from config.database import SessionLocal
    from service.conversation_context_service import ConversationContextService

    session = SessionLocal()
    try:
        return ConversationContextService(session)._build_turn_datasource_snapshot(
            conversation_id=conversation_id,
            datasource_ids=selected_ids,
        )
    finally:
        session.close()


def _query_snapshot_table_names(query_snapshots: dict[str, Any]) -> set[str]:
    table_names: set[str] = set()
    for snapshot in (query_snapshots or {}).values():
        if not isinstance(snapshot, dict):
            continue
        execution = snapshot.get('execution') if isinstance(snapshot.get('execution'), dict) else {}
        if execution.get('status') not in {'success', 'validated', None}:
            continue
        implementation = (
            snapshot.get('implementation')
            if isinstance(snapshot.get('implementation'), dict)
            else {}
        )
        datasource_identifier = str(
            implementation.get('datasource_identifier') or ''
        ).strip().strip('`"[]').lower()
        if datasource_identifier:
            table_names.add(datasource_identifier)
            table_names.add(datasource_identifier.rsplit('.', 1)[-1])
        sql = str(implementation.get('sql') or '').strip()
        if not sql:
            continue
        try:
            from sqlglot import exp, parse

            for statement in parse(sql):
                for table in statement.find_all(exp.Table):
                    parts = [str(part).strip().lower() for part in (table.catalog, table.db, table.name) if str(part).strip()]
                    if not parts:
                        continue
                    table_names.add('.'.join(parts))
                    table_names.add(parts[-1])
        except Exception:
            # Query snapshots remain useful even for a dialect sqlglot cannot parse.
            normalized_sql = sql.lower()
            for match in re.finditer(r'(?i)\b(?:from|join)\s+([`"\[]?[\w.:-]+[`"\]]?)', normalized_sql):
                token = match.group(1).strip('`"[]').lower()
                if token:
                    table_names.add(token)
                    table_names.add(token.rsplit('.', 1)[-1])
    return table_names


def _find_declared_but_unaccessed_datasources(
    exec_result: StructuredResult,
    *,
    used_datasource_ids: list[int] | None,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Find selected datasource IDs declared as used without a physical access."""

    if not used_datasource_ids or not selected_datasource_snapshot:
        return []
    accessed_tokens = _query_snapshot_table_names(exec_result.query_snapshots)
    if not accessed_tokens:
        return []
    selected_by_id: dict[int, dict[str, Any]] = {}
    actually_accessed_ids: set[int] = set()
    for item in selected_datasource_snapshot:
        try:
            datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0:
            continue
        selected_by_id[datasource_id] = item
        if _datasource_relation_tokens(item) & accessed_tokens:
            actually_accessed_ids.add(datasource_id)
    # If none of the physical accesses can be mapped to selected schemas, retain
    # the declaration instead of guessing from an unsupported datasource form.
    if not actually_accessed_ids:
        return []
    findings: list[dict[str, Any]] = []
    for datasource_id in used_datasource_ids:
        if datasource_id in actually_accessed_ids or datasource_id not in selected_by_id:
            continue
        item = selected_by_id[datasource_id]
        findings.append({
            'datasource_id': datasource_id,
            'datasource_name': item.get('datasource_name'),
            'datasource_identifier': item.get('datasource_identifier'),
        })
    return findings


def _canonicalize_used_datasource_ids(
    exec_result: StructuredResult,
    *,
    declared_datasource_ids: list[int] | None,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> list[int]:
    """Use recorded physical accesses as the source of truth for datasource usage."""

    declared_ids = _normalize_used_datasource_ids(declared_datasource_ids)
    if not selected_datasource_snapshot:
        return declared_ids
    accessed_tokens = _query_snapshot_table_names(exec_result.query_snapshots)
    if not accessed_tokens:
        return declared_ids

    selected_ids: set[int] = set()
    accessed_ids: set[int] = set()
    for item in selected_datasource_snapshot:
        try:
            datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0:
            continue
        selected_ids.add(datasource_id)
        if _datasource_relation_tokens(item) & accessed_tokens:
            accessed_ids.add(datasource_id)

    # Preserve declarations for datasource forms that cannot be mapped from a
    # snapshot token. Once mapping succeeds, physical accesses are canonical.
    if not accessed_ids:
        return declared_ids
    external_declared_ids = {
        datasource_id
        for datasource_id in declared_ids
        if datasource_id not in selected_ids
    }
    return sorted(accessed_ids | external_declared_ids)


def _datasource_relation_tokens(item: dict[str, Any]) -> set[str]:
    config = item.get('config') if isinstance(item.get('config'), dict) else {}
    metadata_schema = item.get('metadata_schema') if isinstance(item.get('metadata_schema'), dict) else {}
    raw_values = [
        item.get('datasource_identifier'),
        item.get('table_name'),
        config.get('table_name'),
        metadata_schema.get('name'),
    ]
    tokens: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value or '').strip().strip('`"[]').lower()
        if not value:
            continue
        tokens.add(value)
        tokens.add(value.rsplit('.', 1)[-1])
    return tokens


def _statically_referenced_datasource_ids(
    code: str,
    *,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> set[int]:
    """Map physical datasource identifiers mentioned by generated code to IDs."""

    normalized_code = str(code or '').lower()
    if not normalized_code or not selected_datasource_snapshot:
        return set()
    referenced_ids: set[int] = set()
    for item in selected_datasource_snapshot:
        try:
            datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0:
            continue
        tokens = sorted(_datasource_relation_tokens(item), key=len, reverse=True)
        if any(
            re.search(rf'(?<![\w]){re.escape(token)}(?![\w])', normalized_code)
            for token in tokens
            if token
        ):
            referenced_ids.add(datasource_id)
    return referenced_ids


def _find_untracked_datasource_accesses(
    exec_result: StructuredResult,
    *,
    used_datasource_ids: list[int] | None,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not used_datasource_ids or not selected_datasource_snapshot:
        return []
    used_id_set = {int(item) for item in used_datasource_ids}
    accessed_table_names = _query_snapshot_table_names(exec_result.query_snapshots)
    if not accessed_table_names:
        return []
    accesses: list[dict[str, Any]] = []
    for item in selected_datasource_snapshot:
        try:
            datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0 or datasource_id in used_id_set:
            continue
        matched_tokens = sorted(_datasource_relation_tokens(item) & accessed_table_names)
        if not matched_tokens:
            continue
        accesses.append({
            'datasource_id': datasource_id,
            'datasource_name': item.get('datasource_name'),
            'datasource_identifier': item.get('datasource_identifier'),
            'matched_tokens': matched_tokens[:5],
        })
        if len(accesses) >= 10:
            break
    return accesses


def _find_opaque_dimension_artifacts(
    exec_result: StructuredResult,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Find user-facing chart dimensions backed by fields known to contain opaque IDs."""
    opaque_fields_by_source: list[tuple[set[str], set[str]]] = []
    for datasource in selected_datasource_snapshot or []:
        metadata_schema = (
            datasource.get("metadata_schema")
            if isinstance(datasource.get("metadata_schema"), dict)
            else {}
        )
        properties = (
            metadata_schema.get("properties")
            if isinstance(metadata_schema.get("properties"), dict)
            else {}
        )
        opaque_fields = {
            str(field_name).strip().lower()
            for field_name, definition in properties.items()
            if isinstance(definition, dict)
            and isinstance(definition.get("observed_value_profile"), dict)
            and definition["observed_value_profile"].get("readability")
            == "opaque_identifier"
        }
        if opaque_fields:
            opaque_fields_by_source.append(
                (_datasource_relation_tokens(datasource), opaque_fields)
            )
    def looks_like_opaque_value(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(
            re.search(r"(?<!\d)-?\d{10,}(?:\.0+)?(?!\d)", text)
            or re.search(r"(?<![\w.])-?\d(?:\.\d+)?[eE][+-]?\d+(?!\w)", text)
            or re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                text,
            )
            or re.fullmatch(r"[0-9a-fA-F]{24,}", text)
        )

    def chart_category_values(chart: dict[str, Any], category_field: str) -> list[Any]:
        document = chart.get("chart_document") if isinstance(chart.get("chart_document"), dict) else {}
        dataset = document.get("dataset") if isinstance(document.get("dataset"), dict) else {}
        rows = dataset.get("rows") if isinstance(dataset.get("rows"), list) else []
        values = [row.get(category_field) for row in rows if isinstance(row, dict)]
        if values:
            return values
        spec = chart.get("chart_spec") if isinstance(chart.get("chart_spec"), dict) else {}
        x_axis = spec.get("xAxis")
        if isinstance(x_axis, list):
            x_axis = x_axis[0] if x_axis else {}
        if isinstance(x_axis, dict) and isinstance(x_axis.get("data"), list):
            return x_axis["data"]
        y_axis = spec.get("yAxis")
        if isinstance(y_axis, list):
            y_axis = y_axis[0] if y_axis else {}
        if isinstance(y_axis, dict) and isinstance(y_axis.get("data"), list):
            return y_axis["data"]
        for series in spec.get("series") or []:
            if not isinstance(series, dict) or str(series.get("type") or "").lower() != "pie":
                continue
            pie_values = [
                item.get("name")
                for item in series.get("data") or []
                if isinstance(item, dict)
            ]
            if pie_values:
                return pie_values
        return []

    def query_details(query_key: str) -> tuple[set[str], dict[str, set[str]]]:
        snapshot = (exec_result.query_snapshots or {}).get(query_key) or {}
        implementation = (
            snapshot.get("implementation")
            if isinstance(snapshot, dict)
            and isinstance(snapshot.get("implementation"), dict)
            else {}
        )
        sql = str(implementation.get("sql") or "").strip()
        if not sql:
            return set(), {}
        table_names: set[str] = set()
        output_origins: dict[str, set[str]] = {}
        try:
            from sqlglot import exp, parse

            for statement in parse(re.sub(
                r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s",
                "'__query_parameter__'",
                sql,
            )):
                for table in statement.find_all(exp.Table):
                    parts = [
                        str(part).strip().lower()
                        for part in (table.catalog, table.db, table.name)
                        if str(part).strip()
                    ]
                    if parts:
                        table_names.update({".".join(parts), parts[-1]})
                for select in statement.find_all(exp.Select):
                    for projection in select.expressions:
                        output_name = str(projection.alias_or_name or "").strip().lower()
                        if not output_name:
                            continue
                        output_origins.setdefault(output_name, set()).update(
                            str(column.name).strip().lower()
                            for column in projection.find_all(exp.Column)
                            if str(column.name).strip()
                        )
        except Exception:
            normalized_sql = sql.lower()
            for match in re.finditer(
                r"(?i)\b(?:from|join)\s+([`\"\[]?[\w.:-]+[`\"\]]?)",
                normalized_sql,
            ):
                token = match.group(1).strip('`"[]').lower()
                if token:
                    table_names.update({token, token.rsplit(".", 1)[-1]})
        return table_names, output_origins

    findings: list[dict[str, Any]] = []
    for chart_index, chart in enumerate(exec_result.charts or []):
        if not isinstance(chart, dict):
            continue
        contract = (
            chart.get("chart_data_contract")
            if isinstance(chart.get("chart_data_contract"), dict)
            else {}
        )
        encoding = (
            contract.get("encoding")
            if isinstance(contract.get("encoding"), dict)
            else {}
        )
        if not encoding:
            chart_document = (
                chart.get("chart_document")
                if isinstance(chart.get("chart_document"), dict)
                else {}
            )
            encoding = (
                chart_document.get("encoding")
                if isinstance(chart_document.get("encoding"), dict)
                else {}
            )
        category_field = str(
            encoding.get("category_field") or encoding.get("x_field") or ""
        ).strip().lower()
        if not category_field:
            continue
        source_query_key = str(contract.get("source_query_key") or "").strip()
        table_names, output_origins = query_details(source_query_key)
        origin_fields = output_origins.get(category_field) or {category_field}
        observed_categories = [
            value
            for value in chart_category_values(chart, category_field)
            if str(value or "").strip()
        ]
        if observed_categories and all(looks_like_opaque_value(value) for value in observed_categories):
            findings.append({
                "chart_index": chart_index,
                "chart_title": str(chart.get("title") or ""),
                "source_query_key": source_query_key,
                "category_field": category_field,
                "reason": "all_structured_category_values_are_opaque_identifiers",
            })
            continue
        for source_tokens, opaque_fields in opaque_fields_by_source:
            if table_names and not (source_tokens & table_names):
                continue
            matched_fields = sorted(origin_fields & opaque_fields)
            if not matched_fields:
                continue
            findings.append({
                "chart_index": chart_index,
                "chart_title": str(chart.get("title") or ""),
                "source_query_key": source_query_key,
                "category_field": category_field,
                "opaque_source_fields": matched_fields,
            })
            break
    for table_index, table in enumerate(exec_result.tables or []):
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        if not columns or not rows:
            continue
        # Structured analysis tables conventionally put their grouping/entity
        # dimension first. Checking that explicit column avoids guessing numeric
        # metrics from arbitrary report prose or cell text.
        first_column = columns[0] if isinstance(columns[0], dict) else {}
        dimension_key = str(first_column.get("key") or "").strip()
        if not dimension_key:
            continue
        values = [
            row.get(dimension_key)
            for row in rows
            if isinstance(row, dict) and str(row.get(dimension_key) or "").strip()
        ]
        if values and all(looks_like_opaque_value(value) for value in values):
            findings.append({
                "table_index": table_index,
                "table_title": str(table.get("title") or ""),
                "dimension_field": dimension_key,
                "reason": "all_structured_dimension_values_are_opaque_identifiers",
            })
    return findings


def _find_unreadable_table_column_labels(
    tables: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Find inferred technical identifiers exposed as user-facing headers."""
    findings: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables or []):
        if not isinstance(table, dict):
            continue
        for column_index, column in enumerate(table.get("columns") or []):
            if not isinstance(column, dict):
                continue
            key = str(column.get("key") or "").strip()
            label = str(column.get("label") or "").strip()
            if (
                not key
                or label != key
                or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
            ):
                continue
            findings.append({
                "table_index": table_index,
                "table_title": str(table.get("title") or ""),
                "column_index": column_index,
                "key": key,
                "label": label,
            })
    return findings


def _validate_structured_result_contract(
    exec_result: StructuredResult,
    *,
    generated_code: str = '',
    allow_empty_artifact_no_data: bool = False,
    expected_time_range: Optional[dict[str, Any]] = None,
    used_datasource_ids: list[int] | None = None,
    selected_datasource_snapshot: list[dict[str, Any]] | None = None,
) -> RetryResult | None:
    report_text = (exec_result.analysis_report or '').strip()
    if not report_text:
        return request_retry(
            retry_type="missing_report",
            message="当前代码返回了结构化结果，但 analysis_report 为空。",
            diagnostics={
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
            },
            repair_instructions=[
                "下一版代码必须先构造完整 Markdown 分析报告。",
                "调用 save_analysis_result(...) 时传入非空 analysis_report。",
            ],
        )

    if _is_unfinished_report_sentinel(report_text):
        return request_retry(
            retry_type="missing_report",
            message="当前代码返回的 analysis_report 仍是未完成的占位值，不能作为成功结果保存。",
            diagnostics={
                "violated_contract": "unfinished_report_sentinel",
                "report_value": report_text,
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
            },
            repair_instructions=[
                "保留已经正确完成的查询、计算、图表和表格，不要缩减分析范围。",
                "使用当前执行中已计算的真实结果构造完整 Markdown 洞察报告。",
                "对于数据不足或无法确认的分析问题，在报告中如实说明限制和建议，不要返回占位文本。",
            ],
            analysis_report=report_text,
        )

    empty_leaf_sections = _find_empty_markdown_leaf_sections(report_text)
    if empty_leaf_sections:
        return request_retry(
            retry_type="incomplete_result",
            message="The rendered Markdown report contains empty leaf sections.",
            diagnostics={
                "violated_contract": "empty_markdown_leaf_sections",
                "empty_sections": empty_leaf_sections,
            },
            repair_instructions=[
                "Preserve the complete queries, calculations, charts, tables, report structure and datasource scope.",
                "For each listed leaf heading, render the calculated finding when its population is non-empty; when that population is empty or unsupported, render a concise factual boundary statement instead of leaving the section blank.",
                "Do not delete requested analytical sections merely to satisfy this structural check.",
            ],
            analysis_report=report_text,
        )

    unrendered_placeholders = _find_unrendered_report_placeholders(
        report_text,
        generated_code=generated_code,
    )
    if unrendered_placeholders:
        code_error_context = _locate_unrendered_placeholder_source(
            generated_code,
            unrendered_placeholders,
        )
        return request_retry(
            retry_type="result_contract_error",
            message=_format_error_message_with_code_location(
                "The analysis report contains unrendered Python formatting expressions.",
                code_error_context,
            ),
            diagnostics={
                "violated_contract": "unrendered_report_placeholder",
                "placeholders": unrendered_placeholders,
                "code_error_context": code_error_context,
            },
            repair_instructions=[
                "Keep the existing queries, calculations, charts, tables, and report narrative unchanged.",
                "Render every listed expression before save_analysis_result(...); a common cause is appending a plain triple-quoted string that should be an f-string.",
                "Do not replace the expressions with hard-coded values. Use the already calculated variables so local reruns remain correct.",
            ],
            analysis_report=report_text,
            code_error_context=code_error_context,
        )

    chart_dump = _find_chart_structure_dump_in_report(report_text)
    if chart_dump is not None:
        return request_retry(
            retry_type="chart_contract_error",
            message="当前 analysis_report 中包含图表结构对象 dump，不能把 chart_document/chart_spec/chart dict 写入最终报告。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
                "chart_dump": chart_dump,
            },
            repair_instructions=[
                "下一版代码保留已生成的 charts 结构化产物，但必须重写 analysis_report。",
                "analysis_report 只能包含 Markdown 自然语言解读和表格文本；不要插入 charts[0]、charts[0]['chart_document']、chart_spec、chart_document 或图表 dict。",
                "如果报告中需要引用图表，只写自然语言，例如：下方图表展示每日产量变化趋势。",
                "图表对象只能通过 save_analysis_result(..., charts=charts, ...) 的 charts 参数返回。",
            ],
            analysis_report=report_text,
        )

    repeated_units = _find_repeated_numeric_display_units(report_text)
    if repeated_units:
        return request_retry(
            retry_type="result_contract_error",
            message="分析报告给同一个动态数值重复拼接了展示单位。",
            diagnostics={
                "violated_contract": "repeated_numeric_display_unit",
                "repeated_units": repeated_units,
            },
            repair_instructions=[
                "保留查询、计算、图表和报告叙事，只修正数值展示格式。",
                "先检查维度值或格式化变量是否已经包含单位；已有单位时直接展示原值，只有纯数值才追加一次单位。",
                "同步检查报告中由同一变量生成的其他位置，避免重复出现单位。",
            ],
            analysis_report=report_text,
        )

    sql_limit_clauses = _extract_sql_limit_clauses(generated_code)
    dataframe_reduction_clauses = _extract_dataframe_reduction_clauses(generated_code)
    reduction_clauses = sql_limit_clauses + dataframe_reduction_clauses
    reduction_affects_visible_result = bool(exec_result.charts or exec_result.tables) or not (
        _is_no_data_like_report(report_text)
    )
    if (
        reduction_clauses
        and reduction_affects_visible_result
        and _result_has_proven_population_reduction(exec_result)
        and not _result_discloses_limit_usage(exec_result)
        and not (
            len(reduction_clauses) == 1
            and _text_discloses_relative_window(_result_to_scan_text(exec_result))
        )

    ):
        return request_retry(
            retry_type="result_contract_error",
            message="当前代码对查询或图表数据做了 LIMIT/head/tail/sample/iloc 等截取或采样，但可见产物没有披露该限制，容易让用户误以为展示了完整范围。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
                "sql_limit_clauses": sql_limit_clauses,
                "dataframe_reduction_clauses": dataframe_reduction_clauses,
            },
            repair_instructions=[
                "保留已经正确的分析报告。优先在对应图表或表格的标题、description 中说明展示条数、选择方向和范围，不要仅为披露 TopN 而重写或压缩报告正文。",
                "不要删除、放大或绕过现有 LIMIT/head/tail/sample；本次修复只补充真实的范围披露。删除保护性截取可能让下一版查询超过运行时容量上限。",
                "只有 analysis_report 的正文结论本身基于不完整样本时，才在正文说明该限制对结论的影响。",
                "如果图表展示范围小于用户要求的时间范围，必须说明目标时间范围、实际展示点数、总命中点数和截取方向；例如每个数据源命中约 1193 点，图表仅展示其中 200 点。",
                "如果标题或描述写“最近 N 个点”，代码应使用 tail(N) 或按时间倒序取最近数据；不要用 head(N) 后声称展示最近数据。",
                "如果用户要求完整时间范围趋势，优先改为按时间桶聚合或分批查询完整时间范围，不要悄悄缩短展示范围。",
            ],
            analysis_report=report_text,
        )

    untracked_accesses = _find_untracked_datasource_accesses(
        exec_result,
        used_datasource_ids=used_datasource_ids,
        selected_datasource_snapshot=selected_datasource_snapshot,
    )
    if untracked_accesses:
        return request_retry(
            retry_type="result_contract_error",
            message="当前代码实际查询了未在 used_datasource_ids 中声明的数据源，无法完整记录本轮数据访问范围。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "used_datasource_ids": used_datasource_ids or [],
                "untracked_datasource_accesses": untracked_accesses,
            },
            repair_instructions=[
                "下一版调用 execute_python 时，把结构化诊断列出的 datasource_id 加入 used_datasource_ids。",
                "used_datasource_ids 应覆盖 query_snapshots 中记录的实际数据访问，不需要因为报告中的业务建议或数据缺口说明而额外声明数据源。",
            ],
            analysis_report=report_text,
        )

    unaccessed_declarations = _find_declared_but_unaccessed_datasources(
        exec_result,
        used_datasource_ids=used_datasource_ids,
        selected_datasource_snapshot=selected_datasource_snapshot,
    )
    if unaccessed_declarations:
        return request_retry(
            retry_type="result_contract_error",
            message="used_datasource_ids 声明了本轮代码未实际读取或查询的数据源。",
            diagnostics={
                "violated_contract": "declared_datasource_without_physical_access",
                "scope_change_authorized": True,
                "used_datasource_ids": used_datasource_ids or [],
                "unaccessed_datasources": unaccessed_declarations,
            },
            repair_instructions=[
                "保持已经正确的查询和分析产物；used_datasource_ids 只保留 query_snapshots 能确认实际访问的数据源。",
                "同步删除报告中声称使用、关联或覆盖这些未访问数据源的内容；业务建议可以提及数据缺口，但不能声称已经读取该数据源。",
                "如果这些数据源确实是当前洞察不可缺少的事实来源，应在代码中真实查询并消费其结果，而不是只补声明。",
            ],
            analysis_report=report_text,
        )

    opaque_dimension_artifacts = _find_opaque_dimension_artifacts(
        exec_result,
        selected_datasource_snapshot,
    )
    if opaque_dimension_artifacts:
        return request_retry(
            retry_type="result_contract_error",
            message=(
                "当前结果把原始标识字段作为面向用户的图表或表格维度，"
                "无法形成业务可读的洞察。"
            ),
            diagnostics={
                "violated_contract": "opaque_identifier_as_business_dimension",
                "opaque_dimension_artifacts": opaque_dimension_artifacts,
            },
            repair_instructions=[
                "若已选数据源中存在经过确认的标识到业务名称关系，请使用关系中的可读标签字段。",
                "若当前数据范围没有可验证的标签映射，保留可确认的总量、趋势或其他可读维度分析，并说明该维度暂不可解释；不要展示原始标识值。",
                "同步移除报告、图表和表格中的原始标识分类，所有产物消费同一份业务可读结果。",
            ],
            analysis_report=report_text,
        )

    has_structured_artifact = bool(exec_result.charts or exec_result.tables)
    has_empty_table_shell = any(
        isinstance(table, dict)
        and isinstance(table.get("rows", table.get("data")), list)
        and not table.get("rows", table.get("data"))
        for table in exec_result.tables or []
    )
    if has_empty_table_shell and not _has_materialized_structured_values(exec_result):
        if not _is_no_data_like_report(report_text):
            return request_retry(
                retry_type="result_contract_error",
                message=(
                    "当前结果返回了图表或表格外壳，但其中没有任何可展示的数据行或数据点；"
                    "不能用空结构替代已计算的分析产物。"
                ),
                diagnostics={
                    "violated_contract": "empty_structured_artifact",
                    "chart_count": len(exec_result.charts or []),
                    "table_count": len(exec_result.tables or []),
                    "analysis_report_preview": report_text[:800],
                },
                repair_instructions=[
                    "保留已完成的查询、计算口径和业务范围，不要将中间统计或空壳结果保存为最终分析。",
                    "使用本次已计算的聚合、明细或趋势 DataFrame 构造至少一个实际包含数据行/点的图表或结构化表格。",
                    "报告必须同步解释该真实结构化产物中的关键发现；若所有总体查询确实为空，应走无数据结论而不是返回空壳。",
                ],
                analysis_report=report_text,
            )

    if not has_structured_artifact:
        if allow_empty_artifact_no_data and _is_expected_time_no_data_report(report_text, expected_time_range or {}):
            return None
        return request_retry(
            retry_type="missing_structured_artifact",
            message="当前代码只生成了分析报告，没有生成图表或结构化表格。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": 0,
                "table_count": 0,
            },
            repair_instructions=[
                "下一版代码保留已有统计口径、筛选条件和核心结论。",
                "至少补充一个 charts 或 tables 结构化产物。",
                "趋势、对比、TopN 等场景优先生成 charts；单指标或明细汇总可以生成 tables。",
                "不要把自然语言报告直接作为最终结果结束。",
            ],
            analysis_report=report_text,
        )

    no_data_status_charts = _find_no_data_status_charts(exec_result.charts)
    if no_data_status_charts and _is_no_data_like_report(report_text):
        return request_retry(
            retry_type="chart_contract_error",
            message="当前结果是无数据或数据源暂不可分析结论，但生成了无数据状态/数量类伪图表。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
                "no_data_status_charts": no_data_status_charts,
            },
            repair_instructions=[
                "无数据场景不要生成“无数据数量”“查询状态”这类图表，charts 应置空。",
                "如需说明每个已查询数据源的状态，请使用 tables 展示 datasource_name、查询口径和失败/无数据原因。",
                "只有实际查询到可用于趋势、对比、分布或排名的数据行时，才生成对应图表。",
            ],
            analysis_report=report_text,
        )

    invalid_chart_points = _find_invalid_chart_data_points(exec_result.charts)
    if invalid_chart_points:
        return request_retry(
            retry_type="chart_contract_error",
            message="当前代码生成了图表，但图表数据中包含 None/null/NaN/Inf 等不可展示值，可能导致前端图表空白。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
                "invalid_chart_points": invalid_chart_points,
            },
            repair_instructions=[
                "下一版代码保留已有统计口径、筛选条件和核心结论。",
                "把所有传入 charts/chart_spec/series.data 的统计值转换为 Python 原生 int、float 或 str。",
                "不要把 numpy.int64、numpy.float64、pandas 标量、NaN、Inf 或 None 直接传给 pyecharts/ECharts。",
                "典型修复：total_sales = int(total_sales)，或 add_yaxis(..., [int(total_sales)])。",
                "生成图表前检查 series.data，确保不存在 None/null/NaN/Inf。",
            ],
            analysis_report=report_text,
        )

    overlapping_total_charts = _find_overlapping_total_component_charts(
        exec_result.charts
    )
    if overlapping_total_charts:
        return request_retry(
            retry_type="chart_contract_error",
            message=(
                "当前图表把一个合计值和组成该合计的明细项放在同一平面分类中，"
                "会造成占比、集中度或总量的重复计算。"
            ),
            diagnostics={
                "violated_contract": "overlapping_total_component_categories",
                "charts": overlapping_total_charts,
            },
            repair_instructions=[
                "保留查询、组成项统计、其他图表表格和报告洞察，只修正诊断列出的重复统计边界。",
                "组成项图表只保留互斥的同层类别；合计值可以单独作为总览指标或表格汇总，不要作为组成项的一个同层类别。",
                "占比和合计只能基于组成项求一次，不能再次把已经等于组成项之和的合计值加入分母或总量。",
                "不要通过删除分析问题、数据源或其他有效产物来绕过反馈。",
            ],
            analysis_report=report_text,
        )

    unreadable_table_columns = _find_unreadable_table_column_labels(
        exec_result.tables
    )
    if (generated_code and exec_result.query_snapshots) or unreadable_table_columns:
        # This is an artifact-semantic check, not Evidence generation.  A
        # chart/table value field must represent one calculation boundary
        # across its rows so it remains comparable and can later receive one
        # column/series-level trace.  Run the deterministic lineage half only;
        # no LLM or Evidence metadata participates in formal analysis.
        from service.post_analysis_evidence_service import (
            AnalysisExecutionSnapshot,
            PostAnalysisEvidenceCompiler,
        )

        semantic_snapshot = AnalysisExecutionSnapshot(
            execution_id=0,
            code_hash="runtime-contract",
            result_hash="runtime-contract",
            generated_code=generated_code,
            query_snapshots=exec_result.query_snapshots,
            selected_datasource_snapshot=selected_datasource_snapshot or [],
            user_query="",
            analysis_report=report_text,
            charts=exec_result.charts or [],
            tables=exec_result.tables or [],
        )
        semantic_compiler = PostAnalysisEvidenceCompiler()
        heterogeneous_outputs = (
            semantic_compiler.find_heterogeneous_structured_outputs(semantic_snapshot)
            if generated_code and exec_result.query_snapshots
            else []
        )
        heterogeneous_rankings = (
            semantic_compiler.find_heterogeneous_rankings(semantic_snapshot)
            if generated_code and exec_result.query_snapshots
            else []
        )
        if heterogeneous_outputs or heterogeneous_rankings or unreadable_table_columns:
            findings = [
                {
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "artifact_index": int(item.get("artifact_index") or 0),
                    "field": str(item.get("field") or ""),
                }
                for item in heterogeneous_outputs
            ]
            return request_retry(
                retry_type=(
                    "chart_contract_error"
                    if any(item["artifact_type"] == "chart" for item in findings)
                    else "result_contract_error"
                ),
                message=(
                    "结构化分析产物存在需要一次修正的业务语义问题：不可比数值被放入同一比较边界，"
                    "或表格把底层字段标识直接作为用户表头。"
                    if heterogeneous_outputs or heterogeneous_rankings
                    else "结构化表格把底层字段标识直接作为用户表头，缺少用户可读的业务列名。"
                ),
                diagnostics={
                    "violated_contract": (
                        "multiple_structured_semantic_contracts"
                        if unreadable_table_columns
                        and (heterogeneous_outputs or heterogeneous_rankings)
                        else "heterogeneous_structured_metric"
                        if heterogeneous_outputs
                        else "heterogeneous_ranking_metric"
                        if heterogeneous_rankings
                        else "unreadable_table_column_labels"
                    ),
                    "outputs": findings,
                    "rankings": heterogeneous_rankings,
                    "unreadable_table_columns": unreadable_table_columns,
                },
                repair_instructions=[
                    "保留已经完成的查询、分析范围、有效图表表格和报告内容，只修正诊断列出的不可比计算边界。",
                    "同一个 value_field、数值列或排序值必须共享统计对象、单位、总体、粒度和计算方式；分类可以变化，指标定义不能随分类变化。",
                    "不同指标可以并列展示为独立 series/列，但不能仅因都能计数就按绝对数量形成共同优先级。确需排序时，先转换为同一总体上的覆盖率、发生率或有明确共同定义的标准化得分。",
                    "修复后保留各维度的独立事实与业务解释；不要删除图表、表格、数据源、报告章节或分析问题来绕过反馈。",
                    "为 unreadable_table_columns 中的每一列显式传入 columns=[{'key': 真实字段, 'label': 用户可读业务名称}]；只改展示标签，不重命名数据字段或改动计算。",
                ],
                analysis_report=report_text,
            )

    return None


def _find_unrendered_report_placeholders(
    report_text: str,
    *,
    generated_code: str = "",
) -> list[dict[str, Any]]:
    """Find formatting fields that still refer to variables defined by generated code."""
    try:
        tree = ast.parse(str(generated_code or ""))
    except (SyntaxError, ValueError, TypeError):
        return []

    assigned_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param))
    }
    if not assigned_names:
        return []
    plain_string_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    findings: list[dict[str, Any]] = []
    pattern = re.compile(r"(?<!\{)\{(?P<field>[^{}\r\n]{1,160})\}(?!\})")
    for match in pattern.finditer(str(report_text or "")):
        field = match.group("field").strip()
        expression_tree = None
        expression_candidates = [field]
        if "!" in field:
            expression_candidates.append(field.split("!", 1)[0].strip())
        expression_candidates.extend(
            field[:index].strip()
            for index, char in enumerate(field)
            if char == ":"
        )
        for expression in expression_candidates:
            if not expression:
                continue
            try:
                expression_tree = ast.parse(expression, mode="eval")
                break
            except (SyntaxError, ValueError, TypeError):
                continue
        if expression_tree is None:
            continue
        referenced_names = sorted({
            node.id
            for node in ast.walk(expression_tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        })
        matched_names = [name for name in referenced_names if name in assigned_names]
        plain_identifier_placeholder = bool(
            re.fullmatch(r"[A-Za-z_]\w*", field)
        )
        unbound_source_literal = (
            plain_identifier_placeholder
            and not matched_names
            and any(match.group(0) in value for value in plain_string_literals)
        )
        if not matched_names and not unbound_source_literal:
            continue
        findings.append({
            "start": match.start(),
            "end": match.end(),
            "text": match.group(0),
            "variable_names": matched_names,
            "unbound_identifier": (
                field if unbound_source_literal else ""
            ),
        })
        if len(findings) >= 20:
            break
    return findings


def _locate_unrendered_placeholder_source(
    generated_code: str,
    placeholders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Locate plain source strings that emitted formatting fields verbatim."""
    try:
        tree = ast.parse(str(generated_code or ""))
    except (SyntaxError, ValueError, TypeError):
        return {}

    placeholder_texts = {
        str(item.get("text") or "")
        for item in placeholders
        if isinstance(item, dict) and str(item.get("text") or "")
    }
    if not placeholder_texts:
        return {}

    source_lines = str(generated_code or "").splitlines()
    matched_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(text in node.value for text in placeholder_texts)
    ]
    if not matched_nodes:
        return {}

    matched_nodes.sort(key=lambda node: int(getattr(node, "lineno", 0) or 0))
    primary = matched_nodes[0]
    line_no = int(getattr(primary, "lineno", 0) or 0)
    end_line_no = int(getattr(primary, "end_lineno", line_no) or line_no)
    start = max(1, line_no - GENERATED_CODE_CONTEXT_RADIUS)
    end = min(len(source_lines), end_line_no + GENERATED_CODE_CONTEXT_RADIUS)
    context_lines = [
        {
            "line_no": current_line_no,
            "code": source_lines[current_line_no - 1],
            "is_error_line": line_no <= current_line_no <= end_line_no,
        }
        for current_line_no in range(start, end + 1)
    ]
    related_context_lines = []
    for node in matched_nodes[1:8]:
        related_line_no = int(getattr(node, "lineno", 0) or 0)
        if 1 <= related_line_no <= len(source_lines):
            related_context_lines.append({
                "line_no": related_line_no,
                "code": source_lines[related_line_no - 1],
            })
    return {
        "filename": GENERATED_CODE_FILENAME,
        "line_no": line_no or None,
        "column_no": int(getattr(primary, "col_offset", 0) or 0) + 1,
        "error_line": source_lines[line_no - 1] if 1 <= line_no <= len(source_lines) else "",
        "context_lines": context_lines,
        "related_context_lines": related_context_lines,
        "traceback_excerpt": "",
    }


def _find_repeated_numeric_display_units(report_text: str) -> list[dict[str, Any]]:
    unit_pattern = r"(?:万元|亿元|元|个|台|条|天|月|年|小时|分钟|秒|次|份|件|人|%|‰)"
    pattern = re.compile(
        rf"(?P<number>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
        rf"(?P<unit>{unit_pattern})(?P=unit)"
    )
    findings: list[dict[str, Any]] = []
    for match in pattern.finditer(str(report_text or "")):
        findings.append({
            "start": match.start(),
            "end": match.end(),
            "text": match.group(0),
            "unit": match.group("unit"),
        })
        if len(findings) >= 20:
            break
    return findings


def _classify_process_exit(process_exitcode: int | None) -> tuple[str, str]:
    normalized_exitcode = int(process_exitcode) if process_exitcode is not None else 0
    if normalized_exitcode in (-9, 137):
        return (
            'resource_exhausted',
            f'代码执行子进程异常退出，exitcode={normalized_exitcode}。这通常意味着内存耗尽或被系统因资源压力终止，请按大文件/低内存策略重写加载逻辑。',
        )
    return (
        'runtime_error',
        f'代码执行子进程异常退出，exitcode={normalized_exitcode}',
    )


def _build_execution_result_temp_dir() -> str:
    """返回执行结果临时目录，并确保目录存在。"""
    from config.config import Config

    base_dir = getattr(Config, 'TEMP_DIR', '') or tempfile.gettempdir()
    result_dir = os.path.join(base_dir, 'python_exec_results')
    os.makedirs(result_dir, exist_ok=True)
    return result_dir


def _write_worker_result_payload(payload: dict[str, Any]) -> str:
    """将完整执行结果落到临时文件，避免大对象阻塞 multiprocessing.Queue。"""
    result_dir = _build_execution_result_temp_dir()
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        suffix='.json',
        prefix='python_exec_',
        dir=result_dir,
        delete=False,
    ) as temp_file:
        json.dump(payload, temp_file, ensure_ascii=False)
        temp_file.flush()
        return temp_file.name


def _read_worker_result_payload(result_file_path: str) -> dict[str, Any]:
    with open(result_file_path, 'r', encoding='utf-8') as result_file:
        return json.load(result_file)


def _remove_worker_result_payload(result_file_path: str) -> None:
    if result_file_path:
        try:
            os.remove(result_file_path)
        except FileNotFoundError:
            pass


def _sanitize_tool_output(output: str) -> str:
    """从 stdout/stderr 中去掉流式事件碎片，只保留有意义的执行日志。"""
    if not output:
        return ''

    cleaned_lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: {") and '"type"' in stripped:
            continue
        if stripped.startswith("{'type':") and "'stage':" in stripped:
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _build_analysis_logic_signature(code: str) -> str:
    normalized_code = (code or '').replace('\r\n', '\n').strip()
    if not normalized_code:
        return ''
    return hashlib.sha256(normalized_code.encode('utf-8')).hexdigest()


def _apply_source_code_patches(code: str, patches: list[SourceCodePatch]) -> str:
    """Apply syntax-safe complete-statement edits to an immutable source snapshot."""
    source_lines = str(code or '').splitlines()
    if not source_lines:
        raise ValueError('base_execution_id 对应的执行记录没有可修复代码。')
    if not patches:
        raise ValueError('使用 base_execution_id 修复时必须提供至少一个 patches 行区间替换。')

    try:
        source_tree = ast.parse(code or '')
    except SyntaxError as exc:
        raise ValueError(
            '失败代码本身无法建立可靠的 Python 语句边界，请提交完整 code 重新生成，不能使用 patches。'
        ) from exc
    valid_statement_ranges = {
        (
            int(getattr(node, 'lineno', 0) or 0),
            int(getattr(node, 'end_lineno', 0) or 0),
        )
        for node in ast.walk(source_tree)
        if isinstance(node, ast.stmt)
    }

    normalized = sorted(patches, key=lambda item: (item.start_line, item.end_line))
    previous_end = 0
    for patch in normalized:
        if patch.start_line < 1 or patch.end_line < patch.start_line:
            raise ValueError('patches 行号必须满足 1 <= start_line <= end_line。')
        if patch.end_line > len(source_lines):
            raise ValueError(
                f'patches 行号超出失败代码范围：end_line={patch.end_line}, line_count={len(source_lines)}。'
            )
        if patch.start_line <= previous_end:
            raise ValueError('patches 不能包含重叠的源码行区间。')
        if (patch.start_line, patch.end_line) not in valid_statement_ranges:
            raise ValueError(
                'patches 必须完整替换一个 Python 语句或代码块，不能从字典、字符串、调用参数或缩进块中间截断。'
            )
        previous_end = patch.end_line

    for patch in reversed(normalized):
        source_lines[patch.start_line - 1:patch.end_line] = patch.replacement.splitlines()

    rendered = '\n'.join(source_lines)
    if str(code or '').endswith(('\n', '\r')):
        rendered += '\n'
    try:
        ast.parse(rendered)
    except SyntaxError as exc:
        raise ValueError(
            f'patches 合成后的完整代码存在语法错误（第 {exc.lineno or 0} 行：{exc.msg}）；'
            '本次补丁未创建执行，请按完整语句边界重新修复或提交完整 code。'
        ) from exc
    return rendered


def _resolve_execute_python_source(
    *,
    conversation_id: int,
    turn_id: int,
    code: str,
    used_datasource_ids: list[int] | None,
    base_execution_id: int,
    patches: list[SourceCodePatch] | None,
) -> tuple[str, list[int], int, str]:
    """Resolve a full execution source, optionally by patching one failed execution."""
    normalized_code = str(code or '')
    normalized_ids = _normalize_used_datasource_ids(used_datasource_ids)
    base_execution_id = int(base_execution_id or 0)
    normalized_patches = [
        item if isinstance(item, SourceCodePatch) else SourceCodePatch.model_validate(item)
        for item in (patches or [])
    ]

    if base_execution_id <= 0:
        if normalized_patches:
            raise ValueError('提供 patches 时必须同时提供 base_execution_id。')
        if not normalized_code.strip():
            raise ValueError('首次执行必须提供完整 code。')
        return normalized_code, normalized_ids, 0, 'llm_generated'
    if normalized_code.strip():
        raise ValueError('局部修复只能提交 base_execution_id + patches，不能同时提交完整 code。')

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        base_execution = session.query(InsightNsExecution).filter(
            InsightNsExecution.id == base_execution_id,
            InsightNsExecution.conversation_id == int(conversation_id or 0),
            InsightNsExecution.turn_id == int(turn_id or 0),
            InsightNsExecution.is_deleted == 0,
        ).first()
        if base_execution is None:
            raise ValueError('base_execution_id 不属于当前会话轮次或执行记录不存在。')
        if str(base_execution.execution_status or '') == 'success':
            raise ValueError('不能以已成功执行的代码作为错误修复基线。')
        try:
            base_ids = _normalize_used_datasource_ids(json.loads(base_execution.used_datasource_ids_json or '[]'))
        except (TypeError, ValueError, json.JSONDecodeError):
            base_ids = []
        if normalized_ids and normalized_ids != base_ids:
            raise ValueError(
                '局部技术修复不能改变 base_execution_id 的 used_datasource_ids；'
                '省略该参数或原样传入上一版范围。'
            )
        resolved_code = _apply_source_code_patches(
            str(base_execution.generated_code or ''),
            normalized_patches,
        )
        if resolved_code == str(base_execution.generated_code or ''):
            raise ValueError(
                '局部修复没有改变失败代码。请依据错误反馈中的 SQL 定义或完整语句范围提交实际修改，'
                '不要重复原语句。'
            )
        return resolved_code, base_ids, base_execution_id, 'llm_patch_repair'
    finally:
        session.close()


def _create_execution_record(
    conversation_id: int,
    turn_id: int,
    tool_call_id: str,
    code: str,
    title: str,
    description: str,
    used_datasource_ids: list[int] | None = None,
    analysis_task_id: int | None = None,
    parent_execution_id: int | None = None,
    execution_source: str = 'llm_generated',
    analysis_parameters_schema: dict[str, Any] | None = None,
    analysis_parameters: dict[str, Any] | None = None,
) -> int:
    """在生成代码真正执行前，先创建一条执行记录。"""
    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        execution = InsightNsExecution(
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id or '',
            title=(title or '')[:255],
            description=description or '',
            generated_code=code or '',
            used_datasource_ids_json=json.dumps(used_datasource_ids or [], ensure_ascii=False),
            execution_status='running',
            analysis_parameters_schema_json=json.dumps(analysis_parameters_schema or {}, ensure_ascii=False),
            analysis_parameters_json=json.dumps(analysis_parameters or {}, ensure_ascii=False),
            analysis_logic_signature=_build_analysis_logic_signature(code),
            parent_execution_id=parent_execution_id,
            execution_source=execution_source or 'llm_generated',
            content_enabled=1,
            analysis_task_id=analysis_task_id,
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)
        return int(execution.id)
    finally:
        session.close()


def _update_execution_record(
    execution_id: int,
    *,
    execution_status: str,
    analysis_report: str = '',
    result_payload_json: str = '',
    stdout_text: str = '',
    stderr_text: str = '',
    execution_seconds: int = 0,
    error_message: str = '',
    used_datasource_ids: list[int] | None = None,
    analysis_parameters_schema: dict[str, Any] | None = None,
    analysis_parameters: dict[str, Any] | None = None,
    content_enabled: int | None = None,
) -> None:
    """在成功、失败或结果不合法后，回写执行记录。"""
    if execution_id <= 0:
        return

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        execution = session.query(InsightNsExecution).filter(
            InsightNsExecution.id == execution_id,
            InsightNsExecution.is_deleted == 0,
        ).first()
        if execution is None:
            return

        if (
            execution_status == 'success'
            and int(getattr(execution, 'analysis_task_id', 0) or 0) > 0
            and is_analysis_task_cancelled(task_pk=int(execution.analysis_task_id or 0))
        ):
            execution.execution_status = 'failed'
            execution.error_message = CANCELLED_MESSAGE
            execution.content_enabled = 0
            execution.updated_at = datetime.now()
            execution.finished_at = datetime.now()
            session.commit()
            raise AnalysisTaskCancelled(CANCELLED_MESSAGE)

        execution.execution_status = execution_status
        execution.analysis_report = analysis_report or ''
        execution.result_payload_json = result_payload_json or '{}'
        execution.stdout_text = stdout_text or ''
        execution.stderr_text = stderr_text or ''
        execution.execution_seconds = int(execution_seconds or 0)
        execution.error_message = error_message or ''
        if content_enabled is not None:
            execution.content_enabled = int(content_enabled or 0)
        if used_datasource_ids is not None:
            execution.used_datasource_ids_json = json.dumps(used_datasource_ids, ensure_ascii=False)
        if analysis_parameters_schema is not None:
            execution.analysis_parameters_schema_json = json.dumps(analysis_parameters_schema or {}, ensure_ascii=False)
        if analysis_parameters is not None:
            execution.analysis_parameters_json = json.dumps(analysis_parameters or {}, ensure_ascii=False)
        execution.updated_at = datetime.now()
        execution.finished_at = datetime.now()
        session.commit()
    finally:
        session.close()


def _is_runtime_task_cancelled(context: Any) -> bool:
    analysis_task_id = int(getattr(context, 'analysis_task_id', 0) or 0)
    return analysis_task_id > 0 and is_analysis_task_cancelled(task_pk=analysis_task_id)


def _raise_if_runtime_task_cancelled(context: Any, execution_id: int = 0, start_time: float | None = None) -> None:
    if not _is_runtime_task_cancelled(context):
        return
    if execution_id > 0:
        elapsed_ms = int((time.time() - (start_time or time.time())) * 1000)
        _update_execution_record(
            execution_id,
            execution_status='failed',
            execution_seconds=max(elapsed_ms, 0),
            error_message=CANCELLED_MESSAGE,
            content_enabled=0,
        )
    raise AnalysisTaskCancelled(CANCELLED_MESSAGE)


def _normalize_used_datasource_ids(value: Any) -> list[int]:
    """Normalize tool-declared datasource ids while preserving order."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    normalized: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            datasource_id = int(item)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0 or datasource_id in seen:
            continue
        seen.add(datasource_id)
        normalized.append(datasource_id)
    return normalized


def _get_runtime_selected_datasource_ids(context: CustomContext) -> list[int]:
    snapshot = getattr(context, 'active_datasource_snapshot', {}) or {}
    if not isinstance(snapshot, dict):
        return []
    return _normalize_used_datasource_ids(snapshot.get('selected_datasource_ids'))


def _validate_tool_used_datasource_ids(
    *,
    context: CustomContext,
    used_datasource_ids: list[int],
) -> RetryResult | None:
    selected_ids = _get_runtime_selected_datasource_ids(context)
    if not selected_ids:
        return None
    if not used_datasource_ids:
        return request_retry(
            retry_type="contract_error",
            message="execute_python 调用缺少 used_datasource_ids，无法记录本次实际使用的数据源。",
            diagnostics={
                "selected_datasource_ids": selected_ids,
                "used_datasource_ids": used_datasource_ids,
            },
            repair_instructions=[
                "从 active_datasource_snapshot.selected_datasource_snapshot 中选择本次代码实际读取或查询的数据源。",
                "重新调用 execute_python，并把所选 datasource_id 填入 used_datasource_ids。",
                "不要把所有候选数据源都填入 used_datasource_ids，只填本次代码实际访问的数据源。",
            ],
        )
    invalid_ids = [item for item in used_datasource_ids if item not in set(selected_ids)]
    if invalid_ids:
        return request_retry(
            retry_type="contract_error",
            message="execute_python.used_datasource_ids 包含当前会话未选中的数据源 ID。",
            diagnostics={
                "selected_datasource_ids": selected_ids,
                "used_datasource_ids": used_datasource_ids,
                "invalid_used_datasource_ids": invalid_ids,
            },
            repair_instructions=[
                "used_datasource_ids 只能从当前 active_datasource_snapshot.selected_datasource_ids 中选择。",
                "根据用户问题和历史上下文选择本次代码实际访问的数据源后重新调用 execute_python。",
            ],
        )
    return None


def _build_execution_namespace() -> dict[str, Any]:
    """向生成代码暴露允许使用的辅助函数命名空间。"""
    import pandas as pd

    return {
        'pd': pd,
        'load_local_file': load_local_file,
        'load_local_file_low_memory': load_local_file_low_memory,
        'load_minio_file': load_minio_file,
        'load_data_with_sql': load_data_with_sql,
        'probe_data_with_sql': probe_data_with_sql,
        'load_data_with_api': load_data_with_api,
        'normalize_relationship_key': normalize_relationship_key,
        'get_day_range': get_day_range,
        'describe_time_coverage': describe_time_coverage,
        'probe_distinct_values': probe_distinct_values,
        'probe_text_candidates': probe_text_candidates,
        'get_analysis_parameters': get_analysis_parameters,
        'validate_query_constraints': validate_query_constraints,
        'build_markdown_table': build_markdown_table,
        'build_table_result': build_table_result,
        'build_chart_document': build_chart_document,
        'build_chart_result': build_chart_result,
        'build_chart_suite': build_chart_suite,
        'build_multi_metric_chart_result': build_multi_metric_chart_result,
        'raise_no_data_error': raise_no_data_error,
        'request_retry': request_retry,
        'save_empty_analysis_result': save_empty_analysis_result,
        'save_analysis_result': save_analysis_result,
    }


def _serialize_runtime_user_context(runtime_context: CustomContext | None) -> dict[str, Any]:
    """从 Agent 运行时上下文提取对子进程执行真正有用的最小信息。"""
    if runtime_context is None:
        return {}

    selected_snapshot = _get_runtime_selected_datasource_snapshot(runtime_context)
    numeric_field_profiles = _build_datasource_numeric_field_profiles(selected_snapshot)
    return {
        'platform_authorization': getattr(runtime_context, 'platform_authorization', '') or '',
        'fedquery_authorization': getattr(runtime_context, 'fedquery_authorization', '') or '',
        'database_conn_info': dict(getattr(runtime_context, 'database_conn_info', {}) or {}),
        'expected_time_range': dict(getattr(runtime_context, 'expected_time_range', {}) or {}),
        'analysis_parameters_override': dict(getattr(runtime_context, 'analysis_parameters_override', {}) or {}),
        'datasource_numeric_fields': numeric_field_profiles,
    }


def _build_datasource_numeric_field_profiles(
    selected_datasource_snapshot: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    numeric_types = {
        "number", "integer", "int", "int32", "int64", "long", "short",
        "float", "float32", "float64", "double", "decimal", "numeric",
    }
    profiles: list[dict[str, Any]] = []
    for datasource in selected_datasource_snapshot:
        if not isinstance(datasource, dict):
            continue
        identifier = str(datasource.get("datasource_identifier") or "").strip()
        if not identifier:
            continue
        schema = datasource.get("metadata_schema") if isinstance(datasource.get("metadata_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        fields = []
        for name, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            field_type = str(
                definition.get("type")
                or definition.get("data_type")
                or definition.get("dataType")
                or ""
            ).strip().lower()
            if field_type in numeric_types or any(token in field_type for token in ("decimal", "numeric", "float", "double", "int")):
                fields.append(str(name))
        if not fields:
            continue
        identifier_parts = identifier.split(".")
        identifiers = [identifier]
        if len(identifier_parts) >= 2:
            identifiers.append(".".join(identifier_parts[-2:]))
        identifiers.append(identifier_parts[-1])
        profiles.append({
            "identifiers": list(dict.fromkeys(item for item in identifiers if item)),
            "fields": sorted(set(fields)),
        })
    return profiles


def _prime_worker_runtime_context(runtime_user_context: dict[str, Any] | None) -> None:
    """在子进程里恢复数据库访问所需的最小运行时上下文。"""
    payload = runtime_user_context or {}
    CURRENT_EXPECTED_TIME_RANGE.set(dict(payload.get('expected_time_range') or {}))
    CURRENT_TIME_RANGE_GUARD_ACTIVE.set(bool(payload.get('time_range_guard_active')))
    CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set(dict(payload.get('analysis_parameters_override') or {}))
    CURRENT_DATASOURCE_NUMERIC_FIELDS.set([
        dict(item)
        for item in payload.get('datasource_numeric_fields') or []
        if isinstance(item, dict)
    ])
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set('')
    CURRENT_EXECUTION_INTENT.set('')
    CURRENT_CONFIRMED_NO_DATA_RESULT.set(bool(payload.get('confirmed_no_data_result')))
    conn_info_payload = payload.get('database_conn_info') or {}
    platform_authorization = str(payload.get('platform_authorization') or '').strip()
    # Older worker payloads carried the database credential as ``token``.
    # Keep that transport compatible while preferring the explicit split token.
    fedquery_authorization = str(
        payload.get('fedquery_authorization') or payload.get('token') or ''
    ).strip()
    if conn_info_payload:
        supos_kernel_api._database_conn_info = DatabaseConnInfo(
            host=str(conn_info_payload.get('host') or ''),
            port=str(conn_info_payload.get('port') or ''),
            user=str(conn_info_payload.get('user') or ''),
            password=str(conn_info_payload.get('password') or ''),
            lake_rds_database_name=str(conn_info_payload.get('lake_rds_database_name') or ''),
            ts_query_source=str(conn_info_payload.get('ts_query_source') or ''),
            authorization_token=fedquery_authorization,
        )
        supos_kernel_api._database_pool = None
        return

    if platform_authorization:
        conn_info = supos_kernel_api.get_database_conn_info(platform_authorization)
        conn_info.authorization_token = fedquery_authorization
        supos_kernel_api._database_pool = None


def _relationship_key_counts(
    datasource: dict[str, Any],
    fields: list[str],
) -> dict[tuple[str, ...], int]:
    """Load only candidate key columns and count their normalized tuples."""
    from collections import Counter

    datasource_type = str(datasource.get("datasource_type") or "").strip()
    identifier = str(datasource.get("datasource_identifier") or "").strip()
    if not identifier:
        raise ValueError("relationship Probe datasource lacks datasource_identifier")

    def consume(frame: Any, counts: Counter) -> None:
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise ValueError(
                "relationship Probe fields are unavailable: " + ", ".join(missing)
            )
        has_frequency = "__relationship_frequency" in frame.columns
        projected_columns = [
            *fields,
            *( ["__relationship_frequency"] if has_frequency else [] ),
        ]
        for row in frame[projected_columns].itertuples(index=False, name=None):
            key_values = row[:len(fields)]
            if any(value is None for value in key_values):
                continue
            normalized = tuple(str(value).strip() for value in key_values)
            if any(not value for value in normalized):
                continue
            frequency = row[-1] if has_frequency else 1
            counts[normalized] += int(frequency or 0)

    counts: Counter = Counter()
    if datasource_type == "table":
        columns = ", ".join(fields)
        non_null = " AND ".join(f"{field} IS NOT NULL" for field in fields)
        frame = load_data_with_sql(
            f"SELECT {columns}, COUNT(*) AS __relationship_frequency "
            f"FROM {identifier} WHERE {non_null} GROUP BY {columns} "
            f"ORDER BY {columns} LIMIT 10000"
        )
        consume(frame, counts)
        return dict(counts)

    if datasource_type == "local_file":
        if datasource.get("recommended_loader") == "load_local_file_low_memory":
            for frame in load_local_file_low_memory(identifier, usecols=fields):
                consume(frame, counts)
        else:
            consume(load_local_file(identifier), counts)
        return dict(counts)

    raise ValueError(
        f"relationship Probe does not support datasource_type={datasource_type}"
    )


def _canonical_relationship_key(value: Any) -> str:
    """Normalize physical representations without changing ordinary text keys."""
    from decimal import Decimal, InvalidOperation

    text = str(value).strip()
    if not text:
        return ""
    # Warehouses commonly expose an integer identifier from a DECIMAL column as
    # ``123.00`` or scientific notation while a dictionary stores ``123`` as text.
    # Restrict normalization to an explicit decimal/exponent representation so
    # business codes such as ``0012`` keep their significant leading zeroes.
    if "." not in text and "e" not in text.lower():
        return text
    try:
        decimal_value = Decimal(text)
    except InvalidOperation:
        return text
    if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
        return text
    return format(decimal_value.quantize(Decimal(1)), "f")


def normalize_relationship_key(value: Any) -> Any:
    """Canonicalize scalar or pandas Series join keys without float coercion."""
    try:
        import pandas as pd

        if isinstance(value, pd.Series):
            return value.map(_canonical_relationship_key)
    except ImportError:
        pass
    return _canonical_relationship_key(value)


def _canonicalize_relationship_counts(
    counts: dict[tuple[str, ...], int],
) -> dict[tuple[str, ...], int]:
    from collections import Counter

    normalized: Counter = Counter()
    for key, count in counts.items():
        canonical = tuple(_canonical_relationship_key(item) for item in key)
        if canonical and all(canonical):
            normalized[canonical] += int(count or 0)
    return dict(normalized)


def _relationship_observation_from_counts(
    *,
    relationship_id: str,
    left_counts: dict[tuple[str, ...], int],
    right_counts: dict[tuple[str, ...], int],
    key_alignment: str = "exact",
) -> dict[str, Any]:
    left_keys = set(left_counts)
    right_keys = set(right_counts)
    matched_keys = left_keys & right_keys
    left_coverage = len(matched_keys) / len(left_keys) if left_keys else 0.0
    right_coverage = len(matched_keys) / len(right_keys) if right_keys else 0.0
    left_max = max(left_counts.values(), default=0)
    right_max = max(right_counts.values(), default=0)
    cardinality = (
        "unknown"
        if not left_keys or not right_keys or not matched_keys
        else "one_to_one"
        if left_max <= 1 and right_max <= 1
        else "many_to_one"
        if right_max <= 1
        else "one_to_many"
        if left_max <= 1
        else "many_to_many"
    )
    facts = {
        "join_supported": bool(matched_keys),
        "left_distinct_key_count": len(left_keys),
        "right_distinct_key_count": len(right_keys),
        "matched_distinct_key_count": len(matched_keys),
        "left_match_coverage_ratio": left_coverage,
        "right_match_coverage_ratio": right_coverage,
        "left_max_rows_per_key": left_max,
        "right_max_rows_per_key": right_max,
        "cardinality": cardinality,
        "left_measure_aggregation_safe": right_max <= 1,
        "right_measure_aggregation_safe": left_max <= 1,
        "key_alignment": key_alignment,
    }
    if key_alignment != "exact":
        facts["key_normalization"] = "integral_numeric_text"
        facts["join_execution_strategy"] = "normalize_relationship_key"
    return {
        "observation_id": f"probe:{relationship_id}",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": facts,
    }


def _inspect_preparation_relationship(
    relationship: dict[str, Any],
    datasources_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    left_id = int(relationship.get("left_datasource_id") or 0)
    right_id = int(relationship.get("right_datasource_id") or 0)
    left_fields = [str(item) for item in relationship.get("left_fields") or []]
    right_fields = [str(item) for item in relationship.get("right_fields") or []]
    left_source = datasources_by_id[left_id]
    right_source = datasources_by_id[right_id]
    if (
        str(left_source.get("datasource_type") or "") == "table"
        and str(right_source.get("datasource_type") or "") == "table"
    ):
        exact_observation = _inspect_sql_preparation_relationship(
            relationship=relationship,
            left_source=left_source,
            right_source=right_source,
            left_fields=left_fields,
            right_fields=right_fields,
        )
        if exact_observation["facts"].get("join_supported"):
            exact_observation["facts"].setdefault("key_alignment", "exact")
            return exact_observation
        # A zero exact match can be a physical type mismatch rather than a
        # business relationship failure. Recheck the candidate keys locally so
        # the confirmed schema can carry the normalization needed by Runtime.
        left_counts = _canonicalize_relationship_counts(
            _relationship_key_counts(left_source, left_fields)
        )
        right_counts = _canonicalize_relationship_counts(
            _relationship_key_counts(right_source, right_fields)
        )
        normalized_observation = _relationship_observation_from_counts(
            relationship_id=str(relationship.get("relationship_id") or ""),
            left_counts=left_counts,
            right_counts=right_counts,
            key_alignment="integral_numeric_text",
        )
        return (
            normalized_observation
            if normalized_observation["facts"].get("join_supported")
            else exact_observation
        )
    left_counts = _relationship_key_counts(left_source, left_fields)
    right_counts = _relationship_key_counts(right_source, right_fields)
    exact_observation = _relationship_observation_from_counts(
        relationship_id=str(relationship.get("relationship_id") or ""),
        left_counts=left_counts,
        right_counts=right_counts,
    )
    if exact_observation["facts"].get("join_supported"):
        return exact_observation
    return _relationship_observation_from_counts(
        relationship_id=str(relationship.get("relationship_id") or ""),
        left_counts=_canonicalize_relationship_counts(left_counts),
        right_counts=_canonicalize_relationship_counts(right_counts),
        key_alignment="integral_numeric_text",
    )


def _inspect_sql_preparation_relationship(
    *,
    relationship: dict[str, Any],
    left_source: dict[str, Any],
    right_source: dict[str, Any],
    left_fields: list[str],
    right_fields: list[str],
) -> dict[str, Any]:
    """Measure a table relationship at the database and return one summary row."""
    left_identifier = str(left_source.get("datasource_identifier") or "").strip()
    right_identifier = str(right_source.get("datasource_identifier") or "").strip()
    left_columns = ", ".join(left_fields)
    right_columns = ", ".join(right_fields)
    left_non_null = " AND ".join(f"{field} IS NOT NULL" for field in left_fields)
    right_non_null = " AND ".join(f"{field} IS NOT NULL" for field in right_fields)
    left_non_null_qualified = " AND ".join(
        f"l.{field} IS NOT NULL" for field in left_fields
    )
    right_non_null_qualified = " AND ".join(
        f"r.{field} IS NOT NULL" for field in right_fields
    )
    join_condition = " AND ".join(
        f"l.{left_field} = r.{right_field}"
        for left_field, right_field in zip(left_fields, right_fields)
    )
    left_matched_columns = ", ".join(f"l.{field}" for field in left_fields)
    right_matched_columns = ", ".join(f"r.{field}" for field in right_fields)
    sql = f"""
SELECT
    (SELECT COUNT(*) FROM (
        SELECT {left_columns} FROM {left_identifier}
        WHERE {left_non_null} GROUP BY {left_columns}
    ) left_distinct) AS left_distinct_key_count,
    (SELECT COUNT(*) FROM (
        SELECT {right_columns} FROM {right_identifier}
        WHERE {right_non_null} GROUP BY {right_columns}
    ) right_distinct) AS right_distinct_key_count,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT {left_matched_columns}
        FROM {left_identifier} l
        JOIN {right_identifier} r ON {join_condition}
        WHERE {left_non_null_qualified}
    ) matched_left) AS matched_left_key_count,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT {right_matched_columns}
        FROM {left_identifier} l
        JOIN {right_identifier} r ON {join_condition}
        WHERE {right_non_null_qualified}
    ) matched_right) AS matched_right_key_count,
    COALESCE((SELECT MAX(rows_per_key) FROM (
        SELECT COUNT(*) AS rows_per_key FROM {left_identifier}
        WHERE {left_non_null} GROUP BY {left_columns}
    ) left_frequency), 0) AS left_max_rows_per_key,
    COALESCE((SELECT MAX(rows_per_key) FROM (
        SELECT COUNT(*) AS rows_per_key FROM {right_identifier}
        WHERE {right_non_null} GROUP BY {right_columns}
    ) right_frequency), 0) AS right_max_rows_per_key
"""
    numeric_columns = [
        "left_distinct_key_count",
        "right_distinct_key_count",
        "matched_left_key_count",
        "matched_right_key_count",
        "left_max_rows_per_key",
        "right_max_rows_per_key",
    ]
    frame = load_data_with_sql(sql, numeric_columns=numeric_columns)
    if frame.empty:
        raise ValueError("relationship Probe summary query returned no row")
    row = frame.iloc[0]
    left_count = int(row["left_distinct_key_count"] or 0)
    right_count = int(row["right_distinct_key_count"] or 0)
    matched_left = int(row["matched_left_key_count"] or 0)
    matched_right = int(row["matched_right_key_count"] or 0)
    left_max = int(row["left_max_rows_per_key"] or 0)
    right_max = int(row["right_max_rows_per_key"] or 0)
    left_coverage = matched_left / left_count if left_count else 0.0
    right_coverage = matched_right / right_count if right_count else 0.0
    cardinality = (
        "unknown"
        if not matched_left
        else "one_to_one"
        if left_max <= 1 and right_max <= 1
        else "many_to_one"
        if right_max <= 1
        else "one_to_many"
        if left_max <= 1
        else "many_to_many"
    )
    return {
        "observation_id": f"probe:{relationship.get('relationship_id')}",
        "kind": "relationship_coverage",
        "status": "verified",
        "complete": True,
        "facts": {
            "join_supported": bool(matched_left),
            "left_distinct_key_count": left_count,
            "right_distinct_key_count": right_count,
            "matched_distinct_key_count": min(matched_left, matched_right),
            "left_match_coverage_ratio": left_coverage,
            "right_match_coverage_ratio": right_coverage,
            "left_max_rows_per_key": left_max,
            "right_max_rows_per_key": right_max,
            "cardinality": cardinality,
            "left_measure_aggregation_safe": right_max <= 1,
            "right_measure_aggregation_safe": left_max <= 1,
        },
    }


def _inspect_preparation_schema_fact(
    request: dict[str, Any],
    datasource: dict[str, Any],
) -> dict[str, Any]:
    """Return a bounded aggregate/profile observation, never source sample rows."""
    from collections import Counter
    import pandas as pd

    datasource_id = int(request.get("datasource_id") or 0)
    field = str(request.get("field") or "").strip()
    kind = str(request.get("probe_kind") or "").strip()
    top_n = min(max(int(request.get("top_n") or 20), 1), 50)
    identifier = str(datasource.get("datasource_identifier") or "").strip()
    datasource_type = str(datasource.get("datasource_type") or "").strip()
    if not identifier or not field:
        raise ValueError("Schema Probe requires a datasource identifier and field")

    facts: dict[str, Any]
    if datasource_type == "table":
        if kind == "distinct_values":
            frame = load_data_with_sql(
                f"SELECT {field} AS probed_value, COUNT(*) AS frequency "
                f"FROM {identifier} WHERE {field} IS NOT NULL "
                f"GROUP BY {field} ORDER BY frequency DESC LIMIT {top_n}",
                numeric_columns=["frequency"],
            )
            values = [
                {
                    "value": _serialize_probe_value(row.get("probed_value")),
                    "count": int(row.get("frequency") or 0),
                }
                for row in frame.to_dict("records")
            ]
            facts = {"values": values, "top_n": top_n}
            expected_values = [
                str(item).strip()
                for item in request.get("expected_values") or []
                if str(item).strip()
            ]
            if expected_values:
                facts["requested_value_resolutions"] = _resolve_requested_enum_values(
                    expected_values,
                    [str(item.get("value") or "").strip() for item in values],
                )
        elif kind == "time_coverage":
            frame = load_data_with_sql(
                f"SELECT COUNT(*) AS total_count, COUNT({field}) AS non_null_count, "
                f"MIN({field}) AS min_value, MAX({field}) AS max_value "
                f"FROM {identifier}",
                numeric_columns=["total_count", "non_null_count"],
            )
            row = frame.iloc[0] if not frame.empty else {}
            facts = {
                "total_count": int(row.get("total_count") or 0),
                "non_null_count": int(row.get("non_null_count") or 0),
                "min_value": _serialize_probe_value(row.get("min_value")),
                "max_value": _serialize_probe_value(row.get("max_value")),
            }
        elif kind == "field_profile":
            frame = load_data_with_sql(
                f"SELECT COUNT(*) AS total_count, COUNT({field}) AS non_null_count, "
                f"COUNT(DISTINCT {field}) AS distinct_count FROM {identifier}",
                numeric_columns=["total_count", "non_null_count", "distinct_count"],
            )
            row = frame.iloc[0] if not frame.empty else {}
            facts = {
                "total_count": int(row.get("total_count") or 0),
                "non_null_count": int(row.get("non_null_count") or 0),
                "distinct_count": int(row.get("distinct_count") or 0),
            }
        else:
            raise ValueError(f"unsupported Schema Probe kind={kind}")
    elif datasource_type == "local_file":
        if datasource.get("recommended_loader") == "load_local_file_low_memory":
            frames = load_local_file_low_memory(identifier, usecols=[field])
        else:
            frames = [load_local_file(identifier)[[field]]]
        total_count = 0
        non_null_count = 0
        distinct_values: set[str] = set()
        counts: Counter[str] = Counter()
        min_value = None
        max_value = None
        for frame in frames:
            if field not in frame.columns:
                raise ValueError(f"Schema Probe field is unavailable: {field}")
            series = frame[field]
            total_count += int(len(series))
            valid = series.dropna()
            non_null_count += int(len(valid))
            if kind == "distinct_values":
                for value, count in valid.value_counts().items():
                    counts[str(_serialize_probe_value(value))] += int(count)
            elif kind == "field_profile":
                distinct_values.update(str(_serialize_probe_value(value)) for value in valid)
            elif kind == "time_coverage":
                parsed = pd.to_datetime(valid, errors="coerce").dropna()
                if not parsed.empty:
                    current_min = parsed.min()
                    current_max = parsed.max()
                    min_value = current_min if min_value is None else min(min_value, current_min)
                    max_value = current_max if max_value is None else max(max_value, current_max)
            else:
                raise ValueError(f"unsupported Schema Probe kind={kind}")
        if kind == "distinct_values":
            facts = {
                "values": [
                    {"value": value, "count": count}
                    for value, count in counts.most_common(top_n)
                ],
                "top_n": top_n,
            }
            expected_values = [
                str(item).strip()
                for item in request.get("expected_values") or []
                if str(item).strip()
            ]
            if expected_values:
                facts["requested_value_resolutions"] = _resolve_requested_enum_values(
                    expected_values,
                    [str(item.get("value") or "").strip() for item in facts["values"]],
                )
        elif kind == "field_profile":
            facts = {
                "total_count": total_count,
                "non_null_count": non_null_count,
                "distinct_count": len(distinct_values),
            }
        else:
            facts = {
                "total_count": total_count,
                "non_null_count": non_null_count,
                "min_value": _serialize_probe_value(min_value),
                "max_value": _serialize_probe_value(max_value),
            }
    else:
        raise ValueError(
            f"Schema Probe does not support datasource_type={datasource_type}"
        )

    return {
        "observation_id": f"probe:{request.get('request_id')}",
        "kind": kind,
        "status": "verified",
        "complete": True,
        "datasource_id": datasource_id,
        "field": field,
        "purpose": str(request.get("purpose") or ""),
        "related_fact_ids": list(request.get("related_fact_ids") or []),
        "facts": facts,
    }


def _resolve_requested_enum_values(
    requested_values: list[str],
    observed_values: list[str],
) -> list[dict[str, Any]]:
    """Resolve explicit requested labels against a bounded observed enumeration."""
    observed = list(dict.fromkeys(
        str(item).strip() for item in observed_values if str(item).strip()
    ))
    normalized_observed = {
        value: re.sub(r"[\s\-_]+", "", value).casefold()
        for value in observed
    }
    resolutions = []
    for requested in requested_values:
        requested_text = str(requested).strip()
        requested_normalized = re.sub(r"[\s\-_]+", "", requested_text).casefold()
        exact = next(
            (
                value for value, normalized in normalized_observed.items()
                if normalized == requested_normalized
            ),
            None,
        )
        if exact is not None:
            resolutions.append({
                "requested_value": requested_text,
                "status": "exact",
                "observed_value": exact,
                "similarity": 1.0,
            })
            continue

        ranked = sorted(
            (
                (
                    SequenceMatcher(None, requested_normalized, normalized).ratio(),
                    value,
                )
                for value, normalized in normalized_observed.items()
            ),
            reverse=True,
        )
        best_score, best_value = ranked[0] if ranked else (0.0, "")
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score >= 0.8 and best_score - second_score >= 0.1:
            status = "similar"
            observed_value = best_value
        else:
            status = "unresolved"
            observed_value = ""
        resolutions.append({
            "requested_value": requested_text,
            "status": status,
            "observed_value": observed_value,
            "similarity": round(best_score, 4),
        })
    return resolutions


def _preparation_schema_probe_worker(
    requests: list[dict[str, Any]],
    datasources: list[dict[str, Any]],
    runtime_user_context: dict[str, Any],
    result_queue: mp.Queue,
) -> None:
    try:
        _prime_worker_runtime_context(runtime_user_context)
        datasources_by_id = {
            int(item.get("datasource_id") or item.get("id") or 0): item
            for item in datasources
            if isinstance(item, dict)
        }
        observations = []
        for item in requests:
            try:
                observations.append(_inspect_preparation_schema_fact(
                    item,
                    datasources_by_id[int(item.get("datasource_id") or 0)],
                ))
            except Exception as exc:
                observations.append({
                    "observation_id": f"probe:{item.get('request_id')}",
                    "kind": str(item.get("probe_kind") or ""),
                    "status": "unavailable",
                    "complete": False,
                    "datasource_id": int(item.get("datasource_id") or 0),
                    "field": str(item.get("field") or ""),
                    "purpose": str(item.get("purpose") or ""),
                    "related_fact_ids": list(item.get("related_fact_ids") or []),
                    "error": str(exc),
                })
        result_queue.put({"status": "success", "observations": observations})
    except Exception as exc:
        result_queue.put({
            "status": "error",
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
        })


def _preparation_relationship_probe_worker(
    relationships: list[dict[str, Any]],
    datasources: list[dict[str, Any]],
    runtime_user_context: dict[str, Any],
    result_queue: mp.Queue,
) -> None:
    try:
        _prime_worker_runtime_context(runtime_user_context)
        datasources_by_id = {
            int(item.get("datasource_id") or item.get("id") or 0): item
            for item in datasources
            if isinstance(item, dict)
        }
        observations = []
        for item in relationships:
            try:
                observations.append(
                    _inspect_preparation_relationship(item, datasources_by_id)
                )
            except Exception as exc:
                observations.append({
                    "observation_id": f"probe:{item.get('relationship_id')}",
                    "kind": "relationship_coverage",
                    "status": "unavailable",
                    "complete": False,
                    "error": str(exc),
                })
        result_queue.put({"status": "success", "observations": observations})
    except Exception as exc:
        result_queue.put({
            "status": "error",
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
        })


def execute_preparation_relationship_probe(
    *,
    relationships: list[dict[str, Any]],
    datasources: list[dict[str, Any]],
    runtime_user_context: dict[str, Any],
    timeout_seconds: int = 90,
    cancelled: Any = None,
) -> list[dict[str, Any]]:
    """Execute bounded relationship checks for Business Context."""
    if not relationships:
        return []
    result_queue: mp.Queue = mp.Queue()
    process = mp.Process(
        target=_preparation_relationship_probe_worker,
        args=(relationships, datasources, runtime_user_context, result_queue),
        daemon=True,
    )
    process.start()
    deadline = time.time() + max(int(timeout_seconds or 90), 1)
    result: dict[str, Any] | None = None
    try:
        while time.time() < deadline:
            if cancelled is not None:
                cancelled()
            try:
                result = result_queue.get(
                    timeout=min(0.5, max(deadline - time.time(), 0.01))
                )
                break
            except Empty:
                if not process.is_alive():
                    break
        if result is None:
            raise RuntimeError(
                "Business Context relationship Probe timed out"
                if process.is_alive()
                else "Business Context relationship Probe exited without a result"
            )
        if result.get("status") != "success":
            raise RuntimeError(
                str(result.get("error_message") or "relationship Probe failed")
            )
        return [
            item for item in result.get("observations") or []
            if isinstance(item, dict)
        ]
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
        result_queue.close()
        result_queue.join_thread()


def execute_preparation_schema_probe(
    *,
    requests: list[dict[str, Any]],
    datasources: list[dict[str, Any]],
    runtime_user_context: dict[str, Any],
    timeout_seconds: int = 90,
    cancelled: Any = None,
) -> list[dict[str, Any]]:
    """Execute bounded Schema fact checks for Business Context."""
    if not requests:
        return []
    result_queue: mp.Queue = mp.Queue()
    process = mp.Process(
        target=_preparation_schema_probe_worker,
        args=(requests, datasources, runtime_user_context, result_queue),
        daemon=True,
    )
    process.start()
    deadline = time.time() + max(int(timeout_seconds or 90), 1)
    result: dict[str, Any] | None = None
    try:
        while time.time() < deadline:
            if cancelled is not None:
                cancelled()
            try:
                result = result_queue.get(
                    timeout=min(0.5, max(deadline - time.time(), 0.01))
                )
                break
            except Empty:
                if not process.is_alive():
                    break
        if result is None:
            raise RuntimeError(
                "Business Context Schema Probe timed out"
                if process.is_alive()
                else "Business Context Schema Probe exited without a result"
            )
        if result.get("status") != "success":
            raise RuntimeError(
                str(result.get("error_message") or "Schema Probe failed")
            )
        return [
            item for item in result.get("observations") or []
            if isinstance(item, dict)
        ]
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
        result_queue.close()
        result_queue.join_thread()


def _build_query_capacity_failure_diagnostics() -> dict[str, Any]:
    """Describe the exact physical query that crossed the FedQuery row cap."""
    snapshots = CURRENT_QUERY_SNAPSHOTS.get() or {}
    physical_queries: list[dict[str, Any]] = []
    for query_key, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        implementation = snapshot.get('implementation')
        execution = snapshot.get('execution')
        if not isinstance(implementation, dict) or not implementation.get('physical_execution'):
            continue
        execution = execution if isinstance(execution, dict) else {}
        physical_queries.append({
            'query_key': str(query_key),
            'status': str(execution.get('status') or ''),
            'row_count': execution.get('row_count'),
            'sql': str(implementation.get('sql') or '')[:2000],
            'sql_hash': str(implementation.get('sql_hash') or ''),
            'aggregation_hints': list(implementation.get('aggregation_hints') or [])[:20],
        })
    failed_query = next(
        (
            item for item in reversed(physical_queries)
            if item.get('status') == 'failed'
        ),
        physical_queries[-1] if physical_queries else None,
    )
    return {
        'query_execution_capability': {
            'max_rows_per_sql_result': 10000,
            'capacity_scope': 'each physical load_data_with_sql execution',
            'valid_full_population_strategies': [
                'query-side final aggregation',
                'stable pagination with incremental reduction',
            ],
        },
        'failed_query': failed_query,
        # The model already has its generated code in the ReAct turn. Keep the
        # remaining inventory compact instead of duplicating every SQL string
        # into the tool feedback.
        'query_inventory': [
            {
                'query_key': item['query_key'],
                'status': item['status'],
                'row_count': item['row_count'],
                'sql_hash': item['sql_hash'],
            }
            for item in physical_queries[-12:]
        ],
    }


def _execute_generated_code_worker(
    code: str,
    username: str,
    runtime_user_context: dict[str, Any],
    result_queue: mp.Queue,
) -> None:
    _prime_worker_runtime_context(runtime_user_context)
    try:
        execution_tree = ast.parse(code)
        CURRENT_EXECUTION_INTENT.set(
            _extract_static_string_assignment(execution_tree, 'execution_intent') or ''
        )
    except (SyntaxError, ValueError):
        CURRENT_EXECUTION_INTENT.set('')
    namespace = _build_execution_namespace()
    exec_result: Any = None
    with _capture_runtime_io(username) as (stdout_buffer, stderr_buffer):
        try:
            exec(compile(code, GENERATED_CODE_FILENAME, 'exec'), namespace)
            exec_result = namespace.get('result')
            result_file_path = _write_worker_result_payload({
                'status': 'success',
                'stdout_text': _sanitize_tool_output(stdout_buffer.getvalue()),
                'stderr_text': _sanitize_tool_output(stderr_buffer.getvalue()),
                'exec_result': _serialize_exec_result(exec_result),
            })
            result_queue.put({
                'status': 'success',
                'result_file_path': result_file_path,
            })
        except Exception as exc:
            raw_error_message = str(exc)
            full_traceback = traceback.format_exc()
            code_error_context = _build_generated_code_error_context(
                code=code,
                error=exc,
                traceback_text=full_traceback,
            )
            error_message = _format_error_message_with_code_location(
                raw_error_message,
                code_error_context,
            )
            error_type = _classify_execution_error(exc, raw_error_message)
            error_signature = _extract_error_signature(error_type, error_message)
            repair_instructions = _build_repair_instructions(error_type)
            diagnostics: dict[str, Any] = {}
            if isinstance(exc, NoDataFoundError):
                no_data_feedback = _build_no_data_feedback(
                    reason=exc.reason,
                    detail_lines=exc.detail_lines,
                )
                error_message = no_data_feedback['error_message']
                error_signature = no_data_feedback['error_signature']
                repair_instructions = no_data_feedback['repair_instructions']
                diagnostics = no_data_feedback.get('diagnostics') or {}
            elif error_type == 'query_result_too_large':
                diagnostics = _build_query_capacity_failure_diagnostics()
            result_file_path = _write_worker_result_payload({
                'status': 'error',
                'error_message': error_message,
                'error_type': error_type,
                'error_signature': error_signature,
                'code_error_context': code_error_context,
                'repair_instructions': repair_instructions,
                'diagnostics': diagnostics,
                'stdout_text': _sanitize_tool_output(stdout_buffer.getvalue()),
                'stderr_text': _sanitize_tool_output(
                    '\n'.join(item for item in [stderr_buffer.getvalue(), full_traceback] if item)
                ),
            })
            result_queue.put({
                'status': 'error',
                'result_file_path': result_file_path,
            })


@contextmanager
def _capture_runtime_io(username: str):
    """临时接管 stdout/stderr，并绑定当前运行时上下文变量。"""
    import sys as sys_module

    old_stdout = sys_module.stdout
    old_stderr = sys_module.stderr
    sys_module.stdout = StringIO()
    sys_module.stderr = StringIO()
    username_token = CURRENT_USERNAME.set(username or 'anonymous')

    try:
        yield sys_module.stdout, sys_module.stderr
    finally:
        sys_module.stdout = old_stdout
        sys_module.stderr = old_stderr
        CURRENT_USERNAME.reset(username_token)


def _get_stream_emitter():
    """返回一个尽力可用的 LangGraph 自定义流事件发送器。"""
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    def emit(event_type: str, **payload):
        if writer:
            writer({'type': event_type, **payload})

    return emit


def _classify_execution_error(error: Exception | None, error_message: str) -> str:
    """把执行失败归类成更容易被模型理解的错误类型。"""
    if isinstance(error, SyntaxError):
        return 'syntax_error'
    if isinstance(error, ModuleNotFoundError):
        return 'missing_dependency'
    if isinstance(error, NoDataFoundError):
        return 'no_data_found'
    if isinstance(error, FileNotFoundError):
        return 'data_source_not_found'
    if isinstance(error, KeyError):
        return 'schema_or_column_mismatch'
    if isinstance(error, NameError):
        return 'undefined_name'
    if isinstance(error, MemoryError):
        return 'resource_exhausted'

    lowered = (error_message or '').lower()
    if 'memoryerror' in lowered or 'cannot allocate memory' in lowered or 'out of memory' in lowered:
        return 'resource_exhausted'
    if 'query result exceeds' in lowered and 'rows' in lowered:
        return 'query_result_too_large'
    if _is_chart_contract_error_message(error_message):
        return 'chart_contract_error'
    if _is_time_series_empty_source_error(error_message):
        return 'no_data_found'
    if 'got an unexpected keyword argument' in lowered:
        return 'library_api_signature_mismatch'
    if 'invalid comparison' in lowered or ('datetime' in lowered and 'timestamp' in lowered):
        return 'data_type_or_time_mismatch'
    if (
        'not in index' in lowered
        or 'no such column' in lowered
        or 'unknown column' in lowered
        or 'cannot be resolved' in lowered
        or 'does not exist' in lowered and 'column' in lowered
        or 'referenced column' in lowered and 'not found' in lowered
    ):
        return 'schema_or_column_mismatch'
    if any(marker in lowered for marker in (
        'failed to fetch result for query',
        'binder error',
        'catalog error',
        'connection refused',
        'could not connect',
    )):
        return 'data_source_unavailable'
    if (
        'no_data_found' in lowered
        or '未命中可分析数据' in error_message
        or '筛选结果为空' in error_message
        or '过滤后无数据' in error_message
        or '关联后无数据' in error_message
    ):
        return 'no_data_found'
    if 'save_analysis_result' in lowered or 'analysis_report' in lowered:
        return 'result_contract_error'
    return 'runtime_error'


def _static_sql_candidates(code: str) -> list[dict[str, Any]]:
    """Return statically identifiable SQL expressions and their edit ranges."""
    try:
        tree = ast.parse(str(code or ''))
    except SyntaxError:
        return []

    def render_string(node: ast.AST | None) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if not isinstance(node, ast.JoinedStr):
            return ''
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append('__runtime_value__')
        return ''.join(parts)

    candidates: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = getattr(node, 'value', None)
        sql = render_string(value)
        if not re.search(r'(?is)\bselect\b.+\bfrom\b', sql):
            continue
        target_names: list[str] = []
        if isinstance(node, ast.Assign):
            target_names = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
        candidates.append({
            'sql': sql,
            'start_line': int(getattr(node, 'lineno', 0) or 0),
            'end_line': int(getattr(node, 'end_lineno', 0) or getattr(node, 'lineno', 0) or 0),
            'source': (ast.get_source_segment(code, node) or ast.unparse(node))[:2400],
            'target_names': target_names,
        })
    return candidates


def _failed_sql_variable_name(
    code: str,
    code_error_context: dict[str, Any] | None,
) -> tuple[str, int]:
    """Return the SQL variable consumed by the failing load call, when static."""
    try:
        line_no = int((code_error_context or {}).get('line_no') or 0)
    except (TypeError, ValueError):
        line_no = 0
    if line_no <= 0:
        return '', 0
    try:
        tree = ast.parse(str(code or ''))
    except SyntaxError:
        return '', line_no
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or int(getattr(node, 'lineno', 0) or 0) != line_no:
            continue
        func_name = ''
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name != 'load_data_with_sql':
            continue
        sql_arg = next((item.value for item in node.keywords if item.arg == 'sql'), None)
        if sql_arg is None and node.args:
            sql_arg = node.args[0]
        if isinstance(sql_arg, ast.Name):
            return sql_arg.id, line_no
    return '', line_no


def _build_query_capacity_diagnostics(
    *,
    code: str,
    failed_sql: str,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Expose already-observed high-cardinality grains after an oversized query.

    The limit error proves that one physical result is too large.  Preparation
    may also have observed relationship-key cardinalities for the same selected
    schemas.  Returning those facts together lets the ReAct repair update every
    similarly grained query in one turn instead of discovering them one by one.
    This is advisory feedback; it does not reject a query from a cardinality
    estimate before that query has actually executed.
    """
    if not code or not selected_datasource_snapshot:
        return {}

    def normalize(value: Any) -> str:
        return str(value or '').strip().strip('`"[]').casefold()

    source_risks: list[dict[str, Any]] = []
    for datasource in selected_datasource_snapshot:
        if not isinstance(datasource, dict):
            continue
        schema = (
            datasource.get('metadata_schema')
            if isinstance(datasource.get('metadata_schema'), dict)
            else {}
        )
        try:
            datasource_id = int(datasource.get('datasource_id') or datasource.get('id') or 0)
        except (TypeError, ValueError):
            continue
        tokens = {
            normalize(token)
            for token in _datasource_relation_tokens(datasource)
            if normalize(token)
        }
        for relationship in schema.get('relationships') or []:
            if not isinstance(relationship, dict):
                continue
            cardinality = relationship.get('observed_key_cardinality')
            if not isinstance(cardinality, dict):
                continue
            try:
                distinct_count = int(cardinality.get('local_distinct_key_count') or 0)
            except (TypeError, ValueError):
                distinct_count = 0
            fields = [
                str(field).strip()
                for field in relationship.get('local_fields') or []
                if str(field).strip()
            ]
            if datasource_id <= 0 or not tokens or not fields or distinct_count <= MAX_SQL_RESULT_ROWS:
                continue
            source_risks.append({
                'datasource_id': datasource_id,
                'datasource_name': str(datasource.get('datasource_name') or ''),
                'tokens': tokens,
                'fields': fields,
                'observed_distinct_count': distinct_count,
                'relationship_id': str(relationship.get('relationship_id') or ''),
            })
    if not source_risks:
        return {}

    failed_sql_normalized = str(failed_sql or '').casefold()
    matched_failed_risks = [
        risk for risk in source_risks
        if any(token and token in failed_sql_normalized for token in risk['tokens'])
        and any(normalize(field) in failed_sql_normalized for field in risk['fields'])
    ]
    if not matched_failed_risks:
        return {}

    related_queries: list[dict[str, Any]] = []
    for candidate in _static_sql_candidates(code):
        sql_normalized = str(candidate.get('sql') or '').casefold()
        matched = [
            risk for risk in source_risks
            if any(token and token in sql_normalized for token in risk['tokens'])
            and any(normalize(field) in sql_normalized for field in risk['fields'])
        ]
        if not matched:
            continue
        related_queries.append({
            'start_line': candidate.get('start_line'),
            'end_line': candidate.get('end_line'),
            'source': candidate.get('source'),
            'capacity_risks': [
                {
                    'datasource_id': risk['datasource_id'],
                    'fields': risk['fields'],
                    'observed_distinct_count': risk['observed_distinct_count'],
                    'max_rows_per_sql_result': MAX_SQL_RESULT_ROWS,
                }
                for risk in matched
            ],
        })

    return {
        'violated_contract': 'observed_query_capacity_boundary',
        'required_result_design': {
            'max_rows_per_physical_query': MAX_SQL_RESULT_ROWS,
            'final_result_grain_required': True,
            'high_cardinality_keys_may_only_remain_inside_join_or_reduction_subqueries': True,
            'stable_pagination_is_required_if_high_cardinality_rows_must_be_consumed': True,
        },
        'known_high_cardinality_result_dimensions': sorted({
            field
            for risk in matched_failed_risks
            for field in risk.get('fields') or []
        }),
        'failed_query_capacity_risks': [
            {
                'datasource_id': risk['datasource_id'],
                'datasource_name': risk['datasource_name'],
                'fields': risk['fields'],
                'observed_distinct_count': risk['observed_distinct_count'],
                'max_rows_per_sql_result': MAX_SQL_RESULT_ROWS,
                'relationship_id': risk['relationship_id'],
            }
            for risk in matched_failed_risks
        ],
        'related_high_cardinality_queries': related_queries,
    }


def _augment_query_capacity_feedback(
    *,
    code: str,
    error_type: str,
    diagnostics: dict[str, Any],
    repair_instructions: list[str],
    code_error_context: dict[str, Any],
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if error_type != 'query_result_too_large':
        return diagnostics, repair_instructions, code_error_context
    failed_query = diagnostics.get('failed_query') if isinstance(diagnostics, dict) else {}
    failed_sql = str(failed_query.get('sql') or '') if isinstance(failed_query, dict) else ''
    capacity = _build_query_capacity_diagnostics(
        code=code,
        failed_sql=failed_sql,
        selected_datasource_snapshot=selected_datasource_snapshot,
    )
    if not capacity:
        return diagnostics, repair_instructions, code_error_context

    enriched_diagnostics = dict(diagnostics or {})
    enriched_diagnostics.update(capacity)
    enriched_context = dict(code_error_context or {})
    patch_ranges = list(enriched_context.get('suggested_patch_ranges') or [])
    for query in capacity.get('related_high_cardinality_queries') or []:
        query_range = {
            'start_line': query.get('start_line'),
            'end_line': query.get('end_line'),
            'kind': 'sql_definition',
            'source': query.get('source'),
            'role': 'related_high_cardinality_query',
        }
        if query_range not in patch_ranges:
            patch_ranges.append(query_range)
    enriched_context['suggested_patch_ranges'] = patch_ranges
    observed = '; '.join(
        f"datasource {item.get('datasource_id')} fields {item.get('fields')} "
        f"observed_distinct_count={item.get('observed_distinct_count')}"
        for item in capacity.get('failed_query_capacity_risks') or []
    )
    instruction = (
        f"Preparation has already observed high-cardinality result grains: {observed}. "
        "A query grouped at one of those grains can still exceed the 10000-row physical result boundary. "
        "Repair the failed query and every related_high_cardinality_query in one pass: return final aggregates/TopN, "
        "or use stable pagination with incremental reduction. Unless stable pagination is implemented, a known "
        "high-cardinality key such as batch_id may appear inside a JOIN/CTE or COUNT(DISTINCT ...), but it must not "
        "remain in the outer SELECT/GROUP BY result grain. Do not replace a detail query with the same or a superset "
        "high-cardinality GROUP BY; the repaired physical result itself must be bounded below 10000 rows."
    )
    return enriched_diagnostics, [instruction, *repair_instructions], enriched_context


def _build_schema_field_mismatch_diagnostics(
    *,
    code: str,
    error_message: str,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
    code_error_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain all known field ownership errors in the failed single-source SQL.

    Databases commonly report only the first invalid projection. The Runtime can
    repair the query in one turn when the selected Schema proves that additional
    fields belong to another selected datasource.
    """
    if not code or not selected_datasource_snapshot:
        return {}
    try:
        from sqlglot import exp, parse_one
    except Exception:
        return {}

    def normalize_identifier(value: Any) -> str:
        return str(value or '').strip().strip('`"[]').casefold()

    source_catalog: list[dict[str, Any]] = []
    field_owners: dict[str, list[dict[str, Any]]] = {}
    for datasource in selected_datasource_snapshot:
        if not isinstance(datasource, dict):
            continue
        try:
            datasource_id = int(datasource.get('datasource_id') or datasource.get('id') or 0)
        except (TypeError, ValueError):
            continue
        schema = datasource.get('metadata_schema') if isinstance(datasource.get('metadata_schema'), dict) else {}
        properties = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
        if datasource_id <= 0 or not properties:
            continue
        owner = {
            'datasource_id': datasource_id,
            'datasource_name': str(datasource.get('datasource_name') or schema.get('title') or ''),
            'datasource_identifier': str(datasource.get('datasource_identifier') or ''),
        }
        tokens = {normalize_identifier(item) for item in _datasource_relation_tokens(datasource) if item}
        source_catalog.append({
            **owner,
            'tokens': tokens,
            'fields': {str(item).strip().casefold() for item in properties if str(item).strip()},
        })
        for field_name in properties:
            normalized_field = str(field_name).strip().casefold()
            if normalized_field:
                field_owners.setdefault(normalized_field, []).append(owner)

    reported_fields = {
        match.group(1).strip().casefold()
        for pattern in (
            r"(?i)column\s+['\"`]([^'\"`]+)['\"`]\s+cannot be resolved",
            r"(?i)unknown column\s+['\"`]([^'\"`]+)['\"`]",
            r"(?i)column\s+['\"`]([^'\"`]+)['\"`]\s+does not exist",
        )
        for match in re.finditer(pattern, str(error_message or ''))
    }

    sql_variable_name, failed_line_no = _failed_sql_variable_name(code, code_error_context)
    candidates = _static_sql_candidates(code)
    if sql_variable_name:
        reaching_candidates = [
            candidate for candidate in candidates
            if sql_variable_name in (candidate.get('target_names') or [])
            and int(candidate.get('end_line') or 0) <= failed_line_no
        ]
        if reaching_candidates:
            # Python uses the nearest preceding assignment when generated code
            # redefines a query variable during probe or repair.
            candidates = [max(reaching_candidates, key=lambda item: int(item.get('end_line') or 0))]

    findings: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            statement = parse_one(re.sub(
                r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s",
                "'__query_parameter__'",
                candidate['sql'],
            ))
        except Exception:
            continue
        table_tokens = {
            normalize_identifier('.'.join(
                str(part).strip()
                for part in (table.catalog, table.db, table.name)
                if str(part).strip()
            ))
            for table in statement.find_all(exp.Table)
        }
        matched_sources = [
            source for source in source_catalog
            if source['tokens'].intersection(table_tokens)
        ]
        if len(matched_sources) != 1:
            continue
        source = matched_sources[0]
        aliases = {
            str(projection.alias or '').strip().casefold()
            for select in statement.find_all(exp.Select)
            for projection in select.expressions
            if isinstance(projection, exp.Alias) and str(projection.alias or '').strip()
        }
        referenced_fields = {
            str(column.name or '').strip().casefold()
            for column in statement.find_all(exp.Column)
            if str(column.name or '').strip()
        }
        incompatible_fields: list[dict[str, Any]] = []
        for field_name in sorted(referenced_fields - source['fields'] - aliases):
            owners = [
                owner for owner in field_owners.get(field_name, [])
                if int(owner.get('datasource_id') or 0) != source['datasource_id']
            ]
            if not owners:
                continue
            incompatible_fields.append({
                'field_name': field_name,
                'actual_owners': owners,
            })
        if not incompatible_fields:
            continue
        incompatible_names = {item['field_name'] for item in incompatible_fields}
        if reported_fields and not reported_fields.intersection(incompatible_names):
            continue
        findings.append({
            'query_datasource_id': source['datasource_id'],
            'query_datasource_name': source['datasource_name'],
            'query_datasource_identifier': source['datasource_identifier'],
            'incompatible_fields': incompatible_fields,
            'sql_assignment_range': {
                'start_line': candidate['start_line'],
                'end_line': candidate['end_line'],
                'kind': 'sql_definition',
                'source': candidate['source'],
                'role': 'failed_query_definition',
            },
        })
    if len(findings) != 1:
        return {}
    return {
        'violated_contract': 'datasource_field_ownership_mismatch',
        **findings[0],
    }


def _augment_schema_field_mismatch_feedback(
    *,
    code: str,
    error_type: str,
    error_message: str,
    error_signature: dict[str, Any],
    diagnostics: dict[str, Any],
    repair_instructions: list[str],
    code_error_context: dict[str, Any],
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[str], dict[str, Any]]:
    if error_type != 'schema_or_column_mismatch':
        return error_type, error_signature, diagnostics, repair_instructions, code_error_context
    ownership = _build_schema_field_mismatch_diagnostics(
        code=code,
        error_message=error_message,
        selected_datasource_snapshot=selected_datasource_snapshot,
        code_error_context=code_error_context,
    )
    if not ownership:
        return error_type, error_signature, diagnostics, repair_instructions, code_error_context

    incompatible_fields = ownership.get('incompatible_fields') or []
    field_names = [str(item.get('field_name') or '') for item in incompatible_fields]
    owner_descriptions = []
    for item in incompatible_fields:
        owners = item.get('actual_owners') or []
        owner_descriptions.append(
            f"{item.get('field_name')} -> "
            + ', '.join(
                f"datasource {owner.get('datasource_id')} ({owner.get('datasource_name') or owner.get('datasource_identifier')})"
                for owner in owners
            )
        )
    enriched_signature = dict(error_signature or {})
    enriched_signature.update({
        'signature_type': 'schema_or_column_mismatch',
        'field_names': field_names,
        'query_datasource_id': ownership.get('query_datasource_id'),
    })
    enriched_diagnostics = dict(diagnostics or {})
    enriched_diagnostics.update(ownership)
    enriched_context = dict(code_error_context or {})
    patch_ranges = list(enriched_context.get('suggested_patch_ranges') or [])
    sql_range = ownership.get('sql_assignment_range')
    if isinstance(sql_range, dict) and sql_range not in patch_ranges:
        patch_ranges.insert(0, sql_range)
    enriched_context['suggested_patch_ranges'] = patch_ranges
    instruction = (
        f"The failed query targets datasource {ownership.get('query_datasource_id')} "
        f"({ownership.get('query_datasource_name') or ownership.get('query_datasource_identifier')}), "
        f"but all incompatible fields are: {'; '.join(owner_descriptions)}. "
        "Repair all listed fields in one pass using their owning datasource queries and preserve the analysis scope; "
        "do not merely remove the first field reported by the database."
    )
    return (
        error_type,
        enriched_signature,
        enriched_diagnostics,
        [instruction, *repair_instructions],
        enriched_context,
    )


def _augment_missing_mapping_key_feedback(
    *,
    code: str,
    error_type: str,
    error_message: str,
    diagnostics: dict[str, Any],
    repair_instructions: list[str],
    code_error_context: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Expose every static use of one missing mapping key in a single repair."""
    if error_type != 'schema_or_column_mismatch':
        return diagnostics, repair_instructions, code_error_context
    key_match = re.match(r"^['\"]([^'\"]+)['\"](?:\r?\n|$)", str(error_message or '').strip())
    if not key_match:
        return diagnostics, repair_instructions, code_error_context
    missing_key = key_match.group(1).strip()
    if not missing_key:
        return diagnostics, repair_instructions, code_error_context
    try:
        tree = ast.parse(str(code or ''))
    except SyntaxError:
        return diagnostics, repair_instructions, code_error_context

    parent_by_node = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    occurrences: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        slice_value = node.slice.value if isinstance(node.slice, ast.Constant) else None
        if slice_value != missing_key:
            continue
        statement: ast.AST = node
        while statement in parent_by_node and not isinstance(statement, ast.stmt):
            statement = parent_by_node[statement]
        if not isinstance(statement, ast.stmt):
            continue
        occurrences.append({
            'mapping_expression': ast.unparse(node.value),
            'line_no': int(getattr(node, 'lineno', 0) or 0),
            'statement_range': {
                'start_line': int(getattr(statement, 'lineno', 0) or 0),
                'end_line': int(getattr(statement, 'end_lineno', 0) or getattr(statement, 'lineno', 0) or 0),
                'kind': type(statement).__name__,
                'source': (ast.get_source_segment(code, statement) or ast.unparse(statement))[:2400],
                'role': 'missing_mapping_key_usage',
            },
        })
    if not occurrences:
        return diagnostics, repair_instructions, code_error_context

    try:
        failed_line = int((code_error_context or {}).get('line_no') or 0)
    except (TypeError, ValueError):
        failed_line = 0
    failed_mapping = next(
        (
            item['mapping_expression'] for item in occurrences
            if item['line_no'] == failed_line
        ),
        occurrences[0]['mapping_expression'],
    )
    related = [
        item for item in occurrences
        if item['mapping_expression'] == failed_mapping
    ]
    enriched_diagnostics = dict(diagnostics or {})
    enriched_diagnostics.update({
        'violated_contract': 'missing_mapping_key',
        'missing_key': missing_key,
        'mapping_expression': failed_mapping,
        'usage_lines': [item['line_no'] for item in related],
    })
    enriched_context = dict(code_error_context or {})
    patch_ranges = list(enriched_context.get('suggested_patch_ranges') or [])
    for item in related:
        statement_range = item['statement_range']
        if statement_range not in patch_ranges:
            patch_ranges.append(statement_range)
    enriched_context['suggested_patch_ranges'] = patch_ranges
    instruction = (
        f"Mapping expression {failed_mapping} has no key {missing_key!r}, and the generated program "
        f"uses that same missing key at lines {[item['line_no'] for item in related]}. "
        "Inspect the mapping's actual construction or helper return contract and repair every listed use in one pass."
    )
    return enriched_diagnostics, [instruction, *repair_instructions], enriched_context


def _extract_error_signature(error_type: str, error_message: str) -> dict[str, Any]:
    """从错误信息中提取适合下一轮修补使用的错误签名。"""
    signature: dict[str, Any] = {
        'error_type': error_type,
        'message': error_message or '',
    }

    keyword_match = re.search(
        r"(?P<owner>[A-Za-z_][\w.]*)\.__init__\(\) got an unexpected keyword argument '(?P<argument>[^']+)'",
        error_message or '',
    )
    if keyword_match:
        signature.update({
            'signature_type': 'unexpected_keyword_argument',
            'owner': keyword_match.group('owner'),
            'argument': keyword_match.group('argument'),
        })
        return signature

    module_match = re.search(r"No module named '([^']+)'", error_message or '')
    if module_match:
        signature.update({
            'signature_type': 'missing_dependency',
            'module_name': module_match.group(1),
        })
        return signature

    schema_field_match = next((
        match
        for pattern in (
            r"(?i)column\s+['\"`]([^'\"`]+)['\"`]\s+cannot be resolved",
            r"(?i)unknown column\s+['\"`]([^'\"`]+)['\"`]",
            r"(?i)column\s+['\"`]([^'\"`]+)['\"`]\s+does not exist",
        )
        if (match := re.search(pattern, error_message or '')) is not None
    ), None)
    key_match = schema_field_match or re.search(r"'([^']+)'", error_message or '')
    if error_type == 'schema_or_column_mismatch' and key_match:
        signature.update({
            'signature_type': 'schema_or_column_mismatch',
            'field_name': key_match.group(1),
        })
        return signature

    if error_type == 'data_type_or_time_mismatch':
        signature['signature_type'] = 'data_type_or_time_mismatch'
        return signature

    if error_type == 'resource_exhausted':
        exitcode_match = re.search(r'exitcode=(-?\d+)', error_message or '')
        signature.update({
            'signature_type': 'resource_exhausted',
            'exitcode': int(exitcode_match.group(1)) if exitcode_match else None,
        })
        return signature

    if error_type == 'no_data_found':
        if _is_time_series_empty_source_error(error_message):
            signature.update({
                'signature_type': 'time_series_empty_source',
                'empty_source_reason': 'unknown_tag_or_no_results',
            })
            return signature
        signature['signature_type'] = 'no_data_found'
        return signature

    if error_type == 'chart_contract_error':
        signature['signature_type'] = 'chart_contract_error'
        return signature

    if error_type == 'query_result_too_large':
        signature['signature_type'] = 'query_result_too_large'
        return signature

    signature['signature_type'] = 'generic'
    return signature


def _build_turn_failure_memory(turn_id: int, current_execution_id: int) -> dict[str, Any]:
    """汇总同一轮中之前已失败的执行记录，供模型避免重复犯错。"""
    if turn_id <= 0:
        return {
            'previous_failure_messages': [],
            'previous_failure_hints': [],
        }

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        previous_failures = session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id,
            InsightNsExecution.id != current_execution_id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.execution_status != 'success',
        ).order_by(InsightNsExecution.created_at.asc()).all()

        failure_messages: list[str] = []
        failure_hints: list[str] = []
        seen_hints: set[str] = set()

        for execution in previous_failures:
            error_message = (execution.error_message or '').strip()
            if not error_message:
                continue

            failure_messages.append(error_message)
            error_signature = _extract_error_signature(
                _classify_execution_error(None, error_message),
                error_message,
            )
            signature_type = error_signature.get('signature_type')

            if signature_type == 'unexpected_keyword_argument':
                owner = error_signature.get('owner', '当前库')
                argument = error_signature.get('argument', '')
                hint = f'之前已经确认 {owner} 不支持参数 `{argument}`，不要再次生成这段写法。'
            elif signature_type == 'missing_dependency':
                module_name = error_signature.get('module_name', '')
                hint = f'之前已经确认当前环境缺少模块 `{module_name}`，不要再次 import 它。'
            elif signature_type == 'schema_or_column_mismatch':
                field_name = error_signature.get('field_name', '')
                hint = f'之前已经出现字段或列不匹配问题（{field_name}），请先核对真实列名。'
            elif signature_type == 'data_type_or_time_mismatch':
                hint = '之前已经出现时间或数据类型比较不兼容问题，请先统一类型后再过滤。'
            elif signature_type == 'resource_exhausted':
                hint = '之前已经出现内存或资源耗尽问题，请优先改用 load_local_file_low_memory(...) 分批读取，并在批次内完成过滤、聚合或累计统计。'
            elif signature_type == 'time_series_empty_source':
                hint = '之前已有时序数据源返回 unknown tag/no results：该数据源本轮视为无可分析数据，应从后续独立数据源分析中剔除；如果还有其他数据源，继续分析其他数据源。'
            elif signature_type == 'no_data_found':
                hint = '之前已经出现“筛选/关联后无数据”，请优先检查 SQL、时间范围、筛选条件或 JOIN 条件，不要继续输出空图表。'
            elif signature_type == 'query_result_too_large':
                hint = (
                    '本轮已经确认至少一条明细或细粒度聚合查询会超过单次 10000 行上限；'
                    '不要只修当前报错行，请同步审查其余按批次、事件或样本粒度返回的查询，'
                    '统一改为服务端最终聚合，或使用稳定排序分页并逐批累计。'
                )
            else:
                hint = f'不要再次重复已出现过的错误：{error_message[:120]}'

            if hint not in seen_hints:
                seen_hints.add(hint)
                failure_hints.append(hint)

        return {
            'previous_failure_messages': failure_messages[-3:],
            'previous_failure_hints': failure_hints[-3:],
        }
    finally:
        session.close()


def _validate_scope_preserving_technical_repair(
    *,
    previous_used_datasource_ids: list[int],
    current_used_datasource_ids: list[int],
    previous_failure_payload: dict[str, Any] | None,
) -> RetryResult | None:
    """A static implementation rejection cannot authorize silent analysis-scope loss."""
    failure_payload = dict(previous_failure_payload or {})
    diagnostics = failure_payload.get('diagnostics')
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    if not _failure_requires_scope_preservation(failure_payload):
        return None

    previous_ids = _normalize_used_datasource_ids(previous_used_datasource_ids)
    current_ids = _normalize_used_datasource_ids(current_used_datasource_ids)
    missing_ids = [item for item in previous_ids if item not in set(current_ids)]
    if not missing_ids:
        return None

    return request_retry(
        retry_type='contract_error',
        message=(
            '技术契约修复静默缩减了上一版已声明的多数据源分析范围。'
            '上一版的错误只证明具体实现需要修正，没有结构化观测支持删除这些数据源。'
        ),
        diagnostics={
            'violated_contract': 'analysis_scope_regression_after_technical_repair',
            'previous_used_datasource_ids': previous_ids,
            'current_used_datasource_ids': current_ids,
            'missing_datasource_ids': missing_ids,
            'scope_change_authorized': False,
            'previous_failure_phase': diagnostics.get('failure_phase') or 'execution',
            'previous_retry_type': failure_payload.get('retry_type'),
            'previous_error_type': failure_payload.get('error_type'),
        },
        repair_instructions=[
            '恢复 missing_datasource_ids 中的查询路径及其下游计算、图表、表格和报告表达。',
            '只修正上一版报错的查询语法、聚合下推、Runtime API 或异常处理实现，不要借技术修复改写业务分析范围。',
            '如果恢复后的真实查询执行失败，再根据该次具体错误或结构化 Probe 观测调整可用路径。',
        ],
    )


def _failure_requires_scope_preservation(
    previous_failure_payload: dict[str, Any] | None,
) -> bool:
    failure_payload = dict(previous_failure_payload or {})
    diagnostics = failure_payload.get('diagnostics')
    diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    scope_preserving_retry_types = {
        'missing_query_constraints',
        'invalid_query_constraints',
        'query_constraint_violation',
        'chart_contract_error',
        'missing_structured_artifact',
        'missing_report',
        'incomplete_result',
    }
    scope_preserving_error_types = {
        'syntax_error',
        'missing_dependency',
        'schema_or_column_mismatch',
        'undefined_name',
        'data_type_or_time_mismatch',
        'query_result_too_large',
        'runtime_error',
        'library_api_signature_mismatch',
        'chart_contract_error',
    }
    violated_contract = str(diagnostics.get('violated_contract') or '').strip()
    contract_preserves_scope = bool(violated_contract) and not bool(
        diagnostics.get('scope_change_authorized')
    )
    return (
        diagnostics.get('violated_contract') == 'sql_load_error_handling'
        or contract_preserves_scope
        or failure_payload.get('retry_type') in scope_preserving_retry_types
        or failure_payload.get('error_type') in scope_preserving_error_types
    )


def _validate_analysis_scope_against_execution_history(
    *,
    previous_executions: list[Any],
    current_used_datasource_ids: list[int],
    current_code: str,
    selected_datasource_snapshot: list[dict[str, Any]] | None = None,
) -> RetryResult | None:
    """Keep the last failed analysis as repair anchor across intervening Probe calls."""
    try:
        current_tree = ast.parse(current_code or '')
    except SyntaxError:
        return None
    if _extract_static_string_assignment(current_tree, 'execution_intent') != 'analysis':
        return None

    for previous in previous_executions:
        try:
            previous_tree = ast.parse(getattr(previous, 'generated_code', '') or '')
        except SyntaxError:
            previous_tree = None
        if (
            previous_tree is None
            or _extract_static_string_assignment(previous_tree, 'execution_intent') != 'analysis'
        ):
            continue
        try:
            payload = json.loads(getattr(previous, 'result_payload_json', '') or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not _failure_requires_scope_preservation(payload):
            continue
        try:
            previous_used_ids = json.loads(
                getattr(previous, 'used_datasource_ids_json', '') or '[]'
            )
        except (TypeError, ValueError):
            previous_used_ids = []
        diagnostics = payload.get('diagnostics')
        diagnostics = dict(diagnostics) if isinstance(diagnostics, dict) else {}
        inherited_scope_anchor = (
            _normalize_used_datasource_ids(
                diagnostics.get('previous_used_datasource_ids')
            )
            if diagnostics.get('violated_contract')
            == 'analysis_scope_regression_after_technical_repair'
            else []
        )
        if inherited_scope_anchor:
            # This execution was rejected precisely because its generated code
            # had already lost scope. Keep carrying the pre-regression anchor;
            # the rejected code cannot redefine the next repair's authority.
            previous_used_ids = inherited_scope_anchor
        referenced_ids = _statically_referenced_datasource_ids(
            getattr(previous, 'generated_code', '') or '',
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        if selected_datasource_snapshot and not referenced_ids:
            # A rejected attempt with declarations but no data-access implementation
            # cannot become the semantic scope anchor for later repairs.
            continue
        if referenced_ids and not inherited_scope_anchor:
            previous_used_ids = [
                datasource_id
                for datasource_id in _normalize_used_datasource_ids(previous_used_ids)
                if datasource_id in referenced_ids
            ]
        return _validate_scope_preserving_technical_repair(
            previous_used_datasource_ids=_normalize_used_datasource_ids(previous_used_ids),
            current_used_datasource_ids=current_used_datasource_ids,
            previous_failure_payload=payload,
        )
    return None


def _validate_probe_observation_provenance(
    *,
    previous_executions: list[Any],
    current_code: str,
) -> RetryResult | None:
    """Keep Probe observations diagnostic until formal analysis recomputes them."""
    try:
        tree = ast.parse(current_code or "")
    except SyntaxError:
        return None
    if _extract_static_string_assignment(tree, "execution_intent") != "analysis":
        return None

    observed_values: dict[str, float] = {}
    for previous in previous_executions:
        try:
            payload = json.loads(
                getattr(previous, "result_payload_json", "") or "{}"
            )
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        signal = str(payload.get("retry_type") or payload.get("error_type") or "")
        if signal != "probe_feedback":
            continue
        diagnostics = payload.get("diagnostics")
        observations = (
            diagnostics.get("query_observations")
            if isinstance(diagnostics, dict)
            else None
        )
        for observation in observations or []:
            if not isinstance(observation, dict):
                continue
            columns = observation.get("columns") or []
            samples = observation.get("value_samples") or []
            if len(columns) != len(samples):
                continue
            for column, sample in zip(columns, samples):
                text = str(sample or "").strip().replace(",", "")
                if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
                    continue
                observed_values[str(column or "").strip()] = float(text)
        if observed_values:
            break
    if not observed_values:
        return None

    rendered_names = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.FormattedValue)
        and isinstance(node.value, ast.Name)
    }
    violations: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or isinstance(value.value, bool):
            continue
        if not isinstance(value.value, (int, float)):
            continue
        targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in rendered_names:
                continue
            observed = observed_values.get(target.id)
            if observed is None or not math.isclose(
                float(value.value), observed, rel_tol=0.0, abs_tol=1e-12
            ):
                continue
            violations.append({
                "variable": target.id,
                "literal_value": value.value,
                "source": "probe_feedback.query_observations",
                "line": int(getattr(node, "lineno", 0) or 0),
            })

    if not violations:
        return None
    return request_retry(
        retry_type="result_contract_error",
        message=(
            "Formal analysis copied diagnostic Probe observations into report metrics "
            "without recomputing them from a formal query."
        ),
        diagnostics={
            "violated_contract": "probe_observation_used_as_formal_metric",
            "metrics": violations,
        },
        repair_instructions=[
            "Keep Probe observations only for choosing verified fields, values, and query paths.",
            "Requery and recompute every final report, chart, and table metric inside execution_intent='analysis'.",
            "Replace each diagnosed literal assignment with a value derived from the formal query result; keep the analysis scope and other artifacts unchanged.",
        ],
    )


def _validate_technical_repair_scope_from_history(
    *,
    turn_id: int,
    current_execution_id: int,
    current_used_datasource_ids: list[int],
    current_code: str,
    selected_datasource_snapshot: list[dict[str, Any]] | None = None,
) -> RetryResult | None:
    if turn_id <= 0 or current_execution_id <= 0:
        return None

    from config.database import SessionLocal
    from model import InsightNsExecution, InsightNsTurn

    session = SessionLocal()
    try:
        turn = session.query(InsightNsTurn).filter(InsightNsTurn.id == turn_id).first()
        previous_query = session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id,
            InsightNsExecution.id < current_execution_id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.execution_status != 'success',
        )
        if turn is not None and turn.started_at is not None:
            previous_query = previous_query.filter(InsightNsExecution.created_at >= turn.started_at)
        previous_executions = previous_query.order_by(
            InsightNsExecution.id.desc()
        ).limit(20).all()
        provenance_retry = _validate_probe_observation_provenance(
            previous_executions=previous_executions,
            current_code=current_code,
        )
        if provenance_retry is not None:
            return provenance_retry
        return _validate_analysis_scope_against_execution_history(
            previous_executions=previous_executions,
            current_used_datasource_ids=current_used_datasource_ids,
            current_code=current_code,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
    finally:
        session.close()


def _turn_has_prior_time_range_guard_signal(turn_id: int, current_execution_id: int) -> bool:
    """Whether this turn already hit no-data/probe feedback before the current execution."""
    if turn_id <= 0:
        return False

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        query = session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id,
            InsightNsExecution.id != current_execution_id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.execution_status != 'success',
        )
        if current_execution_id > 0:
            query = query.filter(InsightNsExecution.id < current_execution_id)

        previous_failures = query.order_by(InsightNsExecution.created_at.desc()).limit(12).all()
        for execution in previous_failures:
            error_message = str(getattr(execution, 'error_message', '') or '').lower()
            payload_text = str(getattr(execution, 'result_payload_json', '') or '')
            if 'no_data_found' in error_message or 'probe_feedback' in error_message:
                return True

            try:
                payload = json.loads(payload_text or '{}')
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                continue

            signal_values = {
                str(payload.get('retry_type') or ''),
                str(payload.get('error_type') or ''),
                str(payload.get('retry_reason_code') or ''),
            }
            if {'no_data_found', 'probe_feedback'} & signal_values:
                return True
    finally:
        session.close()

    return False


def _extract_probe_no_data_signal(execution: Any) -> str:
    payload_text = str(getattr(execution, 'result_payload_json', '') or '')
    try:
        payload = json.loads(payload_text or '{}')
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        for key in ('retry_type', 'error_type', 'retry_reason_code'):
            value = str(payload.get(key) or '').strip()
            if value in PROBE_NO_DATA_RETRY_TYPES:
                return value

    error_message = str(getattr(execution, 'error_message', '') or '')
    classified = _classify_execution_error(None, error_message)
    if classified in PROBE_NO_DATA_RETRY_TYPES:
        return classified
    lowered = error_message.lower()
    for signal in PROBE_NO_DATA_RETRY_TYPES:
        if signal in lowered:
            return signal
    return ''


def _count_probe_no_data_failures(turn_id: int, current_execution_id: int) -> int:
    if turn_id <= 0:
        return 0

    from config.database import SessionLocal
    from model import InsightNsExecution

    session = SessionLocal()
    try:
        query = session.query(InsightNsExecution).filter(
            InsightNsExecution.turn_id == turn_id,
            InsightNsExecution.is_deleted == 0,
            InsightNsExecution.execution_status != 'success',
        )
        if current_execution_id > 0:
            query = query.filter(InsightNsExecution.id <= current_execution_id)
        executions = query.order_by(InsightNsExecution.created_at.asc()).all()
        return sum(1 for execution in executions if _extract_probe_no_data_signal(execution))
    finally:
        session.close()


def _apply_probe_no_data_retry_limit(
    *,
    turn_id: int,
    current_execution_id: int,
    error_type: str,
    repair_instructions: list[str],
    diagnostics: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    if error_type not in PROBE_NO_DATA_RETRY_TYPES:
        return repair_instructions, diagnostics

    count = _count_probe_no_data_failures(turn_id=turn_id, current_execution_id=current_execution_id)
    if count < PROBE_NO_DATA_RETRY_LIMIT:
        limit_diagnostics = dict(diagnostics or {})
        limit_diagnostics.update({
            'probe_no_data_retry_count': count,
            'probe_no_data_retry_limit': PROBE_NO_DATA_RETRY_LIMIT,
            'probe_no_data_limit_reached': False,
        })
        return repair_instructions, limit_diagnostics

    limit_diagnostics = dict(diagnostics or {})
    limit_diagnostics.update({
        'probe_no_data_retry_count': count,
        'probe_no_data_retry_limit': PROBE_NO_DATA_RETRY_LIMIT,
        'probe_no_data_limit_reached': True,
    })
    limit_instructions = [
        f'本轮 probe_feedback/no_data_found 已达到 {count}/{PROBE_NO_DATA_RETRY_LIMIT} 次上限。',
        '不要再生成 probe 代码，不要继续猜测时序查询语法、字段名或表名，也不要继续扩大查询到其他数据源。',
        '下一次必须切换为 execution_intent = "analysis"，保持用户原始问题和原始时间范围，调用 save_analysis_result(...) 以无数据或数据源暂不可分析的结论收口。',
        '如果已经尝试过多个数据源，请至少返回一个表格汇总已尝试的数据源、使用的查询口径和失败原因；used_datasource_ids 填写实际尝试过的数据源 ID。',
    ]
    return [*limit_instructions, *repair_instructions], limit_diagnostics


def _build_repair_instructions(error_type: str) -> list[str]:
    """根据错误类型生成定向修复建议。"""
    common_instructions = [
        '重新生成完整可执行代码，不要只修改局部片段。',
        '正式分析代码应调用 save_analysis_result(...) 并把返回值赋给 result；探测或结构化反馈代码应调用 request_retry(...) 并把返回值赋给 result。',
        '修复技术错误时必须保持当前业务问题和 analysis_semantic_context 的事实边界不变；不得改用 unsupported_field_bindings 已排除的字段或未确认关系。可选拆解无法可靠实现时，删除该可选拆解，不要用相邻代理指标补齐。',
        '如果当前修正思路与上一次相同且未解决错误，请更换实现方式后再重试。',
    ]

    specific_instructions = {
        'syntax_error': [
            '先检查引号、括号、缩进和多行字符串是否闭合，再重新生成代码。',
            '构造分析报告时优先使用 report_sections/report_lines 加 "\\n\\n".join(...)，避免手写长字符串拼接。',
        ],
        'missing_dependency': [
            '不要依赖执行环境中未注入或未安装的第三方模块。',
            '优先使用当前工具已提供的内置辅助函数，而不是额外 import 新库。',
        ],
        'data_source_not_found': [
            '检查数据源标识、文件路径、表名或接口地址是否与当前数据源上下文一致。',
            '不要凭空假设新的文件路径或表名。',
            '这类错误表示必要数据源不可访问或标识不匹配，不属于 no_data_found；不要调用 raise_no_data_error(...) 包装文件不存在、表不存在或接口不可访问。',
            '如果已原样使用当前数据源上下文中的标识但仍不可访问，应调用 request_retry(retry_type="data_source_unavailable", ...) 返回结构化反馈，或让本轮失败，不要伪装成空数据分析。',
        ],
        'schema_or_column_mismatch': [
            '字段选择必须以 metadata_schema.properties 中提供的真实字段名为准。',
            '重新检查 DataFrame 列名后再生成过滤、聚合和展示逻辑。',
            '如果过滤后或关联后的结果为空，不要继续修改字段名；应先判断是否为空，并调用 raise_no_data_error(reason=..., detail_lines=[...]) 把信息返回给上层重试。',
        ],
        'undefined_name': [
            '重新检查变量名、函数名和 import 是否一致，避免引用未定义对象。',
            '尽量复用前面已经定义好的中间变量，不要混用多个命名。',
        ],
        'data_type_or_time_mismatch': [
            '先统一时间列或比较字段的数据类型，再进行过滤或比较。',
            '遇到相对日期或时区处理时，优先考虑使用 get_day_range() 并保证两侧都是可比较的带时区时间对象。',
            '如果目标时间窗口没有命中数据，不要输出空图表或直接调用 save_empty_analysis_result() 收口；应调用 raise_no_data_error(reason=..., detail_lines=[...]) 把信息返回给上层重试。',
        ],
        'no_data_found': [
            '当前正式查询未命中数据，下一版优先生成独立 probe：声明 execution_intent = "probe"，严格依据 diagnostics.probe_targets 中的真实查询实现、数据源与已应用筛选进行探测，不要扩散到其他已选数据源。',
            'Probe 必须执行轻量只读查询，并把真实行数、枚举、格式或时间覆盖写入 request_retry(..., diagnostics=...)；不要只返回 SQL 文本、预期值或检查计划。',
            '如果错误信息里包含 unknown tag 或 no results，表示当前时序数据源在时序库中没有可查询的数据或尚未初始化；将该数据源从本轮分析中剔除。如果用户问题涉及多个互相独立的数据源，继续分析其他数据源；只有所有相关数据源都无数据时，才以整体无数据收口。',
            '请先复核 SQL、时间范围、筛选条件、JOIN 条件或聚合口径是否过严，再生成下一版完整代码。',
            '如果用户问题里有明确时间范围，必须保持原始 query_constraints.time_range 不变；探测得到的数据覆盖范围只能用于解释无数据原因，不能替换成正式分析的时间范围。',
            '在数据加载、过滤、关联、聚合完成后，必须先检查 DataFrame 是否为空；只要为空，就调用 raise_no_data_error(reason=..., detail_lines=[...])，不要继续生成空图表或空结果产物。',
            'reason 要直接说明是哪一步没有命中数据，detail_lines 可补充当前时间范围、筛选条件、关联键或中间行数，方便上层感知后继续重试。',
        ],
        'probe_feedback': [
        '当前反馈是数据探测结果，不是最终分析报告；diagnostics 只用于确认字段、候选值、时间边界和查询路径，最终报告、图表和表格中的全部数值必须在 execution_intent="analysis" 中重新查询并计算，不能复制探测值作为正式指标。',
            'Probe 只纠正字段取值、时间边界和访问实现，不会授权使用 analysis_semantic_context 中已明确排除的事实字段或未确认关系。',
            '如果用户问题里有明确时间范围，下一版 analysis 只能保留原始 query_constraints.time_range；diagnostics 里的数据覆盖范围不能被写回 query_constraints.time_range。',
            '如果探测确认用户明确时间范围内无数据，应返回无数据反馈或无数据结论，不要为了命中数据改查其他时间范围。',
            '下一版代码应切换为 execution_intent = "analysis"，并调用 save_analysis_result(...) 返回最终分析结果。',
            '不要再次只输出探测报告；如果候选值仍有歧义，再返回 request_retry(retry_type="probe_feedback", ...)。',
        ],
        'data_source_unavailable': [
            '当前反馈表示必要数据源不可访问或标识无法解析，不是筛选条件未命中数据。',
            '不要调用 raise_no_data_error(...)，也不要继续生成空图表或空报告伪装成功。',
            '只能原样使用当前数据源上下文提供的文件路径、表名或接口地址；如果仍不可访问，应保持 data_source_unavailable 语义并让上层失败收口。',
        ],
        'missing_structured_artifact': [
            '当前代码已经生成分析报告，但没有生成 charts 或 tables 结构化产物。',
            '下一版代码应保留已有统计口径、筛选条件和核心结论，至少补充一个 charts 或 tables。',
            '趋势、对比、TopN 等场景优先生成 charts；单指标或明细汇总可以生成 tables。',
        ],
        'missing_report': [
            '当前结果缺少非空 analysis_report；请先构造完整 Markdown 分析报告，再调用 save_analysis_result(...)。',
            '不要只返回图表或表格，最终结果必须同时包含可读报告和结构化产物。',
        ],
        'incomplete_result': [
            '当前执行结果不完整；请根据 diagnostics 补齐缺失的报告、图表或表格后重新生成完整代码。',
            '不要把中间探测信息包装成最终分析报告。',
        ],
        'contract_error': [
            '当前代码主动返回了契约问题反馈；请根据 message、diagnostics 和 repair_instructions 重新生成完整代码。',
            '如果本轮是探测，请返回 request_retry(...)；如果本轮是正式分析，请返回 save_analysis_result(...)。',
        ],
        'chart_contract_error': [
            '如果错误提示 bar/line 存在 duplicate plotting keys，必须自行按业务语义选择一种修复：先聚合到每个绘图键一行、补充 series_field 使 (category_field, series_field) 唯一，或构造唯一复合类别；禁止丢弃重复行或依赖后写覆盖前写。',
            '当前失败是图表产物契约错误，不是数据查询失败；不要改成 no_data_found，也不要只输出自然语言报告。',
            '下一版禁止继续使用 matplotlib/base64 图片、手写 chart_spec 或自造 chart_type/chart_kind 结构。',
            '必须优先用 build_chart_result(...)、build_multi_metric_chart_result(...) 或 build_chart_suite(...) 生成 charts；bar/line 需要 category_field 和 value_field，多指标柱图需要 category_field 和 value_fields，pie 需要 category_field 和 value_field，scatter 需要 x_field 和 y_field，boxplot 用于异常值/分布/四分位数场景，需要 category_field 和 value_field。',
            'charts 应直接等于 helper 的返回结果，例如 charts = [build_chart_result(...)]、charts = [build_multi_metric_chart_result(...)] 或 charts = build_chart_suite(...)，然后调用 save_analysis_result(analysis_report=..., charts=charts, tables=...)。',
            '如果确实不适合生成图表，也必须用 tables 返回结构化汇总，不能让 charts 和 tables 同时为空。',
        ],
        'result_contract_error': [
            '如果错误与 charts/chart_spec/chart_document 有关，下一版不要手写图表 JSON，直接改用 build_chart_result(...)、build_multi_metric_chart_result(...) 或 build_chart_suite(...)。',
            '最终必须产出完整 analysis_report，并调用 save_analysis_result(analysis_report=..., charts=[...], tables=[...])。',
            '不要遗漏 result 变量，也不要返回空的 analysis_report 或 charts/tables 同时为空的结构化结果。',
        ],
        'missing_query_constraints': [
            '正式 analysis 代码必须在顶部声明 query_constraints；probe 代码不要生成 query_constraints。',
            '先描述与当前问题相关的 time_range、target_filters 和 aggregation，再生成 SQL 或 pandas/DataFrame 筛选实现。',
        ],
        'invalid_query_constraints': [
            '请先修正 query_constraints 结构，再重新生成 SQL 或 pandas/DataFrame 实现。',
            'target_filters 中每个对象至少要包含 semantic_role、value 和 match_mode；其中 match_mode 只能是 exact 或 fuzzy。',
        ],
        'query_constraint_violation': [
            '保持 query_constraints 不变，重写 SQL / pandas / 文件筛选实现，使其符合声明的时间范围、目标筛选和聚合粒度。',
            'analysis 代码应先生成 SQL 或过滤表达式，再调用 validate_query_constraints(...)；只有在其返回 None 时才能继续正式分析。',
            '当 match_mode = "exact" 时，不要把同一个值实现成 LIKE、ILIKE、contains 或 str.contains(...)；如果需要先探测候选值，请切换为 execution_intent = "probe"。',
        ],
        'library_api_signature_mismatch': [
            '如果报错来自 pyecharts/matplotlib 或图表参数，优先改用 build_chart_result(...)、build_multi_metric_chart_result(...) 或 build_chart_suite(...)，不要继续试底层图表 API。',
            '当前错误属于库函数签名不兼容；请只修正报错位置的非法参数，不要重写与该错误无关的数据处理和图表逻辑。',
            '如果某个 opts 或图表参数不被当前版本支持，请删除该非法参数，或改成更基础、更稳定的默认写法。',
            '优先保留图表结构、数据处理和报告逻辑，只对报错的 API 调用做最小修改。',
        ],
        'resource_exhausted': [
            '当前失败更像是大文件加载或内存耗尽，不要再一次性把整份本地文件完整加载到 pandas 内存中。',
            '如果是本地文件，请改用 load_local_file_low_memory(...) 分批读取；可以先取第一个小批次确认列名、类型、时间列和候选取值，再继续正式分析。',
            '优先只读取必要列，并在每个批次内完成过滤、聚合、TopN 累计或有限明细截取，不要把所有批次重新拼成一个超大 DataFrame。',
            'CSV 和 Excel 都优先走 load_local_file_low_memory(...)；其中 .xlsx/.xlsm 支持流式批量读取，.xls 只能 best-effort 低内存处理。',
            '如果用户要的是聚合、趋势、分布、TopN 或有限明细，就在批次循环中累计最终结果，只保留用于图表和报告生成的小结果。',
            '如果旧版 .xls 文件在当前环境仍无法安全低内存处理，应明确返回受限说明或建议转换为 .xlsx/CSV，不要继续反复 OOM 重试。',
        ],
        'query_result_too_large': [
            '当前查询结果超过单次加载上限。保持用户问题、筛选条件和总体统计口径不变，优先把过滤、JOIN、分组聚合和 TopN 下推到查询层，只加载报告、图表和有限明细真正需要的结果。',
            '如果业务结论确实依赖全部明细，使用稳定排序键配合 LIMIT/OFFSET 分页逐批处理，并在批次内完成累计聚合；不要把所有分页明细重新拼成一个超大 DataFrame。',
            '不要用无业务依据的 LIMIT 截断总体统计。只有 TopN、有限明细或代表性样本本来就是目标口径时才直接限制行数，并在对应产物中披露范围。',
            '修复查询范围时不要改写已经正确的报告结构、图表语义和其他查询；只调整导致超量的查询及其下游计算。',
        ],
        'runtime_error': [
            '优先查看 error_message 中的“错误定位”和 code_error_context.line_no/code_error_context.context_lines，直接修正生成代码中对应行附近的逻辑。',
            '如果 code_error_context 中给出了 traceback_excerpt，请结合最后一帧生成代码位置判断根因，不要只根据异常类型泛泛重写。',
            '如果错误发生在数据处理阶段，请先验证中间结果再进入图表和报告生成。',
        ],
        'execution_timeout': [
            '当前代码执行超时，说明存在无界循环、超大范围数据处理或阻塞式等待。',
            '请先缩小数据范围、减少无必要的明细拼接与循环处理，并确保所有外部请求都具备明确超时。',
            '如果当前分析使用的是表数据源，优先把时间过滤、字段裁剪、JOIN、GROUP BY、LIMIT 下推到 SQL 层完成，不要继续把大结果集加载到 pandas 后再 merge 或聚合。',
            '如果跨表 JOIN 或明细关联已经连续超时，请先退化成单表分析：优先完成主表趋势、分布和有限明细，再在分析报告中明确说明关联部分受查询成本限制暂未展开。',
            '如果需要展示明细，请先限制结果行数，再生成图表和分析报告。',
        ],
    }
    return [*specific_instructions.get(error_type, specific_instructions['runtime_error']), *common_instructions]


def _augment_repair_instructions(
    error_type: str,
    error_message: str,
    repair_instructions: list[str],
) -> list[str]:
    """Add facts learned from the concrete failure without changing analysis scope."""
    augmented = list(repair_instructions)
    message = str(error_message or '')
    if (
        error_type == 'runtime_error'
        and 'mysql query' in message.lower()
        and re.search(r'::\s*(?:int|integer|signed|decimal|numeric|float|double)\b', message, re.I)
    ):
        augmented = [
            '错误已确认发生在 MySQL 源查询：当前 SQL 使用了 PostgreSQL 的 ::type 强转语法。保持查询口径不变，改用 MySQL CAST(expression AS SIGNED/DECIMAL)；若字段是带单位文本，优先查询原字段后在 pandas 中解析，不能再次生成 ::type。',
            *augmented,
        ]
    return list(dict.fromkeys(augmented))


def _tool_error_message(
    *,
    tool_call_id: str,
    execution_id: int = 0,
    error_type: str,
    error_message: str,
    repair_instructions: list[str],
    error_signature: dict[str, Any] | None = None,
    code_error_context: dict[str, Any] | None = None,
    feedback_kind: str = '',
    retry_reason_code: str = '',
    diagnostics: dict[str, Any] | None = None,
    analysis_report: str = '',
    current_failed_code: str = '',
    previous_failure_messages: list[str] | None = None,
    previous_failure_hints: list[str] | None = None,
    stdout_text: str = '',
    stderr_text: str = '',
) -> ToolMessage:
    """构造统一的结构化工具错误反馈，供 Agent 循环继续修正。"""
    repair_instructions = _augment_repair_instructions(
        error_type,
        error_message,
        repair_instructions,
    )
    clipped_current_code = _clip_retry_context_text(current_failed_code, TOOL_ERROR_CURRENT_CODE_MAX_CHARS)
    clipped_error_message = _clip_retry_context_text(error_message, TOOL_ERROR_TEXT_MAX_CHARS)
    clipped_error_signature = dict(error_signature or {})
    if 'message' in clipped_error_signature:
        clipped_error_signature['message'] = _clip_retry_context_text(
            clipped_error_signature.get('message'),
            TOOL_ERROR_TEXT_MAX_CHARS,
        )
    clipped_code_error_context = dict(code_error_context or {})
    if clipped_code_error_context.get('traceback_excerpt'):
        clipped_code_error_context['traceback_excerpt'] = _clip_retry_context_text(
            clipped_code_error_context.get('traceback_excerpt'),
            TOOL_ERROR_CODE_CONTEXT_MAX_CHARS,
        )

    patch_ranges = clipped_code_error_context.get('suggested_patch_ranges')
    patch_ranges = patch_ranges if isinstance(patch_ranges, list) else []
    patch_available_error_types = {
        'chart_contract_error',
        'data_type_or_time_mismatch',
        'library_api_signature_mismatch',
        'runtime_error',
        'schema_or_column_mismatch',
        'undefined_name',
    }
    patch_required_error_types = {
        'data_type_or_time_mismatch',
        'library_api_signature_mismatch',
        'schema_or_column_mismatch',
        'undefined_name',
    }
    patch_available = bool(
        execution_id > 0
        and current_failed_code
        and patch_ranges
        and error_type in patch_available_error_types
    )
    patch_required = patch_available and error_type in patch_required_error_types

    payload = {
        'tool': 'execute_python',
        'status': 'failed',
        'execution_id': int(execution_id or 0),
        'error_type': error_type,
        'error_message': clipped_error_message,
        'error_message_truncated': clipped_error_message != (error_message or ''),
        'repair_instructions': _clip_retry_context_list(
            repair_instructions,
            max_items=8,
            max_chars=TOOL_ERROR_INSTRUCTION_MAX_CHARS,
        ),
        'error_signature': clipped_error_signature,
        'code_error_context': clipped_code_error_context,
        'minimal_patch_required': patch_required,
        'current_failed_code': clipped_current_code,
        'current_failed_code_truncated': clipped_current_code != (current_failed_code or ''),
        'previous_failure_messages': _clip_retry_context_list(
            previous_failure_messages,
            max_items=3,
            max_chars=TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS,
        ),
        'previous_failure_hints': _clip_retry_context_list(
            previous_failure_hints,
            max_items=3,
            max_chars=TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS,
        ),
        'previous_failure_count': len(previous_failure_messages or []),
    }
    if patch_available:
        payload['repair_target'] = {
            'base_execution_id': int(execution_id),
            'source_sha256': _build_analysis_logic_signature(current_failed_code),
            'line_count': len(str(current_failed_code).splitlines()),
            'mode': 'line_patch',
            'allowed_ranges': patch_ranges,
            'usage': (
                '仅当修复可完整落在 allowed_ranges 的语句边界内时，才令 code 为空并提交 '
                'base_execution_id 与 patches；涉及查询范围、整体契约或多处逻辑调整时提交完整 code。'
            ),
        }
    if feedback_kind:
        payload['feedback_kind'] = feedback_kind
    if retry_reason_code:
        payload['retry_reason_code'] = retry_reason_code
    if diagnostics:
        payload['diagnostics'] = diagnostics
    if analysis_report:
        payload['analysis_report'] = _clip_retry_context_text(analysis_report, TOOL_ERROR_TEXT_MAX_CHARS)
    if stdout_text:
        payload['stdout_text'] = _clip_retry_context_text(stdout_text, TOOL_ERROR_STDIO_MAX_CHARS)
    if stderr_text:
        payload['stderr_text'] = _clip_retry_context_text(stderr_text, TOOL_ERROR_STDIO_MAX_CHARS)

    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=tool_call_id,
    )


def _tool_success_message(
    *,
    tool_call_id: str,
    analysis_report: str,
    charts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
) -> ToolMessage:
    """
    构造成功的工具结果反馈。

    这里不再把完整 charts/tables 配置原样塞回模型上下文，
    避免结构化图表配置把后续一轮模型请求撑爆。
    完整结果已经持久化到 execution.result_payload_json 中，
    后续由 invoker 基于最新成功执行记录回填真正结果。
    """
    payload = {
        'tool': 'execute_python',
        'status': 'success',
        'analysis_report': (analysis_report or '').strip(),
        'chart_count': len(charts or []),
        'table_count': len(tables or []),
        'chart_titles': [
            str(item.get('title') or '').strip()
            for item in (charts or [])
            if isinstance(item, dict) and str(item.get('title') or '').strip()
        ][:5],
        'table_titles': [
            str(item.get('title') or '').strip()
            for item in (tables or [])
            if isinstance(item, dict) and str(item.get('title') or '').strip()
        ][:5],
    }
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=tool_call_id,
    )


def _terminal_tool_success(message: ToolMessage) -> Command:
    """Finish the agent graph after a fully validated analysis result."""
    return Command(update={"messages": [message]}, goto=END)


def run_generated_code_locally(
    *,
    conversation_id: int,
    turn_id: int,
    username: str,
    code: str,
    title: str = '',
    description: str = '',
    used_datasource_ids: list[int] | None = None,
    runtime_user_context: dict[str, Any] | None = None,
    analysis_task_id: int | None = None,
    parent_execution_id: int | None = None,
    execution_source: str = 'manual_param_rerun',
    analysis_parameters_schema: dict[str, Any] | None = None,
    analysis_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run persisted generated code without invoking the LLM."""
    from config.config import Config

    start_time = time.time()
    normalized_used_datasource_ids = _normalize_used_datasource_ids(used_datasource_ids)
    runtime_payload = dict(runtime_user_context or {})
    runtime_payload['analysis_parameters_override'] = dict(analysis_parameters or {})
    execution_id = _create_execution_record(
        conversation_id=conversation_id,
        turn_id=turn_id,
        tool_call_id='local_rerun',
        code=code,
        title=title,
        description=description,
        used_datasource_ids=normalized_used_datasource_ids,
        analysis_task_id=analysis_task_id,
        parent_execution_id=parent_execution_id,
        execution_source=execution_source,
        analysis_parameters_schema=analysis_parameters_schema,
        analysis_parameters=analysis_parameters,
    )

    timeout_seconds = max(int(getattr(Config, 'PYTHON_EXEC_TIMEOUT_SECONDS', 90) or 90), 1)
    result_queue: mp.Queue = mp.Queue()
    process = mp.Process(
        target=_execute_generated_code_worker,
        args=(code, username or 'anonymous', runtime_payload, result_queue),
        daemon=True,
    )
    worker_result: dict[str, Any] | None = None
    process.start()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining_seconds = max(deadline - time.time(), 0)
        try:
            worker_result = result_queue.get(timeout=min(0.5, remaining_seconds))
            break
        except Empty:
            if not process.is_alive():
                break

    if worker_result is None and process.is_alive():
        process.terminate()
        process.join(5)
        execution_seconds = int((time.time() - start_time) * 1000)
        error_message = f'代码执行超时，已超过 {timeout_seconds} 秒'
        _update_execution_record(
            execution_id,
            execution_status='failed',
            execution_seconds=execution_seconds,
            error_message=error_message,
            analysis_parameters_schema=analysis_parameters_schema,
            analysis_parameters=analysis_parameters,
        )
        result_queue.close()
        result_queue.join_thread()
        return {'execution_id': execution_id, 'status': 'failed', 'error_message': error_message}

    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    process_exitcode = process.exitcode
    process.close()
    result_queue.close()
    result_queue.join_thread()

    if worker_result is None:
        error_type, error_message = _classify_process_exit(process_exitcode)
        _update_execution_record(
            execution_id,
            execution_status='failed',
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=error_message,
            analysis_parameters_schema=analysis_parameters_schema,
            analysis_parameters=analysis_parameters,
        )
        return {'execution_id': execution_id, 'status': 'failed', 'error_type': error_type, 'error_message': error_message}

    result_file_path = str(worker_result.get('result_file_path') or '').strip()
    try:
        worker_payload = _read_worker_result_payload(result_file_path) if result_file_path else worker_result
    finally:
        _remove_worker_result_payload(result_file_path)

    stdout_text = _sanitize_tool_output(worker_payload.get('stdout_text', ''))
    stderr_text = _sanitize_tool_output(worker_payload.get('stderr_text', ''))
    execution_seconds = int((time.time() - start_time) * 1000)

    if worker_payload.get('status') == 'error':
        error_message = worker_payload.get('error_message') or '代码执行失败'
        error_type = worker_payload.get('error_type') or 'runtime_error'
        result_payload_json = json.dumps({
            'status': 'failed',
            'error_type': error_type,
            'error_message': error_message,
            'error_signature': worker_payload.get('error_signature') or _extract_error_signature(error_type, error_message),
            'code_error_context': worker_payload.get('code_error_context') or {},
            'repair_instructions': worker_payload.get('repair_instructions') or _build_repair_instructions(error_type),
        }, ensure_ascii=False)
        _update_execution_record(
            execution_id,
            execution_status='failed',
            result_payload_json=result_payload_json,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=execution_seconds,
            error_message=error_message,
            analysis_parameters_schema=analysis_parameters_schema,
            analysis_parameters=analysis_parameters,
        )
        return {
            'execution_id': execution_id,
            'status': 'failed',
            'error_type': error_type,
            'error_message': error_message,
            'stdout_text': stdout_text,
            'stderr_text': stderr_text,
        }

    exec_result = _deserialize_exec_result(worker_payload.get('exec_result') or {})
    if not isinstance(exec_result, StructuredResult):
        error_message = '生成的代码未按模板产出 result'
        _update_execution_record(
            execution_id,
            execution_status='invalid',
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=execution_seconds,
            error_message=error_message,
            analysis_parameters_schema=analysis_parameters_schema,
            analysis_parameters=analysis_parameters,
        )
        return {'execution_id': execution_id, 'status': 'invalid', 'error_message': error_message}

    result_schema = exec_result.analysis_parameters_schema or analysis_parameters_schema or {}
    result_parameters = exec_result.analysis_parameters or analysis_parameters or {}
    _update_execution_record(
        execution_id,
        execution_status='success',
        analysis_report=exec_result.analysis_report,
        result_payload_json=exec_result.model_dump_json(),
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        execution_seconds=execution_seconds,
        used_datasource_ids=normalized_used_datasource_ids,
        analysis_parameters_schema=result_schema,
        analysis_parameters=result_parameters,
    )
    return {
        'execution_id': execution_id,
        'status': 'success',
        'analysis_report': exec_result.analysis_report,
        'charts': exec_result.charts,
        'tables': exec_result.tables,
        'analysis_parameters_schema': result_schema,
        'analysis_parameters': result_parameters,
        'stdout_text': stdout_text,
        'stderr_text': stderr_text,
        'execution_seconds': execution_seconds,
    }


@tool(
    description=(
        "执行一份独立闭环的 Python 洞察分析或探测代码。analysis 必须在本次调用内生成完整产物并保存；"
        "probe 必须返回结构化探测反馈；调用之间不共享运行时变量。"
        "局部技术错误可用失败反馈中的 base_execution_id 和行级 patches 修复，后端会保留其余完整代码。"
    ),
    args_schema=ExePythonCodeInput,
)
def execute_python(
    runtime: ToolRuntime[CustomContext],
    code: str = '',
    title: str = '',
    description: str = "",
    used_datasource_ids: list[int] | None = None,
    base_execution_id: int = 0,
    patches: list[SourceCodePatch] | None = None,
) -> Optional[ToolMessage | Command]:
    """
    执行模型生成的 Python 分析代码。

    需要注意：
    - 这里保持当前对外工具契约不变。
    - 执行历史仍会内部持久化到 `insight_ns_execution`。
    """
    emit = _get_stream_emitter()
    start_time = time.time()
    _raise_if_runtime_task_cancelled(runtime.context)
    conversation_id = int(getattr(runtime.context, 'conversation_id', 0) or 0)
    turn_id = int(getattr(runtime.context, 'turn_id', 0) or 0)
    try:
        code, normalized_used_datasource_ids, parent_execution_id, execution_source = _resolve_execute_python_source(
            conversation_id=conversation_id,
            turn_id=turn_id,
            code=code,
            used_datasource_ids=used_datasource_ids,
            base_execution_id=base_execution_id,
            patches=patches,
        )
    except (TypeError, ValueError) as exc:
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            error_type='contract_error',
            error_message=str(exc),
            repair_instructions=[
                '首次执行请提交完整 code；局部修复请提交 code=""、失败的 base_execution_id 和非重叠 patches。',
                'patches 的 start_line/end_line 必须引用错误反馈 code_error_context 中的原始行号。',
            ],
        )
    execution_id = _create_execution_record(
        conversation_id=conversation_id,
        turn_id=turn_id,
        tool_call_id=getattr(runtime, 'tool_call_id', '') or '',
        code=code,
        title=title,
        description=description,
        used_datasource_ids=normalized_used_datasource_ids,
        analysis_task_id=getattr(runtime.context, 'analysis_task_id', None),
        parent_execution_id=parent_execution_id or None,
        execution_source=execution_source,
    )
    _raise_if_runtime_task_cancelled(runtime.context, execution_id=execution_id, start_time=start_time)
    execution_log_context = {
        'username': getattr(runtime.context, 'username', 'anonymous'),
        'namespace_id': int(getattr(runtime.context, 'namespace_id', 0) or 0),
        'conversation_id': int(getattr(runtime.context, 'conversation_id', 0) or 0),
        'turn_id': int(getattr(runtime.context, 'turn_id', 0) or 0),
        'execution_id': execution_id,
    }

    task_title = title or '数据分析任务'
    emit(
        'status',
        stage='tool_start',
        level='info',
        tool='execute_python',
        message=f"正在执行分析代码：{task_title}",
    )
    with logger.context(**execution_log_context):
        logger.info(f"开始执行：{task_title}")
        logger.info(f"代码：\n```python\n{code}\n```")
        logger.info("代码已提交到本地执行器。")
    emit(
        'status',
        stage='tool_running',
        level='info',
        tool='execute_python',
        message='分析代码已提交到本地执行器。',
    )

    expected_time_range = dict(getattr(runtime.context, 'expected_time_range', {}) or {})
    time_range_guard_active = _turn_has_prior_time_range_guard_signal(
        turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
        current_execution_id=execution_id,
    )
    selected_datasource_snapshot = _get_runtime_selected_datasource_snapshot(runtime.context)
    contract_retry = _validate_tool_used_datasource_ids(
        context=runtime.context,
        used_datasource_ids=normalized_used_datasource_ids,
    )
    if contract_retry is None:
        contract_retry = _validate_generated_code_contract(code)
    if contract_retry is None:
        contract_retry = _validate_technical_repair_scope_from_history(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
            current_used_datasource_ids=normalized_used_datasource_ids,
            current_code=code,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
    if contract_retry is None:
        contract_retry = _validate_analysis_relationship_contract(
            runtime.context,
            code,
        )
    if contract_retry is None:
        contract_retry = _validate_explicit_time_range_guard(
            code=code,
            expected_time_range=expected_time_range,
            time_range_guard_active=time_range_guard_active,
        )
    if contract_retry is not None:
        retry_repair_instructions = contract_retry.repair_instructions or _build_repair_instructions(contract_retry.retry_type)
        failure_memory = _build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            analysis_report=contract_retry.analysis_report,
            result_payload_json=contract_retry.model_dump_json(),
            stdout_text='',
            stderr_text='',
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=contract_retry.message,
            used_datasource_ids=normalized_used_datasource_ids,
        )
        with logger.context(**execution_log_context):
            logger.warning(contract_retry.message)
        emit(
            'status',
            stage='tool_retry',
            level='warning',
            tool='execute_python',
            message=FRONTEND_TOOL_RETRY_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=contract_retry.retry_type,
            error_message=contract_retry.message,
            repair_instructions=retry_repair_instructions,
            error_signature=_build_retry_error_signature(contract_retry),
            feedback_kind='retryable_feedback',
            retry_reason_code=contract_retry.retry_type,
            diagnostics=contract_retry.diagnostics,
            code_error_context=contract_retry.code_error_context,
            analysis_report=contract_retry.analysis_report,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
        )

    from config.config import Config

    timeout_seconds = max(int(getattr(Config, 'PYTHON_EXEC_TIMEOUT_SECONDS', 90) or 90), 1)
    result_queue: mp.Queue = mp.Queue()
    runtime_user_context = _serialize_runtime_user_context(runtime.context)
    runtime_user_context['time_range_guard_active'] = time_range_guard_active
    runtime_user_context['confirmed_no_data_result'] = (
        _count_probe_no_data_failures(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=max(execution_id - 1, 0),
        )
        >= PROBE_NO_DATA_RETRY_LIMIT
    )
    process = mp.Process(
        target=_execute_generated_code_worker,
        args=(code, getattr(runtime.context, 'username', 'anonymous'), runtime_user_context, result_queue),
        daemon=True,
    )

    worker_result: dict[str, Any] | None = None
    stdout_text = ''
    stderr_text = ''
    exec_result: Any = None
    process.start()
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if _is_runtime_task_cancelled(runtime.context):
            if process.is_alive():
                process.terminate()
                process.join(5)
            _update_execution_record(
                execution_id,
                execution_status='failed',
                stdout_text='',
                stderr_text='',
                execution_seconds=int((time.time() - start_time) * 1000),
                error_message=CANCELLED_MESSAGE,
                content_enabled=0,
            )
            emit(
                'status',
                stage='cancelled',
                level='warning',
                tool='execute_python',
                message=CANCELLED_MESSAGE,
            )
            raise AnalysisTaskCancelled(CANCELLED_MESSAGE)
        remaining_seconds = max(deadline - time.time(), 0)
        try:
            worker_result = result_queue.get(timeout=min(0.5, remaining_seconds))
            break
        except Empty:
            if not process.is_alive():
                break

    if worker_result is None and process.is_alive():
        process.terminate()
        process.join(5)
        error_message = f'代码执行超时，已超过 {timeout_seconds} 秒'
        error_type = 'execution_timeout'
        repair_instructions = _build_repair_instructions(error_type)
        error_signature = _extract_error_signature(error_type, error_message)
        failure_memory = _build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            stdout_text='',
            stderr_text='',
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=error_message,
        )
        with logger.context(**execution_log_context):
            logger.error(error_message)
        emit(
            'status',
            stage='tool_error',
            level='error',
            tool='execute_python',
            message=FRONTEND_TOOL_TIMEOUT_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=error_type,
            error_message=error_message,
            repair_instructions=repair_instructions,
            error_signature=error_signature,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
        )

    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    process_exitcode = process.exitcode
    process.close()
    result_queue.close()
    result_queue.join_thread()
    _raise_if_runtime_task_cancelled(runtime.context, execution_id=execution_id, start_time=start_time)

    if worker_result is None:
        error_type, error_message = _classify_process_exit(process_exitcode)
        repair_instructions = _build_repair_instructions(error_type)
        error_signature = _extract_error_signature(error_type, error_message)
        failure_memory = _build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            stdout_text='',
            stderr_text='',
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=error_message,
        )
        with logger.context(**execution_log_context):
            logger.error(error_message)
        emit(
            'status',
            stage='tool_error',
            level='error',
            tool='execute_python',
            message=FRONTEND_TOOL_ERROR_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=error_type,
            error_message=error_message,
            repair_instructions=repair_instructions,
            error_signature=error_signature,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
        )

    result_file_path = str(worker_result.get('result_file_path') or '').strip()
    try:
        worker_payload = _read_worker_result_payload(result_file_path) if result_file_path else worker_result
    except Exception as exc:
        error_message = f'代码执行结果回读失败: {exc}'
        error_type = 'runtime_error'
        repair_instructions = _build_repair_instructions(error_type)
        error_signature = _extract_error_signature(error_type, error_message)
        failure_memory = _build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            stdout_text='',
            stderr_text='',
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=error_message,
        )
        with logger.context(**execution_log_context):
            logger.error(error_message)
        emit(
            'status',
            stage='tool_error',
            level='error',
            tool='execute_python',
            message=FRONTEND_TOOL_ERROR_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=error_type,
            error_message=error_message,
            repair_instructions=repair_instructions,
            error_signature=error_signature,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
        )
    finally:
        _remove_worker_result_payload(result_file_path)

    stdout_text = _sanitize_tool_output(worker_payload.get('stdout_text', ''))
    stderr_text = _sanitize_tool_output(worker_payload.get('stderr_text', ''))
    if worker_payload.get('status') == 'error':
        error_message = worker_result.get('error_message', '代码执行失败')
        error_type = worker_result.get('error_type', 'runtime_error')
        repair_instructions = worker_result.get('repair_instructions') or _build_repair_instructions(error_type)
        error_signature = worker_result.get('error_signature') or _extract_error_signature(error_type, error_message)
        error_message = worker_payload.get('error_message', error_message)
        error_type = worker_payload.get('error_type', error_type)
        repair_instructions = worker_payload.get('repair_instructions') or _build_repair_instructions(error_type)
        error_signature = worker_payload.get('error_signature') or _extract_error_signature(error_type, error_message)
        code_error_context = worker_payload.get('code_error_context') if isinstance(worker_payload.get('code_error_context'), dict) else {}
        worker_diagnostics = (
            worker_payload.get('diagnostics')
            if isinstance(worker_payload.get('diagnostics'), dict)
            else {}
        )
        (
            error_type,
            error_signature,
            worker_diagnostics,
            repair_instructions,
            code_error_context,
        ) = _augment_schema_field_mismatch_feedback(
            code=code,
            error_type=error_type,
            error_message=error_message,
            error_signature=error_signature,
            diagnostics=worker_diagnostics,
            repair_instructions=repair_instructions,
            code_error_context=code_error_context,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        (
            worker_diagnostics,
            repair_instructions,
            code_error_context,
        ) = _augment_missing_mapping_key_feedback(
            code=code,
            error_type=error_type,
            error_message=error_message,
            diagnostics=worker_diagnostics,
            repair_instructions=repair_instructions,
            code_error_context=code_error_context,
        )
        (
            worker_diagnostics,
            repair_instructions,
            code_error_context,
        ) = _augment_query_capacity_feedback(
            code=code,
            error_type=error_type,
            diagnostics=worker_diagnostics,
            repair_instructions=repair_instructions,
            code_error_context=code_error_context,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        turn_id = int(getattr(runtime.context, 'turn_id', 0) or 0)
        failure_memory = _build_turn_failure_memory(
            turn_id=turn_id,
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            result_payload_json=json.dumps({
                'status': 'failed',
                'error_type': error_type,
                'error_message': error_message,
                'error_signature': error_signature,
                'diagnostics': worker_diagnostics,
                'code_error_context': code_error_context,
                'repair_instructions': repair_instructions,
            }, ensure_ascii=False),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=int((time.time() - start_time) * 1000),
            error_message=error_message,
        )
        repair_instructions, limit_diagnostics = _apply_probe_no_data_retry_limit(
            turn_id=turn_id,
            current_execution_id=execution_id,
            error_type=error_type,
            repair_instructions=repair_instructions,
            diagnostics=worker_diagnostics,
        )
        with logger.context(**execution_log_context):
            logger.error(f"代码执行失败（{error_type}）：{error_message}")
        emit(
            'status',
            stage='tool_error',
            level='error',
            tool='execute_python',
            message=FRONTEND_TOOL_ERROR_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=error_type,
            error_message=error_message,
            repair_instructions=repair_instructions,
            error_signature=error_signature,
            diagnostics=limit_diagnostics,
            code_error_context=code_error_context,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        )

    exec_result = _deserialize_exec_result(worker_result.get('exec_result') or {})
    exec_result = _deserialize_exec_result(worker_payload.get('exec_result') or {})
    if isinstance(exec_result, StructuredResult):
        from service.table_presentation_service import normalize_table_column_labels

        label_normalization = normalize_table_column_labels(
            exec_result.tables,
            selected_datasource_snapshot,
        )
        exec_result.tables = label_normalization.tables
        if label_normalization.local_count or label_normalization.model_count:
            with logger.context(**execution_log_context):
                logger.info(
                    "normalized table labels: local=%s model=%s unresolved=%s",
                    label_normalization.local_count,
                    label_normalization.model_count,
                    len(label_normalization.unresolved),
                )

    execution_seconds = int((time.time() - start_time) * 1000)
    with logger.context(**execution_log_context):
        logger.info(f"执行完成，耗时 {execution_seconds / 1000:.2f} 秒")
    emit(
        'status',
        stage='tool_finished',
        level='success',
        tool='execute_python',
        message=f'代码执行完成，耗时 {execution_seconds / 1000:.2f} 秒',
    )

    if stdout_text:
        # stdout/stderr 既用于调试，也用于后续回看执行历史。
        with logger.context(**execution_log_context):
            logger.info(stdout_text)
        emit(
            'tool_log',
            stage='tool_output',
            level='info',
            tool='execute_python',
            message=stdout_text[:1000],
        )
    if stderr_text:
        with logger.context(**execution_log_context):
            logger.info(f"标准错误：\n{stderr_text}")
        emit(
            'tool_log',
            stage='tool_output',
            level='warning',
            tool='execute_python',
            message=stderr_text[:1000],
        )

    if isinstance(exec_result, RetryResult):
        retry_error_signature = _build_retry_error_signature(exec_result)
        retry_repair_instructions = exec_result.repair_instructions or _build_repair_instructions(exec_result.retry_type)
        turn_id = int(getattr(runtime.context, 'turn_id', 0) or 0)
        failure_memory = _build_turn_failure_memory(
            turn_id=turn_id,
            current_execution_id=execution_id,
        )
        _update_execution_record(
            execution_id,
            execution_status='failed',
            analysis_report=exec_result.analysis_report,
            result_payload_json=exec_result.model_dump_json(),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=execution_seconds,
            error_message=exec_result.message,
        )
        retry_repair_instructions, retry_diagnostics = _apply_probe_no_data_retry_limit(
            turn_id=turn_id,
            current_execution_id=execution_id,
            error_type=exec_result.retry_type,
            repair_instructions=retry_repair_instructions,
            diagnostics=exec_result.diagnostics,
        )
        emit(
            'status',
            stage='tool_retry',
            level='warning',
            tool='execute_python',
            message=FRONTEND_TOOL_RETRY_MESSAGE,
        )
        return _tool_error_message(
            tool_call_id=runtime.tool_call_id,
            execution_id=execution_id,
            error_type=exec_result.retry_type,
            error_message=exec_result.message,
            repair_instructions=retry_repair_instructions,
            error_signature=retry_error_signature,
            feedback_kind='retryable_feedback',
            retry_reason_code=exec_result.retry_type,
            diagnostics=retry_diagnostics,
            analysis_report=exec_result.analysis_report,
            current_failed_code=code,
            previous_failure_messages=failure_memory.get('previous_failure_messages', []),
            previous_failure_hints=failure_memory.get('previous_failure_hints', []),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        )

    if isinstance(exec_result, StructuredResult):
        canonical_used_datasource_ids = _canonicalize_used_datasource_ids(
            exec_result,
            declared_datasource_ids=normalized_used_datasource_ids,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        if canonical_used_datasource_ids != normalized_used_datasource_ids:
            with logger.context(**execution_log_context):
                logger.info(
                    "canonicalized used datasource ids from query snapshots: declared=%s actual=%s",
                    normalized_used_datasource_ids,
                    canonical_used_datasource_ids,
                )
            normalized_used_datasource_ids = canonical_used_datasource_ids
        scope_retry = _validate_technical_repair_scope_from_history(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
            current_used_datasource_ids=normalized_used_datasource_ids,
            current_code=code,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        if scope_retry is not None:
            failure_memory = _build_turn_failure_memory(
                turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
                current_execution_id=execution_id,
            )
            _update_execution_record(
                execution_id,
                execution_status='failed',
                analysis_report=exec_result.analysis_report,
                result_payload_json=scope_retry.model_dump_json(),
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                execution_seconds=execution_seconds,
                error_message=scope_retry.message,
                used_datasource_ids=normalized_used_datasource_ids,
            )
            emit(
                'status',
                stage='tool_retry',
                level='warning',
                tool='execute_python',
                message=FRONTEND_TOOL_RETRY_MESSAGE,
            )
            return _tool_error_message(
                tool_call_id=runtime.tool_call_id,
                execution_id=execution_id,
                error_type=scope_retry.retry_type,
                error_message=scope_retry.message,
                repair_instructions=scope_retry.repair_instructions,
                error_signature=_build_retry_error_signature(scope_retry),
                feedback_kind='retryable_feedback',
                retry_reason_code=scope_retry.retry_type,
                diagnostics=scope_retry.diagnostics,
                analysis_report=exec_result.analysis_report,
                current_failed_code=code,
                previous_failure_messages=failure_memory.get('previous_failure_messages', []),
                previous_failure_hints=failure_memory.get('previous_failure_hints', []),
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )
        contract_retry = _validate_structured_result_contract(
            exec_result,
            generated_code=code,
            allow_empty_artifact_no_data=time_range_guard_active,
            expected_time_range=expected_time_range,
            used_datasource_ids=normalized_used_datasource_ids,
            selected_datasource_snapshot=selected_datasource_snapshot,
        )
        if contract_retry is not None:
            failure_memory = _build_turn_failure_memory(
                turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
                current_execution_id=execution_id,
            )
            _update_execution_record(
                execution_id,
                execution_status='failed',
                analysis_report=contract_retry.analysis_report or exec_result.analysis_report,
                result_payload_json=contract_retry.model_dump_json(),
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                execution_seconds=execution_seconds,
                error_message=contract_retry.message,
            )
            emit(
                'status',
                stage='tool_retry',
                level='warning',
                tool='execute_python',
                message=FRONTEND_TOOL_RETRY_MESSAGE,
            )
            return _tool_error_message(
                tool_call_id=runtime.tool_call_id,
                execution_id=execution_id,
                error_type=contract_retry.retry_type,
                error_message=contract_retry.message,
                repair_instructions=contract_retry.repair_instructions or _build_repair_instructions(contract_retry.retry_type),
                error_signature=_build_retry_error_signature(contract_retry),
                feedback_kind='retryable_feedback',
                retry_reason_code=contract_retry.retry_type,
                diagnostics=contract_retry.diagnostics,
                code_error_context=contract_retry.code_error_context,
                analysis_report=contract_retry.analysis_report,
                current_failed_code=code,
                previous_failure_messages=failure_memory.get('previous_failure_messages', []),
                previous_failure_hints=failure_memory.get('previous_failure_hints', []),
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )

        # 对外继续保持 ToolMessage JSON 结构，对内持久化完整结构化执行结果。
        _raise_if_runtime_task_cancelled(runtime.context, execution_id=execution_id, start_time=start_time)
        _update_execution_record(
            execution_id,
            execution_status='success',
            analysis_report=exec_result.analysis_report,
            result_payload_json=exec_result.model_dump_json(),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            execution_seconds=execution_seconds,
            analysis_parameters_schema=exec_result.analysis_parameters_schema,
            analysis_parameters=exec_result.analysis_parameters,
            used_datasource_ids=normalized_used_datasource_ids,
        )
        emit(
            'status',
            stage='tool_result',
            level='success',
            tool='execute_python',
            message='分析结果已生成，正在整理最终报告。',
        )
        success_message = _tool_success_message(
            tool_call_id=runtime.tool_call_id,
            analysis_report=exec_result.analysis_report,
            charts=exec_result.charts,
            tables=exec_result.tables,
        )
        # A contract-valid StructuredResult is the terminal product of this
        # agent run. Returning a graph command prevents another model turn from
        # treating the lightweight success summary as a reason to probe again.
        # Retry/error ToolMessages above still return to the same ReAct loop.
        return _terminal_tool_success(success_message)

    _update_execution_record(
        execution_id,
        execution_status='invalid',
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        execution_seconds=execution_seconds,
        error_message='生成的代码未按模板产出 result',
    )
    # 如果生成代码没有产出 `result`，这里会要求模型重试，
    # 而不是静默接受一次不完整的执行。
    emit(
        'status',
        stage='tool_retry',
        level='warning',
        tool='execute_python',
        message=FRONTEND_TOOL_RETRY_MESSAGE,
    )
    return _tool_error_message(
        tool_call_id=runtime.tool_call_id,
        execution_id=execution_id,
        error_type='result_contract_error',
        error_message='生成的代码未按模板产出 result',
        repair_instructions=_build_repair_instructions('result_contract_error'),
        error_signature=_extract_error_signature('result_contract_error', '生成的代码未按模板产出 result'),
        current_failed_code=code,
        previous_failure_messages=_build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        ).get('previous_failure_messages', []),
        previous_failure_hints=_build_turn_failure_memory(
            turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
            current_execution_id=execution_id,
        ).get('previous_failure_hints', []),
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
