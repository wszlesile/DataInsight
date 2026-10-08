from copy import deepcopy
import queue

import pytest

import agent.tools as rt
from analysis_runtime.contracts import AnalysisExecutionSnapshot, AnalysisRuntimeResult, delivery_requirements_from_result
from analysis_runtime.executor import LocalAnalysisRuntime


REQUIREMENTS = [{'question_id': 'q1', 'result_id': 'total'}]


def test_legacy_snapshot_and_result_serialization_remain_compatible():
    snapshot = AnalysisExecutionSnapshot(1, 1, 1, 'result = None')
    assert 'semantic_delivery_requirements' not in snapshot.to_dict()
    assert AnalysisExecutionSnapshot.from_dict(snapshot.to_dict()).to_dict() == snapshot.to_dict()
    result = AnalysisRuntimeResult.from_raw({'status': 'success', 'analysis_report': 'legacy'})
    assert 'semantic_execution_traces' not in result.result_payload()
    assert 'semantic_execution_coverage' not in result.result_payload()


def test_snapshot_carries_only_requirement_identities_not_prior_receipts():
    payload = {'semantic_execution_coverage': {'results': [{
        **REQUIREMENTS[0], 'execution_token': 'old', 'fact_id': 'old-fact', 'report_spans': [{'start': 1}]}]}}
    identities = delivery_requirements_from_result(payload)
    assert identities == REQUIREMENTS
    snapshot = AnalysisExecutionSnapshot(1, 1, 1, 'result = None', semantic_delivery_requirements=identities)
    saved = snapshot.to_dict()
    restored = AnalysisExecutionSnapshot.from_dict(saved)
    saved['semantic_delivery_requirements'][0]['result_id'] = 'mutated'
    assert restored.semantic_delivery_requirements == REQUIREMENTS


def test_result_roundtrip_preserves_fresh_receipts_without_shared_nested_state():
    raw = {'status': 'success', 'semantic_execution_traces': [{'execution_token': 'fresh'}],
           'semantic_execution_coverage': {'results': deepcopy(REQUIREMENTS)}}
    result = AnalysisRuntimeResult.from_raw(raw)
    raw['semantic_execution_traces'][0]['execution_token'] = 'changed'
    saved = result.result_payload()
    assert saved['semantic_execution_traces'][0]['execution_token'] == 'fresh'
    saved['semantic_execution_coverage']['results'][0]['result_id'] = 'changed'
    assert result.result_payload()['semantic_execution_coverage']['results'] == REQUIREMENTS


@pytest.mark.parametrize('bind', [True, False])
def test_local_runtime_enforces_snapshot_requirements_and_parameter_rerun_regenerates_receipts(bind):
    code = '''execution = SemanticExecution('rerun')
import pandas as pd
data = pd.DataFrame({'value': [2, 4]})
execution.add_source('input', data, query_key='declared')
parameters = get_analysis_parameters({'scale': 1})
def calculate(inputs, parameters):
    return inputs['input']['value'].sum() * parameters['scale']
fact = execution.execute(PythonCalculation('sum', calculate, ('input',), parameters=parameters))
'''
    if bind:
        code += "execution.bind_result('q1', 'total', fact)\n"
    code += "report = execution.compose_report(['合计：', execution.render_value(fact)])\nresult = save_analysis_result(analysis_report=report)"
    snapshot = AnalysisExecutionSnapshot(1, 1, 1, code, semantic_delivery_requirements=deepcopy(REQUIREMENTS),
        parameter_schema={'parameters': [{'name': 'scale', 'type': 'number', 'required': True}], 'defaults': {'scale': 1}},
        default_parameters={'scale': 1})
    def execute_code(**kwargs):
        context = kwargs['runtime_user_context']
        assert context['semantic_delivery_requirements'] == REQUIREMENTS
        context['analysis_parameters_override'] = kwargs['analysis_parameters']
        result_queue = queue.Queue()
        rt._execute_generated_code_worker(kwargs['code'], 'snapshot-rerun', context, result_queue)
        notice = result_queue.get_nowait()
        worker = rt._read_worker_result_payload(notice['result_file_path']) if notice.get('result_file_path') else notice
        if worker['status'] == 'success':
            actual = rt._deserialize_exec_result(worker['exec_result'])
            return {'status': 'success', **actual.model_dump()}
        return worker
    runtime = LocalAnalysisRuntime(execute_code)
    results = [runtime.execute(snapshot=snapshot, username='test', analysis_parameters={'scale': scale},
                              runtime_user_context={'semantic_delivery_requirements': []}) for scale in (2, 3)]
    if bind:
        assert [result.analysis_report for result in results] == ['合计：12', '合计：18']
        assert all(result.semantic_execution_coverage['execution_status'] == 'covered' for result in results)
        tokens = [result.semantic_execution_traces[0]['execution_token'] for result in results]
        assert tokens[0] != tokens[1]
        assert all(result.semantic_execution_coverage['results'][0]['report_spans'] for result in results)
    else:
        assert all(result.status == 'error' for result in results)
        assert all('actual execution producer' in result.error_message for result in results)
    rt._prime_worker_runtime_context({})
