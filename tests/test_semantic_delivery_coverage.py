import queue
from types import SimpleNamespace

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.provenance import (
    reset_capture, set_delivery_requirements, execution_delivery_coverage,
    calculation_delivery_requirements,
)
from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount, PythonCalculation


PLAN = {'questions': [{'question_id': 'q1', 'calculation_rules': [
    {'output_fact_ids': ['count']}, {'output_fact_ids': ['count', 'custom']}
]}]}


@pytest.fixture(autouse=True)
def isolated_worker():
    rt._prime_worker_runtime_context({})
    yield
    rt._prime_worker_runtime_context({})


def native():
    execution = SemanticExecution('current')
    execution.add_source('input', pd.DataFrame({'id': [1, 2, 2]}), query_key='declared')
    fact = execution.execute(DistinctCount('actual-count', 'input', 'id'))
    return execution, fact


def test_host_extracts_only_calculation_outputs_without_rewriting_algorithms():
    assert calculation_delivery_requirements(PLAN) == [
        {'question_id': 'q1', 'result_id': 'count'}, {'question_id': 'q1', 'result_id': 'custom'}]
    payload = rt._serialize_runtime_user_context(SimpleNamespace(runtime_work_packet={'semantic_plan': PLAN}))
    assert payload['semantic_delivery_requirements'] == calculation_delivery_requirements(PLAN)


def test_unbound_and_duplicate_producers_fail_save_even_when_values_are_equal():
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'count'}])
    first, fact = native()
    with pytest.raises(ValueError, match='actual execution producer'):
        rt.save_analysis_result(analysis_report='完整分析结果。')
    first.bind_result('q1', 'count', fact)
    second, another = native()
    second.bind_result('q1', 'count', another)
    assert execution_delivery_coverage()['results'][0]['status'] == 'ambiguous'
    with pytest.raises(ValueError, match='ambiguous'):
        rt.save_analysis_result(analysis_report='完整分析结果。')


def test_real_native_and_custom_producers_cover_host_identities_without_certifying_prose():
    set_delivery_requirements(calculation_delivery_requirements(PLAN))
    execution, fact = native()
    execution.bind_result('q1', 'count', fact)
    custom = execution.execute(PythonCalculation('mean', lambda inputs, parameters: 5, ('input',)))
    execution.bind_result('q1', 'custom', custom)
    result = rt.save_analysis_result(analysis_report='静态文字仍需业务验收。')
    coverage = result.semantic_execution_coverage
    assert coverage['execution_status'] == 'covered'
    assert coverage['presentation_status'] == 'not_verified'
    assert coverage['business_status'] == 'not_evaluated'
    assert [item['operation'] for item in coverage['results']] == ['DistinctCount', 'PythonCalculation']
    assert len(result.semantic_execution_traces[0]['result_bindings']) == 2


def test_binding_rejects_old_receipt_and_changed_producer_and_reset_removes_bindings():
    first, fact = native()
    second, another = native()
    with pytest.raises(ValueError, match='another execution'):
        second.bind_result('q1', 'count', fact)
    first.bind_result('q1', 'count', fact)
    changed = first.execute(DistinctCount('other', 'input', 'id'))
    with pytest.raises(ValueError, match='different producer'):
        first.bind_result('q1', 'count', changed)
    reset_capture()
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'count'}])
    assert execution_delivery_coverage()['results'][0]['status'] == 'missing'


@pytest.mark.parametrize('bind, expected', [(False, 'error'), (True, 'success')])
def test_actual_worker_enforces_host_requirements_without_save_arguments(bind, expected):
    code = '''execution = SemanticExecution('worker')
import pandas as pd
execution.add_source('input', pd.DataFrame({'id': [1, 2, 2]}), query_key='declared')
fact = execution.execute(DistinctCount('count-code', 'input', 'id'))
'''
    if bind:
        code += "execution.bind_result('q1', 'count', fact)\n"
    code += "result = save_analysis_result(analysis_report='本次去重计数为2。')\n"
    result_queue = queue.Queue()
    rt._execute_generated_code_worker(code, 'delivery-worker', {
        'semantic_delivery_requirements': [{'question_id': 'q1', 'result_id': 'count'}]
    }, result_queue)
    notice = result_queue.get_nowait()
    result = rt._read_worker_result_payload(notice['result_file_path']) if notice.get('result_file_path') else notice
    assert result['status'] == expected
    if not bind:
        assert 'actual execution producer' in result['error_message']


def test_legacy_save_does_not_require_migration_or_claim_coverage():
    result = rt.save_analysis_result(analysis_report='旧 Python 路径继续可用。')
    assert result.semantic_execution_coverage['execution_status'] == 'not_required'


def test_only_actual_unchanged_table_consumption_is_recorded_not_a_forged_binding():
    from copy import deepcopy
    from analysis_runtime.provenance import snapshot_digest
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'count'}])
    execution, fact = native()
    execution.bind_result('q1', 'count', fact)
    view = execution.table(fact)
    table = rt.build_table_result(title='设备数量', data=view, columns=[{'key': 'value', 'label': '数量'}])['table']
    result = rt.save_analysis_result(analysis_report='设备数量为2。', tables=[table])
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'table_consumed'
    modified = deepcopy(table)
    modified['rows'][0]['value'] = 999
    modified['_semantic_execution_binding']['artifact_digest'] = snapshot_digest({
        'columns': modified['columns'], 'rows': modified['rows']})
    result = rt.save_analysis_result(analysis_report='设备数量为999。', tables=[modified])
    assert result.semantic_execution_coverage['results'][0]['presentation_status'] == 'not_verified'


def test_non_numeric_calculation_keeps_actual_date_range_and_detached_object_output():
    from datetime import date
    execution, _ = native()
    output = {'range': [date(2024, 1, 1), date(2026, 1, 1)], 'classes': ['A', 'B']}
    definition = PythonCalculation('date-range', lambda inputs, parameters: output, ('input',), value_kind='object')
    fact = execution.execute(definition)
    execution.bind_result('q1', 'range', fact)
    output['classes'].append('C')
    view = execution.table(fact)
    assert view.iloc[0]['value']['classes'] == ['A', 'B']
    view.iloc[0]['value']['classes'].append('D')
    assert execution.table(fact).iloc[0]['value']['classes'] == ['A', 'B']
    assert execution.trace()['facts']['date-range']['definition']['value_kind'] == 'object'
    trace = execution.trace()
    trace['facts']['date-range']['values'][0]['value']['classes'].append('E')
    assert execution.table(fact).iloc[0]['value']['classes'] == ['A', 'B']
    with pytest.raises(ValueError, match='not a numeric'):
        execution.evaluate_extrema('bad-decision', fact)


def test_parameter_identity_preserves_types_that_json_strings_would_conflate():
    from decimal import Decimal
    execution, _ = native()
    def calculate(inputs, parameters):
        return 2
    definition = PythonCalculation('typed', calculate, ('input',), parameters={'scale': Decimal('2')})
    execution.execute(definition)
    definition.parameters['scale'] = '2'
    with pytest.raises(ValueError, match='different producer'):
        execution.execute(definition)
