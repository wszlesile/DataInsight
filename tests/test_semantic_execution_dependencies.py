from decimal import Decimal
import queue

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount, RowProductSum, PythonCalculation, DecimalColumns, OneToOneJoin
from analysis_runtime.semantic_evidence import build_semantic_execution_catalog


@pytest.fixture(autouse=True)
def isolated_worker():
    rt._prime_worker_runtime_context({})
    yield
    rt._prime_worker_runtime_context({})


def test_shared_intermediate_results_execute_once_in_dependency_order_and_are_detached():
    calls = []
    execution = SemanticExecution('graph')
    execution.add_source('input', pd.DataFrame({'id': [1, 2, 3], 'group': ['a', 'a', 'b']}), query_key='query')
    def multiply(inputs, parameters):
        calls.append('multiply')
        frame = inputs['counts']
        frame['value'] *= parameters['scale']
        return frame
    def total(inputs, parameters):
        calls.append('total')
        return inputs['scaled']['value'].sum()
    execution.define(PythonCalculation('total', total, ('scaled',)))
    execution.define(PythonCalculation('scaled', multiply, ('counts',), parameters={'scale': 3}, output_grain=('group',)))
    execution.define(DistinctCount('counts', 'input', 'id', ('group',)))
    execution.define_extrema('largest', 'scaled')
    assert execution.table(execution.result('total'))['value'].tolist() == [9]
    assert execution.matches(execution.decision('largest'))['group'].tolist() == ['a']
    execution.run()
    detached = execution.table(execution.result('scaled'))
    detached.loc[0, 'value'] = 999
    assert calls == ['multiply', 'total']
    assert execution.table(execution.result('counts'))['value'].tolist() == [2, 1]
    assert execution.table(execution.result('scaled'))['value'].tolist() == [6, 3]
    trace = execution.trace()
    assert trace['facts']['total']['input_fact_ids'] == ['scaled']
    assert trace['facts']['scaled']['input_fact_ids'] == ['counts']
    assert trace['facts']['total']['query_keys'] == ('query',)


def test_planned_dependency_aliases_resolve_forward_and_record_actual_inputs():
    execution = SemanticExecution('planned-graph')
    execution.add_source('rows', pd.DataFrame({'id': [1, 2, 2]}), query_key='query')
    calls = []
    def scaled(inputs, parameters):
        calls.append(1)
        return inputs.frame('population')['value'].sum() * 2
    execution.define(PythonCalculation('physical-total', scaled, ('population',)),
                     question_id='q2', result_id='total')
    execution.define(DistinctCount('physical-count', 'rows', 'id'), question_id='q1', result_id='population')
    execution.run()
    assert execution.table(execution.result(('q2', 'total')))['value'].tolist() == [4]
    assert calls == [1]
    fact = execution.trace()['facts']['physical-total']
    assert fact['input_bindings'] == {'population': 'physical-count'}
    assert fact['input_fact_ids'] == ['physical-count']
    assert fact['query_keys'] == ('query',)


def test_ambiguous_planned_inputs_and_alias_cycles_fail_before_callbacks():
    execution = SemanticExecution('alias-cycles')
    calls = []
    def calculate(inputs, parameters):
        calls.append(1)
        return 1
    execution.define(PythonCalculation('a', calculate, ('second',)), question_id='q1', result_id='first')
    execution.define(PythonCalculation('b', calculate, ('first',)), question_id='q2', result_id='second')
    with pytest.raises(ValueError, match='Cyclic'):
        execution.result('first')
    execution.define(PythonCalculation('c', calculate, ()), question_id='q3', result_id='first')
    with pytest.raises(ValueError, match='ambiguous'):
        execution.result('b')
    assert calls == []


def test_native_consumer_uses_saved_result_grain_without_claiming_raw_query_formula():
    execution = SemanticExecution('native-chain')
    execution.add_source('input', pd.DataFrame({'id': [1, 2], 'group': ['a', 'b']}), query_key='query')
    def prices(inputs, parameters):
        frame = inputs['counts']
        frame['price'] = Decimal('2.10')
        return frame
    execution.define(DistinctCount('counts', 'input', 'id', ('group',)))
    execution.define(PythonCalculation('priced', prices, ('counts',), output_grain=('group',)))
    execution.define(RowProductSum('amount', 'priced', 'price', 'value'))
    assert execution.table(execution.result('amount'))['value'].iloc[0] == Decimal('4.20')
    nodes = build_semantic_execution_catalog([execution.trace()], {})
    amount = next(node for node in nodes if node.get('fact_id') == 'amount')
    assert amount['verification'] == 'execution_only'
    assert amount['reason'] == 'calculated_input_composition_not_yet_compiled'
    assert amount['input_fact_node_ids'] == [next(node['node_id'] for node in nodes if node.get('fact_id') == 'priced')]


def test_cycles_fail_before_calculations_run_and_do_not_poison_other_results():
    calls = []
    def calculate(inputs, parameters):
        calls.append(1)
        return 1
    execution = SemanticExecution('cycle')
    execution.define(PythonCalculation('left', calculate, ('right',)))
    execution.define(PythonCalculation('right', calculate, ('left',)))
    with pytest.raises(ValueError, match='Cyclic'):
        execution.result('left')
    assert calls == []
    execution.add_source('input', pd.DataFrame({'id': [1]}), query_key='query')
    execution.define(DistinctCount('unrelated', 'input', 'id'))
    assert execution.table(execution.result('unrelated'))['value'].tolist() == [1]


def test_transform_dependencies_resolve_out_of_order_and_preserve_exact_native_amount():
    execution = SemanticExecution('source-graph')
    execution.add_source('prices', pd.DataFrame({'id': ['a', 'b'], 'price': ['2.10', '3.20']}), query_key='prices', grain=('id',))
    execution.add_source('quantities', pd.DataFrame({'id': ['a', 'b'], 'qty': [2, 3]}), query_key='quantities', grain=('id',))
    execution.define_transform(DecimalColumns('normalized', 'joined', ('price',)))
    execution.define_transform(OneToOneJoin('joined', 'prices', 'quantities', ('id',)))
    execution.define(RowProductSum('amount', 'normalized', 'price', 'qty'))
    execution.run()
    assert execution.table(execution.result('amount'))['value'].tolist() == [Decimal('13.80')]
    assert set(execution.trace()['facts']['amount']['query_keys']) == {'prices', 'quantities'}
    execution.run()
    assert len(execution.trace()['facts']) == 1


def test_save_schedules_program_and_publishes_bindings_without_manual_execution_or_binding():
    rt._prime_worker_runtime_context({'semantic_delivery_requirements': [{'question_id': 'q1', 'result_id': 'count'}]})
    execution = SemanticExecution('host')
    execution.add_source('input', pd.DataFrame({'id': [1, 2, 2]}), query_key='query')
    execution.define(DistinctCount('count', 'input', 'id'), question_id='q1', result_id='count')
    result = rt.save_analysis_result(analysis_report='自由正文仍未通过业务语义验收。')
    assert result.semantic_execution_coverage['execution_status'] == 'covered'
    assert result.semantic_execution_coverage['business_status'] == 'not_evaluated'
    assert result.semantic_execution_traces[0]['facts']['count']['values'] == [{'value': 2}]


def test_executable_identity_carries_actual_consumption_without_a_planning_symbol_graph():
    execution = SemanticExecution('actual-definition')
    execution.add_source('input', pd.DataFrame({'id': [1, 2]}), query_key='query')
    execution.define(DistinctCount('count', 'input', 'id'), question_id='q1')
    report = execution.compose_report(['合计：', execution.render_value(execution.result('count'))])
    result = rt.save_analysis_result(analysis_report=report)
    coverage = result.semantic_execution_coverage
    assert coverage['execution_status'] == 'not_required'
    assert coverage['results'][0]['result_id'] == 'count'
    assert coverage['results'][0]['required_by_plan'] is False
    assert coverage['results'][0]['consumed_channels'] == ['report']


@pytest.mark.parametrize('scale', [2, 3])
def test_actual_worker_parameter_replay_regenerates_dependency_results_and_decisions(scale):
    code = '''execution = SemanticExecution('worker-graph')
import pandas as pd
execution.add_source('input', pd.DataFrame({'id': [1, 2, 3], 'group': ['a', 'a', 'b']}), query_key='query')
parameters = get_analysis_parameters({'scale': 1})
def calculate(inputs, parameters):
    frame = inputs['counts']
    frame['value'] *= parameters['scale']
    return frame
execution.define(PythonCalculation('scaled', calculate, ('counts',), parameters=parameters, output_grain=('group',)), question_id='q1', result_id='scaled')
execution.define(DistinctCount('counts', 'input', 'id', ('group',)), question_id='q1', result_id='counts')
execution.define_extrema('ranking', 'scaled')
report = execution.compose_report([execution.render_extrema(execution.decision('ranking'), 'group')])
result = save_analysis_result(analysis_report=report)
'''
    notice_queue = queue.Queue()
    rt._execute_generated_code_worker(code, 'dependency-test', {
        'analysis_parameters_override': {'scale': scale},
        'semantic_delivery_requirements': [{'question_id': 'q1', 'result_id': value} for value in ('counts', 'scaled')],
    }, notice_queue)
    notice = notice_queue.get_nowait()
    worker = rt._read_worker_result_payload(notice['result_file_path'])
    assert worker['status'] == 'success', worker
    result = rt._deserialize_exec_result(worker['exec_result'])
    assert result.semantic_execution_coverage['execution_status'] == 'covered'
    assert result.semantic_execution_traces[0]['facts']['scaled']['values'] == [
        {'group': 'a', 'value': 2 * scale}, {'group': 'b', 'value': scale}]
    assert f'为 {2 * scale}' in result.analysis_report
