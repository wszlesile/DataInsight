from copy import deepcopy
from decimal import Decimal
import queue
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.provenance import reset_capture
from analysis_runtime.semantic_execution import SemanticExecution, PythonCalculation, DistinctCount
from analysis_runtime.semantic_evidence import build_semantic_execution_catalog
from agent.runtime_contract_registry import load_runtime_work_contract_text


@pytest.fixture(autouse=True)
def isolated_capture():
    reset_capture()
    yield
    reset_capture()


def execution():
    instance = SemanticExecution('custom-test')
    instance.add_source('input', pd.DataFrame({'id': [1, 2], 'value': [2, 4]}), query_key='declared')
    return instance


def test_custom_function_executes_once_and_consumers_share_detached_result():
    calls = []
    def calculate(inputs, parameters):
        calls.append(1)
        mean = Decimal(str(inputs['input']['value'].mean())) * parameters['scale']
        inputs['input'].loc[0, 'value'] = 999
        parameters['scale'] = 999
        return pd.DataFrame({'group': ['all'], 'value': [mean]})
    instance = execution()
    definition = PythonCalculation('mean', calculate, ('input',), parameters={'scale': Decimal('2')}, output_grain=('group',))
    fact = instance.execute(definition)
    saved = deepcopy(instance.trace())
    assert instance.execute(definition) == fact
    assert len(calls) == 1
    assert instance.table(fact)['value'].tolist() == [Decimal('6.0')]
    view = instance.table(fact)
    view.loc[0, 'value'] = 999
    assert instance.source_table('input')['value'].tolist() == [2, 4]
    assert definition.parameters == {'scale': Decimal('2')}
    assert instance.trace() == saved
    node = build_semantic_execution_catalog([saved], {})[0]
    assert node['verification'] == 'execution_only'
    assert node['query_node_ids'] == []
    assert node['declared_query_keys'] == ['declared']
    assert 'def calculate' in saved['facts']['mean']['definition']['implementation']['source']


def test_custom_producer_cannot_change_parameters_function_or_overwrite_native_fact():
    instance = execution()
    def calculate(inputs, parameters):
        return inputs['input']['value'].sum() * parameters['scale']
    definition = PythonCalculation('total', calculate, ('input',), parameters={'scale': 1})
    instance.execute(definition)
    definition.parameters['scale'] = 2
    with pytest.raises(ValueError, match='different producer'):
        instance.execute(definition)
    with pytest.raises(ValueError, match='different producer'):
        instance.execute(PythonCalculation('total', lambda inputs, parameters: 6, ('input',)))
    with pytest.raises(ValueError, match='different producer'):
        instance.execute(DistinctCount('total', 'input', 'id'))


@pytest.mark.parametrize('output,expected', [(None, 'missing'), (pd.DataFrame(columns=['value']), 'empty')])
def test_custom_missing_or_empty_output_is_not_invented_zero(output, expected):
    instance = execution()
    fact = instance.execute(PythonCalculation('output', lambda inputs, parameters: output, ('input',)))
    assert instance.fact_state(fact)['state'] == expected
    assert instance.table(fact).empty if expected == 'empty' else instance.table(fact)['value'].iloc[0] is None


@pytest.mark.parametrize('output', [float('inf'), True, '6', pd.DataFrame({'wrong': [1]})])
def test_custom_invalid_numeric_output_does_not_publish_fact(output):
    instance = execution()
    with pytest.raises((ValueError, TypeError)):
        instance.execute(PythonCalculation('output', lambda inputs, parameters: output, ('input',)))
    assert not instance.trace()['facts']


def test_worker_injected_custom_api_captures_function_and_recomputes_parameter_override(tmp_path):
    path = tmp_path / 'input.csv'
    path.write_text('id,value\n1,2\n2,4\n', encoding='utf-8')
    code = f'''
execution = SemanticExecution("analysis")
analysis_parameters_schema = {{"parameters": [{{"name": "scale", "label": "情景倍数", "type": "number"}}], "defaults": {{"scale": 2}}}}
analysis_parameters = get_analysis_parameters(analysis_parameters_schema["defaults"])
data = query_local_file(query_key="input", file_path={path.as_posix()!r}, query_constraints={{"time_range": None, "target_filters": [], "aggregation": {{"grain": "detail", "metric": "detail"}}}})
execution.add_query_source("input", data, grain=("id",))
def calculate(inputs, parameters):
    return pd.DataFrame({{"category": ["显式参数情景"], "value": [inputs["input"]["value"].mean() * parameters["scale"]]}})
definition = PythonCalculation("scenario", calculate, ("input",), parameters=analysis_parameters, output_grain=("category",))
fact = execution.execute(definition)
assert execution.execute(definition) == fact
view = execution.table(fact)
value = view["value"].iloc[0]
table = build_table_result(title="情景统计", data=view, columns=[{{"key": "category", "label": "情景"}}, {{"key": "value", "label": "统计值"}}])
result = save_analysis_result(analysis_report=f"本次显式参数情景统计值为 {{value:g}}，此计算不认证业务风险。", charts=[], tables=[table], analysis_parameters_schema=analysis_parameters_schema, analysis_parameters=analysis_parameters)
'''
    bound = rt._bind_program_execution_mode(code, 'analysis')
    bound, _ = rt._complete_analysis_result_terminal(bound, output_format='standard')
    assert rt._validate_generated_code_contract(bound) is None
    outputs = []
    for scale in (2, 3):
        captured = {}
        original = rt._build_execution_namespace
        def namespace_builder():
            namespace = original()
            captured['namespace'] = namespace
            return namespace
        notifications = queue.Queue()
        with patch.object(rt, '_build_execution_namespace', namespace_builder):
            rt._execute_generated_code_worker(bound, 'bridge-test', {'analysis_parameters_override': {'scale': scale}}, notifications)
        notification = notifications.get_nowait()
        worker = rt._read_worker_result_payload(notification['result_file_path'])
        assert worker['status'] == 'success', worker
        result = captured['namespace']['result']
        assert rt._validate_structured_result_contract(result, generated_code=bound) is None
        trace = result.semantic_execution_traces[0]
        implementation = trace['facts']['scenario']['definition']['implementation']
        assert implementation['source_status'] == 'captured'
        assert 'def calculate' in implementation['source']
        assert result.tables[0]['rows'][0]['value'] == scale * 3
        assert result.analysis_parameters['scale'] == scale
        assert build_semantic_execution_catalog([trace], result.query_snapshots)[0]['verification'] == 'execution_only'
        outputs.append(trace)
    assert outputs[0]['execution_token'] != outputs[1]['execution_token']
    assert outputs[0]['facts']['scenario']['definition']['implementation']['code_sha256'] == outputs[1]['facts']['scenario']['definition']['implementation']['code_sha256']


def test_implementation_and_repair_receive_actual_executable_interfaces():
    for work in ('implement', 'repair'):
        contract = load_runtime_work_contract_text(('execution.implementation',), work=work)
        assert 'PythonCalculation(' in contract
        assert 'SemanticExecution(' in contract
        assert 'inputs' in contract and 'execution_only' in contract


def test_native_definition_without_builder_is_rejected_before_worker():
    runtime = SimpleNamespace(context=SimpleNamespace(output_contract={'type': 'standard'}))
    with patch.object(rt.execute_python, 'func') as execute:
        with pytest.raises(ValueError, match='must define build_analysis'):
            rt.submit_analysis_program.func(runtime=runtime, definition_source='result = save_analysis_result()')
    execute.assert_not_called()
