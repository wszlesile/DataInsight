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
from utils.datasource_utils import safe_json_loads
from utils.llm_model_factory import create_data_insight_model

VALID_CONTEXT_MODES = {'followup', 'fresh_analysis', 'general'}

INTENT_CLASSIFIER_SYSTEM_PROMPT = """你是 DataInsight 的轻量级对话意图分类器。
你只判断 current_user_message 和最近上下文的关系，不做数据分析，不写 SQL，不调用工具。

请在以下三个 mode 中选择一个：
- followup：当前输入主要承接最近一次或前几次分析，例如要求补图表、换图表、换展示方式、继续分析、修正上次结果、基于刚才结果补充报告；且没有明确提出新的数据源、新对象、新指标或新的时间范围。
- fresh_analysis：当前输入本身提出了新的数据分析目标，或者明确要求分析新的对象、数据源、指标、时间范围、全部/所有数据源。
- general：普通问答、问候、解释概念、闲聊，或不需要进入数据分析链路的问题。

注意：
- “生成/补充/整理/换一种图表/图表报告/可视化/报告”这类短句，如果语义上是在延续最近分析结果，通常是 followup。
- 如果当前问题显式指定了新数据范围、新数据源、新对象、新时间范围，应归为 fresh_analysis。
- 最近执行记录中的 used_datasource_ids 只是判断上下文关系的参考，不代表当前必须沿用。

只输出 JSON，不要输出其他文字：
{"mode":"followup|fresh_analysis|general","confidence":0.0-1.0,"reason":"不超过80字的中文理由"}"""


@dataclass(frozen=True)
class IntentClassification:
    mode: str
    confidence: float
    reason: str = ''
    source: str = 'local_fallback'


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
        'selected_datasources': _selected_datasource_payload(runtime),
    }


def resolve_context_mode_with_llm(
    runtime: ConversationRunContext,
    user_message: str,
) -> IntentClassification:
    """Resolve followup/fresh/general with an LLM classifier, falling back locally."""
    payload = _build_classifier_payload(runtime, user_message)
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
        if mode in VALID_CONTEXT_MODES:
            return IntentClassification(
                mode=mode,
                confidence=max(0.0, min(confidence, 1.0)),
                reason=reason,
                source='llm',
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
        return _fallback_classification(user_message)
