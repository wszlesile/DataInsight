from copy import deepcopy
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults, register_program_source
from analysis_runtime.provenance import ResultViewFrame, record_query_frame
from analysis_runtime.runtime_evidence import compile_runtime_evidence, finalize_runtime_evidence
from analysis_runtime.semantic_execution import PythonCalculation
from utils.chart_document_utils import build_chart_result, build_multi_metric_chart_result


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


def test_finding_components_keep_distinct_evidence_boundaries_after_persistence():
    from analysis_runtime.findings import compose_referenced_report
    candidate, results, calls = material()
    results.finding('span', 'summary', kind='range', field='amount', category_field='group')
    traces = [results._execution.trace()]
    refs = {component: {'kind': 'finding', 'trace_execution_id': 'executable-analysis',
                       'finding_id': 'span', 'component': component}
            for component in ['maximum', 'difference']}
    report, bindings = compose_referenced_report(traces,
        '最大值 {{maximum}}；差值 {{difference}}。', refs)
    candidate.semantic_execution_traces = traces
    candidate.analysis_report = report
    candidate.report_finding_bindings = bindings
    candidate.runtime_evidence = finalize_runtime_evidence(candidate.runtime_evidence,
        traces=traces, queries=candidate.query_snapshots, tables=candidate.tables,
        charts=candidate.charts, report=report, bindings=bindings)
    restored = rt.StructuredResult.model_validate_json(candidate.model_dump_json())
    bundle = compile_candidate(restored)
    assert report == '最大值 5；差值 3。'
    assert calls == [1]
    assert len(bundle['report_trace_refs']) == 2
    assert len({ref['evidence_key'] for ref in bundle['report_trace_refs']}) == 2
    assert {ref['presentation_binding']['component'] for ref in bundle['report_trace_refs']} == {'maximum', 'difference'}


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


def test_query_source_chart_keeps_query_and_display_view_lineage_without_formula_claims():
    program = ExecutablePythonProgram()
    source = pd.DataFrame({
        'timeStamp': pd.date_range('2026-10-08', periods=8, freq='min'),
        'value': list(range(8)),
    })
    query = {
        'query_key': 'series',
        'implementation': {'datasource_identifier': '__series__'},
        'execution': {'status': 'success', 'row_count': 8, 'columns': list(source.columns)},
    }
    record_query_frame('series', source, query)
    program.execution.add_query_source('series_source', source, grain=('timeStamp',))
    results = ExecutableResults(program.execution)
    view = results.source_table('series_source')
    view['display_time'] = pd.to_datetime(view['timeStamp'])
    view = view.sort_values('timeStamp').reset_index(drop=True).iloc[::2].copy()
    chart = build_chart_result(
        chart_kind='line', data=view, title='趋势',
        category_field='timeStamp', value_field='value',
    )
    rt.CURRENT_QUERY_SNAPSHOTS.set({'series': query})
    saved = rt.save_analysis_result(charts=[chart], defer_presentation=True)

    bundle = compile_candidate(saved)
    assert bundle['diagnostics']['unbound'] == []
    assert len(bundle['chart_trace_refs']) == 1
    definition = bundle['evidence_definitions'][0]
    assert definition['metric_name'] == 'value'
    assert definition['query_refs'] == ['series']
    assert definition['calculation']['lineage_verification'] == 'query_bound'
    assert definition['calculation']['formula_semantics_verification'] == 'not_evaluated'
    operation = bundle['chart_trace_refs'][0]['presentation_binding']
    assert operation['view']['kind'] == 'iloc'
    assert operation['view']['input']['kind'] == 'reset_index'


@pytest.mark.parametrize('copied', [False, True])
def test_chart_suite_retains_actual_query_consumer_across_dataset_normalization(copied):
    from utils.chart_document_utils import build_chart_suite
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'ts': pd.date_range('2026-10-08', periods=8, freq='min').strftime('%Y-%m-%d %H:%M:%S'), 'amount': list(range(8))})
    query = {'query_key': 'windows', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 8, 'columns': list(source.columns)}}
    record_query_frame('windows', source, query)
    program.execution.add_query_source('windows', source, grain=('ts',))
    view = ExecutableResults(program.execution).table('windows').sort_values('ts').reset_index(drop=True)
    if copied:
        view = pd.DataFrame(view)
    charts = build_chart_suite(data=view, title='Window amounts', category_field='ts', value_field='amount',
                               include_line=True, include_bar=True, include_pie=False, limit=4)
    rt.CURRENT_QUERY_SNAPSHOTS.set({'windows': query})
    saved = rt.save_analysis_result(charts=charts, defer_presentation=True)
    bundle = compile_candidate(saved)
    assert len(saved.charts) == 2
    assert len(bundle['chart_trace_refs']) == (0 if copied else 2)
    if not copied:
        assert bundle['status'] == 'complete'
        assert all(item['query_refs'] == ['windows'] for item in bundle['evidence_definitions'])
        assert all(item['calculation']['lineage_verification'] == 'query_bound' for item in bundle['evidence_definitions'])
    else:
        assert bundle['status'] == 'partial'


@pytest.mark.parametrize('operation,bound', [('copy', True), ('astype', True), ('numeric', True), ('multiply', False), ('foreign', False)])
def test_query_chart_alias_records_actual_series_operation_without_value_matching(operation, bound):
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'time': ['2026-10-11 10:00', '2026-10-11 10:01'], 'amount': ['3', '5']})
    query = {'query_key': 'series', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    record_query_frame('series', source, query)
    program.execution.add_query_source('series', source)
    view = ExecutableResults(program.execution).table('series')
    if operation == 'copy':
        alias = view['amount']
    elif operation == 'astype':
        alias = view['amount'].astype(float)
    elif operation == 'numeric':
        alias = pd.to_numeric(view['amount'], errors='coerce')
    elif operation == 'foreign':
        alias = pd.Series([3, 5])
    else:
        alias = pd.to_numeric(view['amount'], errors='coerce') * 2
    view['display_amount'] = alias
    chart = build_chart_result(chart_kind='line', data=view, title='Amounts',
                               category_field='time', value_field='display_amount')
    rt.CURRENT_QUERY_SNAPSHOTS.set({'series': query})
    saved = rt.save_analysis_result(charts=[chart], defer_presentation=True)
    bundle = compile_candidate(saved)
    assert len(bundle['chart_trace_refs']) == (1 if bound else 0)
    if bound:
        assert bundle['evidence_definitions'][0]['metric_name'] == 'amount'
        assert bundle['chart_trace_refs'][0]['value_field'] == 'display_amount'
        assert bundle['chart_trace_refs'][0]['presentation_binding']['view']['kind'] == 'column_assignment'


@pytest.mark.parametrize('operation', ['rename', 'numeric_alias'])
def test_saved_query_view_presentation_addresses_saved_fields_then_upstream_columns(operation):
    program = ExecutablePythonProgram()
    source = ResultViewFrame({'time': ['a', 'b'], 'amount': ['3', '5']})
    query = {'query_key': 'raw', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    rt.CURRENT_QUERY_SNAPSHOTS.set({'raw': query})
    record_query_frame('raw', source, query)
    if operation == 'rename':
        projected = source.rename(columns={'amount': 'saved_amount'})
    else:
        projected = source.copy()
        projected['saved_amount'] = pd.to_numeric(projected['amount'], errors='coerce')
        projected = projected[['time', 'saved_amount']]
    register_program_source(program.execution, 'saved_source', projected, ('time',))
    view = ExecutableResults(program.execution).table('saved_source')
    view = view.rename(columns={'saved_amount': 'display_amount'})
    chart = build_chart_result(chart_kind='line', data=view, title='Amounts',
                               category_field='time', value_field='display_amount')
    table = rt.build_table_result(title='Amounts', data=view,
        columns=[{'key': 'display_amount', 'label': 'Amount'}])
    saved = rt.save_analysis_result(charts=[chart], tables=[table], defer_presentation=True)
    bundle = compile_candidate(saved)
    assert bundle['status'] == 'complete'
    assert len(bundle['chart_trace_refs']) == len(bundle['table_trace_refs']) == 1
    graph = saved.runtime_evidence['graph'][0]
    assert graph['field_origins']['saved_amount'] == [{'query_key': 'raw', 'field': 'amount'}]
    assert saved.charts[0]['_semantic_execution_binding']['field_bindings']['display_amount'] == 'saved_amount'


@pytest.mark.parametrize('projection', [['time', 'display_amount'], pd.Index(['time', 'display_amount'])])
def test_numeric_round_and_column_projection_keep_query_receipt_without_binding_arithmetic(projection):
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'time': [1, 2], 'amount': ['3.456', 'bad']})
    query = {'query_key': 'projected', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    record_query_frame('projected', source, query)
    program.execution.add_query_source('projected', source)
    view = ExecutableResults(program.execution).table('projected')
    view['display_amount'] = pd.to_numeric(view['amount'], errors='coerce').round(2)
    projected = view[projection]
    chart = build_chart_result(chart_kind='line', data=projected, title='Amounts',
                               category_field='time', value_field='display_amount')
    rt.CURRENT_QUERY_SNAPSHOTS.set({'projected': query})
    bundle = compile_candidate(rt.save_analysis_result(charts=[chart], defer_presentation=True))
    assert len(bundle['chart_trace_refs']) == 1
    assert bundle['evidence_definitions'][0]['metric_name'] == 'amount'
    operation = bundle['chart_trace_refs'][0]['presentation_binding']['view']
    assert operation['kind'] == 'column_projection'
    assert operation['input']['source_series_operation']['kind'] == 'round'
    assert projected['display_amount'].iloc[0] == 3.46
    assert pd.isna(projected['display_amount'].iloc[1])
    projected['display_amount'] = projected['display_amount'] * 2
    changed = build_chart_result(chart_kind='line', data=projected, title='Changed',
                                category_field='time', value_field='display_amount')
    assert compile_candidate(rt.save_analysis_result(charts=[changed], defer_presentation=True))['chart_trace_refs'] == []


def test_numeric_conversion_capture_leaves_ordinary_and_orphaned_series_behavior_unchanged():
    import gc
    from analysis_runtime.provenance import enable_runtime_evidence
    enable_runtime_evidence()
    wrapper = pd.to_numeric
    enable_runtime_evidence()
    assert pd.to_numeric is wrapper
    ordinary = pd.Series(['3', 'bad'])
    pd.testing.assert_series_equal(pd.to_numeric(ordinary, errors='coerce'), wrapper.__wrapped__(ordinary, errors='coerce'))
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'amount': ['3', 'bad']})
    query = {'query_key': 'orphan', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': ['amount']}}
    record_query_frame('orphan', source, query)
    program.execution.add_query_source('orphan', source)
    view = ExecutableResults(program.execution).table('orphan')
    series = view['amount']
    del view
    gc.collect()
    pd.testing.assert_series_equal(pd.to_numeric(series, errors='coerce'), wrapper.__wrapped__(series, errors='coerce'))


def test_query_source_chart_does_not_bind_a_field_after_that_field_is_replaced():
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'timeStamp': [1, 2], 'value': [3, 5]})
    query = {'query_key': 'series', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    record_query_frame('series', source, query)
    program.execution.add_query_source('series_source', source)
    results = ExecutableResults(program.execution)
    view = results.source_table('series_source')
    view['value'] = view['value'] * 10
    chart = build_chart_result(chart_kind='line', data=view, title='趋势',
                               category_field='timeStamp', value_field='value')
    rt.CURRENT_QUERY_SNAPSHOTS.set({'series': query})
    saved = rt.save_analysis_result(charts=[chart], defer_presentation=True)
    bundle = compile_candidate(saved)
    assert bundle['chart_trace_refs'] == []
    assert any(item['reason'] == 'no_runtime_consumption' for item in bundle['diagnostics']['unbound'])


def test_query_loader_safe_views_bind_only_fields_retained_from_successful_snapshot():
    program = ExecutablePythonProgram()
    raw = pd.DataFrame({'timeStamp': pd.date_range('2026-10-08', periods=8, freq='min'),
                        'value': list(range(8))})
    query = {'query_key': 'series', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 8, 'columns': list(raw.columns)}}
    rt.CURRENT_QUERY_SNAPSHOTS.set({'series': query})
    loaded = ResultViewFrame(raw)
    record_query_frame('series', loaded, query)
    view = loaded.copy()
    view['bucket'] = pd.to_datetime(view['timeStamp']).dt.floor('5min')
    view = view.sort_values('timeStamp').reset_index(drop=True).iloc[::2].copy()
    register_program_source(program.execution, 'series_source', view, ())
    results = ExecutableResults(program.execution)
    chart_data = results.source_table('series_source')
    chart = build_chart_result(chart_kind='line', data=chart_data, title='趋势',
                               category_field='timeStamp', value_field='value')
    saved = rt.save_analysis_result(charts=[chart], defer_presentation=True)

    bundle = compile_candidate(saved)
    assert bundle['chart_trace_refs']
    definition = bundle['evidence_definitions'][0]
    assert definition['metric_name'] == 'value'
    assert definition['query_refs'] == ['series']
    assert definition['calculation']['lineage_verification'] == 'query_bound'
    assert definition['calculation']['formula_semantics_verification'] == 'not_evaluated'
    assert definition['transformations'][0]['view']['kind'] == 'query_source'


def test_query_backed_table_preserves_named_cast_and_rounding_views():
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'day': pd.date_range('2026-10-08', periods=2, tz='Asia/Shanghai'),
                           'value': [1.234, 5.678]})
    query = {'query_key': 'daily', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': list(source.columns)}}
    rt.CURRENT_QUERY_SNAPSHOTS.set({'daily': query})
    record_query_frame('daily', source, query)
    program.execution.add_query_source('daily_source', source)
    results = ExecutableResults(program.execution)
    view = results.source_table('daily_source').rename(columns={'value': 'metric'})
    view['metric'] = view['metric'].astype('float64').round(1)
    table = rt.build_table_result(title='逐日', data=view)
    saved = rt.save_analysis_result(tables=[table], defer_presentation=True)

    bundle = compile_candidate(saved)
    assert {ref['column_key'] for ref in bundle['table_trace_refs']} == {'day', 'metric'}
    assert {item['metric_name'] for item in bundle['evidence_definitions']} == {'day', 'value'}
    assert all(item['query_refs'] == ['daily'] for item in bundle['evidence_definitions'])
    assert bundle['diagnostics']['unbound'] == []


@pytest.mark.parametrize('cast_mode', ['series_type', 'frame_type', 'frame_mapping', 'extension_dtype'])
def test_query_backed_cast_receipts_survive_json_persistence(cast_mode):
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'value': [1, 2]})
    query = {'query_key': 'rows', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': ['value']}}
    rt.CURRENT_QUERY_SNAPSHOTS.set({'rows': query})
    record_query_frame('rows', source, query)
    program.execution.add_query_source('source', source)
    view = ExecutableResults(program.execution).source_table('source')
    if cast_mode == 'series_type':
        view['value'] = view['value'].astype(str)
    elif cast_mode == 'frame_type':
        view = view.astype(float)
    elif cast_mode == 'frame_mapping':
        view = view.astype({'value': float})
    else:
        view['value'] = view['value'].astype(pd.StringDtype())
    saved = rt.save_analysis_result(tables=[rt.build_table_result(title='明细', data=view)],
                                    defer_presentation=True)
    restored = rt.StructuredResult.model_validate_json(saved.model_dump_json())
    bundle = compile_candidate(restored)
    json.dumps(bundle)
    assert {ref['column_key'] for ref in bundle['table_trace_refs']} == {'value'}
    assert bundle['evidence_definitions'][0]['query_refs'] == ['rows']
    assert bundle['diagnostics']['unbound'] == []


def test_inserted_display_column_preserves_query_fields_without_certifying_new_values():
    program = ExecutablePythonProgram()
    source = pd.DataFrame({'value': [111., 222.]})
    query = {'query_key': 'rows', 'implementation': {},
             'execution': {'status': 'success', 'row_count': 2, 'columns': ['value']}}
    rt.CURRENT_QUERY_SNAPSHOTS.set({'rows': query})
    record_query_frame('rows', source, query)
    program.execution.add_query_source('source', source)
    view = ExecutableResults(program.execution).source_table('source').copy()
    view.insert(0, 'ordinal', range(1, len(view) + 1))
    chart_view = view.copy()
    chart_view['ordinal'] = chart_view['ordinal'].astype(str)
    table = rt.build_table_result(title='明细', data=view)
    chart = build_chart_result(chart_kind='bar', data=chart_view, title='数值',
                               category_field='ordinal', value_field='value')
    saved = rt.save_analysis_result(tables=[table], charts=[chart], defer_presentation=True)
    restored = rt.StructuredResult.model_validate_json(saved.model_dump_json())
    bundle = compile_candidate(restored)
    assert {ref['column_key'] for ref in bundle['table_trace_refs']} == {'value'}
    assert len(bundle['chart_trace_refs']) == 1
    assert all(item['metric_name'] == 'value' for item in bundle['evidence_definitions'])
    assert all(item['query_refs'] == ['rows'] for item in bundle['evidence_definitions'])


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
    calculation_node = next(node for node in candidate.runtime_evidence['graph']
                            if node['fact_id'] == 'calc')
    assert calculation_node['planned_results'][0]['result_id'] == 'summary'
    assert compile_candidate(rt.StructuredResult.model_validate_json(candidate.model_dump_json())) == bundle


def test_derived_daily_table_keeps_all_calculated_columns_bound_to_timeseries_query():
    program = ExecutablePythonProgram()
    source = pd.DataFrame({
        'timeStamp': pd.date_range('2026-10-08', periods=39, freq='h'),
        'value': [49.0 + (index % 5) for index in range(39)],
    })
    query = {
        'query_key': 'hourly_series',
        'query_constraints': {
            'time_range': {'start': '2026-10-08', 'end': '2026-10-10', 'end_exclusive': True},
            'time_series_query': {'mode': 'aggregate', 'window': '1 hour'},
        },
        'implementation': {'datasource_identifier': '__series_a'},
        'execution': {'status': 'success', 'row_count': len(source), 'columns': list(source.columns)},
    }
    record_query_frame('hourly_series', source, query)
    program.execution.add_query_source('hourly', source, grain=('timeStamp',))

    def summarize_days(inputs, parameters):
        frame = inputs.frame('hourly').copy()
        frame['day'] = frame['timeStamp'].dt.strftime('%Y-%m-%d')
        return frame.groupby('day', as_index=False).agg(
            value_avg=('value', 'mean'), sample_count=('value', 'count'))

    program.register_calculation(
        PythonCalculation('daily_summary', summarize_days, ('hourly',),
                          value_field='value_avg', output_grain=('day',)),
        question_id='timeseries', result_id='daily_summary')
    program.execution.run()
    results = ExecutableResults(program.execution)
    daily = results.view('daily_summary', rename={
        'day': '日期', 'value_avg': '日均值', 'sample_count': '采样点数'})
    table = rt.build_table_result(data=daily, title='时序日汇总')
    rt.CURRENT_QUERY_SNAPSHOTS.set({'hourly_series': query})
    saved = rt.save_analysis_result(tables=[table], defer_presentation=True)

    bundle = compile_candidate(saved)
    assert bundle['diagnostics']['unbound'] == []
    assert {reference['column_key'] for reference in bundle['table_trace_refs']} == {
        '日期', '日均值', '采样点数'}
    assert {tuple(definition['query_refs']) for definition in bundle['evidence_definitions']} == {
        ('hourly_series',)}


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
