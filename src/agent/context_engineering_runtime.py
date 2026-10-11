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
from agent.runtime_schema_memory import merge_schema_sources
from utils import logger
from utils.context_compression import clip_text
from utils.datasource_utils import (
    build_table_query_access,
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
    "relational_sql_dialect": "fedquery_portable_subset",
    "relational_identifier_quote": '"',
    "relational_sql_profile": "fedquery_portable_subset",
    "operation_capabilities": {
        "string_aggregation": "unsupported",
    },
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
    # This is a runtime-owned work-state attribute. The request compiler must
    # carry it into every projected model view instead of silently falling
    # back to the fresh-analysis contract set on follow-up turns.
    context_mode: str = "fresh_analysis"
    clarification_mode: str = "same_turn"
    evidence_enabled: bool = True
    # Local-only metadata used by the request compiler. It is intentionally
    # excluded from the LLM-facing datasource envelope.
    working_memory_catalog: list[dict[str, Any]] = Field(default_factory=list)
    # Bodies are held separately from the catalog so the working-context
    # compiler can project them once for the active work, rather than the
    # outer ReAct recovery prompt replaying the same payloads as free text.
    runtime_probe_fact_ledgers: list[dict[str, Any]] = Field(default_factory=list)
    runtime_schema_detail_workspace: list[dict[str, Any]] = Field(default_factory=list)
    # Small control-plane packet. It contains lifecycle state and references,
    # while producer-owned Schema, Probe facts, code and artifacts stay in
    # their existing authoritative stores.
    runtime_work_packet: dict[str, Any] = Field(default_factory=dict)


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
        payload["query_access"] = build_table_query_access(config_json)
    sheet_name = str(config_json.get("sheet_name") or "").strip()
    if sheet_name:
        payload["sheet_name"] = sheet_name
    return payload


def _build_datasource_prompt_item(item: dict[str, Any]) -> dict[str, Any]:
    """Remove duplicated schema indexes while preserving every field definition."""
    payload = copy.deepcopy(item)
    payload.pop("selection_origin", None)
    if payload.get("datasource_type") == "local_file":
        payload["query_helper"] = (
            "query_local_file_low_memory"
            if payload.get("recommended_loader") == "load_local_file_low_memory"
            else "query_local_file"
        )
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
    query_access = payload.pop("query_access", None)
    query_contract: dict[str, Any] = {
        "datasource_id": payload.get("datasource_id"),
        "datasource_identifier": payload.get("datasource_identifier"),
    }
    if isinstance(query_access, dict):
        query_contract.update({
            key: copy.deepcopy(value)
            for key, value in query_access.items()
            if value not in (None, "", [], {})
        })
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
        query_contract["identifier_quote"] = QUERY_EXECUTION_CAPABILITIES["relational_identifier_quote"]
        query_contract["sql_profile"] = QUERY_EXECUTION_CAPABILITIES["relational_sql_profile"]
        query_contract["operation_capabilities"] = copy.deepcopy(
            QUERY_EXECUTION_CAPABILITIES["operation_capabilities"]
        )
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
    if isinstance(snapshot.get("runtime_schema_workspace"), dict):
        payload["runtime_schema_workspace"] = copy.deepcopy(snapshot["runtime_schema_workspace"])

    instruction_lines = [
        "<runtime-context-instructions>",
        "以下 JSON 是本轮完整分析上下文，包含 Schema 工作视图、业务规则与带确认状态的准备信息，不是报告模板或指标计划：",
        "- `datasources` 是 Preparation 选定的完整数据源范围。默认工作视图保留全部字段名、已绑定事实字段和关系键；若 `runtime_schema_workspace.mode=indexed`，未展开字段的精确定义可用 `read_runtime_datasource_context` 本地读取，不会改变分析范围。",
        "- `datasource_name` 是用户可读名称；关系来源必须按 `query_contract.access_mode` 访问。`access_mode=uns_get` 时，SQL 的 FROM/JOIN 必须直接使用 `relation_expression`；`datasource_identifier` 仅保留为技术定位与血缘标识。`allowed_fields` 是显式字段边界；若为 `allowed_fields_ref=metadata_schema.properties`，则完整 properties 键集合就是字段边界。",
        "- `analysis_semantic_context` 保留用户业务目标、开放式问题和选源范围；字段含义与来源关系由数据源 Schema 承载。Runtime 自主决定查询、指标、产物和叙事。",
        "- `analysis_semantic_context.question_fact_coverage` 是 Selection 已完成的每个问题/业务事实/数据源覆盖状态；`runtime_probe_agenda` 是后端据此生成的技术工作状态。二者都不预先规定指标、图表、结论或报告章节。",
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
        instruction_lines.append(
            "- `runtime_probe_agenda.pending_technical_checks` 汇总准备信息中已知的待核实点；结合实际查询是否依赖它决定探测，已有观察直接复用。"
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
        "<runtime-datasource-envelope>\n"
        + "\n".join(instruction_lines) + "\n</runtime-context-instructions>\n"
        + payload_json
        + "\n</runtime-datasource-envelope>",
        additional_kwargs={
            "runtime_query_capabilities": list(infer_query_capabilities_from_snapshot({
                "selected_datasource_snapshot": datasource_items,
            })),
        },
    )


def build_runtime_datasource_working_message(
    prompt_snapshot: dict[str, Any] | None,
    execution_snapshot: dict[str, Any] | None,
    *,
    work: str,
    datasource_ids: list[int] | tuple[int, ...] = (),
    field_names_by_datasource: dict[str, list[str]] | None = None,
    material_plan: dict[str, Any] | None = None,
    schema_detail_workspace: list[dict[str, Any]] | None = None,
    omit_replayed_details: bool = False,
) -> SystemMessage:
    """Build the state-specific Runtime datasource carrier.

    Preparation's full snapshot remains the execution authority.  This function
    only changes how that authority is presented to the model: every selected
    datasource remains available. Semantic understanding receives complete
    selected Schema; executable work retains its current field projection.
    """
    prompt_snapshot = prompt_snapshot if isinstance(prompt_snapshot, dict) else {}
    execution_snapshot = (
        execution_snapshot if isinstance(execution_snapshot, dict) else prompt_snapshot
    )
    prompt_rows = _runtime_snapshot_rows(prompt_snapshot)
    execution_rows = _runtime_snapshot_rows(execution_snapshot)
    if not prompt_rows:
        prompt_rows = execution_rows
    execution_by_id = {
        _runtime_datasource_id(row): row
        for row in execution_rows
        if _runtime_datasource_id(row) > 0
    }
    selected_ids = _runtime_selected_ids(prompt_snapshot, prompt_rows)
    requested_ids = [
        datasource_id
        for datasource_id in dict.fromkeys(to_int(value, 0) for value in datasource_ids)
        if datasource_id > 0 and datasource_id in set(selected_ids)
    ]
    fields_by_id = _runtime_requested_fields(field_names_by_datasource or {})
    workspace_by_id = {
        _runtime_datasource_id(source): source
        for source in (schema_detail_workspace or [])
        if isinstance(source, dict) and _runtime_datasource_id(source) > 0
    }
    if work == 'understand':
        requested_ids = list(selected_ids)
        understanding_sources = merge_schema_sources(execution_rows or prompt_rows, list(workspace_by_id.values()))
        execution_by_id = {
            _runtime_datasource_id(row): row for row in understanding_sources
            if _runtime_datasource_id(row) in set(selected_ids)
        }
        fields_by_id = {
            datasource_id: tuple((source.get('metadata_schema') or {}).get('properties') or {})
            for datasource_id, source in execution_by_id.items()
        }
    elif work == 'plan':
        requested_ids = list(dict.fromkeys([*requested_ids, *workspace_by_id]))
    # Exact Schema definitions are selected by the material plan or reader;
    # the source index does not imply a second, parallel field-binding carrier.
    for datasource_id in requested_ids:
        workspace_schema = workspace_by_id.get(datasource_id, {}).get('metadata_schema')
        workspace_properties = (
            workspace_schema.get('properties')
            if isinstance(workspace_schema, dict) and isinstance(workspace_schema.get('properties'), dict)
            else {}
        )
        # A detail read is useful while composing the material plan, where the
        # model is deciding which exact fields belong to the next unit of
        # work.  Once the plan exists, replaying every field from every prior
        # detail read defeats that projection and makes implementation grow
        # with exploratory reads.  The planned field set remains authoritative
        # for Probe/implementation; a newly discovered dependency can still
        # be fetched through the detail reader in that focused work state.
        if workspace_properties and work in {'understand', 'plan'}:
            fields_by_id[datasource_id] = tuple(dict.fromkeys([
                *fields_by_id.get(datasource_id, ()),
                *[str(field) for field in workspace_properties],
            ]))
    datasource_index = [
        _runtime_source_identity(row) if work in {'understand', 'review'} else _runtime_datasource_index_item(row)
        for row in prompt_rows
    ]
    # After a successful reader call the exact field definitions live in the
    # typed workspace.  Reuse that canonical copy for the current material
    # scope instead of making the model reconstruct its state from one or
    # more historical ToolMessages.  The complete execution snapshot remains
    # the fallback and the source of the complete field-name index.
    detail_sources = workspace_by_id if omit_replayed_details and work != 'understand' else execution_by_id
    datasource_details = [
        _runtime_datasource_detail_item(
            detail_sources.get(datasource_id) or execution_by_id.get(datasource_id)
            or _runtime_row_by_id(prompt_rows, datasource_id),
            requested_fields=fields_by_id.get(datasource_id, ()),
            fallback_index=_runtime_row_by_id(prompt_rows, datasource_id),
        )
        for datasource_id in requested_ids
        if detail_sources.get(datasource_id) is not None
            or execution_by_id.get(datasource_id) is not None
            or _runtime_row_by_id(prompt_rows, datasource_id) is not None
    ]
    payload = {
        "runtime_capabilities": {
            "query_execution": copy.deepcopy(QUERY_EXECUTION_CAPABILITIES),
        },
        "analysis_semantic_context": _runtime_semantic_work_view(
            prompt_snapshot.get("analysis_semantic_context"),
            requested_ids=requested_ids,
            material_plan=material_plan,
        ),
        "selected_datasource_ids": selected_ids,
        "datasource_index": datasource_index,
        "datasource_details": datasource_details,
        "runtime_schema_workspace": {
            "mode": "state_scoped",
            "current_work": str(work or "understand"),
            "detail_tool": "read_runtime_datasource_context",
            "detail_datasource_ids": requested_ids,
            "detail_fields_by_datasource": {
                str(datasource_id): list(fields)
                for datasource_id, fields in fields_by_id.items()
                if datasource_id in requested_ids and fields
            },
        },
    }
    # ``runtime_material_plan`` is deliberately carried by
    # ``runtime-working-context`` below, not duplicated inside this datasource
    # envelope.  The envelope owns source facts; the work context owns the
    # current decision and plan.  Keeping both copies made an implementation
    # request grow linearly with the plan without adding a new fact.
    instruction = [
        "<runtime-datasource-envelope>",
        "<runtime-context-instructions>",
        "以下 JSON 是当前 Runtime 工作视图。它保持 Preparation 已确认的完整分析范围，但按当前工作提供不同粒度的材料。",
        (
            "- `selected_datasource_ids` 和 `datasource_index` 是本轮完整数据源语义空间；Review 只需使用来源身份核对程序、查询事实与候选产物，不重新展开 Schema。"
            if work == "review" else
            "- `selected_datasource_ids` 和 `datasource_index` 是本轮完整数据源语义空间；索引中的字段名可用于决定后续需要哪些精确定义，不能据此缩小分析范围。"
        ),
        (
            "- 当前语义确认工作直接提供全部已选来源的 `datasource_details`，完整承接字段定义、枚举、公式、时间语义及来源关系；索引只保留来源身份，避免重复字段定义。"
            if work == 'understand' else
            "- `datasource_details` 只展开当前工作明确声明的数据源和字段，字段定义、枚举、公式和格式以这里为准；数据源用途说明单独位于 `dataset_context`，其证据等级为 context_only。它可能来自刚读取的 canonical Schema 工作区；需要新的精确字段或关系时，调用 `read_runtime_datasource_context`，该读取不改变完整数据源范围。"
        ),
        "- 如果 `datasource_details` 为空，说明当前材料尚未形成足够的精确 Schema；先读取实际依赖，不能猜测字段。",
        "- `analysis_semantic_context` 只保留用户意图和选源覆盖。字段语义、枚举、时间含义与来源关系都在数据源 Schema 中；需要精确定义时使用 `read_runtime_datasource_context` 读取。",
        "- `runtime_capabilities.query_execution` 是每次物理查询的确定性容量边界，具体查询 API 由当前 Runtime Contract Pack 提供。",
        "</runtime-context-instructions>",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "</runtime-datasource-envelope>",
    ]
    return SystemMessage(
        "\n".join(instruction),
        additional_kwargs={
            "runtime_query_capabilities": list(infer_query_capabilities_from_snapshot({
                "selected_datasource_snapshot": prompt_rows,
            })),
        },
    )


def runtime_schema_workspace_covers_material_scope(
    schema_detail_workspace: list[dict[str, Any]] | None,
    datasource_ids: list[int] | tuple[int, ...],
    field_names_by_datasource: dict[str, list[str]] | None,
) -> bool:
    """Whether canonical exact Schema covers every field of the active work.

    A reader may be invoked several times while the model refines a material
    plan.  Only compact its historical ToolMessages after the typed workspace
    itself can reproduce every active source/field dependency.  This keeps
    compaction lossless and lets a new dependency remain visible until it has
    actually been read.
    """
    requested_ids = [
        datasource_id
        for datasource_id in dict.fromkeys(to_int(value, 0) for value in datasource_ids)
        if datasource_id > 0
    ]
    if not requested_ids:
        return False
    fields_by_id = _runtime_requested_fields(field_names_by_datasource or {})
    workspace_by_id = {
        _runtime_datasource_id(source): source
        for source in (schema_detail_workspace or [])
        if isinstance(source, dict) and _runtime_datasource_id(source) > 0
    }
    for datasource_id in requested_ids:
        required_fields = fields_by_id.get(datasource_id, ())
        if not required_fields:
            return False
        source = workspace_by_id.get(datasource_id)
        schema = source.get('metadata_schema') if isinstance(source, dict) else {}
        properties = (
            schema.get('properties')
            if isinstance(schema, dict) and isinstance(schema.get('properties'), dict)
            else {}
        )
        if any(not isinstance(properties.get(field), dict) for field in required_fields):
            return False
    return True


def _runtime_snapshot_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in snapshot.get("selected_datasource_snapshot") or []
        if isinstance(row, dict)
    ]


def _runtime_datasource_id(row: dict[str, Any]) -> int:
    return to_int(row.get("datasource_id") or row.get("id"), 0)


def _runtime_selected_ids(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> list[int]:
    declared = [to_int(value, 0) for value in snapshot.get("selected_datasource_ids") or []]
    return list(dict.fromkeys([
        *[value for value in declared if value > 0],
        *[_runtime_datasource_id(row) for row in rows if _runtime_datasource_id(row) > 0],
    ]))


def _runtime_row_by_id(rows: list[dict[str, Any]], datasource_id: int) -> dict[str, Any] | None:
    return next((row for row in rows if _runtime_datasource_id(row) == datasource_id), None)


def _runtime_requested_fields(raw: dict[str, list[str]]) -> dict[int, tuple[str, ...]]:
    normalized: dict[int, tuple[str, ...]] = {}
    for datasource_id, values in raw.items():
        normalized_id = to_int(datasource_id, 0)
        if normalized_id <= 0 or not isinstance(values, list):
            continue
        fields = tuple(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))
        if fields:
            normalized[normalized_id] = fields
    return normalized


def _runtime_query_contract(
    row: dict[str, Any],
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the single executable access contract exposed to Runtime.

    Execution snapshots retain ``query_access`` for backend compatibility,
    while prompt snapshots expose ``query_contract``.  A state-scoped work
    view may be built from either snapshot, so normalize both here instead of
    making the model reconcile two representations.
    """
    fallback = fallback if isinstance(fallback, dict) else {}
    contract: dict[str, Any] = {}
    for source in (fallback, row):
        query_contract = source.get("query_contract")
        if isinstance(query_contract, dict):
            contract.update(copy.deepcopy(query_contract))
        query_access = source.get("query_access")
        if isinstance(query_access, dict):
            contract.update({
                key: copy.deepcopy(value)
                for key, value in query_access.items()
                if value not in (None, "", [], {})
            })
    datasource_id = _runtime_datasource_id(row) or _runtime_datasource_id(fallback)
    if datasource_id > 0:
        contract["datasource_id"] = datasource_id
    if contract.get("access_mode") != "uns_get":
        identifier = str(
            row.get("datasource_identifier")
            or fallback.get("datasource_identifier")
            or ""
        ).strip()
        if identifier:
            contract.setdefault("datasource_identifier", identifier)
    else:
        # A physical alias is backend lineage metadata, not an alternative
        # executable relation for an UNS-permission-bound source.
        contract.pop("datasource_identifier", None)
    return contract


def _runtime_source_identity(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "datasource_id", "datasource_name", "datasource_type", "source_role", "uns_path", "schema_scope", "reference_target",
        "table_engine", "ts_query_source", "time_field", "sheet_name", "recommended_loader", "query_helper",
    )
    item = {
        key: copy.deepcopy(row[key])
        for key in keys
        if row.get(key) not in (None, "", [], {})
    }
    if item.get("datasource_type") == "local_file":
        item["query_helper"] = (
            "query_local_file_low_memory"
            if item.get("recommended_loader") == "load_local_file_low_memory"
            else "query_local_file"
        )
    query_contract = _runtime_query_contract(row)
    if query_contract:
        item["query_contract"] = query_contract
    if query_contract.get("access_mode") != "uns_get" and row.get("datasource_identifier"):
        item["datasource_identifier"] = copy.deepcopy(row["datasource_identifier"])
    return item


def _clip_runtime_work_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _runtime_datasource_index_item(row: dict[str, Any]) -> dict[str, Any]:
    schema = row.get("metadata_schema") if isinstance(row.get("metadata_schema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    fields: dict[str, dict[str, Any]] = {}
    for field_name, definition in properties.items():
        definition = definition if isinstance(definition, dict) else {}
        fields[str(field_name)] = {
            key: copy.deepcopy(definition[key])
            for key in ("type", "format", "title", "displayName")
            if key in definition
        }
    item = _runtime_source_identity(row)
    item["schema_summary"] = {
        "name": schema.get("name"),
        "fields": fields,
    }
    dataset_context = _runtime_dataset_context(schema)
    if dataset_context:
        item["dataset_context"] = dataset_context
    if schema.get("semantic_enrichment_status"):
        item["schema_summary"]["semantic_enrichment_status"] = schema["semantic_enrichment_status"]
    return item


def _runtime_dataset_context(schema: dict[str, Any]) -> dict[str, str]:
    """Keep a datasource-purpose description distinct from field-level evidence."""
    description = _clip_runtime_work_text(str(schema.get("description") or ""), 320)
    if not description:
        return {}
    return {
        "description": description,
        "role": "dataset_purpose_context",
        "evidence_authority": "context_only",
    }


def _runtime_datasource_detail_item(
    row: dict[str, Any] | None,
    *,
    requested_fields: tuple[str, ...],
    fallback_index: dict[str, Any] | None,
) -> dict[str, Any]:
    row = row or fallback_index or {}
    schema = row.get("metadata_schema") if isinstance(row.get("metadata_schema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    fallback_schema = (
        (fallback_index or {}).get("metadata_schema")
        if isinstance((fallback_index or {}).get("metadata_schema"), dict)
        else {}
    )
    fallback_properties = (
        fallback_schema.get("properties")
        if isinstance(fallback_schema.get("properties"), dict)
        else {}
    )
    # A work selection normally provides the exact fields.  Bound fields from
    # Preparation remain visible in the compact index and can be explicitly
    # added through the local reader when a new dependency is discovered.
    detail_properties = {
        field: copy.deepcopy(properties.get(field, fallback_properties.get(field)))
        for field in requested_fields
        if isinstance(properties.get(field, fallback_properties.get(field)), dict)
    }
    item = {
        key: copy.deepcopy(value) for key, value in row.items()
        if key not in {
            'metadata_schema', 'field_capabilities', 'query_access', 'query_contract',
        }
    }
    query_contract = _runtime_query_contract(row, fallback_index)
    if query_contract:
        item["query_contract"] = query_contract
    if query_contract.get("access_mode") == "uns_get":
        item.pop("datasource_identifier", None)
    dataset_context = _runtime_dataset_context(schema or fallback_schema)
    if dataset_context:
        item["dataset_context"] = dataset_context
    projected_schema = copy.deepcopy(schema)
    projected_schema.pop("description", None)
    item["metadata_schema"] = {
        **projected_schema,
        "name": schema.get("name") or fallback_schema.get("name"),
        "properties": detail_properties,
    }
    item["loaded_field_names"] = list(detail_properties)
    item["all_field_names"] = list(dict.fromkeys([*fallback_properties, *properties]))
    return item


def _runtime_semantic_work_view(
    semantic_context: Any,
    *,
    requested_ids: list[int],
    material_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic = semantic_context if isinstance(semantic_context, dict) else {}
    result = {
        key: copy.deepcopy(semantic.get(key))
        for key in (
            "analysis_as_of_date", "explicit_time_range", "business_goal",
            "business_subjects", "analytical_facets", "business_questions",
        )
        if semantic.get(key) not in (None, "", [], {})
    }
    # Preserve Selection's source discovery so Runtime can ground facts rather
    # than repeat selection. Exact fields and relations remain owned by Schema.
    result["question_fact_coverage"] = [
        {
            "question_id": item.get("question_id"),
            "coverage_status": item.get("coverage_status"),
            **({"fact_coverage": [
                {
                    key: copy.deepcopy(fact.get(key))
                    for key in (
                        "fact_id", "coverage_status", "datasource_ids",
                        "supporting_datasource_ids", "semantic_reference_datasource_ids",
                        "derived_from_fact_ids",
                    )
                    if fact.get(key) not in (None, "", [], {})
                }
                for fact in item.get("fact_coverage") or []
                if isinstance(fact, dict)
            ]} if item.get("fact_coverage") else {}),
        }
        for item in semantic.get("question_fact_coverage") or []
        if isinstance(item, dict)
    ]
    agenda = semantic.get("runtime_probe_agenda")
    if isinstance(agenda, dict):
        result["runtime_probe_agenda"] = {
            "version": agenda.get("version"),
            "pending_technical_checks": [
                copy.deepcopy(item)
                for item in agenda.get("pending_technical_checks") or []
                if isinstance(item, dict)
            ],
        }
    references = semantic.get("semantic_references")
    if isinstance(references, list) and references:
        result["semantic_references"] = [
            {
                key: copy.deepcopy(item.get(key))
                for key in ("datasource_id", "datasource_name", "description", "field_names", "metadata_schema")
                if item.get(key) not in (None, "", [], {})
            }
            for item in references
            if isinstance(item, dict)
        ]
    return result


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
