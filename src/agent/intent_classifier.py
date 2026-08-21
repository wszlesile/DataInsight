import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.exc import SQLAlchemyError

from agent.context_engineering_runtime import classify_analysis_context_mode
from config.database import session_scope
from service.conversation_context_service import ConversationContextService, ConversationRunContext
from utils import logger
from utils.context_compression import clip_text
from utils.datasource_utils import safe_json_loads, to_int
from utils.llm_model_factory import create_data_insight_model

VALID_CONTEXT_MODES = {'followup', 'fresh_analysis', 'general'}
VALID_CLARIFICATION_RELATIONS = {
    'none', 'answer', 'new_request', 'cancel', 'uncertain',
}
PENDING_SCOPE_MEMORY_TYPE = 'pending_scope_clarification'

INTENT_CLASSIFIER_SYSTEM_PROMPT = """你是 DataInsight 的轻量级对话意图分类器。
你判断当前请求是否需要执行新的数据分析，以及它与最近上下文的关系；不做数据分析，不写 SQL，不调用工具。

请在以下三个 mode 中选择一个：
- followup：当前输入要求继续、重算、修改或扩展最近一次数据分析，例如补图表、换展示方式、修正上次结果、基于刚才结果补充报告；且没有明确提出新的数据源、新对象、新指标或新的时间范围。只有需要再次进入数据分析执行链路时才使用 followup。
- fresh_analysis：当前输入本身提出了新的数据分析目标，或者明确要求分析新的对象、数据源、指标、时间范围、全部/所有数据源。
- general：普通问答、问候、解释概念、闲聊、产品或当前会话状态查询，或其他不需要进入数据分析执行链路的问题。问题即使承接上一轮，只要是在询问当前已选/关联的数据源数量、名称、类型等会话状态，也属于 general。

注意：
- “生成/补充/整理/换一种图表/图表报告/可视化/报告”这类短句，如果语义上是在延续最近分析结果，通常是 followup。
- “现在呢”“目前呢”这类省略表达要结合最近问题判断：承接会话状态查询时是 general，承接需要重新查询或计算的数据分析时才是 followup。上下文承接关系本身不等于需要数据分析。
- 严格区分“查询数据源状态”与“以数据源为对象做分析”：只有询问已选/关联数据源的数量、名称、类型或列表才是 general；“分析/比较/计算/展示当前已选、已关联或全部数据源”仍是 fresh_analysis，不能因 selected_datasource_summary 为空或数量较大而改判为状态查询。
- 如果当前问题显式指定了新数据范围、新数据源、新对象、新时间范围，应归为 fresh_analysis。
- 最近执行记录中的 used_datasource_ids 只是判断上下文关系的参考，不代表当前必须沿用。
- 当 `pending_scope_clarification.status=pending` 时，同时判断当前输入与待补充范围的关系：
  - answer：直接补充了业务主题、目录、类型或具体数据源范围，应继续原分析；
  - new_request：提出独立的新问题，不应拼接到原分析；
  - cancel：明确取消或不再继续原分析；
  - uncertain：既不能确认是范围补充，也不是明确的新问题。
- 只有真正缩小或具体化分析范围才是 answer；“还是全部分析”没有解决容量问题，应为 uncertain。

只输出 JSON，不要输出其他文字：
{"mode":"followup|fresh_analysis|general","clarification_relation":"none|answer|new_request|cancel|uncertain","confidence":0.0-1.0,"reason":"不超过80字的中文理由"}"""


@dataclass(frozen=True)
class IntentClassification:
    mode: str
    confidence: float
    reason: str = ''
    source: str = 'local_fallback'
    clarification_relation: str = 'none'


DATASOURCE_STATE_OBJECT_PATTERN = re.compile(r'(数据源|datasources?)', re.IGNORECASE)
DATASOURCE_STATE_SELECTION_PATTERN = re.compile(r'(选中|已选|选择了|关联|绑定|接入)')
DATASOURCE_STATE_REQUEST_PATTERN = re.compile(r'(多少|几个|数量|构成|哪些|列表|清单)')
DATASOURCE_ANALYSIS_ACTION_PATTERN = re.compile(
    r'(分析|洞察|比较|对比|计算|统计|汇总|评估|诊断|趋势|分布|异常|'
    r'analy[sz]e|compare|calculate|summari[sz]e)',
    re.IGNORECASE,
)
DATASOURCE_CAPABILITY_QUERY_PATTERN = re.compile(
    r'(最多|上限|限制|一次性|支持.{0,8}(多少|几个|数量)|'
    r'(多少|几个|数量).{0,8}支持)',
    re.IGNORECASE,
)
DATASOURCE_CONCEPT_QUERY_PATTERN = re.compile(r'(是什么|什么意思|如何理解|请解释|介绍一下)')
ELLIPTICAL_STATE_QUESTIONS = {
    '现在呢', '那现在呢', '目前呢', '那目前呢', '当前呢', '那当前呢',
}


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get('text') or item.get('content') or ''))
            else:
                parts.append(str(item))
        return ''.join(parts)
    return str(content or '')


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or '').strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass

    match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _fallback_classification(user_message: str, reason: str = '') -> IntentClassification:
    mode = classify_analysis_context_mode(user_message)
    return IntentClassification(
        mode=mode if mode in VALID_CONTEXT_MODES else 'general',
        confidence=0.0,
        reason=reason or 'LLM intent classifier unavailable; fallback to local classifier',
        source='local_fallback',
    )


def _is_datasource_selection_state_text(text: str) -> bool:
    normalized = re.sub(r'\s+', '', str(text or ''))
    return bool(
        DATASOURCE_STATE_OBJECT_PATTERN.search(normalized)
        and DATASOURCE_STATE_SELECTION_PATTERN.search(normalized)
        and DATASOURCE_STATE_REQUEST_PATTERN.search(normalized)
    )


def _is_explicit_datasource_analysis_request(text: str) -> bool:
    """Identify an explicit command to analyze datasource contents.

    Whether that scope is sufficiently narrow is deliberately left to
    intent_analysis/scope_capacity. This boundary only separates an analysis
    command from datasource state, capability, and concept questions.
    """
    normalized = re.sub(r'\s+', '', str(text or ''))
    if not DATASOURCE_STATE_OBJECT_PATTERN.search(normalized):
        return False
    if DATASOURCE_CAPABILITY_QUERY_PATTERN.search(normalized):
        return False
    if DATASOURCE_CONCEPT_QUERY_PATTERN.search(normalized):
        return False
    return bool(DATASOURCE_ANALYSIS_ACTION_PATTERN.search(normalized))


def _is_conversation_state_request(payload: dict[str, Any]) -> bool:
    """Recognize read-only conversation metadata queries before analysis routing."""
    current = str(payload.get('current_user_message') or '').strip()
    if _is_datasource_selection_state_text(current):
        return True

    normalized = re.sub(r'\s+', '', current)
    if normalized not in ELLIPTICAL_STATE_QUESTIONS:
        return False
    recent_messages = payload.get('recent_messages') or []
    for message in reversed(recent_messages):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        return _is_datasource_selection_state_text(message.get('content') or '')
    return False


def _selected_datasource_payload(runtime: ConversationRunContext) -> list[dict[str, Any]]:
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = snapshot.get('selected_datasource_snapshot') or []
    payload: list[dict[str, Any]] = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        payload.append({
            'datasource_id': row.get('datasource_id') or row.get('id'),
            'datasource_name': row.get('datasource_name') or row.get('name') or '',
            'datasource_identifier': row.get('datasource_identifier') or '',
            'datasource_type': row.get('datasource_type') or row.get('type') or '',
        })
    return payload


def _selected_datasource_summary(runtime: ConversationRunContext) -> dict[str, Any]:
    snapshot = runtime.active_datasource_snapshot if isinstance(runtime.active_datasource_snapshot, dict) else {}
    rows = [row for row in snapshot.get('selected_datasource_snapshot') or [] if isinstance(row, dict)]
    selected_ids = {
        to_int(value, 0)
        for value in snapshot.get('selected_datasource_ids') or []
        if to_int(value, 0) > 0
    }
    selected_rows = [
        row for row in rows
        if not selected_ids or to_int(row.get('datasource_id') or row.get('id'), 0) in selected_ids
    ]

    type_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    for row in selected_rows:
        datasource_type = str(row.get('datasource_type') or row.get('type') or 'unknown')
        selection_origin = str(row.get('selection_origin') or 'unknown')
        type_counts[datasource_type] = type_counts.get(datasource_type, 0) + 1
        origin_counts[selection_origin] = origin_counts.get(selection_origin, 0) + 1
    return {
        'selected_count': len(selected_ids) if selected_ids else len(selected_rows),
        'datasource_type_counts': type_counts,
        'selection_origin_counts': origin_counts,
    }


def _build_latest_execution_payload(executions: list[Any]) -> dict[str, Any]:
    latest_success = next(
        (
            execution for execution in reversed(executions or [])
            if getattr(execution, 'execution_status', '') == 'success'
        ),
        None,
    )
    if latest_success is None:
        return {}

    result_payload = safe_json_loads(getattr(latest_success, 'result_payload_json', ''), {})
    if not isinstance(result_payload, dict):
        result_payload = {}

    return {
        'turn_no': getattr(latest_success, 'turn_no', None),
        'execution_status': getattr(latest_success, 'execution_status', ''),
        'used_datasource_ids': safe_json_loads(getattr(latest_success, 'used_datasource_ids_json', ''), []),
        'analysis_report': clip_text(result_payload.get('analysis_report', '') or '', 700),
        'chart_count': len(result_payload.get('charts') or []),
        'table_count': len(result_payload.get('tables') or []),
    }


def _build_classifier_payload(runtime: ConversationRunContext, user_message: str) -> dict[str, Any]:
    previous_turn_no = max(int(getattr(runtime.turn, 'turn_no', 0) or 0) - 1, 0)
    recent_messages: list[dict[str, Any]] = []
    recent_executions: list[Any] = []
    pending_scope_clarification: dict[str, Any] = {}

    try:
        with session_scope(expire_on_commit=False) as session:
            service = ConversationContextService(session)
            rows = service.get_recent_prompt_messages(
                runtime.conversation.id,
                limit_messages=8,
                max_turn_no=previous_turn_no,
            )
            recent_messages = [
                {
                    'turn_no': getattr(row, 'turn_no', None),
                    'role': getattr(row, 'role', ''),
                    'content': clip_text(getattr(row, 'content', '') or '', 500),
                }
                for row in rows
                if (getattr(row, 'content', '') or '').strip()
            ]
            recent_executions = service.get_recent_executions(
                runtime.conversation.id,
                limit_items=3,
                max_turn_no=previous_turn_no,
                terminal_only=True,
            )
            pending_memory = service.get_memory(
                runtime.conversation.id,
                PENDING_SCOPE_MEMORY_TYPE,
            )
            pending_payload = safe_json_loads(
                getattr(pending_memory, 'content_json', ''),
                {},
            )
            if (
                isinstance(pending_payload, dict)
                and pending_payload.get('status') == 'pending'
            ):
                pending_scope_clarification = pending_payload
    except SQLAlchemyError:
        logger.warning(
            "LLM intent classifier failed to read recent context: conversation_id=%s turn_id=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            exc_info=True,
        )

    return {
        'current_user_message': user_message,
        'recent_messages': recent_messages,
        'latest_successful_execution': _build_latest_execution_payload(recent_executions),
        'selected_datasource_summary': _selected_datasource_summary(runtime),
        'selected_datasources': _selected_datasource_payload(runtime),
        'pending_scope_clarification': pending_scope_clarification,
    }


def resolve_context_mode_with_llm(
    runtime: ConversationRunContext,
    user_message: str,
) -> IntentClassification:
    """Resolve followup/fresh/general with an LLM classifier, falling back locally."""
    payload = _build_classifier_payload(runtime, user_message)
    if _is_conversation_state_request(payload):
        return IntentClassification(
            mode='general',
            confidence=1.0,
            reason='当前请求读取会话数据源选择状态，不需要执行数据分析',
            source='conversation_state',
            clarification_relation=(
                'new_request'
                if payload.get('pending_scope_clarification')
                else 'none'
            ),
        )
    if (
        not payload.get('pending_scope_clarification')
        and _is_explicit_datasource_analysis_request(user_message)
    ):
        return IntentClassification(
            mode='fresh_analysis',
            confidence=1.0,
            reason='当前请求明确要求分析数据源内容，范围完整性由后续准备阶段判断',
            source='explicit_analysis_request',
            clarification_relation='none',
        )
    try:
        response = create_data_insight_model().invoke([
            SystemMessage(INTENT_CLASSIFIER_SYSTEM_PROMPT),
            HumanMessage(json.dumps(payload, ensure_ascii=False, indent=2)),
        ])
        parsed = _extract_json_object(_message_content_to_text(getattr(response, 'content', response)))
        mode = str(parsed.get('mode') or '').strip()
        try:
            confidence = float(parsed.get('confidence', 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(parsed.get('reason') or '').strip()
        clarification_relation = str(
            parsed.get('clarification_relation') or 'none'
        ).strip()
        if clarification_relation not in VALID_CLARIFICATION_RELATIONS:
            clarification_relation = 'none'
        if not payload.get('pending_scope_clarification'):
            clarification_relation = 'none'
        elif clarification_relation == 'none':
            clarification_relation = 'uncertain'
            mode = 'general'
        elif clarification_relation == 'answer':
            mode = 'followup'
        elif clarification_relation in {'cancel', 'uncertain'}:
            mode = 'general'
        if mode in VALID_CONTEXT_MODES:
            return IntentClassification(
                mode=mode,
                confidence=max(0.0, min(confidence, 1.0)),
                reason=reason,
                source='llm',
                clarification_relation=clarification_relation,
            )
        return _fallback_classification(
            user_message,
            reason=f'LLM intent classifier returned invalid mode: mode={mode}, confidence={confidence}',
        )
    except Exception as exc:
        logger.warning(
            "LLM intent classifier unavailable: conversation_id=%s turn_id=%s error=%s",
            getattr(runtime.conversation, 'id', ''),
            getattr(runtime.turn, 'id', ''),
            exc,
            exc_info=True,
        )
        if payload.get('pending_scope_clarification'):
            return IntentClassification(
                mode='general',
                confidence=0.0,
                reason='待补充范围存在，但当前无法可靠判断本轮输入与澄清问题的关系',
                source='local_fallback',
                clarification_relation='uncertain',
            )
        return _fallback_classification(user_message)
