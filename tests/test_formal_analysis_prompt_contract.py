from pathlib import Path


PROMPT_PATH = Path(__file__).resolve().parents[1] / "src" / "agent" / "sys_prompt.md"


def test_formal_prompt_requires_complete_user_requested_analysis_scope():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "每个明确对象都应有对应的计算/图表/表格或报告解读" in prompt
    assert "不能因为其中一个数据源先有结果就静默省略另一个" in prompt


def test_formal_prompt_keeps_business_labels_distinct_from_codes():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "不能把原始编码冒充部门、客户或产品名称" in prompt


def test_formal_prompt_keeps_similar_business_measures_semantically_distinct():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "不能把签约额、合同金额、回款额、预算额等代理值静默写成营收" in prompt


def test_formal_prompt_respects_preparation_and_keeps_runtime_probe_reactive():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "正式分析直接生成完整可执行分析" in prompt
    assert "不得重新发现或改写 Business Context 已定稿的跨源关系" in prompt
    assert "只有该正式查询无数据或日期格式异常时，下一轮才进入 Probe" in prompt


def test_formal_prompt_does_not_restore_runtime_evidence_authoring():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "EvidenceDefinition" not in prompt
    assert "TraceRef" not in prompt
    assert "Evidence" not in prompt
    assert "evidence_definitions = [...]" not in prompt
    assert "不要只手写 `columns/rows` 后遗漏列角色" not in prompt


def test_formal_prompt_examples_compute_insight_judgements_before_narration():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "判断先计算对应的占比、差值、变化率或离散程度" in prompt
    assert "variation =" in prompt
    assert "peak_share =" in prompt
    assert "if variation >= 0.5" in prompt
    assert "用量并非均匀释放" not in prompt
