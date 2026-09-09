from pathlib import Path


RUNTIME_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent"
    / "prompts"
    / "runtime"
)


def _semantic_prompt() -> str:
    return (RUNTIME_ROOT / "sys_prompt.md").read_text(encoding="utf-8")


def _contract(name: str) -> str:
    return (RUNTIME_ROOT / "contracts" / name).read_text(encoding="utf-8")


def test_runtime_is_single_semantic_owner_and_preparation_is_advisory():
    prompt = _semantic_prompt()

    assert "唯一语义所有权" in prompt
    assert "当前 Runtime 是查询口径、指标计算、产物设计和报告叙事的唯一语义所有者" in prompt
    assert "Preparation 提供已选数据源、完整 Schema、已确认关系和开放式业务方向" in prompt
    assert "它不规定 SQL、指标、图表、表格、结论或报告章节" in prompt


def test_runtime_keeps_complete_insight_narrative_and_parent_population():
    prompt = _semantic_prompt()

    assert "一个路径为空不能抹去其他有数据路径" in prompt
    assert "父实体定义统计总体时，先形成父实体范围" in prompt
    assert "不能用子事件时间替代父实体的业务归属时间" in prompt
    assert "不要退化为“指标 A：数值；指标 B：数值”" in prompt


def test_clarification_stays_in_same_react_without_second_semantic_tool():
    prompt = _contract("interaction/clarification.md")

    assert "<user-clarification-answers>" in prompt
    assert "直接在当前 ReAct 中映射到完整 Schema" in prompt
    assert "confirm_analysis_semantics" not in prompt
    assert "request_analysis_clarification" in prompt


def test_runtime_receives_mechanical_contracts_before_execution():
    from agent.runtime_prompt_loader import load_runtime_prompt

    prompt = load_runtime_prompt(("sql",), "fresh_analysis")

    assert "initial_summaries_injected_before_model_invocation" in prompt
    assert "# execution.core" in prompt
    assert "# query.relational" in prompt
    assert "load_runtime_contracts" not in prompt


def test_state_owned_non_code_contracts_are_loaded_in_full():
    from agent.runtime_contract_registry import load_runtime_work_contract_text

    planning = load_runtime_work_contract_text(
        ("context.analysis", "execution.planning"),
        work="plan",
    )
    review = load_runtime_work_contract_text(
        ("context.analysis", "execution.result_review"),
        work="review",
    )

    assert "当前工作只把完整洞察语义组织成一份可执行前的数据模型" in planning
    assert "正式程序已经成功执行" in review
    assert "报告中的指标公式、总体、时间、粒度、比较方向和单位" in review
    assert "当前工作进入对应执行状态后" in planning  # surrounding context stays summarized


def test_execution_contract_keeps_probe_no_data_and_independent_queries():
    contract = _contract("execution/core.md")

    assert 'execution_intent = "probe"' in contract
    assert "每条物理查询使用独立稳定的 `query_key`" in contract
    assert "函数和循环中同样使用组合查询 API" in contract
    assert "全部必要路径成功查询但为空" in contract
    assert "部分路径为空时保留成功路径" in contract


def test_artifact_contract_uses_real_helpers_and_business_labels():
    contract = _contract("result/artifacts.md")

    assert "build_chart_result" in contract
    assert "build_table_result(...)" in contract
    assert "没有 `.table` 属性" in contract
    assert "不使用底层编码、临时变量或 `Metric`" in contract


def test_runtime_prompt_does_not_restore_evidence_authoring():
    prompt = _semantic_prompt()
    contracts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNTIME_ROOT / "contracts").rglob("*.md")
    )

    assert "EvidenceDefinition" not in prompt + contracts
    assert "TraceRef" not in prompt + contracts
    assert "evidence_definitions = [...]" not in prompt + contracts
