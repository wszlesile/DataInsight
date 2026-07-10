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
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
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

TOOL_ERROR_CURRENT_CODE_MAX_CHARS = int(os.environ.get('TOOL_ERROR_CURRENT_CODE_MAX_CHARS', '3500'))
TOOL_ERROR_TEXT_MAX_CHARS = int(os.environ.get('TOOL_ERROR_TEXT_MAX_CHARS', '1800'))
TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS = int(os.environ.get('TOOL_ERROR_PREVIOUS_ITEM_MAX_CHARS', '700'))
TOOL_ERROR_INSTRUCTION_MAX_CHARS = int(os.environ.get('TOOL_ERROR_INSTRUCTION_MAX_CHARS', '700'))
TOOL_ERROR_STDIO_MAX_CHARS = int(os.environ.get('TOOL_ERROR_STDIO_MAX_CHARS', '800'))
TOOL_ERROR_CODE_CONTEXT_MAX_CHARS = int(os.environ.get('TOOL_ERROR_CODE_CONTEXT_MAX_CHARS', '1800'))
PROBE_NO_DATA_RETRY_LIMIT = max(int(os.environ.get('PROBE_NO_DATA_RETRY_LIMIT', '3') or 3), 1)
PROBE_NO_DATA_RETRY_TYPES = {'probe_feedback', 'no_data_found'}
GENERATED_CODE_FILENAME = '<generated_analysis_code>'
GENERATED_CODE_CONTEXT_RADIUS = 2
FRONTEND_TOOL_RETRY_MESSAGE = '分析代码规范需要调整，正在反馈给大模型重新生成。'
FRONTEND_TOOL_ERROR_MESSAGE = '分析代码逻辑需要调整，正在反馈给大模型重新生成。'
FRONTEND_TOOL_TIMEOUT_MESSAGE = '分析代码执行时间较长，正在反馈给大模型优化后重试。'
QUERY_SNAPSHOT_SQL_MAX_CHARS = int(os.environ.get('QUERY_SNAPSHOT_SQL_MAX_CHARS', '8000'))
QUERY_SNAPSHOT_CONTEXT_MAX_CHARS = int(os.environ.get('QUERY_SNAPSHOT_CONTEXT_MAX_CHARS', '12000'))

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

    return {
        'filename': GENERATED_CODE_FILENAME,
        'line_no': line_no,
        'column_no': column_no,
        'error_line': error_line,
        'context_lines': context_lines,
        'traceback_excerpt': _clip_retry_context_text(traceback_text, TOOL_ERROR_CODE_CONTEXT_MAX_CHARS),
    }


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
                if not isinstance(item.get('value'), str) or not str(item.get('value') or '').strip():
                    errors.append(f'target_filters[{index}].value 必须是非空字符串。')
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

        expected_value = _normalize_filter_text(item.get('value', ''))
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
                f'query_constraints 要求 `{item.get("semantic_role", "target_filter")}` 使用精确匹配，但当前代码把 `{item.get("value", "")}` 实现成了模糊匹配。',
                diagnostics={
                    'violated_constraint': 'target_filters',
                    'constraint_item': item,
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

    missing_parts = []
    if start and not _time_range_part_present(code, start):
        missing_parts.append('start')
    if end and not _time_range_part_present(code, end):
        missing_parts.append('end')
    if not missing_parts:
        return None

    return _build_query_constraint_error(
        'query_constraint_violation',
        f'query_constraints 中声明的时间范围没有完整体现在当前代码里，缺失部分：{", ".join(missing_parts)}。',
        diagnostics={
            'violated_constraint': 'time_range',
            'time_range': time_range,
            'missing_parts': missing_parts,
        },
    )


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

    这个辅助函数属于 `sys_prompt.md` 中约定的 Python 执行工具契约。
    """
    import pandas as pd

    _ensure_local_file_exists(file_path)
    lower_file_path = file_path.lower()

    if lower_file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    if lower_file_path.endswith(('.xlsx', '.xls', '.xlsm')):
        return pd.read_excel(file_path, sheet_name=sheet_name if sheet_name else 0)

    raise ValueError(f"不支持的文件格式: {file_path}")


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
        return pd.read_csv(
            file_path,
            chunksize=normalized_chunk_size,
            usecols=usecols,
            dtype=dtype,
            parse_dates=parse_dates,
            low_memory=low_memory,
        )

    if lower_file_path.endswith(('.xlsx', '.xls', '.xlsm')):
        return _iter_excel_rows_in_batches(
            file_path=file_path,
            sheet_name=sheet_name,
            chunk_size=normalized_chunk_size,
            usecols=usecols,
            dtype=dtype,
            parse_dates=parse_dates,
        )

    raise ValueError(f"不支持的文件格式: {file_path}")


def load_minio_file(bucket: str, object_name: str, sheet_name: Optional[str] = None):
    """预留给后续 MinIO 文件加载场景的辅助函数。"""
    pass


def load_data_with_sql(sql: str, params: Optional[list[Any]] = None):
    start_time = time.time()
    try:
        data = _load_data_with_fed_query(sql=sql, params=params)
    except Exception:
        _mark_latest_query_snapshot_execution(status='failed', duration_ms=int((time.time() - start_time) * 1000))
        raise
    _mark_latest_query_snapshot_execution(
        status='success',
        duration_ms=int((time.time() - start_time) * 1000),
        data=data,
    )
    return data

def load_local_data_with_sql(sql: str, params: Optional[list[Any]] = None):
    """通过 SQL 从当前配置的数据源中加载表格数据。"""
    import pandas as pd

    from config.database import engine

    with engine.connect() as connection:
        return pd.read_sql(sql, connection, params=params if params else None)


def _copy_jsonable(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
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


def _normalize_query_key(query_key: str | None = None) -> str:
    normalized = re.sub(r'[^A-Za-z0-9_-]+', '_', str(query_key or '').strip()).strip('_')
    if normalized:
        return normalized[:80]
    snapshots = CURRENT_QUERY_SNAPSHOTS.get() or {}
    return f"query_{len(snapshots) + 1}"


def _register_query_snapshot(
    *,
    query_key: str,
    query_constraints: Any,
    sql: str = '',
    filter_expressions: Optional[list[str]] = None,
    aggregation_hints: Optional[list[str]] = None,
    sql_context: Optional[dict[str, Any]] = None,
) -> "RetryResult | None":
    snapshots = dict(CURRENT_QUERY_SNAPSHOTS.get() or {})
    sql_text, sql_truncated = _clip_snapshot_text(sql, QUERY_SNAPSHOT_SQL_MAX_CHARS)
    sql_context_json, sql_context_truncated = _clip_snapshot_json(sql_context or {}, QUERY_SNAPSHOT_CONTEXT_MAX_CHARS)
    snapshot = {
        "query_key": query_key,
        "query_constraints": _copy_jsonable(query_constraints, {}),
        "implementation": {
            "sql": sql_text,
            "sql_hash": hashlib.sha256(str(sql or '').encode('utf-8')).hexdigest() if str(sql or '').strip() else '',
            "sql_truncated": sql_truncated,
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
            "implementation": existing.get("implementation") or {},
        }
        comparable_new = {
            "query_constraints": snapshot["query_constraints"],
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


def _mark_latest_query_snapshot_execution(
    *,
    status: str,
    duration_ms: int = 0,
    data: Any = None,
) -> None:
    query_key = str(CURRENT_LAST_QUERY_KEY.get() or '').strip()
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
    return value.item() if hasattr(value, 'item') else value


def build_markdown_table(dataframe, columns: Optional[list[str]] = None, max_rows: int = 10) -> str:
    """
    把 DataFrame 转成 Markdown 表格文本。

    这个辅助函数适合在“需要输出 Markdown 表格”的场景中直接复用，
    用来减少手写表格拼接时的引号、换行和格式错误。
    如果当前分析任务并不需要表格展示，则不必使用它。
    """
    import pandas as pd

    if dataframe is None:
        return ''

    if not isinstance(dataframe, pd.DataFrame):
        dataframe = pd.DataFrame(dataframe)

    if columns:
        available_columns = [column for column in columns if column in dataframe.columns]
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

    headers = [str(column) for column in table_df.columns]
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
    evidence_definitions: list[dict[str, Any]] = Field(default_factory=list, description="Evidence definitions keyed by calculation logic")
    chart_trace_refs: list[dict[str, Any]] = Field(default_factory=list, description="Chart position refs to evidence definitions")
    table_trace_refs: list[dict[str, Any]] = Field(default_factory=list, description="Table position refs to evidence definitions")
    report_trace_refs: list[dict[str, Any]] = Field(default_factory=list, description="Report text refs to evidence definitions")
    table_trace_metadata: dict[str, Any] = Field(default_factory=dict, description="Sidecar metadata for table trace validation")
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
        "missing_evidence_contract",
        "invalid_evidence_definition",
        "invalid_evidence_query_ref",
        "invalid_chart_trace_ref",
        "invalid_table_trace_ref",
        "invalid_report_trace_ref",
        "evidence_over_expansion",
        "missing_query_constraints",
        "invalid_query_constraints",
        "query_constraint_violation",
    ] = Field(description="Why the current execution needs another attempt.")
    message: str = Field(description="Human-readable retry reason.")
    diagnostics: dict[str, Any] = Field(default_factory=dict, description="Structured diagnostic facts.")
    repair_instructions: list[str] = Field(default_factory=list, description="Targeted next-step instructions.")
    analysis_report: str = Field(default="", description="Optional diagnostic report; not a final answer.")


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
        "missing_evidence_contract",
        "invalid_evidence_definition",
        "invalid_evidence_query_ref",
        "invalid_chart_trace_ref",
        "invalid_table_trace_ref",
        "invalid_report_trace_ref",
        "evidence_over_expansion",
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
    return RetryResult(
        retry_type=normalized_retry_type,  # type: ignore[arg-type]
        message=normalized_message,
        diagnostics=diagnostics or {},
        repair_instructions=normalized_instructions,
        analysis_report=str(analysis_report or "").strip(),
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
            records_query_failure = _handler_records_query_failure(handler)
            if (
                _handler_has_pass_only(handler)
                or _handler_assigns_none(handler)
                or not _handler_references_exception(handler)
                or not records_query_failure
            ):
                return request_retry(
                    retry_type="contract_error",
                    message="SQL 加载异常是阻断性错误。load_data_with_sql(...) 执行失败时必须立即反馈明确的 SQL 错误并停止当前分析流程，要求修正 SQL 后重试。",
                    diagnostics={
                        'violated_contract': 'sql_load_error_handling',
                        'try_line_no': getattr(node, 'lineno', None),
                        'except_line_no': getattr(handler, 'lineno', None),
                    },
                    repair_instructions=[
                        "不得将 SQL 加载异常降级为无数据、空数据、部分失败、可跳过数据源或普通查询失败。",
                        "不得通过宽泛 except、仅记录日志、返回默认结果、保存部分成功报告等方式掩盖异常并继续分析。",
                        "必须直接 raise 原异常，或立即把 request_retry(...) 的返回值赋给 result 并结束当前尝试；只有 load_data_with_sql(...) 成功返回且 DataFrame 为空时，才可以判断为无数据。",
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


def _validate_generated_code_contract(code: str) -> RetryResult | None:
    """Validate the static tool contract of generated Python code before execution."""
    try:
        tree = ast.parse(code or '')
    except SyntaxError as exc:
        return request_retry(
            retry_type="contract_error",
            message=f"生成的 Python 代码存在语法错误，无法执行：{exc.msg}",
            diagnostics={"line_no": exc.lineno, "offset": exc.offset},
            repair_instructions=[
                "重新生成完整可执行 Python 代码。",
                "必须在顶部声明 execution_intent = \"analysis\" 或 execution_intent = \"probe\"。",
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

        validation_order_retry = _validate_analysis_sql_validation_order(tree)
        if validation_order_retry is not None:
            return validation_order_retry

        helper_chart_retry = _validate_helper_chart_usage_contract(tree, calls)
        if helper_chart_retry is not None:
            return helper_chart_retry

    if execution_intent == 'probe':
        violations: list[str] = []
        if has_query_constraints:
            violations.append('probe 代码不要声明 query_constraints')
        if 'validate_query_constraints' in calls:
            violations.append('probe 代码不要调用 validate_query_constraints(...)')
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
                    'probe 代码不要生成 query_constraints，也不要调用 validate_query_constraints(...) 或 save_analysis_result(...)。',
                ],
            )

    return None


def validate_query_constraints(
    query_constraints: Any,
    query_key: str = '',
    sql: str = '',
    filter_expressions: Optional[list[str]] = None,
    aggregation_hints: Optional[list[str]] = None,
    sql_context: Optional[dict[str, Any]] = None,
) -> RetryResult | None:
    """Validate analysis-stage query constraints against SQL / pandas filtering implementations."""
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

    filter_fragments = [
        str(sql or '').strip(),
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
                    '如果 target_filters 里的 match_mode = "exact"，正式 analysis 代码不要把同一个值实现成 LIKE、ILIKE、contains 或 str.contains(...)。',
                    '如果需要先探测候选值，请改成 execution_intent = "probe"，不要在 analysis 阶段继续放宽口径。',
                ],
            )
    normalized_query_key = _normalize_query_key(query_key)
    return _register_query_snapshot(
        query_key=normalized_query_key,
        query_constraints=query_constraints,
        sql=sql,
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
    charts: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """统一图表结果结构。"""
    normalized_items: list[dict[str, Any]] = []

    for index, chart in enumerate(charts or [], start=1):
        normalized_items.append(normalize_chart_result_item(chart, index=index))

    return normalized_items


def _normalize_table_items(tables: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """统一表格结果结构。"""
    normalized_items: list[dict[str, Any]] = []
    for index, table in enumerate(tables or [], start=1):
        if not isinstance(table, dict):
            raise ValueError('tables 中的每一项都必须是 dict。')

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
) -> dict[str, Any]:
    """Build a clean table artifact plus sidecar trace metadata."""
    clean_columns: list[dict[str, Any]] = []
    column_roles: dict[str, str] = {}
    column_value_types: dict[str, str] = {}
    column_units: dict[str, str] = {}
    for index, column in enumerate(columns or [], start=1):
        if not isinstance(column, dict):
            raise ValueError('build_table_result.columns 中的每一项都必须是 dict。')
        key = str(column.get('key') or column.get('dataIndex') or column.get('label') or f'col_{index}').strip()
        label = str(column.get('label') or column.get('title') or key).strip()
        clean_columns.append({"key": key, "label": label})
        role = str(column.get('role') or '').strip()
        if role:
            column_roles[key] = role
        value_type = str(column.get('value_type') or '').strip()
        if value_type:
            column_value_types[key] = value_type
        unit = str(column.get('unit') or '').strip()
        if unit:
            column_units[key] = unit

    clean_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            raise ValueError('build_table_result.rows 中的每一项都必须是 dict。')
        clean_rows.append(dict(row))

    return {
        "table": {
            "title": str(title or '表格').strip() or '表格',
            "description": str(description or '').strip(),
            "columns": clean_columns,
            "rows": clean_rows,
        },
        "trace_metadata": {
            "column_roles": column_roles,
            "column_value_types": column_value_types,
            "column_units": column_units,
        },
    }


REPORT_TRACE_PATTERN = re.compile(
    r"\[\[trace:evidence_key=(?P<key>[A-Za-z0-9_-]+)\]\](?P<text>.*?)\[\[/trace\]\]",
    re.DOTALL,
)


def build_report_result(report_markdown_with_trace_tags: str) -> dict[str, Any]:
    """Strip report trace tags and return clean Markdown plus report trace refs."""
    source = str(report_markdown_with_trace_tags or '')
    if not source.strip():
        raise ValueError('报告内容不能为空。')
    if '[[trace:' in source and '[[/trace]]' not in source:
        raise ValueError('报告 trace 标签未闭合。')

    output_parts: list[str] = []
    refs: list[dict[str, Any]] = []
    cursor = 0
    clean_length = 0
    for match in REPORT_TRACE_PATTERN.finditer(source):
        prefix = source[cursor:match.start()]
        output_parts.append(prefix)
        clean_length += len(prefix)
        evidence_key = str(match.group('key') or '').strip()
        text = str(match.group('text') or '')
        if not evidence_key or not text:
            raise ValueError('报告 trace 标签必须包含 evidence_key 和非空文本。')
        if '[[trace:' in text or '[[/trace]]' in text:
            raise ValueError('报告 trace 标签不允许嵌套。')
        start = clean_length
        output_parts.append(text)
        clean_length += len(text)
        refs.append({
            "evidence_key": evidence_key,
            "start": start,
            "end": clean_length,
            "text": text,
        })
        cursor = match.end()
    output_parts.append(source[cursor:])
    clean_report = ''.join(output_parts)
    if '[[trace:' in clean_report or '[[/trace]]' in clean_report:
        raise ValueError('报告 trace 标签格式不合法或未被完整清洗。')
    for ref in refs:
        if clean_report[int(ref["start"]):int(ref["end"])] != ref["text"]:
            raise ValueError('报告 trace ref 字符区间校验失败。')
    return {
        "analysis_report": clean_report.strip(),
        "report_trace_refs": refs,
    }


def get_analysis_parameters(defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return generated-code defaults merged with local rerun overrides."""
    base = dict(defaults or {})
    override = CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.get() or {}
    if isinstance(override, dict):
        base.update(override)
    return base


def define_evidence(
    *,
    evidence_key: str,
    metric_name: str,
    query_refs: list[str],
    metric_fields: list[str],
    value_type: str = '',
    unit: str = '',
    aggregation: str = '',
    group_by: Optional[list[str]] = None,
    formula: str = '',
    derivation_description: str = '',
    derivation_mode: str = 'query_output',
    transformations: Optional[list[str]] = None,
    intermediate_outputs: Optional[list[dict[str, Any]]] = None,
    quality_notes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build a normalized EvidenceDefinition for one calculation-logic boundary."""
    key = str(evidence_key or '').strip()
    if not key:
        raise ValueError('define_evidence.evidence_key 不能为空。')
    normalized_query_refs = [str(item).strip() for item in (query_refs or []) if str(item).strip()]
    normalized_metric_fields = [str(item).strip() for item in (metric_fields or []) if str(item).strip()]
    if not normalized_query_refs:
        raise ValueError('define_evidence.query_refs 至少需要包含一个 validate_query_constraints(...) 的 query_key。')
    if not normalized_metric_fields:
        raise ValueError('define_evidence.metric_fields 至少需要包含一个最终可点击结果字段。')

    calculation: dict[str, Any] = {"metric_fields": normalized_metric_fields}
    aggregation_text = str(aggregation or '').strip()
    if aggregation_text:
        calculation["aggregation"] = aggregation_text
    normalized_group_by = [str(item).strip() for item in (group_by or []) if str(item).strip()]
    if normalized_group_by:
        calculation["group_by"] = normalized_group_by
    formula_text = str(formula or '').strip()
    if formula_text:
        calculation["formula"] = formula_text

    return {
        "evidence_key": key,
        "metric_name": str(metric_name or key).strip() or key,
        "value_type": str(value_type or '').strip(),
        "unit": str(unit or '').strip(),
        "query_refs": normalized_query_refs,
        "derivation": {
            "mode": str(derivation_mode or 'query_output').strip() or 'query_output',
            "description": str(derivation_description or '').strip(),
        },
        "calculation": calculation,
        "transformations": _copy_jsonable(transformations or [], []),
        "intermediate_outputs": _copy_jsonable(intermediate_outputs or [], []),
        "quality_notes": _copy_jsonable(quality_notes or [], []),
    }


def _normalize_metric_evidence_bindings(metric_evidence_bindings: Any) -> list[dict[str, Any]]:
    """Normalize lightweight metric-to-evidence bindings from generated code."""
    bindings: list[dict[str, Any]] = []

    def _append(artifact_type: str, artifact_index: Any, field_name: Any, evidence_key: Any) -> None:
        field = str(field_name or '').strip()
        key = str(evidence_key or '').strip()
        if not field or not key:
            return
        try:
            index = int(artifact_index or 0)
        except Exception:
            index = 0
        bindings.append({
            "artifact_type": str(artifact_type or '').strip().lower(),
            "artifact_index": index,
            "field": field,
            "evidence_key": key,
        })

    if isinstance(metric_evidence_bindings, list):
        for item in metric_evidence_bindings:
            if not isinstance(item, dict):
                continue
            artifact_type = item.get('artifact_type') or item.get('type') or item.get('artifact')
            field_name = item.get('field') or item.get('value_field') or item.get('column_key') or item.get('value_column')
            artifact_index = item.get('artifact_index')
            if artifact_index is None:
                artifact_index = item.get('chart_index') if str(artifact_type or '').lower() == 'chart' else item.get('table_index')
            _append(artifact_type, artifact_index, field_name, item.get('evidence_key'))
        return bindings

    if not isinstance(metric_evidence_bindings, dict):
        return bindings

    for group_name, artifact_type in (('charts', 'chart'), ('chart', 'chart'), ('tables', 'table'), ('table', 'table')):
        group = metric_evidence_bindings.get(group_name)
        if not isinstance(group, (dict, list)):
            continue
        if isinstance(group, list):
            iterable = enumerate(group)
        else:
            iterable = group.items()
        for artifact_index, field_map in iterable:
            if not isinstance(field_map, dict):
                continue
            for field_name, evidence_key in field_map.items():
                _append(artifact_type, artifact_index, field_name, evidence_key)
    return bindings


def _auto_trace_refs_from_metric_bindings(
    *,
    charts: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    table_trace_metadata: dict[str, Any],
    metric_evidence_bindings: Any,
    chart_trace_refs: Optional[list[dict[str, Any]]] = None,
    table_trace_refs: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate chart/table trace refs from field-level metric evidence bindings."""
    normalized_bindings = _normalize_metric_evidence_bindings(metric_evidence_bindings)
    chart_bindings = {
        (int(item["artifact_index"]), str(item["field"])): str(item["evidence_key"])
        for item in normalized_bindings
        if item.get("artifact_type") == "chart"
    }
    table_bindings = {
        (int(item["artifact_index"]), str(item["field"])): str(item["evidence_key"])
        for item in normalized_bindings
        if item.get("artifact_type") == "table"
    }

    resolved_chart_refs = _copy_jsonable(chart_trace_refs or [], [])
    existing_chart_keys = {
        (_normalize_ref_chart_index(ref), _ref_value_field(ref))
        for ref in resolved_chart_refs
        if isinstance(ref, dict)
    }
    for expected in _expected_chart_trace_refs(charts):
        ref_key = (int(expected.get("chart_index") or 0), str(expected.get("value_field") or ''))
        if ref_key in existing_chart_keys:
            continue
        evidence_key = chart_bindings.get(ref_key)
        if not evidence_key:
            continue
        resolved_chart_refs.append({
            "chart_index": ref_key[0],
            "value_field": ref_key[1],
            "evidence_key": evidence_key,
        })
        existing_chart_keys.add(ref_key)

    resolved_table_refs = _copy_jsonable(table_trace_refs or [], [])
    existing_table_keys = {
        (_normalize_ref_table_index(ref), _ref_table_column(ref))
        for ref in resolved_table_refs
        if isinstance(ref, dict)
    }
    for expected in _expected_table_trace_refs(tables, table_trace_metadata):
        ref_key = (int(expected.get("table_index") or 0), str(expected.get("column_key") or ''))
        if ref_key in existing_table_keys:
            continue
        evidence_key = table_bindings.get(ref_key)
        if not evidence_key:
            continue
        resolved_table_refs.append({
            "table_index": ref_key[0],
            "column_key": ref_key[1],
            "evidence_key": evidence_key,
            "row_scope": "all",
        })
        existing_table_keys.add(ref_key)

    return resolved_chart_refs, resolved_table_refs


def save_analysis_result(
    analysis_report: str = '',
    charts: Optional[list[dict[str, Any]]] = None,
    tables: Optional[list[dict[str, Any]]] = None,
    analysis_parameters_schema: Optional[dict[str, Any]] = None,
    analysis_parameters: Optional[dict[str, Any]] = None,
    evidence_definitions: Optional[list[dict[str, Any]]] = None,
    chart_trace_refs: Optional[list[dict[str, Any]]] = None,
    table_trace_refs: Optional[list[dict[str, Any]]] = None,
    report_trace_refs: Optional[list[dict[str, Any]]] = None,
    table_trace_metadata: Optional[dict[str, Any]] = None,
    metric_evidence_bindings: Any = None,
) -> StructuredResult:
    """
    结束一次由生成代码驱动的分析任务，并返回标准结构化结果。

    当前主契约是：
    - analysis_report: Markdown 报告
    - charts: 图表数组
    - tables: 表格数组

    """
    report_text = (analysis_report or '').strip()
    if not report_text:
        raise ValueError('analysis_report 不能为空，必须传入完整的 Markdown 分析报告。')

    source_charts = charts or []
    normalized_charts = _normalize_chart_items(charts=source_charts)
    normalized_tables = _normalize_table_items(tables=tables)
    normalized_table_trace_metadata = _copy_jsonable(table_trace_metadata or {}, {})
    resolved_chart_trace_refs, resolved_table_trace_refs = _auto_trace_refs_from_metric_bindings(
        charts=source_charts,
        tables=normalized_tables,
        table_trace_metadata=normalized_table_trace_metadata,
        metric_evidence_bindings=metric_evidence_bindings,
        chart_trace_refs=chart_trace_refs,
        table_trace_refs=table_trace_refs,
    )
    return StructuredResult(
        analysis_report=report_text,
        charts=normalized_charts,
        tables=normalized_tables,
        analysis_parameters_schema=analysis_parameters_schema or {},
        analysis_parameters=analysis_parameters or {},
        evidence_definitions=_copy_jsonable(evidence_definitions or [], []),
        chart_trace_refs=resolved_chart_trace_refs,
        table_trace_refs=resolved_table_trace_refs,
        report_trace_refs=_copy_jsonable(report_trace_refs or [], []),
        table_trace_metadata=normalized_table_trace_metadata,
        query_snapshots=_copy_jsonable(CURRENT_QUERY_SNAPSHOTS.get() or {}, {}),
    )


class ExePythonCodeInput(BaseModel):
    code: str = Field(description="待执行的 Python 代码")
    title: str = Field(description="本次代码执行任务的简短标题")
    description: str = Field(description="本次代码执行任务的详细说明")
    used_datasource_ids: list[int] = Field(
        default_factory=list,
        description="本次代码实际读取或查询的数据源 datasource_id 列表。analysis 和 probe 都必须先根据当前问题、历史上下文和数据源语义判断相关数据源后准确填写，只能从当前选中的候选数据源 ID 中选择，不要把所有候选数据源都填入。最终 analysis_report、charts、tables 中声称已分析、已覆盖或展示状态的数据源必须与这里声明的实际访问范围一致。",
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
    return {
        'error_type': 'no_data_found',
        'error_message': error_message,
        'error_signature': {
            'error_type': 'no_data_found',
            'signature_type': 'no_data_found',
            'reason': normalized_reason,
            'detail_lines': normalized_details[:5],
        },
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
            for data_index, value in enumerate(data_items):
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
    clauses: list[str] = []
    patterns = (
        r'\.\s*(?:head|tail|sample)\s*\(\s*(?:n\s*=\s*)?\d+',
        r'\.\s*iloc\s*\[\s*(?::\s*\d+|\-\d+\s*:|\d+\s*:\s*\d+)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, code or '', re.IGNORECASE):
            clause = re.sub(r'\s+', ' ', match.group(0)).strip()
            if clause and clause.lower() not in {item.lower() for item in clauses}:
                clauses.append(clause)
            if len(clauses) >= 10:
                return clauses
    return clauses


def _text_discloses_limit_usage(text: str) -> bool:
    scan_text = str(text or '').lower()
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


def _result_discloses_limit_usage(exec_result: StructuredResult) -> bool:
    return _text_discloses_limit_usage(_result_to_scan_text(exec_result))


def _report_discloses_limit_usage(exec_result: StructuredResult) -> bool:
    return _text_discloses_limit_usage(exec_result.analysis_report or '')


def _is_no_data_like_report(report_text: str) -> bool:
    normalized = str(report_text or '').lower()
    return any(marker.lower() in normalized for marker in NO_DATA_RESULT_MARKERS)


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


def _datasource_mention_tokens(item: dict[str, Any]) -> list[str]:
    raw_values = [
        item.get('datasource_name'),
        item.get('datasource_identifier'),
        item.get('table_name'),
        item.get('uns_alias'),
        item.get('refer_alias'),
    ]
    config = item.get('config') if isinstance(item.get('config'), dict) else {}
    raw_values.extend([
        config.get('table_name'),
        config.get('uns_alias'),
        config.get('refer_alias'),
    ])

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
        if '_' in text:
            parts = [part for part in text.split('_') if part]
            if len(parts) >= 2:
                candidates.add('_'.join(parts[-2:]))
            if len(parts) >= 3:
                candidates.add('_'.join(parts[-3:]))
        for candidate in candidates:
            candidate = candidate.strip()
            if len(candidate) >= 4 and candidate not in tokens:
                tokens.append(candidate)
    return tokens


def _find_untracked_datasource_mentions(
    exec_result: StructuredResult,
    *,
    used_datasource_ids: list[int] | None,
    selected_datasource_snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not used_datasource_ids or not selected_datasource_snapshot:
        return []
    used_id_set = {int(item) for item in used_datasource_ids}
    result_text = _result_to_scan_text(exec_result)
    mentions: list[dict[str, Any]] = []
    for item in selected_datasource_snapshot:
        try:
            datasource_id = int(item.get('datasource_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if datasource_id <= 0 or datasource_id in used_id_set:
            continue
        matched_tokens = [
            token for token in _datasource_mention_tokens(item)
            if token and token in result_text
        ]
        if not matched_tokens:
            continue
        mentions.append({
            'datasource_id': datasource_id,
            'datasource_name': item.get('datasource_name'),
            'datasource_identifier': item.get('datasource_identifier'),
            'matched_tokens': matched_tokens[:5],
        })
        if len(mentions) >= 10:
            break
    return mentions


SYSTEM_EVIDENCE_FIELDS = {
    'turn_id',
    'execution_id',
    'artifact_id',
    'evidence_id',
    'datasource_id',
    'datasource_ids',
    'analysis_parameters',
    'query_constraints',
    'sql',
}


def _report_has_traceable_numeric_value(analysis_report: str) -> bool:
    text = str(analysis_report or '')
    if not text.strip():
        return False
    return re.search(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*(?:%|台|个|家|种|年|月|日|kW|KW|kw)?', text) is not None


_REPORT_TRACEABLE_NUMBER_PATTERN = re.compile(
    r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*(?:%|台|个|家|种|条|次|kW|KW|kw|元|万元|小时|分钟)'
)


def _uncovered_report_numeric_values(analysis_report: str, report_trace_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = str(analysis_report or '')
    if not text.strip():
        return []
    ranges: list[tuple[int, int]] = []
    for ref in report_trace_refs or []:
        if not isinstance(ref, dict):
            continue
        try:
            start = int(ref.get('start'))
            end = int(ref.get('end'))
        except Exception:
            continue
        ranges.append((start, end))
    uncovered: list[dict[str, Any]] = []
    for match in _REPORT_TRACEABLE_NUMBER_PATTERN.finditer(text):
        start, end = match.span()
        if any(start >= ref_start and end <= ref_end for ref_start, ref_end in ranges):
            continue
        context_start = max(0, start - 30)
        context_end = min(len(text), end + 30)
        uncovered.append({
            "text": match.group(0),
            "start": start,
            "end": end,
            "context": text[context_start:context_end],
        })
        if len(uncovered) >= 20:
            break
    return uncovered


def _chart_document_columns(chart: dict[str, Any]) -> set[str]:
    document = chart.get('chart_document') if isinstance(chart, dict) else None
    if not isinstance(document, dict):
        return set()
    dataset = document.get('dataset') if isinstance(document.get('dataset'), dict) else {}
    columns = dataset.get('columns') or []
    if not columns and isinstance(dataset.get('rows'), list) and dataset.get('rows') and isinstance(dataset['rows'][0], dict):
        columns = list(dataset['rows'][0].keys())
    return {str(column) for column in columns}


def _expected_chart_trace_refs(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    for chart_index, chart in enumerate(charts or []):
        if not isinstance(chart, dict):
            continue
        document = chart.get('chart_document') or {}
        if not isinstance(document, dict):
            continue
        encoding = document.get('encoding') or {}
        if not isinstance(encoding, dict):
            continue
        chart_kind = str(document.get('chart_kind') or chart.get('chart_type') or '').strip().lower()
        value_fields = [str(item) for item in (encoding.get('value_fields') or []) if str(item).strip()]
        if value_fields:
            for value_field in value_fields:
                expected.append({"chart_index": chart_index, "value_field": value_field, "chart_kind": chart_kind})
            continue
        value_field = str(encoding.get('value_field') or '').strip()
        if value_field:
            expected.append({"chart_index": chart_index, "value_field": value_field, "chart_kind": chart_kind})
            continue
        if chart_kind == 'scatter':
            for field_name in ('x_field', 'y_field'):
                field_value = str(encoding.get(field_name) or '').strip()
                if field_value:
                    expected.append({"chart_index": chart_index, "value_field": field_value, "chart_kind": chart_kind})
    return expected


def _normalize_ref_chart_index(ref: dict[str, Any]) -> int:
    try:
        return int(ref.get('chart_index') or 0)
    except Exception:
        return 0


def _normalize_ref_table_index(ref: dict[str, Any]) -> int:
    try:
        return int(ref.get('table_index') or 0)
    except Exception:
        return 0


def _ref_value_field(ref: dict[str, Any]) -> str:
    return str(ref.get('value_field') or ref.get('field') or ref.get('x_field') or ref.get('y_field') or '').strip()


def _normalize_table_trace_metadata(table_trace_metadata: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if not isinstance(table_trace_metadata, dict) or not table_trace_metadata:
        return {}
    if isinstance(table_trace_metadata.get('tables'), list):
        return {
            index: item for index, item in enumerate(table_trace_metadata.get('tables') or [])
            if isinstance(item, dict)
        }
    if isinstance(table_trace_metadata.get('by_table'), dict):
        result: dict[int, dict[str, Any]] = {}
        for key, value in table_trace_metadata.get('by_table', {}).items():
            if isinstance(value, dict):
                try:
                    result[int(key)] = value
                except Exception:
                    continue
        return result
    return {0: table_trace_metadata}


def _table_column_keys(table: dict[str, Any]) -> set[str]:
    columns = table.get('columns') if isinstance(table, dict) else []
    keys: set[str] = set()
    for column in columns or []:
        if isinstance(column, dict):
            key = str(column.get('key') or column.get('dataIndex') or '').strip()
            label = str(column.get('label') or column.get('title') or '').strip()
            if key:
                keys.add(key)
            if label:
                keys.add(label)
        else:
            text = str(column or '').strip()
            if text:
                keys.add(text)
    return keys


def _expected_table_trace_refs(tables: list[dict[str, Any]], table_trace_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    metadata_by_table = _normalize_table_trace_metadata(table_trace_metadata)
    expected: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables or []):
        metadata = metadata_by_table.get(table_index) or (metadata_by_table.get(0) if len(tables or []) == 1 else {})
        column_roles = metadata.get('column_roles') if isinstance(metadata, dict) else {}
        if not isinstance(column_roles, dict):
            continue
        for column_key, role in column_roles.items():
            if str(role).strip().lower() == 'metric':
                expected.append({"table_index": table_index, "column_key": str(column_key)})
    return expected


def _ref_table_column(ref: dict[str, Any]) -> str:
    return str(ref.get('column_key') or ref.get('value_column') or ref.get('column') or '').strip()


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _find_intermediate_output_issue(evidence_key: str, intermediate_outputs: Any) -> dict[str, Any] | None:
    if intermediate_outputs in (None, []):
        return None
    if not isinstance(intermediate_outputs, list):
        return {"evidence_key": evidence_key, "reason": "intermediate_outputs 必须是 list"}
    if len(intermediate_outputs) > 20:
        return {"evidence_key": evidence_key, "reason": "intermediate_outputs 数量过多", "count": len(intermediate_outputs)}
    if _serialized_size(intermediate_outputs) > 8000:
        return {"evidence_key": evidence_key, "reason": "intermediate_outputs 体积过大"}
    for index, item in enumerate(intermediate_outputs):
        if _serialized_size(item) > 1200:
            return {"evidence_key": evidence_key, "index": index, "reason": "单个中间结果过大"}
        if isinstance(item, list) and len(item) > 20:
            return {"evidence_key": evidence_key, "index": index, "reason": "中间结果不能保存大数组"}
        if isinstance(item, dict):
            if len(item) > 12:
                return {"evidence_key": evidence_key, "index": index, "reason": "中间结果字段过多"}
            lowered_keys = {str(key).lower() for key in item.keys()}
            if {'rows', 'dataframe', 'records', 'details'} & lowered_keys:
                return {"evidence_key": evidence_key, "index": index, "reason": "中间结果疑似包含明细数据"}
    return None


def _validate_evidence_contract(exec_result: StructuredResult, *, generated_code: str = '') -> RetryResult | None:
    evidence_definitions = exec_result.evidence_definitions or []
    trace_ref_groups = {
        'chart_trace_refs': exec_result.chart_trace_refs or [],
        'table_trace_refs': exec_result.table_trace_refs or [],
        'report_trace_refs': exec_result.report_trace_refs or [],
    }
    has_evidence_payload = bool(evidence_definitions or any(trace_ref_groups.values()))
    if not has_evidence_payload:
        if exec_result.charts or exec_result.tables or _report_has_traceable_numeric_value(exec_result.analysis_report):
            return request_retry(
                retry_type="missing_evidence_contract",
                message="当前分析结果包含图表、表格或报告数值，但没有提供 evidence_definitions 和 trace refs。",
                diagnostics={
                    "chart_count": len(exec_result.charts or []),
                    "table_count": len(exec_result.tables or []),
                    "report_has_numeric_value": _report_has_traceable_numeric_value(exec_result.analysis_report),
                },
                repair_instructions=[
                    "按计算逻辑边界补充 evidence_definitions，不要按每个数据点生成 Evidence。",
                    "图表补充 chart_trace_refs，表格补充 table_trace_refs。",
                    "报告中的核心计算数值使用 [[trace:evidence_key=xxx]]数值[[/trace]] 包裹，并通过 build_report_result(...) 生成 report_trace_refs。",
                ],
                analysis_report=exec_result.analysis_report,
            )
        return None
    if not evidence_definitions:
        return request_retry(
            retry_type="missing_evidence_contract",
            message="当前结果声明了 trace refs，但没有提供 evidence_definitions。",
            diagnostics={name: len(items) for name, items in trace_ref_groups.items()},
            repair_instructions=[
                "补充 evidence_definitions，并让每个 trace ref 的 evidence_key 能命中对应 EvidenceDefinition。",
                "EvidenceDefinition 按计算逻辑边界生成，不要按数据点或表格行逐个生成。",
            ],
            analysis_report=exec_result.analysis_report,
        )

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(evidence_definitions):
        if not isinstance(item, dict):
            return request_retry(
                retry_type="invalid_evidence_definition",
                message="evidence_definitions 中的每一项都必须是 dict。",
                diagnostics={"index": index},
                repair_instructions=["按 EvidenceDefinition 结构重写 evidence_definitions。"],
                analysis_report=exec_result.analysis_report,
            )
        evidence_key = str(item.get('evidence_key') or '').strip()
        if not evidence_key:
            return request_retry(
                retry_type="invalid_evidence_definition",
                message="EvidenceDefinition 缺少 evidence_key。",
                diagnostics={"index": index},
                repair_instructions=["为每条 EvidenceDefinition 提供 execution 内唯一的 evidence_key。"],
                analysis_report=exec_result.analysis_report,
            )
        if evidence_key in evidence_by_key:
            return request_retry(
                retry_type="invalid_evidence_definition",
                message=f"EvidenceDefinition.evidence_key={evidence_key} 重复。",
                diagnostics={"evidence_key": evidence_key},
                repair_instructions=["合并重复 Evidence，或为不同计算逻辑使用不同 evidence_key。"],
                analysis_report=exec_result.analysis_report,
            )
        forbidden_fields = sorted(field for field in SYSTEM_EVIDENCE_FIELDS if field in item)
        if forbidden_fields:
            return request_retry(
                retry_type="invalid_evidence_definition",
                message="EvidenceDefinition 包含应由后端注入的系统事实字段。",
                diagnostics={"evidence_key": evidence_key, "forbidden_fields": forbidden_fields},
                repair_instructions=[
                    "移除 turn_id、execution_id、artifact_id、evidence_id、datasource_id、sql、query_constraints 等系统字段。",
                    "EvidenceDefinition 只保留指标名、单位、query_refs、计算说明、转换步骤和必要中间结果。",
                ],
                analysis_report=exec_result.analysis_report,
            )
        intermediate_issue = _find_intermediate_output_issue(evidence_key, item.get('intermediate_outputs'))
        if intermediate_issue is not None:
            return request_retry(
                retry_type="invalid_evidence_definition",
                message="EvidenceDefinition.intermediate_outputs 只能保存小体量摘要，不能保存明细或大对象。",
                diagnostics=intermediate_issue,
                repair_instructions=[
                    "删除完整 DataFrame、明细行数组、大列表和调试变量。",
                    "只保留用户理解指标必要的分子、分母、聚合行数、样本数等小体量摘要。",
                ],
                analysis_report=exec_result.analysis_report,
            )
        query_refs = item.get('query_refs') or []
        if not isinstance(query_refs, list) or not query_refs:
            return request_retry(
                retry_type="invalid_evidence_query_ref",
                message="EvidenceDefinition.query_refs 不能为空。",
                diagnostics={"evidence_key": evidence_key},
                repair_instructions=["每条 EvidenceDefinition 至少引用一个真实 query_key。"],
                analysis_report=exec_result.analysis_report,
            )
        evidence_by_key[evidence_key] = item

    query_snapshots = exec_result.query_snapshots or {}
    missing_query_refs: list[dict[str, Any]] = []
    for evidence_key, item in evidence_by_key.items():
        for query_ref in item.get('query_refs') or []:
            if str(query_ref) not in query_snapshots:
                missing_query_refs.append({"evidence_key": evidence_key, "query_ref": query_ref})
    if missing_query_refs:
        return request_retry(
            retry_type="invalid_evidence_query_ref",
            message="EvidenceDefinition.query_refs 引用了不存在的 query snapshot。",
            diagnostics={
                "missing_query_refs": missing_query_refs[:20],
                "available_query_keys": sorted(query_snapshots.keys()),
            },
            repair_instructions=[
                "query_refs 只能引用 validate_query_constraints(...) 成功采集到的 query_key。",
                "如果是多查询场景，请在 validate_query_constraints(query_key=...) 中显式声明稳定 query_key。",
            ],
            analysis_report=exec_result.analysis_report,
        )

    reduction_clauses = _extract_sql_limit_clauses(generated_code) + _extract_dataframe_reduction_clauses(generated_code)
    if reduction_clauses:
        missing_quality_notes = []
        for evidence_key, item in evidence_by_key.items():
            quality_text = json.dumps(item.get('quality_notes') or [], ensure_ascii=False, default=str)
            if not _text_discloses_limit_usage(quality_text):
                missing_quality_notes.append(evidence_key)
        if missing_quality_notes:
            return request_retry(
                retry_type="invalid_evidence_definition",
                message="代码使用了 LIMIT/采样/截取策略，但部分 Evidence.quality_notes 没有披露该风险。",
                diagnostics={
                    "reduction_clauses": reduction_clauses[:10],
                    "missing_quality_notes": missing_quality_notes[:20],
                },
                repair_instructions=[
                    "保留已通过的查询和产物结构，在对应 EvidenceDefinition.quality_notes 中说明 LIMIT、TopN、采样或截取口径。",
                    "如果当前指标未受截取影响，请在 quality_notes 中明确说明原因。",
                ],
                analysis_report=exec_result.analysis_report,
            )

    used_evidence_keys: set[str] = set()
    for group_name, refs in trace_ref_groups.items():
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                return request_retry(
                    retry_type="invalid_evidence_definition",
                    message=f"{group_name} 中的每一项都必须是 dict。",
                    diagnostics={"group": group_name, "index": index},
                    repair_instructions=["按 trace ref 结构重写 refs。"],
                    analysis_report=exec_result.analysis_report,
                )
            evidence_key = str(ref.get('evidence_key') or '').strip()
            if evidence_key not in evidence_by_key:
                return request_retry(
                    retry_type="invalid_evidence_definition",
                    message=f"{group_name} 引用了不存在的 evidence_key。",
                    diagnostics={"group": group_name, "index": index, "evidence_key": evidence_key},
                    repair_instructions=["修正 trace ref 的 evidence_key，使其命中 evidence_definitions。"],
                    analysis_report=exec_result.analysis_report,
                )
            used_evidence_keys.add(evidence_key)

    for index, ref in enumerate(exec_result.report_trace_refs or []):
        try:
            start = int(ref.get('start'))
            end = int(ref.get('end'))
        except Exception:
            return request_retry(
                retry_type="invalid_report_trace_ref",
                message="report_trace_refs 的 start/end 必须是整数。",
                diagnostics={"index": index, "ref": ref},
                repair_instructions=["优先使用 build_report_result(...) 生成 report_trace_refs，不要手写字符区间。"],
                analysis_report=exec_result.analysis_report,
            )
        text = str(ref.get('text') or '')
        if start < 0 or end < start or exec_result.analysis_report[start:end] != text:
            return request_retry(
                retry_type="invalid_report_trace_ref",
                message="report_trace_refs 的字符区间与最终 Markdown 不一致。",
                diagnostics={"index": index, "ref": ref},
                repair_instructions=["使用 build_report_result(...) 从 trace 标签生成干净 Markdown 和 report_trace_refs。"],
                analysis_report=exec_result.analysis_report,
            )

    if _report_has_traceable_numeric_value(exec_result.analysis_report) and not exec_result.report_trace_refs:
        return request_retry(
            retry_type="invalid_report_trace_ref",
            message="报告中包含计算数值，但缺少 report_trace_refs，报告数值无法点击查看证据链。",
            diagnostics={"analysis_report_preview": str(exec_result.analysis_report or '')[:500]},
            repair_instructions=[
                "将报告中的核心计算数值用 [[trace:evidence_key=xxx]]数值[[/trace]] 包裹。",
                "调用 build_report_result(...) 生成干净 analysis_report 和 report_trace_refs。",
                "把 report_trace_refs 传给 save_analysis_result(...)。",
            ],
            analysis_report=exec_result.analysis_report,
        )

    uncovered_report_values = _uncovered_report_numeric_values(
        exec_result.analysis_report,
        exec_result.report_trace_refs or [],
    )
    if uncovered_report_values:
        return request_retry(
            retry_type="invalid_report_trace_ref",
            message="报告中仍有计算数值没有被 report_trace_refs 覆盖，无法点击查看证据链。",
            diagnostics={
                "uncovered_numeric_values": uncovered_report_values[:12],
                "report_trace_ref_count": len(exec_result.report_trace_refs or []),
            },
            repair_instructions=[
                "将报告正文中所有由代码计算、聚合、统计或格式化得到的数值都用 [[trace:evidence_key=xxx]]数值[[/trace]] 包裹。",
                "同一计算逻辑下的多个报告数值可以复用同一个 evidence_key，例如分布数量和占比可绑定到对应分布 EvidenceDefinition。",
                "章节编号、普通序号、非计算性的日期参数不需要 trace；但概览数值、分布数量/占比、平均值、最大值、最小值、总数、TopN 数值都必须 trace。",
                "调用 build_report_result(...) 生成干净 analysis_report 和完整 report_trace_refs，并传给 save_analysis_result(...)。",
            ],
            analysis_report=exec_result.analysis_report,
        )

    expected_chart_refs = _expected_chart_trace_refs(exec_result.charts or [])
    actual_chart_ref_keys = {
        (_normalize_ref_chart_index(ref), _ref_value_field(ref))
        for ref in (exec_result.chart_trace_refs or [])
        if isinstance(ref, dict)
    }
    missing_chart_refs = [
        item for item in expected_chart_refs
        if (int(item["chart_index"]), str(item["value_field"])) not in actual_chart_ref_keys
    ]
    if expected_chart_refs and missing_chart_refs:
        return request_retry(
            retry_type="invalid_chart_trace_ref",
            message="图表中的指标字段缺少 chart_trace_refs，无法定位到 Evidence。",
            diagnostics={"missing_chart_refs": missing_chart_refs[:20]},
            repair_instructions=[
                "为每个 chart_document.encoding.value_field 或 value_fields[*] 补充 chart_trace_refs。",
                "chart_trace_refs 使用 chart_index、value_field 和 evidence_key；同一计算逻辑可以共享同一个 evidence_key。",
            ],
            analysis_report=exec_result.analysis_report,
        )
    invalid_chart_refs = []
    for index, ref in enumerate(exec_result.chart_trace_refs or []):
        if not isinstance(ref, dict):
            continue
        chart_index = _normalize_ref_chart_index(ref)
        value_field = _ref_value_field(ref)
        if chart_index < 0 or chart_index >= len(exec_result.charts or []):
            invalid_chart_refs.append({"index": index, "reason": "chart_index 越界", "ref": ref})
            continue
        columns = _chart_document_columns((exec_result.charts or [])[chart_index])
        if columns and value_field and value_field not in columns:
            invalid_chart_refs.append({"index": index, "reason": "value_field 不存在于 chart_document.dataset", "ref": ref})
    if invalid_chart_refs:
        return request_retry(
            retry_type="invalid_chart_trace_ref",
            message="chart_trace_refs 指向了不存在的图表或字段。",
            diagnostics={"invalid_chart_refs": invalid_chart_refs[:20]},
            repair_instructions=["按 chart_document.dataset.columns 和 encoding 字段修正 chart_trace_refs。"],
            analysis_report=exec_result.analysis_report,
        )

    expected_table_refs = _expected_table_trace_refs(exec_result.tables or [], exec_result.table_trace_metadata or {})
    actual_table_ref_keys = {
        (_normalize_ref_table_index(ref), _ref_table_column(ref))
        for ref in (exec_result.table_trace_refs or [])
        if isinstance(ref, dict)
    }
    missing_table_refs = [
        item for item in expected_table_refs
        if (int(item["table_index"]), str(item["column_key"])) not in actual_table_ref_keys
    ]
    if expected_table_refs and missing_table_refs:
        return request_retry(
            retry_type="invalid_table_trace_ref",
            message="表格 metric 列缺少 table_trace_refs，无法定位到 Evidence。",
            diagnostics={"missing_table_refs": missing_table_refs[:20]},
            repair_instructions=[
                "通过 build_table_result(...) 声明 metric 列，并为每个 metric 列补充 table_trace_refs。",
                "table_trace_refs 使用 table_index、column_key 或 value_column、evidence_key；列级 Evidence 优先 row_scope=all。",
            ],
            analysis_report=exec_result.analysis_report,
        )
    invalid_table_refs = []
    for index, ref in enumerate(exec_result.table_trace_refs or []):
        if not isinstance(ref, dict):
            continue
        table_index = _normalize_ref_table_index(ref)
        column_key = _ref_table_column(ref)
        if table_index < 0 or table_index >= len(exec_result.tables or []):
            invalid_table_refs.append({"index": index, "reason": "table_index 越界", "ref": ref})
            continue
        column_keys = _table_column_keys((exec_result.tables or [])[table_index])
        if column_keys and column_key and column_key not in column_keys:
            invalid_table_refs.append({"index": index, "reason": "列不存在于表格 columns", "ref": ref})
    if invalid_table_refs:
        return request_retry(
            retry_type="invalid_table_trace_ref",
            message="table_trace_refs 指向了不存在的表格或列。",
            diagnostics={"invalid_table_refs": invalid_table_refs[:20]},
            repair_instructions=["按表格 columns 中的 key/label 修正 table_trace_refs。"],
            analysis_report=exec_result.analysis_report,
        )

    unused_evidence_keys = sorted(set(evidence_by_key) - used_evidence_keys)
    if unused_evidence_keys:
        return request_retry(
            retry_type="invalid_evidence_definition",
            message="存在未被任何 trace ref 使用的 EvidenceDefinition。",
            diagnostics={"unused_evidence_keys": unused_evidence_keys[:20]},
            repair_instructions=[
                "删除未使用的 EvidenceDefinition，或补充对应 chart/table/report trace refs。",
            ],
            analysis_report=exec_result.analysis_report,
        )

    return None


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
                "analysis_report 只能包含 Markdown 自然语言结论、指标说明和表格文本；不要插入 charts[0]、charts[0]['chart_document']、chart_spec、chart_document 或图表 dict。",
                "如果报告中需要引用图表，只写自然语言，例如：下方图表展示每日产量变化趋势。",
                "图表对象只能通过 save_analysis_result(..., charts=charts, ...) 的 charts 参数返回。",
            ],
            analysis_report=report_text,
        )

    sql_limit_clauses = _extract_sql_limit_clauses(generated_code)
    dataframe_reduction_clauses = _extract_dataframe_reduction_clauses(generated_code)
    reduction_clauses = sql_limit_clauses + dataframe_reduction_clauses
    if reduction_clauses and not _report_discloses_limit_usage(exec_result):
        return request_retry(
            retry_type="result_contract_error",
            message="当前代码对查询或图表数据做了 LIMIT/head/tail/sample/iloc 等截取或采样，但 analysis_report 正文没有披露该限制，容易让用户误以为图表覆盖了完整时间范围。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "chart_count": len(exec_result.charts or []),
                "table_count": len(exec_result.tables or []),
                "sql_limit_clauses": sql_limit_clauses,
                "dataframe_reduction_clauses": dataframe_reduction_clauses,
            },
            repair_instructions=[
                "下一版代码可以继续使用 LIMIT、head/tail/sample/iloc 等策略，但必须在 analysis_report 正文明确写出查询或展示条数限制、截取方向、采样方式和实际覆盖范围。",
                "如果图表展示范围小于用户要求的时间范围，必须说明目标时间范围、实际展示点数、总命中点数和截取方向；例如每个数据源命中约 1193 点，图表仅展示其中 200 点。",
                "如果标题或描述写“最近 N 个点”，代码应使用 tail(N) 或按时间倒序取最近数据；不要用 head(N) 后声称展示最近数据。",
                "如果用户要求完整时间范围趋势，优先改为按时间桶聚合或分批查询完整时间范围，不要悄悄缩短展示范围。",
            ],
            analysis_report=report_text,
        )

    untracked_mentions = _find_untracked_datasource_mentions(
        exec_result,
        used_datasource_ids=used_datasource_ids,
        selected_datasource_snapshot=selected_datasource_snapshot,
    )
    if untracked_mentions:
        return request_retry(
            retry_type="result_contract_error",
            message="当前结果提到了未在 used_datasource_ids 中声明的数据源，无法确认这些数据源被本轮代码实际查询或读取。",
            diagnostics={
                "analysis_report_preview": report_text[:800],
                "used_datasource_ids": used_datasource_ids or [],
                "untracked_datasource_mentions": untracked_mentions,
            },
            repair_instructions=[
                "下一版代码必须让 used_datasource_ids、实际查询代码、analysis_report、charts 和 tables 保持一致。",
                "如果某个数据源已经被实际查询或探测，请把它的 datasource_id 加入 used_datasource_ids。",
                "如果某个数据源没有被实际查询或探测，不要在最终报告、表格或图表中声称分析了它，也不要展示它的数据覆盖状态。",
                "数据源展示名称必须使用上下文里的 datasource_name，不要从表名、点位名或标识中裁剪、改写出新名称。",
            ],
            analysis_report=report_text,
        )

    if not (exec_result.charts or exec_result.tables):
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

    evidence_retry = _validate_evidence_contract(exec_result, generated_code=generated_code)
    if evidence_retry is not None:
        return evidence_retry

    return None


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
    return {
        'load_local_file': load_local_file,
        'load_local_file_low_memory': load_local_file_low_memory,
        'load_minio_file': load_minio_file,
        'load_data_with_sql': load_data_with_sql,
        'load_data_with_api': load_data_with_api,
        'get_day_range': get_day_range,
        'describe_time_coverage': describe_time_coverage,
        'probe_distinct_values': probe_distinct_values,
        'probe_text_candidates': probe_text_candidates,
        'get_analysis_parameters': get_analysis_parameters,
        'validate_query_constraints': validate_query_constraints,
        'define_evidence': define_evidence,
        'build_markdown_table': build_markdown_table,
        'build_table_result': build_table_result,
        'build_report_result': build_report_result,
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

    return {
        'token': getattr(runtime_context, 'auth_token', '') or '',
        'database_conn_info': dict(getattr(runtime_context, 'database_conn_info', {}) or {}),
        'expected_time_range': dict(getattr(runtime_context, 'expected_time_range', {}) or {}),
        'analysis_parameters_override': dict(getattr(runtime_context, 'analysis_parameters_override', {}) or {}),
    }


def _prime_worker_runtime_context(runtime_user_context: dict[str, Any] | None) -> None:
    """在子进程里恢复数据库访问所需的最小运行时上下文。"""
    payload = runtime_user_context or {}
    CURRENT_EXPECTED_TIME_RANGE.set(dict(payload.get('expected_time_range') or {}))
    CURRENT_TIME_RANGE_GUARD_ACTIVE.set(bool(payload.get('time_range_guard_active')))
    CURRENT_ANALYSIS_PARAMETERS_OVERRIDE.set(dict(payload.get('analysis_parameters_override') or {}))
    CURRENT_QUERY_SNAPSHOTS.set({})
    CURRENT_LAST_QUERY_KEY.set('')
    conn_info_payload = payload.get('database_conn_info') or {}
    if conn_info_payload:
        supos_kernel_api._database_conn_info = DatabaseConnInfo(
            host=str(conn_info_payload.get('host') or ''),
            port=str(conn_info_payload.get('port') or ''),
            user=str(conn_info_payload.get('user') or ''),
            password=str(conn_info_payload.get('password') or ''),
            lake_rds_database_name=str(conn_info_payload.get('lake_rds_database_name') or ''),
            ts_query_source=str(conn_info_payload.get('ts_query_source') or ''),
        )
        supos_kernel_api._database_pool = None
        return

    token = str(payload.get('token') or '').strip()
    if token:
        supos_kernel_api.get_database_conn_info(token)


def _execute_generated_code_worker(
    code: str,
    username: str,
    runtime_user_context: dict[str, Any],
    result_queue: mp.Queue,
) -> None:
    _prime_worker_runtime_context(runtime_user_context)
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
            if isinstance(exc, NoDataFoundError):
                no_data_feedback = _build_no_data_feedback(
                    reason=exc.reason,
                    detail_lines=exc.detail_lines,
                )
                error_message = no_data_feedback['error_message']
                error_signature = no_data_feedback['error_signature']
                repair_instructions = no_data_feedback['repair_instructions']
            result_file_path = _write_worker_result_payload({
                'status': 'error',
                'error_message': error_message,
                'error_type': error_type,
                'error_signature': error_signature,
                'code_error_context': code_error_context,
                'repair_instructions': repair_instructions,
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
    if _is_chart_contract_error_message(error_message):
        return 'chart_contract_error'
    if _is_time_series_empty_source_error(error_message):
        return 'no_data_found'
    if 'got an unexpected keyword argument' in lowered:
        return 'library_api_signature_mismatch'
    if 'invalid comparison' in lowered or ('datetime' in lowered and 'timestamp' in lowered):
        return 'data_type_or_time_mismatch'
    if 'not in index' in lowered or 'no such column' in lowered:
        return 'schema_or_column_mismatch'
    if (
        'no_data_found' in lowered
        or '未命中可分析数据' in error_message
        or '筛选结果为空' in error_message
        or '过滤后无数据' in error_message
        or '关联后无数据' in error_message
    ):
        return 'no_data_found'
    if 'save_analysis_result' in lowered or 'analysis_report' in lowered or 'result' in lowered:
        return 'result_contract_error'
    return 'runtime_error'


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

    key_match = re.search(r"'([^']+)'", error_message or '')
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
            '如果错误信息里包含 unknown tag 或 no results，表示当前时序数据源在时序库中没有可查询的数据或尚未初始化；将该数据源从本轮分析中剔除。如果用户问题涉及多个互相独立的数据源，继续分析其他数据源；只有所有相关数据源都无数据时，才以整体无数据收口。',
            '如果错误信息里包含 unknown tag 或 no results，表示当前时序数据源在时序库中没有可查询的数据或尚未初始化；按无数据处理，不要继续猜测表名、点位名或语法变体。',
            '请先复核 SQL、时间范围、筛选条件、JOIN 条件或聚合口径是否过严，再生成下一版完整代码。',
            '如果用户问题里有明确时间范围，必须保持原始 query_constraints.time_range 不变；探测得到的数据覆盖范围只能用于解释无数据原因，不能替换成正式分析的时间范围。',
            '在数据加载、过滤、关联、聚合完成后，必须先检查 DataFrame 是否为空；只要为空，就调用 raise_no_data_error(reason=..., detail_lines=[...])，不要继续生成空图表或空结果产物。',
            'reason 要直接说明是哪一步没有命中数据，detail_lines 可补充当前时间范围、筛选条件、关联键或中间行数，方便上层感知后继续重试。',
        ],
        'probe_feedback': [
            '当前反馈是数据探测结果，不是最终分析报告；请基于 diagnostics 中的候选值、时间覆盖或字段分布重写正式分析代码。',
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


def _tool_error_message(
    *,
    tool_call_id: str,
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

    payload = {
        'tool': 'execute_python',
        'status': 'failed',
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
        'minimal_patch_required': True,
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


@tool(description='执行Python代码工具', args_schema=ExePythonCodeInput)
def execute_python(
    runtime: ToolRuntime[CustomContext],
    code: str,
    title: str = '',
    description: str = "",
    used_datasource_ids: list[int] | None = None,
) -> Optional[ToolMessage]:
    """
    执行模型生成的 Python 分析代码。

    需要注意：
    - 这里保持当前对外工具契约不变。
    - 执行历史仍会内部持久化到 `insight_ns_execution`。
    """
    emit = _get_stream_emitter()
    start_time = time.time()
    _raise_if_runtime_task_cancelled(runtime.context)
    normalized_used_datasource_ids = _normalize_used_datasource_ids(used_datasource_ids)
    execution_id = _create_execution_record(
        conversation_id=int(getattr(runtime.context, 'conversation_id', 0) or 0),
        turn_id=int(getattr(runtime.context, 'turn_id', 0) or 0),
        tool_call_id=getattr(runtime, 'tool_call_id', '') or '',
        code=code,
        title=title,
        description=description,
        used_datasource_ids=normalized_used_datasource_ids,
        analysis_task_id=getattr(runtime.context, 'analysis_task_id', None),
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
    contract_retry = _validate_tool_used_datasource_ids(
        context=runtime.context,
        used_datasource_ids=normalized_used_datasource_ids,
    )
    if contract_retry is None:
        contract_retry = _validate_generated_code_contract(code)
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
            error_type=contract_retry.retry_type,
            error_message=contract_retry.message,
            repair_instructions=retry_repair_instructions,
            error_signature=_build_retry_error_signature(contract_retry),
            feedback_kind='retryable_feedback',
            retry_reason_code=contract_retry.retry_type,
            diagnostics=contract_retry.diagnostics,
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
        contract_retry = _validate_structured_result_contract(
            exec_result,
            generated_code=code,
            allow_empty_artifact_no_data=time_range_guard_active,
            expected_time_range=expected_time_range,
            used_datasource_ids=normalized_used_datasource_ids,
            selected_datasource_snapshot=_get_runtime_selected_datasource_snapshot(runtime.context),
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
                error_type=contract_retry.retry_type,
                error_message=contract_retry.message,
                repair_instructions=contract_retry.repair_instructions or _build_repair_instructions(contract_retry.retry_type),
                error_signature=_build_retry_error_signature(contract_retry),
                feedback_kind='retryable_feedback',
                retry_reason_code=contract_retry.retry_type,
                diagnostics=contract_retry.diagnostics,
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
        )
        emit(
            'status',
            stage='tool_result',
            level='success',
            tool='execute_python',
            message='分析结果已生成，正在整理最终报告。',
        )
        return _tool_success_message(
            tool_call_id=runtime.tool_call_id,
            analysis_report=exec_result.analysis_report,
            charts=exec_result.charts,
            tables=exec_result.tables,
        )

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
