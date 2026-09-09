import ast
from pathlib import Path
import re
import sys

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.runtime_contract_registry import (
    infer_query_capabilities_from_snapshot,
    load_runtime_contract_text,
    runtime_contract_pack_ids,
)
from agent.runtime_prompt_loader import load_runtime_prompt
from agent.tools import _validate_generated_code_contract


RUNTIME_ROOT = SRC_ROOT / "agent" / "prompts" / "runtime"
CONTRACT_ROOT = RUNTIME_ROOT / "contracts"


def _contract(name: str) -> str:
    return (CONTRACT_ROOT / name).read_text(encoding="utf-8")


def _execution_namespace_names() -> set[str]:
    tree = ast.parse((SRC_ROOT / "agent" / "tools.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_execution_namespace":
            for statement in node.body:
                if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Dict):
                    return {
                        key.value
                        for key in statement.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
    raise AssertionError("_build_execution_namespace return dict not found")


def test_every_exposed_runtime_api_is_documented_by_a_contract():
    # Check the text delivered to the model, not authoring-only examples that
    # the registry deliberately removes from the runtime prompt.
    contracts = load_runtime_contract_text(runtime_contract_pack_ids())
    undocumented = sorted(
        name
        for name in _execution_namespace_names()
        if name != "pd" and name not in contracts
    )
    assert undocumented == []


def test_execution_contract_preserves_baseline_closed_loop_capabilities():
    contract = _contract("execution/core.md")

    markers = (
        'execution_intent = "analysis"',
        'execution_intent = "probe"',
        '"parameters": [',
        '"defaults":',
        'analysis_parameters = get_analysis_parameters',
        '"aggregation": {"grain":',
        "validate_query_constraints(",
        "query_data_with_sql(",
        "params=sql_params",
        "sql_context=sql_context",
        "scope_source_query_key",
        "probe_data_with_sql",
        "raise_no_data_error",
        "base_execution_id + patches",
        "save_analysis_result(",
    )
    for marker in markers:
        assert marker in contract


def test_file_query_contract_uses_injected_functions_not_contract_id_as_import_path():
    contract = _contract("query/local_file.md")
    assert "已直接注入代码执行命名空间" in contract
    assert "不能从 `query.local_file`、`runtime_api`" in contract


def test_time_series_contract_preserves_native_dialect_capabilities():
    contract = _contract("query/time_series.md")

    markers = (
        "time_bucket('1 minute'",
        "time_bucket_gapfill",
        "date_bin",
        'inner_sql.replace("\'", "\'\'")',
        'interval("1m", "10s")',
        'interpolation("previous")',
        "avg(value)",
        "value_avg/value_max",
        "不支持 `ORDER BY ts`",
        "`LIMIT ... OFFSET ...`",
        '"kind": "time_series_sql"',
        "unknown tag/no results",
    )
    for marker in markers:
        assert marker in contract


def test_context_and_artifact_contracts_preserve_semantic_capabilities():
    context = _contract("context/analysis.md")
    artifacts = _contract("result/artifacts.md")
    followup = _contract("context/followup.md")

    for marker in (
        "metadata_schema.properties",
        "没有按 Prompt 预算截断",
        "analysis_semantic_context",
        "不是 SQL、指标、图表或报告模板",
        "analysis_semantic_context.explicit_time_range",
        "不能用扩展窗口替换用户总体",
        "requested_value_resolutions",
        "unsupported_field_bindings",
        "aggregation_safety=unsafe_repeated_by_join",
        "used_datasource_ids",
    ):
        assert marker in context

    for marker in (
        "build_multi_metric_chart_result",
        "每个 `(category, series)` 组合只能一行",
        "毛利率、处理率、平均成本",
        "可比窗口",
        "动态判断句",
        "建议只对应已计算",
    ):
        assert marker in artifacts

    for marker in (
        "当前分析状态",
        "历史摘要",
        "最近代码执行记录",
        "最近派生产物摘要",
        "当前用户最新要求",
    ):
        assert marker in followup


def test_compiled_prompt_is_capability_complete_without_tool_round():
    relational = load_runtime_prompt(("sql",), "fresh_analysis")
    time_series = load_runtime_prompt(("time_series",), "fresh_analysis")
    mixed = load_runtime_prompt(
        ("sql", "time_series", "local_file"),
        "followup",
    )

    assert "load_runtime_contracts" not in relational + time_series + mixed
    assert "# context.analysis" in relational
    assert "# query.relational" in relational
    assert "# query.time_series" not in relational
    assert "# query.time_series" in time_series
    assert "# query.relational" not in time_series
    assert "# query.cross_source" in mixed
    assert "# context.followup" in mixed
    assert len(mixed) < 35000


def test_runtime_prompt_keeps_evidence_out_of_formal_analysis():
    prompt = load_runtime_prompt(
        ("sql", "time_series", "local_file", "minio_file", "api"),
        "followup",
    )

    assert "EvidenceDefinition" not in prompt
    assert "TraceRef" not in prompt
    assert "evidence_definitions =" not in prompt


def test_canonical_runtime_examples_satisfy_static_contracts():
    for filename, example_id in (
        ("query/relational.md", "relational"),
        ("query/local_file.md", "local_file"),
    ):
        contract = _contract(filename)
        match = re.search(
            rf"<!-- canonical-runtime-example: {example_id} -->\s*```python\s*(.*?)\s*```",
            contract,
            flags=re.DOTALL,
        )
        assert match is not None
        assert _validate_generated_code_contract(match.group(1)) is None


@pytest.mark.parametrize("capabilities", [
    ("sql",), ("time_series",), ("local_file",),
    ("minio_file",), ("api",), ("sql", "time_series", "local_file"),
])
@pytest.mark.parametrize("output_format", ["standard", "text", "json"])
@pytest.mark.parametrize("context_mode", ["fresh_analysis", "followup"])
@pytest.mark.parametrize("clarification_mode", ["same_turn", "disabled"])
def test_delivered_prompt_preserves_common_semantics_across_modes(
    capabilities, output_format, context_mode, clarification_mode,
):
    prompt = load_runtime_prompt(
        capabilities, context_mode, output_format, clarification_mode,
    )
    for marker in (
        "真实列、字段类型、分组粒度和排序键",
        "多维聚合结果继续显式保留全部维度",
        "排序使用原始时间、数值或等级键",
        "不能用 `head(N)`",
        "有效样本量",
        "分子复用同一数量或筛选结果",
        "analysis_semantic_context.analysis_as_of_date",
        "实际最大日期或最大已完成周期",
        "`COUNT` 为 0",
        "某个金额、比率或其他指标为 0 不等于总体为空",
    ):
        assert marker in prompt
    assert ("# context.followup" in prompt) == (context_mode == "followup")
    assert ("# result.artifacts" in prompt) == (output_format == "standard")
    if output_format == "standard":
        for marker in (
            "支持 0 到 10 的整数", "不保证用字符串补齐末尾的 0",
            "多个业务上彼此独立的数据源", "只有编码而无法取得名称",
            "只有 helper 确实无法表达的图表类型",
        ):
            assert marker in prompt


@pytest.mark.parametrize("values,expected", [
    ([10.0], "当前只有一个类别"),
    ([10.0, 10.0], "并列最高"),
    ([10.0, 4.0], "高 6.00"),
])
def test_file_example_judgment_uses_actual_results(values, expected):
    match = re.search(
        r"<!-- canonical-runtime-example: local_file -->\s*```python\s*(.*?)\s*```",
        _contract("query/local_file.md"), re.DOTALL,
    )
    assert match is not None
    data = pd.DataFrame({
        "category": ["类别A", "类别B"][:len(values)], "metric_value": values,
    })
    def fail_no_data(*args, **kwargs):
        raise AssertionError("Nonempty example unexpectedly reported no data")

    namespace = {
        "get_analysis_parameters": lambda defaults: defaults,
        "query_local_file": lambda **kwargs: data.copy(),
        "raise_no_data_error": fail_no_data,
        "build_chart_result": lambda **kwargs: kwargs,
        "build_table_result": lambda **kwargs: kwargs,
        "save_analysis_result": lambda **kwargs: kwargs,
    }
    exec(compile(match.group(1), "<local-file-contract-example>", "exec"), namespace)
    result = namespace["result"]
    assert expected in result["analysis_report"]
    assert result["charts"] and result["tables"]
    assert len(result["tables"][0]["data"]) == len(values)


@pytest.mark.parametrize("source", [
    {"datasource_type": "table", "table_engine": "relational"},
    {"datasource_type": "table", "table_engine": "time_series"},
    {"datasource_type": "local_file"},
])
def test_same_type_multiple_sources_receive_independent_analysis_guidance(source):
    capabilities = infer_query_capabilities_from_snapshot({
        "selected_datasource_snapshot": [
            {**source, "datasource_id": 1}, {**source, "datasource_id": 2},
        ],
    })
    prompt = load_runtime_prompt(capabilities)
    assert "# query.cross_source" not in prompt
    assert "多个业务上彼此独立的数据源默认分别分析、一源一图" in prompt
    assert "用户要求对比、关联或叠加" in prompt
