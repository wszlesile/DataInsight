"""Public work summaries derived from actual Runtime events, not private reasoning."""

import json
import re
import uuid
from datetime import datetime, timezone


TOOL_TITLES = {
    'select_runtime_work': '确定下一步分析工作',
    'plan_runtime_semantics': '规划洞察语义与统计口径',
    'request_runtime_probe': '核实分析所需的技术事实',
    'plan_runtime_materials': '整理分析计划',
    'read_runtime_datasource_context': '读取数据字段与关系',
    'read_runtime_reference': '查阅分析方法与工具规范',
    'read_runtime_query_context': '核对已执行查询',
    'probe_analysis_data': '核实数据与查询口径',
    'submit_analysis_program': '执行分析程序',
    'compose_analysis_presentation': '根据计算结果撰写表达',
    'accept_analysis_result': '复核并确认分析结果',
    'request_analysis_revision': '反馈候选结果问题',
    'execute_python': '执行分析程序',
}
WORK_TITLES = {
    'understand': '规划洞察语义', 'plan': '制定资料读取计划',
    'probe': '准备数据探测', 'probe_repair': '修正数据探测',
    'implement': '生成分析程序', 'repair': '修复分析程序',
    'express': '撰写分析结果表达',
    'review': '复核分析结果',
}


def public_summary(value):
    if not isinstance(value, str):
        return ''
    # Do not turn source, credentials, or private reasoning into a UI message.
    if re.search(
        r'<think|```|Bearer\s|authorization\s*[:=]|\beyJ[\w-]+\.[\w-]+\.'
        r'|\b(?:import|def|class)\s+\w+|save_analysis_result\s*\(|load_data_with_'
        r'|\bselect\b[\s\S]+\bfrom\b|[{}\[\];]',
        value, re.I,
    ):
        return ''
    return re.sub(r'\s+', ' ', value).strip()[:600]


def progress_event(activity_id, title, state, summary=''):
    return {
        'type': 'status', 'stage': 'work_progress',
        'event_id': uuid.uuid4().hex,
        'occurred_at': datetime.now(timezone.utc).isoformat(),
        'level': 'error' if state == 'failed' else 'success' if state == 'completed' else 'info',
        'message': public_summary(summary) or title,
        'progress': {'activity_id': activity_id, 'title': title, 'state': state},
    }


def tool_progress(call, result=None):
    name = call.get('name')
    title = TOOL_TITLES.get(name)
    if not title:
        return None
    args = call.get('args') or {}
    summary = public_summary(args.get('progress_summary')) or public_summary(args.get('focus'))
    state = 'running'
    if result is not None:
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        status = payload.get('status')
        state = ('failed' if status in {'failed', 'error'} else
                 'completed' if status in {'success', 'observation'} else 'finished')
        if name == 'probe_analysis_data' and state == 'completed':
            summary = '数据探测已完成，将结合观察结果判断后续分析；探测结果不等同于正式统计结果。'
        elif name == 'read_runtime_datasource_context' and state == 'completed':
            sources = payload.get('datasources') or []
            summary = f'已取得 {len(sources)} 个数据源的字段资料。'
            if status == 'partial':
                summary += '部分资料仍不可用，需要继续核实。'
        elif state == 'failed':
            summary = '本次工作未完成，正在根据执行反馈调整。'
        elif not summary:
            summary = f'{title}已完成。' if state == 'completed' else f'{title}已返回。'
    return progress_event(f'tool:{call["id"]}', title, state, summary)


def emit_model_progress(activity_id, work, state):
    from langgraph.config import get_stream_writer
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    title = WORK_TITLES.get(work, '处理分析工作')
    summary = f'正在{title}。' if state == 'running' else f'{title}已返回。'
    if state == 'failed':
        summary = f'{title}未完成。'
    writer(progress_event(activity_id, title, state, summary))
