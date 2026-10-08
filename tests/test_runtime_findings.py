import pandas as pd
import pytest

from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults
from analysis_runtime.findings import compose_bound_report
from analysis_runtime.semantic_execution import PythonCalculation


def make_results(values, labels=None, unit=''):
    calls = []
    program = ExecutablePythonProgram()
    program.execution.add_source('filtered', pd.DataFrame({
        'item': labels or list(range(len(values))), 'amount': values}), query_key='q')

    def calculate(inputs, parameters):
        calls.append(1)
        return inputs.frame('filtered')

    program.register_calculation(PythonCalculation('actual', calculate, ('filtered',),
        value_field='amount', output_grain=('item',), unit=unit),
        question_id='question', result_id='planned')
    return program, ExecutableResults(program.execution), calls


def test_findings_consume_once_keep_ties_missing_values_and_the_full_population():
    program, results, calls = make_results([5, None, 5, 2], ['A', 'missing', 'B', 'C'])
    rank = results.finding('rank', 'planned', kind='ranking', category_field='item')
    span = results.finding('range', ('question', 'planned'), kind='range', category_field='item')
    assert len(calls) == 1
    assert [row['rank'] for row in rank['rows']] == [1, 1, 3]
    assert span['highest_labels'] == ['A', 'B']
    assert span['difference'] == 3
    assert span['missing_count'] == rank['missing_count'] == 1
    rank['rows'][0]['value'] = -999
    assert program.execution.trace()['findings']['rank']['rows'][0]['value'] == 5


@pytest.mark.parametrize('values,expected', [([], '没有可比较'), ([3], '仅有一个'), ([3, 3], '全部相等')])
def test_range_handles_empty_single_and_all_equal_without_inventing_a_leader(values, expected):
    _, results, _ = make_results(values)
    finding = results.finding('r', 'planned', kind='range', category_field='item')
    assert expected in finding['text']


def test_scope_reports_each_actual_source_not_loader_receipt_or_sum_as_population():
    _, results, _ = make_results([1, 2, 3])
    finding = results.finding('scope', 'planned', kind='scope')
    assert finding['source_scopes']['filtered']['row_count'] == 3
    assert '3 行' in finding['text']
    assert '不证明业务总体完整性' in finding['text']


def test_table_keeps_heterogeneous_metrics_without_inventing_a_comparison():
    _, results, _ = make_results([10, 2], ['总功率', '平均功率'])
    finding = results.finding('metrics', 'planned', kind='table')
    assert '| 总功率 | 10 |' in finding['text']
    assert '| 平均功率 | 2 |' in finding['text']
    assert 'rank' not in finding and 'difference' not in finding


def test_percentage_range_uses_percentage_points_and_escaped_source_labels():
    _, results, _ = make_results([80, 60], ['A|B', 'C\nD'], unit='%')
    finding = results.finding('range', 'planned', kind='range', category_field='item')
    assert '20 个百分点' in finding['text']
    assert r'A\|B' in finding['text']
    assert 'C D' in finding['text']


def test_finding_identity_cannot_be_reused_for_another_operation_or_absent_field():
    _, results, _ = make_results([1, 2])
    results.finding('same', 'planned', kind='ranking', category_field='item')
    with pytest.raises(ValueError, match='another result/operation'):
        results.finding('same', 'planned', kind='range', category_field='item')
    with pytest.raises(ValueError, match='fields must exist'):
        results.finding('other', 'planned', kind='ranking', field='invented', category_field='item')


def test_bound_report_uses_current_candidate_and_keeps_narrative_without_recalculation():
    program, results, calls = make_results([2, 5])
    finding = results.finding('rank', 'planned', kind='ranking', category_field='item')
    blocks = [{'kind': 'text', 'text': '# 分析\n\n'},
              {'kind': 'finding', 'trace_execution_id': 'executable-analysis', 'finding_id': 'rank'},
              {'kind': 'text', 'text': '\n\n后续可结合已有业务规则解释差异。'}]
    text, bindings = compose_bound_report([program.execution.trace()], blocks)
    assert finding['text'] in text
    assert text.endswith('后续可结合已有业务规则解释差异。')
    assert len(calls) == 1
    assert bindings[0]['fact_id'] == 'actual'
    assert bindings[0]['business_status'] == 'not_evaluated'
    assert text[bindings[0]['start']:bindings[0]['end']] == finding['text']
    with pytest.raises(ValueError, match='current execution candidate'):
        compose_bound_report([], blocks)
    with pytest.raises(ValueError, match='uniquely'):
        compose_bound_report([program.execution.trace(), program.execution.trace()], blocks)


@pytest.mark.parametrize('output_format', ['standard', 'text'])
@pytest.mark.parametrize('authoring', ['blocks', 'inline'])
def test_presentation_tool_finalizes_current_candidate_blocks_and_preserves_artifacts(monkeypatch, output_format, authoring):
    from dataclasses import replace
    from datetime import datetime
    from types import SimpleNamespace
    import agent.tools as rt
    import config.database as database
    from agent.runtime_driver_state import new_runtime_work_packet, RuntimePhase

    program, results, _ = make_results([2, 5])
    results.finding('rank', 'planned', kind='ranking', category_field='item')
    candidate = rt.StructuredResult(analysis_report='', charts=[], tables=[{'title': '已有明细', 'columns': [], 'rows': []}],
        presentation_pending=True, output_format=output_format, semantic_execution_traces=[program.execution.trace()])
    record = SimpleNamespace(result_payload_json=candidate.model_dump_json(), analysis_report='',
        created_at=datetime.now(), execution_seconds=0, used_datasource_ids_json='[]')

    class Session:
        committed = False
        def query(self, *args): return self
        def filter(self, *args): return self
        def with_for_update(self): return self
        def first(self): return record
        def commit(self): self.committed = True
        def rollback(self): raise AssertionError('Unexpected rollback')
        def close(self): pass

    session = Session()
    monkeypatch.setattr(database, 'SessionLocal', lambda: session)
    monkeypatch.setattr(rt, '_get_stream_emitter', lambda: lambda *args, **kwargs: None)
    monkeypatch.setattr(rt, '_terminal_tool_success', lambda message: message)
    packet = replace(new_runtime_work_packet(), phase=RuntimePhase.EXPRESS, active_execution_id=99)
    context = SimpleNamespace(runtime_work_packet=packet.to_dict(), conversation_id=1,
        output_contract={'type': output_format}, execution_datasource_snapshot={}, active_datasource_snapshot={})
    finding = rt.FindingReportBlock(kind='finding', trace_execution_id='executable-analysis', finding_id='rank')
    arguments = ({'report_blocks': [rt.NarrativeReportBlock(kind='text', text='结果：\n\n'), finding]}
                 if authoring == 'blocks' else {'report_text': '结果：\n\n{{ranking}}', 'report_refs': {'ranking': finding}})
    response = rt.compose_analysis_presentation.func(runtime=SimpleNamespace(context=context, tool_call_id='compose'),
        execution_id=99, **arguments)
    import json
    assert json.loads(response.content)['status'] == 'success'
    saved = rt.StructuredResult.model_validate_json(record.result_payload_json)
    assert '| 1 | 1 | 5 |' in saved.analysis_report
    assert saved.tables == candidate.tables
    assert saved.report_finding_bindings[0]['finding_id'] == 'rank'
    assert not saved.presentation_pending
    assert session.committed


@pytest.mark.parametrize('with_findings', [True, False])
@pytest.mark.parametrize('output_format,active_id,expected_bound', [
    ('standard', 99, True), ('text', 99, True), ('json', 99, False), ('standard', 100, False)])
def test_working_tool_schema_uses_blocks_for_current_result_candidate(output_format, active_id, expected_bound, with_findings):
    from dataclasses import replace
    from types import SimpleNamespace
    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage
    import agent.tools as rt
    from agent.context_engineering_runtime import CustomContext
    from agent.runtime_driver_state import new_runtime_work_packet, RuntimePhase
    from agent.runtime_working_context import build_working_request

    program, results, _ = make_results([2, 5])
    if with_findings:
        results.finding('rank', 'planned', kind='ranking', category_field='item')
    else:
        program.execution.run()
    candidate = rt.StructuredResult(analysis_report='', charts=[], tables=[], presentation_pending=True,
        output_format=output_format, semantic_execution_traces=[program.execution.trace()])
    packet = replace(new_runtime_work_packet(), phase=RuntimePhase.EXPRESS, active_execution_id=active_id)
    context = CustomContext(username='test', user_query='分析', namespace_id=1,
        runtime_work_packet=packet.to_dict(), output_contract={'type': output_format})
    request = build_working_request(ModelRequest(model=None,
        runtime=SimpleNamespace(context=context), tools=[rt.compose_analysis_presentation],
        system_message=SystemMessage('Runtime'), messages=[HumanMessage('分析'),
        rt._presentation_candidate_message(tool_call_id='candidate', execution_id=99, candidate=candidate)]))
    assert (request.tools[0].args_schema is rt.ComposeBoundPresentationInput) == expected_bound
    if expected_bound:
        assert 'analysis_report' not in request.tools[0].args_schema.model_fields


def test_inline_cells_use_saved_rows_preserve_narrative_and_do_not_recompute():
    from decimal import Decimal
    program, _, calls = make_results([Decimal('1230.125'), Decimal('0.20')], ['A|B', 'C'])
    program.execution.run()
    def value(row, field, **options):
        return dict(kind='value', trace_execution_id='executable-analysis', fact_id='actual',
                    row_index=row, field=field, **options)
    blocks = [dict(kind='text', text='对象 '), value(0, 'item'),
              dict(kind='text', text=' 为 '), value(0, 'amount', precision=3),
              dict(kind='text', text='；另一对象为 '), value(1, 'amount'),
              dict(kind='text', text='。此处仍是模型的自由判断。')]
    text, bindings = compose_bound_report([program.execution.trace()], blocks)
    assert text == r'对象 A\|B 为 1,230.125；另一对象为 0.2。此处仍是模型的自由判断。'
    assert calls == [1]
    assert all(text[b['start']:b['end']] == b['text'] for b in bindings)
    assert bindings[1]['presentation_operation'] == dict(kind='display_value', row_index=0, precision=3)
    for bad in [value(-1, 'amount'), value(2, 'amount'), value(0, 'absent'), value(0, 'amount', precision=13)]:
        with pytest.raises(ValueError, match='existing row/field'):
            compose_bound_report([program.execution.trace()], [bad])
    with pytest.raises(ValueError, match='uniquely'):
        compose_bound_report([], [value(0, 'amount')])


def test_inline_reference_substitution_preserves_layout_and_repeated_spans():
    from analysis_runtime.findings import compose_referenced_report
    program, _, calls = make_results([1234.5])
    program.execution.run()
    text = '# 分析\n\n金额 **{{amount}}** 元。\n\n| 指标 | 金额 |\n|---|---|\n| 同一结果 | {{amount}} |\n'
    rendered, bindings = compose_referenced_report([program.execution.trace()], text,
        {'amount': {'kind': 'value', 'trace_execution_id': 'executable-analysis',
                    'fact_id': 'actual', 'row_index': 0, 'field': 'amount'}})
    assert rendered == text.replace('{{amount}}', '1,234.5')
    assert len(bindings) == 2
    assert all(rendered[b['start']:b['end']] == '1,234.5' for b in bindings)
    assert calls == [1]
    with pytest.raises(ValueError, match='Unknown report reference'):
        compose_referenced_report([program.execution.trace()], text, {})
