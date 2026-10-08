from copy import deepcopy

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.provenance import set_delivery_requirements, chart_digest
from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount, PythonCalculation


@pytest.fixture(autouse=True)
def isolated():
    rt._prime_worker_runtime_context({})
    yield
    rt._prime_worker_runtime_context({})


def setup(values=('A', 'A', 'B')):
    execution = SemanticExecution('presentation')
    execution.add_source('input', pd.DataFrame({'id': range(len(values)), 'group': values}), query_key='declared')
    fact = execution.execute(DistinctCount('distribution', 'input', 'id', ('group',)))
    execution.bind_result('q1', 'distribution', fact)
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'distribution'}])
    return execution, fact


@pytest.mark.parametrize('helper', ['result', 'document', 'suite'])
def test_existing_chart_helpers_record_actual_consumption_after_save_normalization(helper):
    execution, fact = setup()
    options = {'title': '分布', 'category_field': 'group', 'value_field': 'value'}
    if helper != 'suite':
        options['chart_kind'] = 'bar'
    output = execution.chart(fact, helper=helper, **options)
    charts = output if isinstance(output, list) else [output]
    result = rt.save_analysis_result(analysis_report='完整分析。', charts=charts)
    row = result.semantic_execution_coverage['results'][0]
    assert row['presentation_status'] == 'chart_consumed'
    assert row['chart_indices'] == list(range(len(charts)))
    assert result.semantic_execution_coverage['business_status'] == 'not_evaluated'


def test_chart_changed_after_helper_even_with_updated_digest_is_unverified():
    execution, fact = setup()
    chart = execution.chart(fact, chart_kind='bar', title='分布', category_field='group', value_field='value')
    chart['title'] = '未经核实的另一口径'
    chart['_semantic_execution_binding']['artifact_digest'] = chart_digest(chart)
    result = rt.save_analysis_result(analysis_report='完整分析。', charts=[chart])
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'not_verified'


def test_chart_using_only_auxiliary_field_does_not_certify_primary_fact():
    execution = SemanticExecution('multi')
    execution.add_source('input', pd.DataFrame({'id': [1]}), query_key='declared')
    fact = execution.execute(PythonCalculation('custom', lambda inputs, parameters: pd.DataFrame({
        'group': ['A', 'B'], 'value': [1, 2], 'aux': [10, 20]}), ('input',)))
    execution.bind_result('q1', 'custom', fact)
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'custom'}])
    chart = execution.chart(fact, title='辅助列', chart_kind='bar', category_field='group', value_field='aux')
    result = rt.save_analysis_result(analysis_report='完整分析。', charts=[chart])
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'not_verified'


def test_report_spans_follow_actual_fragments_and_leave_static_claim_outside_proof():
    execution, fact = setup()
    decision = execution.evaluate_extrema('rank', fact)
    rank = execution.render_extrema(decision, 'group')
    value = execution.render_value(fact, row=0, prefix='数量：', suffix='，这意味着风险很高。')
    report = execution.compose_report(['## 分析\n\n', rank, '\n\n', value, '\n结束。'])
    result = rt.save_analysis_result(analysis_report=report)
    row = result.semantic_execution_coverage['results'][0]
    assert row['presentation_status'] == 'report_consumed'
    spans = row['report_spans']
    assert result.analysis_report[spans[0]['start']:spans[0]['end']] == str(rank)
    assert result.analysis_report[spans[1]['start']:spans[1]['end']] == '2'
    assert '风险很高' not in ''.join(result.analysis_report[item['start']:item['end']] for item in spans)
    assert all(item['business_status'] == 'not_evaluated' for item in spans)


def test_equal_plain_text_modified_report_and_unused_fragments_cannot_inherit_proof():
    execution, fact = setup()
    fragment = execution.render_value(fact)
    report = execution.compose_report(['数量为', fragment, '。'])
    for other in (str(report), str(report) + '新的结论。', execution.compose_report(['数量为2。'])):
        result = rt.save_analysis_result(analysis_report=other)
        assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'not_verified'
    result = rt.save_analysis_result(analysis_report=report)
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'report_consumed'


def test_tied_extrema_is_rendered_from_saved_decision_and_retains_all_objects():
    execution, fact = setup(('A', 'B'))
    decision = execution.evaluate_extrema('rank', fact)
    fragment = execution.render_extrema(decision, 'group')
    assert execution.decision_state(decision)['state'] == 'false'
    report = execution.compose_report([fragment])
    result = rt.save_analysis_result(analysis_report=report)
    assert 'A' in result.analysis_report and 'B' in result.analysis_report
    assert result.semantic_execution_coverage['results'][0]['report_spans'][0]['decision_id'] == 'rank'


def test_old_execution_composed_report_is_not_consumed_by_next_worker():
    execution, fact = setup()
    report = execution.compose_report([execution.render_value(fact)])
    rt._prime_worker_runtime_context({})
    next_execution, _ = setup()
    result = rt.save_analysis_result(analysis_report=report)
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'not_verified'


def test_nested_sections_keep_exact_spans_and_free_prose_without_recomputing():
    execution, fact = setup()
    section = execution.compose_report(['## 分类\n\n数量：', execution.render_value(fact), '。'])
    report = execution.compose_report(['\n\n总览。\n\n', section, '\n\n后续说明。\n'])
    result = rt.save_analysis_result(analysis_report=report)
    span = result.semantic_execution_coverage['results'][0]['report_spans'][0]
    assert result.analysis_report[span['start']:span['end']] == '2'
    assert result.analysis_report == '总览。\n\n## 分类\n\n数量：2。\n\n后续说明。'
