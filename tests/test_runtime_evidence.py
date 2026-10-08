from copy import deepcopy
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults
from analysis_runtime.provenance import record_query_frame
from analysis_runtime.runtime_evidence import compile_runtime_evidence, finalize_runtime_evidence
from analysis_runtime.semantic_execution import PythonCalculation
from utils.chart_document_utils import build_multi_metric_chart_result


@pytest.fixture(autouse=True)
def capture():
    rt._prime_worker_runtime_context({})
    yield
    rt._prime_worker_runtime_context({})


def material(multiplier=1, missing=False):
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'id': [1, 2], 'group': ['A', 'B'], 'amount': [2, 5]})
    query = {'query_key': 'rows', 'implementation': {'loader': 'load_local_file'},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    record_query_frame('rows', source, query)
    program.execution.add_query_source('source', source, grain=('id',))
    calls = []
    def calculate(inputs, parameters):
        calls.append(1)
        frame = inputs.frame('source')
        frame['other'] = frame['amount'] * parameters['multiplier']
        if missing:
            frame.loc[1, 'other'] = float('nan')
        return frame
    program.register_calculation(PythonCalculation('calc', calculate, ('source',),
        parameters={'multiplier': multiplier}, value_field='amount', output_grain=('id',)),
        question_id='q', result_id='summary')
    program.execution.run()
    results = ExecutableResults(program.execution)
    view = results.view('summary', columns=['group', 'amount', 'other'], rename={'amount': '金额'})
    table = rt.build_table_result(data=view, title='结果', columns=[{'key': key, 'label': key} for key in view.columns])
    chart = build_multi_metric_chart_result(data=view, title='比较', category_field='group', value_fields=['金额', 'other'])
    rt.CURRENT_QUERY_SNAPSHOTS.set({'rows': query})
    candidate = rt.save_analysis_result(charts=[chart], tables=[table], defer_presentation=True)
    return candidate, results, calls


def compile_candidate(candidate):
    return compile_runtime_evidence(candidate.runtime_evidence, traces=candidate.semantic_execution_traces,
        queries=candidate.query_snapshots, tables=candidate.tables, charts=candidate.charts,
        report=candidate.analysis_report)


def test_missing_calculated_cells_keep_candidate_identity_after_json_persistence():
    candidate, _, calls = material(missing=True)
    bundle = compile_candidate(candidate)
    restored = rt.StructuredResult.model_validate_json(candidate.model_dump_json())
    assert restored.semantic_execution_traces[0]['facts']['calc']['values'][1]['other'] is None
    assert compile_candidate(restored) == bundle
    assert calls == [1]
    restored.tables[0]['rows'][0]['金额'] = 999
    with pytest.raises(ValueError, match='material changed'):
        compile_candidate(restored)


def test_saved_cell_report_spans_project_without_model_or_recalculation():
    from analysis_runtime.findings import compose_bound_report
    candidate, _, calls = material(3)
    report, bindings = compose_bound_report(candidate.semantic_execution_traces, [
        {'kind': 'text', 'text': '第二行金额：'},
        {'kind': 'value', 'trace_execution_id': 'executable-analysis', 'fact_id': 'calc',
         'row_index': 1, 'field': 'other', 'precision': 0},
        {'kind': 'text', 'text': '；自由叙述不受校验。'}])
    candidate.analysis_report = report
    candidate.runtime_evidence = finalize_runtime_evidence(candidate.runtime_evidence,
        traces=candidate.semantic_execution_traces, queries=candidate.query_snapshots,
        tables=candidate.tables, charts=candidate.charts, report=report, bindings=bindings)
    bundle = compile_candidate(rt.StructuredResult.model_validate_json(candidate.model_dump_json()))
    assert calls == [1]
    assert len(bundle['report_trace_refs']) == 1
    assert bundle['diagnostics']['model_calls'] == 0
    assert bundle['diagnostics']['unbound'] == []
    edge = next(c for c in candidate.runtime_evidence['consumptions'] if c['artifact_type'] == 'report')
    assert edge['text'] == '15'
    assert edge['source_field'] == 'other'
    assert edge['presentation_operation'] == {'kind': 'display_value', 'row_index': 1, 'precision': 0}
    assert report[edge['start']:edge['end']] == '15'


def test_runtime_evidence_resolves_declared_alias_to_executed_upstream():
    candidate, results, calls = material()
    execution = results._execution
    def total(inputs, parameters):
        return inputs.frame('summary')['other'].sum()
    execution.define(PythonCalculation('total-physical', total, ('summary',)), question_id='q2', result_id='total')
    table = rt.build_table_result(data=results.table('total'), title='total')
    saved = rt.save_analysis_result(tables=[table], defer_presentation=True)
    bundle = compile_candidate(saved)
    assert calls == [1]
    node = next(n for n in saved.runtime_evidence['graph'] if n['fact_id'] == 'total-physical')
    assert node['input_bindings'] == {'summary': 'calc'}
    assert node['input_fact_ids'] == ['calc']
    assert node['query_refs'] == ['rows']
    assert node['execution_verification'] == 'input_bound'
    assert bundle['diagnostics']['unbound'] == []


def test_result_copy_and_sort_preserve_consumption_but_modified_data_cannot_inherit_it():
    from analysis_runtime.provenance import fact_view_receipt
    candidate, results, calls = material()
    copied = results.table('summary').copy()
    sorted_view = copied.sort_values('amount', ascending=False)
    table = rt.build_table_result(data=sorted_view, title='sorted')
    chart = build_multi_metric_chart_result(data=sorted_view, title='sorted', category_field='group',
                                          value_fields=['amount', 'other'])
    saved = rt.save_analysis_result(tables=[table], charts=[chart], defer_presentation=True)
    assert compile_candidate(saved)['diagnostics']['unbound'] == []
    assert calls == [1]
    assert sorted_view['amount'].tolist() == [5, 2]
    receipt = fact_view_receipt(sorted_view)
    assert receipt['view_operation']['kind'] == 'sort'
    assert receipt['view_operation']['by'] == 'amount'
    copied.loc[0, 'amount'] = 999
    assert fact_view_receipt(copied.copy()) is None
    assert fact_view_receipt(copied.sort_values('amount')) is None
    # Ordinary frames cannot acquire a receipt by matching values or attrs.
    forged = pd.DataFrame(sorted_view)
    forged.attrs.update(sorted_view.attrs)
    assert fact_view_receipt(forged) is None


def test_multimeasure_aliases_bind_actual_inputs_without_certifying_python_semantics():
    candidate, results, calls = material(3)
    bundle = compile_candidate(candidate)
    assert calls == [1]
    assert len(bundle['table_trace_refs']) == 3
    assert len(bundle['chart_trace_refs']) == 2
    assert {ref['column_key'] for ref in bundle['table_trace_refs']} == {'group', '金额', 'other'}
    amount = next(item for item in bundle['evidence_definitions'] if item['metric_name'] == 'amount')
    assert amount['query_refs'] == ['rows']
    assert amount['calculation']['lineage_verification'] == 'execution_recorded'
    assert amount['calculation']['formula_semantics_verification'] == 'not_evaluated'
    assert bundle['diagnostics']['model_calls'] == 0
    assert candidate.runtime_evidence['graph'][0]['planned_results'][0]['result_id'] == 'summary'
    assert compile_candidate(rt.StructuredResult.model_validate_json(candidate.model_dump_json())) == bundle


@pytest.mark.parametrize('kind', ['query', 'table', 'chart', 'report', 'trace'])
def test_saved_manifest_cannot_bind_changed_material(kind):
    candidate, _, _ = material()
    if kind == 'query': candidate.query_snapshots['rows']['execution']['row_count'] = 99
    elif kind == 'table': candidate.tables[0]['rows'][0]['金额'] = 99
    elif kind == 'chart': candidate.charts[0]['title'] = 'different'
    elif kind == 'report': candidate.analysis_report = 'different'
    else: candidate.semantic_execution_traces[0]['facts']['calc']['values'][0]['amount'] = 99
    with pytest.raises(ValueError, match='changed after capture'):
        compile_candidate(candidate)


def test_modified_view_loses_receipt_and_parameter_rerun_is_a_new_execution():
    candidate, results, calls = material()
    view = results.view('summary', rename={'amount': '金额'})
    view.loc[0, '金额'] = 99
    table = rt.build_table_result(title='changed', data=view)
    changed = rt.save_analysis_result(tables=[table], defer_presentation=True)
    assert any(item['reason'] == 'no_runtime_consumption' for item in changed.runtime_evidence['unbound'])
    saved = deepcopy(candidate.runtime_evidence)
    rt._prime_worker_runtime_context({})
    current, _, rerun_calls = material(4)
    assert current.tables[0]['rows'][0]['other'] == 8
    assert candidate.runtime_evidence == saved
    assert calls == rerun_calls == [1]
    assert current.runtime_evidence['graph'][0]['node_id'] != saved['graph'][0]['node_id']


def test_finding_refs_bind_only_exact_host_rendered_spans_after_normalization():
    from analysis_runtime.findings import compose_bound_report
    candidate, results, calls = material()
    results.finding('rank', 'summary', kind='ranking', category_field='group')
    traces = [results._execution.trace()]
    # The presentation candidate includes discoveries produced by the worker.
    candidate.semantic_execution_traces = traces
    text, bindings = compose_bound_report(traces, [{'kind': 'text', 'text': '说明。\n'},
        {'kind': 'finding', 'trace_execution_id': 'executable-analysis', 'finding_id': 'rank'}])
    candidate.analysis_report = text
    candidate.runtime_evidence = finalize_runtime_evidence(candidate.runtime_evidence,
        traces=traces, queries=candidate.query_snapshots, tables=candidate.tables, charts=candidate.charts,
        report=text, bindings=bindings)
    bundle = compile_candidate(candidate)
    assert len(bundle['report_trace_refs']) == 1
    ref = bundle['report_trace_refs'][0]
    assert text[ref['start']:ref['end']] == bindings[0]['text']
    assert calls == [1]
    changed = finalize_runtime_evidence(candidate.runtime_evidence, traces=traces,
        queries=candidate.query_snapshots, tables=candidate.tables, charts=candidate.charts,
        report='prefix' + text, bindings=bindings)
    assert not any(item['artifact_type'] == 'report' for item in changed['consumptions'])


def test_native_assembly_uses_recorded_bundle_without_calling_legacy_model_compiler():
    from types import SimpleNamespace
    from service.evidence_assembly_service import EvidenceAssemblyService
    candidate, _, _ = material()
    execution = SimpleNamespace(id=8, execution_status='success', generated_code='recorded program',
                                result_payload_json=candidate.model_dump_json(), analysis_task_id=None)
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    service = EvidenceAssemblyService(session)
    with patch('service.evidence_assembly_service.PostAnalysisEvidenceCompiler') as legacy, \
         patch.object(service, '_upsert_execution_evidence') as persist, \
         patch.object(service, '_clear_artifact_evidence_metadata'), \
         patch.object(service, '_write_artifact_metadata'), \
         patch.object(service, '_cleanup_execution_payload'):
        service.assemble_for_execution(conversation_id=1, turn_id=1, execution=execution, artifacts=[])
    legacy.assert_not_called()
    assert persist.call_args.kwargs['diagnostics']['model_calls'] == 0
    assert persist.call_args.kwargs['evidence_map']


def test_legacy_trace_capture_does_not_silently_select_native_evidence_ownership():
    from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount
    execution = SemanticExecution('legacy-program')
    execution.add_source('input', pd.DataFrame({'id': [1, 2]}), query_key='declared')
    execution.execute(DistinctCount('count', 'input', 'id'))
    result = rt.save_analysis_result(analysis_report='既有完整报告。')
    assert result.semantic_execution_traces
    assert result.runtime_evidence == {}
