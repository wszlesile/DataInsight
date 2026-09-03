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


RUNTIME_CONTRACT_PACKS: tuple[RuntimeContractPack, ...] = (
    RuntimeContractPack("context.analysis", "context_analysis.md", "完整 Schema、准备信息和分析上下文的解释边界"),
    RuntimeContractPack("execution.core", "execution_core.md", "Python 执行、参数、Probe、无数据、保存与修复", (
        "get_day_range", "describe_time_coverage", "probe_distinct_values",
        "probe_text_candidates", "normalize_relationship_key", "get_analysis_parameters",
        "validate_query_constraints", "raise_no_data_error", "request_retry", "save_analysis_result",
    )),
    RuntimeContractPack("query.relational", "query_relational.md", "关系型查询和 SQL 下推", (
        "query_data_with_sql", "load_data_with_sql", "probe_data_with_sql",
    ), example_output_formats=("standard",)),
    RuntimeContractPack("query.time_series", "query_time_series.md", "timescale/vxbase 原生时序查询", (
        "query_data_with_sql", "load_data_with_sql", "probe_data_with_sql",
    )),
    RuntimeContractPack("query.local_file", "query_local_file.md", "本地文件和低内存读取", (
        "query_local_file", "query_local_file_low_memory", "load_local_file", "load_local_file_low_memory",
    ), example_output_formats=("standard",)),
    RuntimeContractPack("query.minio_file", "query_minio_file.md", "MinIO 对象文件读取", ("load_minio_file",)),
    RuntimeContractPack("query.api", "query_api.md", "只读 API 数据访问", ("load_data_with_api",)),
    RuntimeContractPack("query.cross_source", "query_cross_source.md", "混合数据源组合和跨源边界"),
    RuntimeContractPack("result.artifacts", "result_artifacts.md", "图表、表格和洞察报告产物", (
        "build_markdown_table", "build_table_result", "build_chart_result",
        "build_multi_metric_chart_result", "build_chart_suite",
    )),
    RuntimeContractPack("result.text", "result_text.md", "纯文本洞察结果"),
    RuntimeContractPack("result.json", "result_json.md", "严格 JSON 洞察结果"),
    RuntimeContractPack("interaction.clarification", "interaction_clarification.md", "同轮次业务澄清"),
    RuntimeContractPack("interaction.no_clarification", "interaction_no_clarification.md", "不可挂起调用的语义边界"),
    RuntimeContractPack("context.followup", "context_followup.md", "追问与历史结果复用"),
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
        "delivery": "injected_by_backend_before_model_invocation",
        "reference": {
            "tool": "read_runtime_reference",
            "optional": True,
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


def load_runtime_contract_text(contract_ids: Iterable[Any]) -> str:
    normalized_ids = tuple(dict.fromkeys(str(value).strip() for value in contract_ids if str(value).strip()))
    unknown = [contract_id for contract_id in normalized_ids if contract_id not in _PACK_BY_ID]
    if unknown:
        raise ValueError("Unknown Runtime contract pack(s): " + ", ".join(unknown))
    seen_helpers: set[str] = set()
    return "\n\n---\n\n".join(_render_contract_pack(contract_id, seen_helpers) for contract_id in normalized_ids)


def runtime_contract_pack_ids() -> tuple[str, ...]:
    return tuple(pack.contract_id for pack in RUNTIME_CONTRACT_PACKS)
