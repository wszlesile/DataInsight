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
    assert "不授权从分布比例推断运营健康、原因、风险、产能或行动价值" in prompt
    assert "不把“决策基础”解释为已授权运营评价或行动建议" in prompt
    assert "expression_scope.judgment_scope=descriptive_only" in prompt


def test_recommendations_are_optional_without_actionable_business_basis():
    prompt = _semantic_prompt()
    text_contract = _contract("result/text.md")
    artifact_contract = _contract("result/artifacts.md")
    from agent.runtime_prompt_loader import load_runtime_prompt

    compiled = load_runtime_prompt(("local_file",), "fresh_analysis")

    assert "不为满足报告结构而补写通用建议" in prompt
    assert "是否值得关注或采取行动，仍须有适用判据" in prompt
    assert "不能为了填充结构而补写通用话术" in text_contract
    assert "事实型发现和数学比较足以构成完整分析" in artifact_contract
    assert "占比、排名和数值变化本身不提供这些判据" in artifact_contract
    assert "事实型发现和数学比较足以构成完整分析" in compiled
    assert "业务含义和有事实支撑的建议" not in compiled


def test_datasource_description_is_purpose_context_not_proof_of_available_evidence():
    contract = _contract("context/analysis.md")

    assert "数据源级 `dataset_context`" in contract
    assert "不能证明该来源实际记录了用途所需的业务事实或结果" in contract
    assert "字段定义、字段值和本轮真实查询" in contract
    assert "`evidence_authority=context_only`" in contract


def test_definition_stage_defers_report_authorship_and_keeps_legacy_signature_reference_clear():
    definition = _contract("execution/definition.md")
    expression = _contract("execution/expression.md")
    numeric = _contract("execution/numeric_interpretation.md")

    assert "不要在此生成报告" in definition
    assert "compose_analysis_presentation" in definition
    assert "`judgment_scope` is `descriptive_only`" in expression
    assert "This is not a reason to ask a routine clarification" in expression
    assert "`results.extrema` 不接收 `unit` 参数" not in definition
    assert "不要混用两个签名" in numeric


def test_clarification_revises_semantic_plan_inside_runtime():
    prompt = _contract("interaction/clarification.md")

    assert "<user-clarification-answers>" in prompt
    assert "恢复后先用它修订当前语义计划" in prompt
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

    assert "当前工作根据 `runtime_semantic_plan` 确定下一步需要的来源" in planning
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


def test_formal_repair_distinguishes_process_isolation_from_source_reuse():
    from agent.runtime_contract_registry import load_runtime_work_contract_text

    contract = load_runtime_work_contract_text(
        ("execution.implementation",), work="repair"
    )
    assert "不继承上次进程中的变量" in contract
    assert "当前完整源码和最新失败反馈为基准" in contract
    assert "不是重新设计整份分析" in contract
    assert "不继承 Probe 或旧程序的变量、DataFrame、代码片段" not in contract


def test_implementation_preserves_unresolved_interpretation_boundary():
    from agent.runtime_contract_registry import load_runtime_work_contract_text

    contract = load_runtime_work_contract_text(
        ("execution.implementation",), work="implement"
    )
    assert "`basis=unresolved`" in contract
    assert "从实际结果组织事实说明与待补依据" in contract
    assert "不能因作为回退文本就认证其中的评价" in contract
    assert "条件可计算并不意味着评价已成立" in contract
