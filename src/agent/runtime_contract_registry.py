from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import inspect
import json
from pathlib import Path
import re
from typing import Any, Iterable


CONTRACT_ROOT = Path(__file__).resolve().parent / "prompts" / "runtime" / "contracts"


@dataclass(frozen=True)
class RuntimeContractPack:
    contract_id: str
    filename: str
    description: str
    helpers: tuple[str, ...] = ()
    example_output_formats: tuple[str, ...] = ()
    initial_summary: str = ""


RUNTIME_CONTRACT_PACKS: tuple[RuntimeContractPack, ...] = (
    RuntimeContractPack(
        "context.analysis", "context/analysis.md", "完整 Schema、准备信息和分析上下文的解释边界",
        initial_summary=(
            "当前问题优先；会话规则解释业务语义；Preparation 提供已选范围、确认关系和业务血缘，"
            "但不预定查询、指标、图表或结论。所有 datasource_id 和字段名都在 Schema 工作视图中；"
            "需要未展开字段的精确定义时使用本地 Schema 详情工具，不得扩大选源范围或混用来源字段。"
        ),
    ),
    RuntimeContractPack("execution.core", "execution/core.md", "Python 执行、参数、Probe、无数据、保存与修复", (
        "get_day_range", "describe_time_coverage", "probe_distinct_values",
        "probe_text_candidates", "normalize_relationship_key", "get_analysis_parameters",
        "validate_query_constraints", "raise_no_data_error", "request_retry", "save_analysis_result",
    ), initial_summary=(
        "Probe 与正式程序是独立执行单元。必要技术未知先 Probe；正式程序在一份代码中完成参数、"
        "范围校验、读取、计算、产物和一次保存。每条物理查询使用稳定 query_key，并使参数、"
        "query_constraints、实际过滤和计算粒度一致。成功空集才是无数据；技术失败不得伪装为空集。"
        "正式程序通过 submit_analysis_program 提交，修复保留业务范围和已确认事实。\n\n"
        "正式代码最小闭环：先定义 `analysis_parameters_schema={\"parameters\":[...],\"defaults\":{...}}`，"
        "再执行 `analysis_parameters=get_analysis_parameters(...)`；每条读取构造"
        "`query_constraints={\"time_range\":...,\"target_filters\":[...],\"aggregation\":...,\"notes\":...}`。"
        "这是结构化对象，不得把时间、筛选或聚合说明写成自然语言字符串："
        "`time_range` 只能为 `None` 或 `{\"start\":\"...\",\"end\":\"...\",\"end_exclusive\":True}`；"
        "每个 `target_filters` 项必须为 `{\"semantic_role\":\"...\",\"field_hint\":\"...\","
        "\"value\":\"...\",\"match_mode\":\"exact\"}`；"
        "`aggregation` 只能为 `None` 或 `{\"grain\":\"实际返回粒度\",\"metric\":\"实际聚合指标\"}`。"
        "不承担聚合的读取把 `aggregation` 写为 Python `None`，不写字符串或自造对象；"
        "使用当前来源的组合 query helper；读取后判断空集，基于真实结果计算 charts/tables/report，"
        "最后 `result=save_analysis_result(...)`。需要完整参数、范围或修复细则时读取 execution.core 参考。"
    )),
    RuntimeContractPack("execution.probe", "execution/probe.md", "有界技术探测的执行闭环", (
        "describe_time_coverage", "probe_distinct_values", "probe_text_candidates",
        "normalize_relationship_key", "raise_no_data_error", "request_retry",
    )),
    RuntimeContractPack("execution.planning", "execution/planning.md", "洞察语义到数据访问材料的工作计划"),
    RuntimeContractPack("execution.implementation", "execution/implementation.md", "正式 Python 洞察程序的最小实现闭环", (
        "get_day_range", "describe_time_coverage", "get_analysis_parameters",
        "validate_query_constraints", "raise_no_data_error", "save_analysis_result",
    )),
    RuntimeContractPack(
        "execution.result_review",
        "execution/result_review.md",
        "真实执行结果的语义一致性复核与定稿",
    ),
    RuntimeContractPack("query.relational", "query/relational.md", "关系型查询和 SQL 下推", (
        "query_data_with_sql", "load_data_with_sql", "probe_data_with_sql",
    ), example_output_formats=("standard",), initial_summary=(
        "关系表使用当前来源的 datasource_identifier 和同源字段。优先在 SQL 完成过滤、JOIN、"
        "聚合、排序和 TopN；单次物理结果上限不等于总体范围，明细过大需分页累计或下推聚合。"
        "confirmed 关系才可 JOIN；一对多前先在事实粒度聚合，避免重复计量。"
    )),
    RuntimeContractPack("query.time_series", "query/time_series.md", "timescale/vxbase 原生时序查询", (
        "query_data_with_sql", "load_data_with_sql", "probe_data_with_sql",
    ), initial_summary=(
        "时序查询必须使用来源声明的 timescale/vxbase 方言和原生采样、聚合能力；时间范围写入"
        "内部查询。用户明确 raw/采样/统计时遵从要求；长时间范围默认以原生采样或统计控制点数，"
        "不得先全量原始读取再用 pandas 降采样。读取完整方言示例前调用本地参考工具。"
    )),
    RuntimeContractPack("query.local_file", "query/local_file.md", "本地文件和低内存读取", (
        "query_local_file", "query_local_file_low_memory", "load_local_file", "load_local_file_low_memory",
    ), example_output_formats=("standard",), initial_summary=(
        "文件以 datasource_identifier 作为路径，直接调用已注入的文件函数，不 import 契约名。"
        "查询 API 先登记约束再加载；正式过滤与 filter_expressions 保持一致。大文件按批处理累计，"
        "不得重新拼接成超大 DataFrame。"
    )),
    RuntimeContractPack(
        "query.minio_file", "query/minio_file.md", "MinIO 对象文件读取", ("load_minio_file",),
        initial_summary="对象文件使用已注入的只读 MinIO loader；路径、字段和过滤范围以当前 Schema 为准。",
    ),
    RuntimeContractPack(
        "query.api", "query/api.md", "只读 API 数据访问", ("load_data_with_api",),
        initial_summary="API 数据只能通过已注入的只读 loader 获取；先核对返回字段、分页和业务范围。",
    ),
    RuntimeContractPack(
        "query.cross_source", "query/cross_source.md", "混合数据源组合和跨源边界",
        initial_summary=(
            "不同来源先在原生层完成过滤和聚合，再用已确认键组合小结果。跨源比较必须统一总体、"
            "时间、粒度和单位；无法统一时分别呈现并说明边界。"
        ),
    ),
    RuntimeContractPack("result.artifacts", "result/artifacts.md", "图表、表格和洞察报告产物", (
        "build_markdown_table", "build_table_result", "build_chart_result",
        "build_multi_metric_chart_result", "build_chart_suite",
    ), initial_summary=(
        "正式结果调用 save_analysis_result 一次收口。图表、表格与报告复用同一真实计算变量、"
        "总体、筛选和粒度；空产物不入结果。表头和叙事使用业务名称，报告先结论、再数据发现、"
        "业务含义和有事实支撑的建议；不输出内部实现、空章节或指标清单。\n\n"
        "常规表格使用 `build_table_result(title=..., columns=[{\"key\":真实字段,\"label\":业务名称}], data=DataFrame)`；"
        "columns 必须是字典列表，data 传 DataFrame 或 records，不传 values.tolist()。常规图表使用"
        "`build_chart_result(chart_kind=..., data=已聚合DataFrame, category_field=..., value_field=...)`。"
    )),
    RuntimeContractPack(
        "result.text", "result/text.md", "纯文本洞察结果",
        initial_summary="按输出契约生成已求值的纯文本洞察；结论、数值依据与边界必须来自本轮真实计算。",
    ),
    RuntimeContractPack(
        "result.json", "result/json.md", "严格 JSON 洞察结果",
        initial_summary="按输出契约的 JSON Schema 生成严格 JSON；字段和值来自本轮真实计算，不附加说明文本。",
    ),
    RuntimeContractPack(
        "interaction.clarification", "interaction/clarification.md", "同轮次业务澄清",
        initial_summary="仅当互斥业务定义会实质改变答案且 Schema、规则和 Probe 均不能确认时，才同轮次澄清；技术未知先 Probe。",
    ),
    RuntimeContractPack(
        "interaction.no_clarification", "interaction/no_clarification.md", "不可挂起调用的语义边界",
        initial_summary="当前调用不可挂起澄清；基于可确认事实完成分析，并明确不可消除的业务边界。",
    ),
    RuntimeContractPack(
        "context.followup", "context/followup.md", "追问与历史结果复用",
        initial_summary="追问以当前要求为准；历史结果用于理解指代，不替代本轮需要重新查询的事实。",
    ),
)

_PACK_BY_ID = {pack.contract_id: pack for pack in RUNTIME_CONTRACT_PACKS}
_HELPER_REFERENCE_MARKER = "<!-- runtime-helper-reference -->"
_HELPER_RESULTS = {
    "get_day_range": "(start, end)，带业务时区的 datetime，当日零点与当日最后一微秒",
    "describe_time_coverage": "dict，时间列的真实覆盖范围",
    "probe_distinct_values": "list[dict]，真实取值及频数",
    "probe_text_candidates": "list[dict]，真实值的匹配候选，不自动确认业务含义",
    "normalize_relationship_key": "规范化后的标量或 Series，不改变编码体系",
    "get_analysis_parameters": "dict，默认值与本次执行参数覆盖后的值",
    "validate_query_constraints": "None 表示通过；RetryResult 表示应停止本次查询并反馈",
    "raise_no_data_error": "抛出无数据信号，不返回正常分析结果",
    "request_retry": "RetryResult，结构化观察或修复反馈",
    "save_analysis_result": "StructuredResult，按当前输出协议保存结果",
    "query_data_with_sql": "DataFrame；校验、登记快照后执行，校验失败自动中止查询",
    "load_data_with_sql": "DataFrame；底层 SQL loader，正式查询须先通过约束校验",
    "probe_data_with_sql": (
        "ProbeQueryOutcome；包含 status/data/row_count/error_type/error_message，"
        "先检查 status：success 的 data 是 DataFrame；no_data 的 row_count=0；"
        "query_error 的 data/row_count 为 None，error_type/error_message 保留查询失败原因。"
        "DataFrame 操作作用于 outcome.data（如 len(outcome.data)、outcome.data['field']），"
        "不作用于外层状态字典。Probe 统计 helper 可直接接收 outcome；查询失败会保留原错误"
    ),
    "query_local_file": "DataFrame；校验、登记快照后加载文件",
    "query_local_file_low_memory": "iterator[DataFrame]；校验后分批加载，逐批处理",
    "load_local_file": "DataFrame，底层文件 loader",
    "load_local_file_low_memory": "iterator[DataFrame]，底层分批文件 loader",
    "load_minio_file": "DataFrame，对象文件内容",
    "load_data_with_api": "DataFrame，API 返回的数据",
    "build_markdown_table": "str，已求值 Markdown 表格",
    "build_table_result": "dict，可直接加入 tables 的表格项",
    "build_chart_result": "dict，可直接加入 charts 的图表项",
    "build_multi_metric_chart_result": "dict，多指标图表项",
    "build_chart_suite": "list[dict]，同一指标的多个图表项",
}
_QUERY_PACK_BY_CAPABILITY = {
    "sql": "query.relational",
    "time_series": "query.time_series",
    "local_file": "query.local_file",
    "minio_file": "query.minio_file",
    "api": "query.api",
}


def normalize_query_capabilities(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip().lower() for value in values or () if str(value).strip()}))


def infer_query_capabilities_from_snapshot(snapshot: dict[str, Any] | None) -> tuple[str, ...]:
    payload = snapshot if isinstance(snapshot, dict) else {}
    capabilities: set[str] = set()
    snapshot_items = [
        item
        for item in payload.get("selected_datasource_snapshot") or []
        if isinstance(item, dict)
    ]
    for item in snapshot_items:
        if not isinstance(item, dict):
            continue
        datasource_type = str(item.get("datasource_type") or "").strip().lower()
        if datasource_type == "local_file":
            capabilities.add("local_file")
            continue
        if datasource_type == "minio_file":
            capabilities.add("minio_file")
            continue
        if datasource_type == "api":
            capabilities.add("api")
            continue
        if (
            str(item.get("table_engine") or "").strip().lower() == "time_series"
            or str(item.get("ts_query_source") or "").strip().lower() in {"timescale", "vxbase"}
        ):
            capabilities.add("time_series")
        else:
            capabilities.add("sql")
    if snapshot_items:
        return tuple(sorted(capabilities))
    declared = normalize_query_capabilities(payload.get("query_capabilities"))
    if declared:
        return declared
    return tuple(sorted(capabilities))


def required_runtime_contract_ids(
    query_capabilities: Iterable[Any] | None,
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> tuple[str, ...]:
    capabilities = normalize_query_capabilities(query_capabilities)
    required = ["context.analysis", "execution.core"]
    capability_set = set(capabilities)
    query_contracts = [
        contract_id
        for capability, contract_id in _QUERY_PACK_BY_CAPABILITY.items()
        if capability in capability_set
    ]
    required.extend(query_contracts)
    if len(set(query_contracts)) > 1:
        required.append("query.cross_source")
    normalized_output_format = str(output_format or "standard").strip().lower()
    result_contract_id = {
        "standard": "result.artifacts",
        "text": "result.text",
        "json": "result.json",
    }.get(normalized_output_format)
    if result_contract_id is None:
        raise ValueError(f"Unknown Runtime output format: {output_format}")
    required.append(result_contract_id)
    required.append(
        "interaction.clarification"
        if str(clarification_mode or "same_turn").strip().lower() == "same_turn"
        else "interaction.no_clarification"
    )
    if str(context_mode or "").strip().lower() == "followup":
        required.append("context.followup")
    return tuple(dict.fromkeys(required))


def runtime_contract_manifest(
    query_capabilities: Iterable[Any] | None,
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> dict[str, Any]:
    required = required_runtime_contract_ids(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    return {
        "version": 2,
        "required": list(required),
        "output_format": output_format,
        "clarification_mode": clarification_mode,
        "delivery": "initial_summaries_injected_before_model_invocation",
        "reference": {
            "tool": "read_runtime_reference",
            "optional": True,
            "purpose": "Read complete API, dialect, edge-case and worked-example details only for contracts needed by the current work step.",
            "contracts": {
                contract_id: _PACK_BY_ID[contract_id].description
                for contract_id in required
            },
        },
    }


def render_runtime_contract_manifest(
    query_capabilities: Iterable[Any] | None,
    context_mode: str = "fresh_analysis",
    output_format: str = "standard",
    clarification_mode: str = "same_turn",
) -> str:
    payload = runtime_contract_manifest(
        query_capabilities,
        context_mode,
        output_format,
        clarification_mode,
    )
    return (
        "<runtime-contract-manifest>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</runtime-contract-manifest>"
    )


@lru_cache(maxsize=2 * len(RUNTIME_CONTRACT_PACKS))
def _load_runtime_contract_body(contract_id: str, include_examples: bool = False) -> str:
    normalized = str(contract_id or "").strip()
    pack = _PACK_BY_ID.get(normalized)
    if pack is None:
        raise ValueError(f"Unknown Runtime contract pack: {normalized}")
    source = (CONTRACT_ROOT / pack.filename).read_text(encoding="utf-8")
    inherited_sections = {
        'execution.probe': ('Python 执行环境',),
        'execution.implementation': ('Python 执行环境', '查询结果与计算语义', '可复跑分析参数', '保存、修复与安全'),
    }.get(normalized)
    if inherited_sections:
        core = (CONTRACT_ROOT / _PACK_BY_ID['execution.core'].filename).read_text(encoding='utf-8')
        inherited = []
        for title in inherited_sections:
            heading = f'## {title}'
            section = next((
                part for part in re.split(r'(?m)(?=^## )', core)
                if part.splitlines() and part.splitlines()[0] == heading
            ), None)
            if section is None:
                raise ValueError(f'Missing authoritative execution.core section: {title}')
            inherited.append(section.strip())
        if source.count('<!-- runtime-core-sections -->') != 1:
            raise ValueError(f'{normalized} must inherit execution.core sections once')
        source = source.replace('<!-- runtime-core-sections -->', '\n\n'.join(inherited))
    if include_examples:
        source = source.replace("<!-- runtime-prompt-omit-start -->", "").replace(
            "<!-- runtime-prompt-omit-end -->", ""
        )
    else:
        source = re.sub(
            r"<!-- runtime-prompt-omit-start -->.*?<!-- runtime-prompt-omit-end -->",
            "",
            source,
            flags=re.DOTALL,
        )
    return source.strip()


@lru_cache(maxsize=len(_HELPER_RESULTS))
def runtime_helper_signature(name: str) -> str:
    # Resolve lazily: the agent imports tools before assembling its prompts.
    from agent.tools import _build_execution_namespace

    helper = _build_execution_namespace().get(name)
    if not callable(helper):
        raise ValueError(f"Unknown Runtime helper: {name}")
    signature = inspect.signature(helper)
    signature = signature.replace(
        parameters=[p.replace(annotation=inspect.Parameter.empty) for p in signature.parameters.values()],
        return_annotation=inspect.Signature.empty,
    )
    return f"{name}{signature}"


def _render_contract_pack(
    contract_id: str, seen_helpers: set[str], *, include_examples: bool = False,
) -> str:
    body = _load_runtime_contract_body(contract_id, include_examples)
    pack = _PACK_BY_ID[contract_id]
    if not pack.helpers:
        return body
    if body.count(_HELPER_REFERENCE_MARKER) != 1:
        raise ValueError(f"Runtime helper reference must appear once in {contract_id}")
    entries = []
    for name in pack.helpers:
        if name not in seen_helpers:
            entries.append(f"- `{runtime_helper_signature(name)}`\n  返回：{_HELPER_RESULTS[name]}。")
            seen_helpers.add(name)
    reference = "\n\n".join(entries) or "本节使用前文已列出的同名查询 API。"
    return body.replace(_HELPER_REFERENCE_MARKER, reference)


def _render_initial_contract_pack(contract_id: str, seen_helpers: set[str]) -> str:
    """Render the bounded first-turn view of a contract.

    The complete contract remains the source of truth and is returned by
    ``read_runtime_reference``.  The initial view is deliberately an executable
    summary plus live signatures, so a Runtime starts with one semantic picture
    instead of every dialect example and exceptional branch competing for attention.
    """
    pack = _PACK_BY_ID[contract_id]
    entries = []
    for name in pack.helpers:
        if name not in seen_helpers:
            entries.append(f"- `{runtime_helper_signature(name)}`\n  返回：{_HELPER_RESULTS[name]}。")
            seen_helpers.add(name)
    helper_reference = "\n\n".join(entries)
    sections = [f"# {contract_id}", pack.initial_summary.strip()]
    if helper_reference:
        sections.append("## 可直接调用的函数\n" + helper_reference)
    sections.append(
        "当前工作进入对应执行状态后，系统会提供该能力所需的完整方言、"
        "组合示例和边界细则；当前摘要只用于识别后续应进入的工作。"
    )
    return "\n\n".join(section for section in sections if section).strip()


@lru_cache(maxsize=len(RUNTIME_CONTRACT_PACKS))
def load_runtime_contract_pack(contract_id: str) -> str:
    return _render_contract_pack(str(contract_id or "").strip(), set())


@lru_cache(maxsize=3 * len(RUNTIME_CONTRACT_PACKS))
def load_runtime_contract_reference(contract_id: str, *, output_format: str = "standard") -> str:
    """Return a standalone reference with live signatures and complete examples."""
    normalized = str(contract_id or "").strip()
    pack = _PACK_BY_ID.get(normalized)
    if pack is None:
        raise ValueError(f"Unknown Runtime contract pack: {normalized}")
    include_examples = not pack.example_output_formats or output_format in pack.example_output_formats
    return _render_contract_pack(
        normalized, set(), include_examples=include_examples,
    )


def load_runtime_contract_text(
    contract_ids: Iterable[Any], *, output_format: str = "standard",
    example_contract_ids: Iterable[str] = (),
) -> str:
    normalized_ids = tuple(dict.fromkeys(str(value).strip() for value in contract_ids if str(value).strip()))
    unknown = [contract_id for contract_id in normalized_ids if contract_id not in _PACK_BY_ID]
    if unknown:
        raise ValueError("Unknown Runtime contract pack(s): " + ", ".join(unknown))
    seen_helpers: set[str] = set()
    return "\n\n---\n\n".join(
        _render_contract_pack(
            contract_id, seen_helpers,
            include_examples=(contract_id in example_contract_ids
                              and (not _PACK_BY_ID[contract_id].example_output_formats
                                   or output_format in _PACK_BY_ID[contract_id].example_output_formats)),
        ) for contract_id in normalized_ids
    )


def load_runtime_work_contract_text(
    contract_ids: Iterable[Any], *, work: str, output_format: str = "standard",
    example_contract_ids: Iterable[str] = (),
) -> str:
    """Render the state-scoped Runtime work packet.

    A code-writing work step receives the complete protocol set required by
    the selected datasource capabilities. Non-code work receives the complete
    contract it actively owns (for example planning or result review) and only
    summaries of surrounding contracts. This is responsibility-based loading,
    not character-budget truncation.
    """
    normalized_ids = tuple(dict.fromkeys(
        str(value).strip() for value in contract_ids if str(value).strip()
    ))
    unknown = [contract_id for contract_id in normalized_ids if contract_id not in _PACK_BY_ID]
    if unknown:
        raise ValueError("Unknown Runtime contract pack(s): " + ", ".join(unknown))
    seen_helpers: set[str] = set()
    rendered = []
    active_non_code_contracts = {
        "plan": {"execution.planning"},
        "review": {"execution.result_review"},
    }.get(str(work or "").strip(), set())
    for contract_id in normalized_ids:
        pack = _PACK_BY_ID[contract_id]
        is_code_execution_work = work in {"probe", "probe_repair", "implement", "repair"}
        wants_example = (
            contract_id in example_contract_ids
            and (not pack.example_output_formats or output_format in pack.example_output_formats)
        )
        if is_code_execution_work or contract_id in active_non_code_contracts or wants_example:
            rendered.append(_render_contract_pack(
                contract_id, seen_helpers, include_examples=wants_example,
            ))
        else:
            rendered.append(_render_initial_contract_pack(contract_id, seen_helpers))
    return "\n\n---\n\n".join(rendered)


def load_runtime_initial_contract_text(contract_ids: Iterable[Any]) -> str:
    """Return the first-turn work packet for the selected contracts.

    A datasource query dialect is required before the very first Probe, so its
    complete contract is delivered immediately. Context, execution, result and
    interaction packs start as concise summaries and their detailed branches are
    delivered only when the ReAct work reaches that phase.
    """
    normalized_ids = tuple(dict.fromkeys(str(value).strip() for value in contract_ids if str(value).strip()))
    unknown = [contract_id for contract_id in normalized_ids if contract_id not in _PACK_BY_ID]
    if unknown:
        raise ValueError("Unknown Runtime contract pack(s): " + ", ".join(unknown))
    seen_helpers: set[str] = set()
    rendered = []
    for contract_id in normalized_ids:
        if contract_id.startswith("query."):
            rendered.append(_render_contract_pack(contract_id, seen_helpers))
        else:
            rendered.append(_render_initial_contract_pack(contract_id, seen_helpers))
    return "\n\n---\n\n".join(rendered)


def runtime_contract_pack_ids() -> tuple[str, ...]:
    return tuple(pack.contract_id for pack in RUNTIME_CONTRACT_PACKS)
