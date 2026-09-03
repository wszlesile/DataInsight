import ast
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.runtime_contract_registry import (
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
    contract = _contract("execution_core.md")

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


def test_time_series_contract_preserves_native_dialect_capabilities():
    contract = _contract("query_time_series.md")

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
    context = _contract("context_analysis.md")
    artifacts = _contract("result_artifacts.md")
    followup = _contract("context_followup.md")

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
        ("query_relational.md", "relational"),
        ("query_local_file.md", "local_file"),
    ):
        contract = _contract(filename)
        match = re.search(
            rf"<!-- canonical-runtime-example: {example_id} -->\s*```python\s*(.*?)\s*```",
            contract,
            flags=re.DOTALL,
        )
        assert match is not None
        assert _validate_generated_code_contract(match.group(1)) is None
