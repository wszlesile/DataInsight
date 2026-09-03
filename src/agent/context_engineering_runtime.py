import copy
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config import Config
from config.database import SessionLocal
from model import InsightDatasource, InsightNsConversation, InsightNsExecution, InsightNsRelDatasource
from service.conversation_context_service import ConversationContextService
from agent.runtime_contract_registry import infer_query_capabilities_from_snapshot
from utils import logger
from utils.context_compression import clip_text
from utils.datasource_utils import (
    build_table_runtime_metadata,
    extract_datasource_identifier,
    extract_datasource_schema,
    normalize_datasource_type,
    recommend_local_file_loader,
    safe_json_loads,
    to_int,
)
from utils.token_budget import fit_system_messages_within_budget, take_tail_messages_within_budget


ANALYSIS_SIGNAL_PATTERN = re.compile(
    r"(今天|昨天|前天|最近|本周|上周|本月|上月|今年|去年|季度|Q[1-4]|同比|环比|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}日|"
    r"明细|详情|图表|报告|趋势|统计|分析|计算|对比|稼动率|开机率|根因|时间线)"
)

QUERY_EXECUTION_CAPABILITIES = {
    "max_rows_per_sql_result": 10000,
    "capacity_scope": "each physical load_data_with_sql execution",
    "relational_sql_dialect": "mysql_compatible",
    "full_population_strategies": [
        "push filters, joins and final aggregation into the query",
        "use stable pagination and incrementally reduce each page",
    ],
    "truncation_policy": (
        "Do not apply an arbitrary LIMIT to a full-population metric. "
        "LIMIT is valid only for an intentional TopN, bounded detail or sample output."
    ),
}
FOLLOWUP_ANALYSIS_KEYWORDS = (
    "继续", "刚才", "上次", "上一轮", "沿用", "基于刚才", "按刚才",
    "继续上一个", "继续上一轮", "修复一个", "重试", "重新生成", "继续分析",
    "前面对话", "前面的需求", "前面说的", "上面说的", "刚才说的",
    "不想再重复", "不想重复", "我已经说过", "已经说过", "自己看前面",
    "你自己看前面", "接着前面", "按之前的需求", "之前的需求",
    "continue", "previous", "last result", "same logic",
)
OUTPUT_FOLLOWUP_TARGET_WORDS = (
    "图表报告", "分析图表", "趋势图", "折线图", "柱状图", "饼图", "曲线图",
    "明细表", "汇总表", "数据表", "图表", "可视化", "报告", "表格",
    "图形", "图像", "图", "展示", "呈现", "显示",
    "chart", "charts", "graph", "plot", "report", "table",
)
OUTPUT_FOLLOWUP_NEUTRAL_WORDS = (
    "帮我", "请", "麻烦", "给我", "一下", "一个", "个", "一份", "一版",
    "的", "地", "得", "吧", "呢", "吗", "成", "为",
    "一种", "种", "这份", "这个", "当前", "现在", "分析", "数据", "结果", "内容",
    "类型", "形式", "样式", "样子", "方式",
    "换", "改", "转", "变", "重新", "再", "另", "另外", "用", "按", "以",
    "做", "出", "生成", "补充", "整理", "展示", "显示", "呈现", "看看",
    "看下", "看一下", "画", "绘制", "重画", "输出",
) + OUTPUT_FOLLOWUP_TARGET_WORDS
EXPLICIT_TIME_SCOPE_PATTERN = re.compile(
    r"(今天|昨天|前天|最近|本周|上周|本月|上月|今年|去年|季度|Q[1-4]|"
    r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}月\d{1,2}日)"
)


class CustomContext(BaseModel):
    """Runtime context passed through the agent and tool layers."""

    username: str
    namespace_id: int = 0
    conversation_id: int = 0
    turn_id: int = 0
    user_query: str = ''
    expected_time_range: dict[str, Any] = Field(default_factory=dict)
    platform_authorization: str = ''
    fedquery_authorization: str = ''
    database_conn_info: dict[str, Any] = Field(default_factory=dict)
    active_datasource_snapshot: dict[str, Any] = Field(default_factory=dict)
    execution_datasource_snapshot: dict[str, Any] = Field(default_factory=dict)
    analysis_task_id: int | None = None
    analysis_parameters_override: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(
        default_factory=lambda: {"type": "standard"}
    )
    clarification_mode: str = "same_turn"
    evidence_enabled: bool = True


def classify_analysis_context_mode(user_message: str) -> str:
    text = (user_message or '').strip()
    if not text:
        return 'general'

    lower_text = text.lower()
    if any(keyword in text for keyword in FOLLOWUP_ANALYSIS_KEYWORDS) or any(
        keyword in lower_text for keyword in FOLLOWUP_ANALYSIS_KEYWORDS
    ):
        return 'followup'
    if _is_short_output_followup_request(text, lower_text):
        return 'followup'
    if ANALYSIS_SIGNAL_PATTERN.search(text):
        return 'fresh_analysis'
    return 'general'


def _is_short_output_followup_request(text: str, lower_text: str) -> bool:
    if len(text) > 40:
        return False
    if not (
        any(keyword in text for keyword in OUTPUT_FOLLOWUP_TARGET_WORDS)
        or any(keyword in lower_text for keyword in OUTPUT_FOLLOWUP_TARGET_WORDS)
    ):
        return False
    if EXPLICIT_TIME_SCOPE_PATTERN.search(text):
        return False

    remainder = lower_text
    for word in sorted(OUTPUT_FOLLOWUP_NEUTRAL_WORDS, key=len, reverse=True):
        if word:
            remainder = remainder.replace(word.lower(), '')
    remainder = re.sub(r"[\s,，。.!！?？、;；:：'\"“”‘’（）()【】\[\]{}<>《》/\\|-]+", '', remainder)
    return not remainder


def is_analysis_like_request(user_message: str) -> bool:
    return classify_analysis_context_mode(user_message) in ('followup', 'fresh_analysis')


def _build_success_fact_execution_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": item.get("execution_id"),
        "turn_id": item.get("turn_id"),
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "execution_status": item.get("execution_status", ""),
        "used_datasource_ids": item.get("used_datasource_ids", []),
        "analysis_report_preview": item.get("analysis_report_preview", ""),
        "chart_count": item.get("chart_count", 0),
        "table_count": item.get("table_count", 0),
        "execution_seconds": item.get("execution_seconds"),
        "finished_at": item.get("finished_at"),
    }


def _prune_analysis_state_payload(payload: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == 'followup':
        return payload

    sanitized = dict(payload)
    recent_execution_summaries = payload.get("recent_execution_summaries") or []
    successful_summaries = [
        _build_success_fact_execution_summary(item)
        for item in recent_execution_summaries
        if isinstance(item, dict) and item.get("execution_status") == "success"
    ]

    if mode in {'fresh_analysis', 'general'}:
        sanitized["recent_execution_summaries"] = successful_summaries[-2:]
        latest_execution = payload.get("latest_execution") or {}
        if isinstance(latest_execution, dict) and latest_execution.get("execution_status") == "success":
            sanitized["latest_execution"] = _build_success_fact_execution_summary(latest_execution)
        else:
            sanitized["latest_execution"] = {}
        return sanitized

    sanitized["recent_execution_summaries"] = []
    sanitized["latest_execution"] = {}
    return sanitized


def _build_datasource_payload_item(datasource: InsightDatasource) -> dict[str, Any]:
    config_json = safe_json_loads(datasource.datasource_config_json, {})
    datasource_type = normalize_datasource_type(datasource.datasource_type)
    payload = {
        "datasource_id": datasource.id,
        "datasource_type": datasource_type,
        "datasource_name": datasource.datasource_name,
        "datasource_identifier": extract_datasource_identifier(datasource, config_json),
        "metadata_schema": extract_datasource_schema(datasource, config_json),
    }
    if datasource_type == "local_file":
        payload["recommended_loader"] = recommend_local_file_loader(
            config_json,
            Config.LOCAL_FILE_LOW_MEMORY_THRESHOLD_BYTES,
        )
    if datasource_type == "table":
        payload.update(build_table_runtime_metadata(config_json))
    sheet_name = str(config_json.get("sheet_name") or "").strip()
    if sheet_name:
        payload["sheet_name"] = sheet_name
    return payload


def _build_datasource_prompt_item(item: dict[str, Any]) -> dict[str, Any]:
    """Remove duplicated schema indexes while preserving every field definition."""
    payload = copy.deepcopy(item)
    payload.pop("selection_origin", None)
    schema = payload.get("metadata_schema")
    if not isinstance(schema, dict):
        return payload

    properties = schema.get("properties")
    property_names = list(properties) if isinstance(properties, dict) else []
    if isinstance(properties, dict):
        for definition in properties.values():
            if isinstance(definition, dict) and definition.get("example") is None:
                definition.pop("example", None)
    queryable_fields = list(property_names)
    capabilities = payload.get("field_capabilities")
    if isinstance(capabilities, dict) and isinstance(
        capabilities.get("queryable_fields"),
        list,
    ):
        queryable_fields = [
            str(value)
            for value in capabilities["queryable_fields"]
            if str(value).strip()
        ]
    if str(payload.get("table_engine") or "").lower() == "time_series":
        backend = str(payload.get("ts_query_source") or "").lower()
        physical_time_field = "ts" if backend == "vxbase" else "timeStamp"
        value_fields = [
            str(value)
            for value in payload.get("value_fields") or []
            if str(value).strip()
        ]
        quality_field = str(payload.get("quality_field") or "status").strip()
        queryable_fields = list(dict.fromkeys([
            physical_time_field,
            "name",
            quality_field,
            *value_fields,
        ]))
        payload["time_field"] = physical_time_field
    query_contract: dict[str, Any] = {
        "datasource_id": payload.get("datasource_id"),
        "datasource_identifier": payload.get("datasource_identifier"),
    }
    if property_names and set(queryable_fields) == set(property_names):
        query_contract["allowed_fields_ref"] = "metadata_schema.properties"
        query_contract["all_schema_properties_allowed"] = True
    else:
        query_contract["allowed_fields"] = queryable_fields
    if str(payload.get("table_engine") or "").lower() == "time_series":
        query_contract["physical_time_field"] = payload.get("time_field")
        query_contract["ts_query_source"] = payload.get("ts_query_source")
    elif payload.get("datasource_type") == "table":
        query_contract["sql_dialect"] = QUERY_EXECUTION_CAPABILITIES["relational_sql_dialect"]
    known_key_cardinalities: list[dict[str, Any]] = []
    seen_key_cardinalities: set[tuple[tuple[str, ...], int]] = set()
    for relationship in schema.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        cardinality = relationship.get("observed_key_cardinality")
        if not isinstance(cardinality, dict):
            continue
        fields = tuple(
            str(value).strip()
            for value in relationship.get("local_fields") or []
            if str(value).strip()
        )
        try:
            distinct_count = int(cardinality.get("local_distinct_key_count") or 0)
        except (TypeError, ValueError):
            distinct_count = 0
        signature = (fields, distinct_count)
        if not fields or distinct_count <= 0 or signature in seen_key_cardinalities:
            continue
        seen_key_cardinalities.add(signature)
        known_key_cardinalities.append({
            "fields": list(fields),
            "observed_distinct_count": distinct_count,
            "relationship_id": str(relationship.get("relationship_id") or ""),
        })
    if known_key_cardinalities:
        query_contract["known_key_cardinalities"] = known_key_cardinalities
        max_rows = int(QUERY_EXECUTION_CAPABILITIES["max_rows_per_sql_result"])
        capacity_risks = [
            {
                "fields": item["fields"],
                "observed_distinct_count": item["observed_distinct_count"],
                "max_rows_per_sql_result": max_rows,
                "required_strategy": (
                    "query_side_final_aggregation_or_stable_pagination"
                ),
            }
            for item in known_key_cardinalities
            if item["observed_distinct_count"] > max_rows
        ]
        if capacity_risks:
            query_contract["capacity_risks"] = capacity_risks
    payload["query_contract"] = query_contract
    required = schema.get("required")
    if (
        property_names
        and isinstance(required, list)
        and set(str(value) for value in required) == set(property_names)
    ):
        schema.pop("required", None)
        schema["all_properties_required"] = True

    if isinstance(capabilities, dict):
        queryable_fields = capabilities.get("queryable_fields")
        if (
            property_names
            and isinstance(queryable_fields, list)
            and set(str(value) for value in queryable_fields) == set(property_names)
        ):
            capabilities.pop("queryable_fields", None)
            capabilities["all_properties_queryable"] = True
        if not any(capabilities.values()):
            payload.pop("field_capabilities", None)
    return payload


def _build_execution_context_item(execution: InsightNsExecution, include_code: bool = False) -> dict[str, Any]:
    payload = {
        "execution_id": execution.id,
        "turn_id": execution.turn_id,
        "title": execution.title,
        "description": execution.description,
        "execution_status": execution.execution_status,
        "used_datasource_ids": safe_json_loads(getattr(execution, 'used_datasource_ids_json', ''), []),
        "analysis_report": clip_text(
            execution.analysis_report or '',
            Config.CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS,
        ),
        "error_message": execution.error_message,
        "execution_seconds": execution.execution_seconds,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
    }
    if include_code:
        payload["generated_code"] = clip_text(
            execution.generated_code or '',
            Config.CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS,
        )
    else:
        payload["generated_code_preview"] = clip_text(
            execution.generated_code or '',
            Config.CONTEXT_COMPRESSION_EXECUTION_PREVIEW_CHARS,
        )
    return payload


def _load_conversation(conversation_id: int) -> InsightNsConversation | None:
    if conversation_id <= 0:
        return None

    session = SessionLocal()
    try:
        return session.query(InsightNsConversation).filter(
            InsightNsConversation.id == conversation_id,
            InsightNsConversation.is_deleted == 0,
        ).first()
    finally:
        session.close()


def _load_conversation_datasources(conversation_id: int) -> list[InsightDatasource]:
    if conversation_id <= 0:
        return []

    session = SessionLocal()
    try:
        return session.query(InsightDatasource).join(
            InsightNsRelDatasource,
            InsightNsRelDatasource.datasource_id == InsightDatasource.id,
        ).filter(
            InsightNsRelDatasource.insight_conversation_id == conversation_id,
            InsightNsRelDatasource.is_deleted == 0,
            InsightDatasource.is_deleted == 0,
        ).order_by(
            InsightNsRelDatasource.sort_no.asc(),
            InsightNsRelDatasource.id.asc(),
        ).all()
    finally:
        session.close()


def get_datasource_message(
    namespace_id: int,
    conversation_id: int,
    snapshot_override: dict[str, Any] | None = None,
    context_mode: str = "analysis",
) -> SystemMessage | None:
    namespace_id_int = to_int(namespace_id, 0)
    conversation_id_int = to_int(conversation_id, 0)
    if conversation_id_int <= 0 and namespace_id_int <= 0:
        return None

    snapshot: dict[str, Any] = snapshot_override or {}
    if not snapshot:
        session = SessionLocal()
        try:
            snapshot = ConversationContextService(session).get_active_datasource_snapshot(conversation_id_int)
        finally:
            session.close()

    conversation = _load_conversation(conversation_id_int)
    if conversation is not None:
        namespace_id_int = conversation.insight_namespace_id
    elif namespace_id_int <= 0:
        namespace_id_int = to_int(snapshot.get('namespace_id'), 0)

    snapshot_items = snapshot.get('selected_datasource_snapshot', [])
    if snapshot_items:
        datasource_items = [
            _build_datasource_prompt_item(item)
            for item in snapshot_items
            if isinstance(item, dict)
        ]
    else:
        datasource_items = [
            _build_datasource_payload_item(datasource)
            for datasource in _load_conversation_datasources(conversation_id_int)
        ]
    if not datasource_items:
        payload: dict[str, Any] = {"datasources": [], "selected_datasource_ids": []}
        if namespace_id_int > 0:
            payload["namespace_id"] = namespace_id_int
        if context_mode == "general":
            return SystemMessage(
                "当前会话数据源上下文：当前没有关联任何可用数据源。\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
            )
        return SystemMessage(
            "当前会话没有关联任何可直接使用的数据源。\n"
            "- 如果用户当前是在发起数据分析、统计、绘图、报表或依赖数据源内容的问题，不要调用分析探测或正式程序工具，不要生成 Python 代码。\n"
            "- 请直接用自然语言告诉用户：当前会话还没有关联数据源，需要先关联相关数据源后再进行分析。\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    semantic_context = snapshot.get("analysis_semantic_context")
    selected_ids = [
        to_int(item, 0)
        for item in snapshot.get('selected_datasource_ids', [])
        if to_int(item, 0) > 0
    ]
    if context_mode == "general":
        general_items: list[dict[str, Any]] = []
        selected_id_set = set(selected_ids)
        datasource_type_counts: dict[str, int] = {}
        selection_origin_counts: dict[str, int] = {}
        snapshot_item_by_id = {
            to_int(row.get("datasource_id") or row.get("id"), 0): row
            for row in snapshot_items
            if isinstance(row, dict)
        }
        for item in datasource_items:
            datasource_id = to_int(item.get("datasource_id"), 0)
            general_item = {
                "datasource_id": datasource_id or item.get("datasource_id"),
                "datasource_name": item.get("datasource_name"),
                "datasource_type": item.get("datasource_type"),
                "metadata_schema": copy.deepcopy(item.get("metadata_schema") or {}),
            }
            datasource_identifier = item.get("datasource_identifier")
            if datasource_identifier:
                general_item["datasource_identifier"] = datasource_identifier
            sheet_name = item.get("sheet_name")
            if sheet_name:
                general_item["sheet_name"] = sheet_name
            if selected_id_set:
                general_item["selected_for_current_context"] = datasource_id in selected_id_set
            general_items.append(general_item)

            if not selected_id_set or datasource_id in selected_id_set:
                datasource_type = str(item.get("datasource_type") or "unknown")
                datasource_type_counts[datasource_type] = datasource_type_counts.get(datasource_type, 0) + 1
                original_item = snapshot_item_by_id.get(datasource_id, {})
                selection_origin = str(original_item.get("selection_origin") or "unknown")
                selection_origin_counts[selection_origin] = selection_origin_counts.get(selection_origin, 0) + 1

        general_payload: dict[str, Any] = {
            "datasource_selection_summary": {
                "selected_count": len(selected_ids) if selected_ids else len(general_items),
                "datasource_type_counts": datasource_type_counts,
                "selection_origin_counts": selection_origin_counts,
            },
            "datasources": general_items,
        }
        if namespace_id_int > 0:
            general_payload["namespace_id"] = namespace_id_int
        return SystemMessage(
            "当前会话数据源上下文：\n"
            "- `datasource_selection_summary` 是后端根据当前会话快照计算的权威选择状态；询问已选数据源数量或构成时直接使用它，不要自行遍历 Schema 计数，也不要沿用历史轮次中的旧数量。\n"
            "- `datasources` 提供当前会话可用于理解问题的数据源名称、类型和完整 Schema。\n"
            "- `metadata_schema.properties` 中的字段名、展示名和说明可用于解释数据结构与字段语义。\n"
            "- `selected_for_current_context=true` 表示该数据源属于当前已选范围；未提供该标记时，表示系统没有额外声明选择范围。\n"
            "- `datasource_id` 和 `datasource_identifier` 只用于定位，不代表业务含义；除非用户明确询问技术标识，否则使用 `datasource_name` 对外表述。\n"
            f"{json.dumps(general_payload, ensure_ascii=False, indent=2)}"
        )

    payload: dict[str, Any] = {
        "runtime_capabilities": {
            "query_execution": copy.deepcopy(QUERY_EXECUTION_CAPABILITIES),
        },
    }
    if isinstance(semantic_context, dict) and semantic_context:
        # Put the semantic handoff before the large Schema payload.  The runtime
        # still owns queries, metrics and narrative, but it must first see every
        # user-requested business question that the Schema is meant to answer.
        payload["analysis_semantic_context"] = semantic_context
    if selected_ids:
        payload["selected_datasource_ids"] = selected_ids
    if namespace_id_int > 0:
        payload["namespace_id"] = namespace_id_int
    payload["datasources"] = datasource_items

    instruction_lines = [
        "<runtime-context-instructions>",
        "以下 JSON 是本轮完整分析上下文，包含原始 Schema、业务规则与带确认状态的准备信息，不是报告模板或指标计划：",
        "- `datasources` 是 Preparation 选定的完整数据源上下文；每个 `metadata_schema.properties` 都是完整字段定义，没有按 Prompt 预算截断。",
        "- `datasource_name` 是用户可读名称；`query_contract.datasource_identifier` 是物理访问标识。`allowed_fields` 是显式字段边界；若为 `allowed_fields_ref=metadata_schema.properties`，则完整 properties 键集合就是字段边界。",
        "- `analysis_semantic_context` 是用户业务目标、开放式问题和已确认血缘的准备信息。Runtime 仍自主决定查询、指标、产物和叙事。",
        "- Schema 中关系、observed_value_profile、requested_value_resolutions、aggregation_safety 等状态按其原始含义使用；unresolved/unknown 不等同于已确认事实。",
        "- `runtime_capabilities.query_execution` 是当前执行环境的确定性容量与查询能力；容量边界作用于每次物理查询。具体 API 和方言由 Runtime Contract Pack 提供。",
    ]
    if selected_ids and semantic_context:
        instruction_lines.append(
            f"- `selected_datasource_ids` 当前为 {selected_ids}，表示 Preparation 围绕本轮业务意图选定的数据源语义空间；正式分析不再重新选源。"
        )
        instruction_lines.append(
            "- 逐项阅读 `analysis_semantic_context.business_questions`，保持用户明确要求的业务范围；它不规定具体指标和报告章节。"
        )
        instruction_lines.append(
            "- `analysis_semantic_context.analytical_facets` 是开放式洞察方向，不规定报告章节或结论。"
        )
    elif selected_ids:
        instruction_lines.append(
            f"- `selected_datasource_ids` 当前为 {selected_ids}，表示当前会话已选候选范围。"
        )
    else:
        instruction_lines.append(
            "- 当前快照没有显式 selected_datasource_ids；以 `datasources` 中实际提供的完整来源为当前可用范围。"
        )

    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
    )
    logger.info(
        "Runtime事实信封体积 semantic_chars=%s datasource_chars=%s capability_chars=%s total_chars=%s datasource_count=%s",
        len(json.dumps(payload.get("analysis_semantic_context") or {}, ensure_ascii=False, separators=(',', ':'))),
        len(json.dumps(payload.get("datasources") or [], ensure_ascii=False, separators=(',', ':'))),
        len(json.dumps(payload.get("runtime_capabilities") or {}, ensure_ascii=False, separators=(',', ':'))),
        len(payload_json),
        len(payload.get("datasources") or []),
    )
    return SystemMessage(
        "\n".join(instruction_lines) + "\n</runtime-context-instructions>\n"
        + payload_json,
        additional_kwargs={
            "runtime_query_capabilities": list(infer_query_capabilities_from_snapshot({
                "selected_datasource_snapshot": datasource_items,
            })),
        },
    )


def get_history_messages(
    conversation_id: int,
    max_turn_no: int | None = None,
    user_message: str = '',
    token_budget: int | None = None,
    context_mode: str | None = None,
) -> list[Any]:
    if not conversation_id:
        return []

    session = SessionLocal()
    try:
        service = ConversationContextService(session)
        rows = service.get_recent_prompt_messages(
            conversation_id,
            limit_messages=Config.CONTEXT_COMPRESSION_HISTORY_MESSAGE_LIMIT,
            max_turn_no=max_turn_no,
        )
    finally:
        session.close()

    context_mode = context_mode or classify_analysis_context_mode(user_message)
    result: list[Any] = []
    last_human_text = ''
    for row in rows:
        text = (row.content or '').strip()
        if not text:
            continue
        if row.role == 'user':
            if context_mode == 'fresh_analysis' and text == last_human_text:
                continue
            last_human_text = text
            result.append(HumanMessage(text))
        elif row.role == 'assistant':
            if context_mode == 'fresh_analysis':
                continue
            result.append(AIMessage(text))

    return take_tail_messages_within_budget(result, token_budget)


def get_memory_messages(
    conversation_id: int,
    user_message: str = '',
    max_turn_no: int | None = None,
    active_snapshot_override: dict[str, Any] | None = None,
    token_budget: int | None = None,
    context_mode: str | None = None,
) -> list[SystemMessage]:
    if not conversation_id:
        return []

    session = SessionLocal()
    try:
        service = ConversationContextService(session)
        messages: list[SystemMessage] = []
        context_mode = context_mode or classify_analysis_context_mode(user_message)

        summary_text = service.build_runtime_summary_text(conversation_id, max_turn_no=max_turn_no)
        if summary_text:
            messages.append(SystemMessage(f"历史摘要：\n{summary_text}"))

        compression_metadata = service.get_context_compression_metadata(
            conversation_id,
            max_turn_no=max_turn_no,
        )
        if compression_metadata:
            messages.append(SystemMessage(
                "上下文压缩元数据：\n"
                f"{json.dumps(compression_metadata, ensure_ascii=False, indent=2)}"
            ))

        analysis_state_payload = service.build_runtime_analysis_state(
            conversation_id,
            max_turn_no=max_turn_no,
            active_snapshot_override=active_snapshot_override,
        )
        if analysis_state_payload:
            if active_snapshot_override:
                # The same snapshot is already part of the mandatory datasource
                # message. Keeping it here would duplicate full schemas and may
                # present the canonical semantic context as optional memory.
                analysis_state_payload.pop("active_datasource_snapshot", None)
            analysis_state_payload = _prune_analysis_state_payload(
                analysis_state_payload,
                mode=context_mode,
            )
            messages.append(SystemMessage(
                "当前分析状态：\n"
                f"{json.dumps(analysis_state_payload, ensure_ascii=False, indent=2)}"
            ))

        if context_mode == 'followup':
            recent_executions = service.get_recent_executions(
                conversation_id,
                limit_items=3,
                max_turn_no=max_turn_no,
                terminal_only=True,
            )
            if recent_executions:
                execution_payload = [
                    _build_execution_context_item(execution)
                    for execution in recent_executions
                ]
                messages.append(SystemMessage(
                    "最近代码执行记录：\n"
                    f"{json.dumps(execution_payload, ensure_ascii=False, indent=2)}"
                ))

                latest_execution = recent_executions[-1]
                if latest_execution.generated_code:
                    messages.append(SystemMessage(
                        "最近一次成功或最新执行的 Python 分析代码：\n"
                        f"{clip_text(latest_execution.generated_code, Config.CONTEXT_COMPRESSION_EXECUTION_CODE_CHARS)}"
                    ))

            recent_artifacts = service.get_recent_artifacts(
                conversation_id,
                limit_items=3,
                max_turn_no=max_turn_no,
            )
            if recent_artifacts:
                artifact_payload = [
                    service._build_artifact_summary_item(artifact)
                    for artifact in recent_artifacts
                ]
                messages.append(SystemMessage(
                    "最近派生产物摘要：\n"
                    f"{json.dumps(artifact_payload, ensure_ascii=False, indent=2)}"
                ))

        return fit_system_messages_within_budget(messages, token_budget)
    finally:
        session.close()
