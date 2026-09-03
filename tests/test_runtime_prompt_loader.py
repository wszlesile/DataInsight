from pathlib import Path
import json
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.runtime_contract_registry import (
    infer_query_capabilities_from_snapshot,
    load_runtime_contract_text,
    required_runtime_contract_ids,
    runtime_contract_pack_ids,
)
from agent.runtime_prompt_loader import (
    describe_runtime_prompt,
    load_runtime_contract_prompt,
    load_runtime_prompt,
    load_runtime_semantic_prompt,
)


def _manifest(prompt: str) -> dict:
    match = re.search(
        r"<runtime-contract-manifest>\s*(\{.*?\})\s*</runtime-contract-manifest>",
        prompt,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_initial_runtime_prompt_is_small_semantic_kernel_with_manifest():
    prompt = load_runtime_prompt(("sql",), "fresh_analysis")

    assert len(prompt) < 30000
    assert "唯一语义所有权" in prompt
    assert "不要退化为“指标 A：数值；指标 B：数值”" in prompt
    assert "load_runtime_contracts" not in prompt
    assert "load_data_with_sql" in prompt
    assert "injected_by_backend_before_model_invocation" in prompt
    assert _manifest(prompt)["required"] == [
        "context.analysis",
        "execution.core",
        "query.relational",
        "result.artifacts",
        "interaction.clarification",
    ]


def test_semantic_and_mechanical_prompt_sections_can_be_ordered_around_context():
    semantic = load_runtime_semantic_prompt(("sql",), "fresh_analysis")
    contracts = load_runtime_contract_prompt(("sql",), "fresh_analysis")

    assert "唯一语义所有权" in semantic
    assert "# query.relational" not in semantic
    assert "<runtime-contract-manifest>" in semantic
    assert contracts.startswith("<runtime-contracts>")
    assert "# query.relational" in contracts
    assert "唯一语义所有权" not in contracts


def test_runtime_contract_delivery_omits_authoring_only_full_analysis_examples():
    prompt = load_runtime_prompt(("sql", "local_file"), "fresh_analysis")

    assert "canonical-runtime-example" not in prompt
    assert "SUM(metric_value) AS metric_value" not in prompt
    assert "目标类别核心指标合计" not in prompt


def test_mixed_source_manifest_loads_capability_union_and_cross_source_pack():
    prompt = load_runtime_prompt(("sql", "time_series", "local_file"), "fresh_analysis")
    required = _manifest(prompt)["required"]

    assert required == [
        "context.analysis",
        "execution.core",
        "query.relational",
        "query.time_series",
        "query.local_file",
        "query.cross_source",
        "result.artifacts",
        "interaction.clarification",
    ]
    contracts = load_runtime_contract_text(required)
    assert "load_data_with_sql" in contracts
    assert "`timescale` 与 `vxbase`" in contracts
    assert "load_local_file_low_memory" in contracts
    assert "所有相关查询协议同时有效" in contracts


def test_followup_adds_only_followup_contract():
    fresh = required_runtime_contract_ids(("sql",), "fresh_analysis")
    followup = required_runtime_contract_ids(("sql",), "followup")

    assert followup[:-1] == fresh
    assert followup[-1] == "context.followup"


def test_minio_file_uses_object_loader_contract_not_local_path_contract():
    required = required_runtime_contract_ids(("minio_file",), "fresh_analysis")
    contracts = load_runtime_contract_text(required)

    assert "query.minio_file" in required
    assert "load_minio_file(bucket, object_name" in contracts
    assert "本地文件使用 `datasource_identifier` 作为路径" not in contracts


def test_single_time_series_source_does_not_load_cross_source_pack():
    capabilities = infer_query_capabilities_from_snapshot({
        "query_capabilities": ["sql", "time_series"],
        "selected_datasource_snapshot": [{
            "datasource_type": "table",
            "table_engine": "time_series",
            "ts_query_source": "vxbase",
        }],
    })

    assert capabilities == ("time_series",)
    assert required_runtime_contract_ids(capabilities) == (
        "context.analysis",
        "execution.core",
        "query.time_series",
        "result.artifacts",
        "interaction.clarification",
    )


def test_every_registered_contract_file_is_loadable_and_nonempty():
    for contract_id in runtime_contract_pack_ids():
        text = load_runtime_contract_text((contract_id,))
        assert text.startswith(f"# {contract_id}")
        assert len(text) > 100


def test_runtime_prompt_diagnostics_report_each_required_pack_without_content():
    diagnostics = describe_runtime_prompt(("sql", "local_file"), "followup")

    assert diagnostics["query_capabilities"] == ["sql", "local_file"]
    assert diagnostics["compiled"]["chars"] > 0
    assert diagnostics["compiled"]["estimated_tokens"] > 0
    assert diagnostics["semantic_kernel"]["chars"] > 0
    assert [item["contract_id"] for item in diagnostics["contract_packs"]] == [
        "context.analysis",
        "execution.core",
        "query.relational",
        "query.local_file",
        "query.cross_source",
        "result.artifacts",
        "interaction.clarification",
        "context.followup",
    ]
    assert all(item["chars"] > 0 for item in diagnostics["contract_packs"])


def test_output_and_interaction_contracts_are_selected_exclusively():
    required = required_runtime_contract_ids(
        ("sql",),
        "fresh_analysis",
        output_format="json",
        clarification_mode="disabled",
    )

    assert "result.json" in required
    assert "result.artifacts" not in required
    assert "result.text" not in required
    assert "interaction.no_clarification" in required
    assert "interaction.clarification" not in required
