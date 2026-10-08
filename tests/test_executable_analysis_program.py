import queue
from types import SimpleNamespace

import pytest
import pandas as pd

import agent.tools as rt


def run_worker(source, parameters=None, output_format='standard', defer_presentation=False):
    code = rt.compile_python_analysis_definition(
        source, output_format=output_format, defer_presentation=defer_presentation,
    )
    assert rt._validate_generated_code_contract(code, output_format=output_format) is None
    notices = queue.Queue()
    rt._execute_generated_code_worker(code, 'definition-test', {
        'analysis_parameters_override': parameters or {},
        'output_contract': {'type': output_format, 'schema': {'type': 'object'}},
    }, notices)
    notice = notices.get_nowait()
    worker = rt._read_worker_result_payload(notice['result_file_path'])
    assert worker['status'] == 'success', worker
    return code, rt._deserialize_exec_result(worker['exec_result'])


@pytest.mark.parametrize('output_format,key,value', [('text', 'output_text', '完整文本'), ('json', 'output_json', {'完整': True})])
def test_host_saves_nonstandard_outputs_from_the_same_native_definition_port(output_format, key, value):
    source = f'''def build_analysis(program, parameters):
    def present(results, parameters):
        return {{{key!r}: {value!r}}}
    program.register_presentation(present)
'''
    _, result = run_worker(source, output_format=output_format)
    assert result.output_format == output_format
    assert result.output_payload == value


def test_presentation_distinguishes_query_sources_from_calculated_result_tables():
    from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults

    program = ExecutablePythonProgram()
    program.execution.add_source('equipment', pd.DataFrame({'id': [1]}), query_key='equipment-query')
    results = ExecutableResults(program.execution)
    with pytest.raises(ValueError, match=r"registered query source.*use source_table\(\)"):
        results.table('equipment')
    assert results.source_table('equipment')['id'].tolist() == [1]


def test_worker_preserves_bound_findings_and_filtered_scope_for_parameterized_reruns(tmp_path):
    from analysis_runtime.findings import compose_bound_report
    path = tmp_path / 'records.csv'
    path.write_text('id,month,item,amount\n1,03,A,2\n2,03,B,5\n3,04,A,8\n', encoding='utf-8')
    source = f'''def build_analysis(program, parameters):
    def load(parameters):
        frame = query_local_file(query_key='records', file_path={path.as_posix()!r},
            query_constraints={{'time_range': None, 'target_filters': [], 'aggregation': None}})
        return frame.loc[frame['month'].astype(str).str.zfill(2) == parameters['month']].copy()
    def calculate(inputs, parameters):
        return inputs.frame('records').groupby('item', as_index=False)['amount'].sum()
    def present(results, parameters):
        results.finding('rank', 'summary', kind='ranking', category_field='item')
        results.finding('scope', 'summary', kind='scope')
        return {{'tables': [build_table_result(data=results.table('summary'), title='金额',
            columns=[{{'key': 'item', 'label': '对象'}}, {{'key': 'amount', 'label': '金额'}}])]}}
    program.register_query('records', load, grain=('id',))
    program.register_calculation(PythonCalculation('summary', calculate, ('records',),
        value_field='amount', output_grain=('item',)), question_id='q')
    program.register_presentation(present)
'''
    _, march = run_worker(source, {'month': '03'}, defer_presentation=True)
    _, april = run_worker(source, {'month': '04'}, defer_presentation=True)
    blocks = [{'kind': 'finding', 'trace_execution_id': 'executable-analysis', 'finding_id': 'rank'}]
    report, _ = compose_bound_report(march.semantic_execution_traces, blocks)
    assert '| 1 | B | 5 |' in report
    assert march.semantic_execution_traces[0]['findings']['scope']['source_scopes']['records']['row_count'] == 2
    assert april.semantic_execution_traces[0]['findings']['scope']['source_scopes']['records']['row_count'] == 1
    report, _ = compose_bound_report(april.semantic_execution_traces, blocks)
    assert '| 1 | A | 8 |' in report
    assert march.presentation_pending and april.presentation_pending


def test_presentation_resolves_planned_result_identity_to_its_calculation():
    from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults
    from analysis_runtime.semantic_execution import PythonCalculation

    program = ExecutablePythonProgram()
    program.execution.add_source('source', pd.DataFrame({'amount': [3, 5]}), query_key='source-query')
    program.register_calculation(
        PythonCalculation(
            'internal_calculation',
            lambda inputs, parameters: pd.DataFrame({'total': [inputs['source']['amount'].sum()]}),
            input_source_ids=('source',), value_field='total',
        ),
        question_id='question_1', result_id='planned_total',
    )
    program.execution.run()

    results = ExecutableResults(program.execution)
    assert results.table('planned_total')['total'].tolist() == [8]
    assert results.table(('question_1', 'planned_total'))['total'].tolist() == [8]
    assert results.table('internal_calculation')['total'].tolist() == [8]
    status = results.status('planned_total')
    assert status == {'state': 'valid', 'missing_count': 0, 'input_rows': 2, 'unit': ''}
    assert results.state('planned_total') == status
    assert 'total' not in status


def test_extrema_resolves_planned_result_identity_to_its_calculation():
    from analysis_runtime.executable_program import ExecutablePythonProgram
    from analysis_runtime.semantic_execution import PythonCalculation

    program = ExecutablePythonProgram()
    program.execution.add_source('source', pd.DataFrame({'category': ['A', 'B']}), query_key='source-query')
    program.register_calculation(
        PythonCalculation(
            'internal_calculation',
            lambda inputs, parameters: pd.DataFrame({
                'category': inputs['source']['category'], 'value': [2, 5],
            }),
            input_source_ids=('source',), value_field='value', output_grain=('category',),
        ),
        question_id='question_1', result_id='planned_total',
    )
    program.register_extrema('largest', 'planned_total', mode='max')

    def present(results, parameters):
        return {
            'state': results.decision_state('largest')['state'],
            'labels': results.matches('largest')['category'].tolist(),
        }

    program.register_presentation(present)
    assert program.deliver({}) == {'state': 'true', 'labels': ['B']}


def test_calculation_callback_receives_dataframes_keyed_by_declared_input_source_ids():
    from analysis_runtime.executable_program import ExecutablePythonProgram
    from analysis_runtime.semantic_execution import CalculationInputs, PythonCalculation

    program = ExecutablePythonProgram()
    program.execution.add_source('left_source', pd.DataFrame({'amount': [2]}), query_key='left-query')
    program.execution.add_source('right_source', pd.DataFrame({'amount': [5]}), query_key='right-query')
    observed = {}

    def calculate(inputs, parameters):
        assert isinstance(inputs, CalculationInputs)
        observed['keys'] = tuple(sorted(inputs))
        observed['named_frame'] = inputs.frame('left_source')['amount'].iloc[0]
        with pytest.raises(TypeError, match=r'not positions.*inputs\.frame\(source_id\)'):
            inputs[0]
        observed['parameter'] = parameters['scale']
        return pd.DataFrame({'total': [
            inputs['left_source']['amount'].sum() + inputs['right_source']['amount'].sum()
        ]})

    program.register_calculation(
        PythonCalculation(
            'combined_total', calculate,
            input_source_ids=('left_source', 'right_source'),
            parameters={'scale': 3}, value_field='total',
        ),
        question_id='question_1', result_id='combined_total',
    )
    program.execution.run()

    assert observed == {
        'keys': ('left_source', 'right_source'), 'named_frame': 2, 'parameter': 3,
    }


def test_presentation_requires_qualified_identity_when_planned_result_names_collide():
    from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults
    from analysis_runtime.semantic_execution import PythonCalculation

    program = ExecutablePythonProgram()
    program.execution.add_source('source', pd.DataFrame({'amount': [3]}), query_key='source-query')
    for question_id, calculation_id in (('question_1', 'first'), ('question_2', 'second')):
        program.register_calculation(
            PythonCalculation(
                calculation_id,
                lambda inputs, parameters: pd.DataFrame({'total': [inputs['source']['amount'].sum()]}),
                input_source_ids=('source',), value_field='total',
            ),
            question_id=question_id, result_id='total',
        )
    program.execution.run()

    results = ExecutableResults(program.execution)
    with pytest.raises(ValueError, match=r'ambiguous.*\(question_id, result_id\)'):
        results.table('total')
    with pytest.raises(ValueError, match=r'ambiguous.*\(question_id, result_id\)'):
        program.register_extrema('largest', 'total')
    program.register_extrema('largest_question_2', ('question_2', 'total'))
    assert results.table(('question_2', 'total'))['total'].tolist() == [3]


def test_presentation_rejects_collision_between_internal_and_planned_result_identity():
    from analysis_runtime.executable_program import ExecutablePythonProgram, ExecutableResults
    from analysis_runtime.semantic_execution import PythonCalculation

    program = ExecutablePythonProgram()
    program.execution.add_source('source', pd.DataFrame({'amount': [3]}), query_key='source-query')
    for question_id, calculation_id, result_id in (
        ('question_1', 'shared', 'first_result'),
        ('question_2', 'other', 'shared'),
    ):
        program.register_calculation(
            PythonCalculation(
                calculation_id,
                lambda inputs, parameters: pd.DataFrame({'total': [inputs['source']['amount'].sum()]}),
                input_source_ids=('source',), value_field='total',
            ),
            question_id=question_id, result_id=result_id,
        )
    program.execution.run()

    results = ExecutableResults(program.execution)
    with pytest.raises(ValueError, match=r'ambiguous.*\(question_id, result_id\)'):
        results.table('shared')
    assert results.table(('question_2', 'shared'))['total'].tolist() == [3]


def test_public_native_submission_uses_existing_execution_path_and_preserves_legacy_reader(monkeypatch):
    calls = []
    monkeypatch.setattr(rt.execute_python, 'func', lambda **kwargs: calls.append(kwargs) or 'submitted')
    runtime = SimpleNamespace(context=SimpleNamespace(output_contract={'type': 'standard'}))
    source = 'def build_analysis(program, parameters):\n    pass'
    assert rt.submit_analysis_program.func(runtime=runtime, definition_source=source, used_datasource_ids=[7]) == 'submitted'
    assert calls[0]['used_datasource_ids'] == [7]
    assert calls[0]['execution_mode'] == 'analysis'
    assert 'build_analysis(_program_definition' in calls[0]['code']
    assert 'defer_presentation=True' in calls[0]['code']
    with pytest.raises(ValueError, match='multiple programs'):
        rt.submit_analysis_program.func(runtime=runtime, definition_source=source, code='other implementation')
    assert rt.SubmitAnalysisProgramInput(code='legacy()', used_datasource_ids=[7]).code == 'legacy()'
    assert rt.SubmitAnalysisProgramInput(definition_source=source, used_datasource_ids=[7]).definition_source == source


@pytest.mark.parametrize('output_format', ['standard', 'text', 'json'])
def test_native_candidate_defers_all_model_authored_output_until_after_fact_delivery(output_format):
    source = '''def build_analysis(program, parameters):
    def present(results, parameters):
        return {"analysis_report": "precomputed prose", "output_text": "precomputed text",
                "output_json": {"precomputed": True}, "charts": [], "tables": []}
    program.register_presentation(present)
'''
    code, candidate = run_worker(
        source, output_format=output_format, defer_presentation=True,
    )
    assert 'defer_presentation=True' in code
    assert candidate.presentation_pending is True
    assert candidate.analysis_report == ''
    assert candidate.output_payload is None
    assert candidate.output_format == output_format


def test_presentation_candidate_rejects_an_execution_id_outside_runtime_packet():
    from agent.runtime_driver_state import RuntimePhase, RuntimeWorkPacket
    from agent.context_engineering_runtime import CustomContext

    packet = RuntimeWorkPacket(
        workspace_id='current', revision=3, phase=RuntimePhase.EXPRESS,
        active_execution_id=41,
    )
    runtime = SimpleNamespace(
        context=CustomContext(username='test', conversation_id=7, runtime_work_packet=packet.to_dict()),
        tool_call_id='compose-call',
    )
    result = rt.compose_analysis_presentation.func(
        runtime=runtime, execution_id=42, analysis_report='Report',
    )
    payload = __import__('json').loads(result.content)
    assert payload['status'] == 'failed'
    assert payload['error_type'] == 'presentation_candidate_mismatch'


def test_candidate_stays_running_until_post_execution_expression_is_saved(monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from agent.context_engineering_runtime import CustomContext
    from agent.runtime_driver_state import RuntimePhase, RuntimeWorkPacket
    from model import InsightNsExecution

    engine = create_engine('sqlite://')
    InsightNsExecution.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr('config.database.SessionLocal', sessions)
    candidate = rt.StructuredResult(
        analysis_report='', charts=[], tables=[],
        output_format='standard', presentation_pending=True,
        semantic_execution_traces=[{'facts': {'years': {'values': [{'year': 2022}, {'year': 2023}]}}}],
    )
    with sessions() as session:
        session.add(InsightNsExecution(
            id=41, conversation_id=7, turn_id=9, tool_call_id='calc', title='Analysis',
            description='', generated_code='definition code', used_datasource_ids_json='[12]',
            execution_status='running', analysis_report='', result_payload_json='{}',
            created_at=datetime.now(),
        ))
        session.commit()

    assert rt._store_presentation_candidate(41, candidate, used_datasource_ids=[12])
    with sessions() as session:
        stored = session.query(InsightNsExecution).filter_by(id=41).one()
        assert stored.execution_status == 'running'
        assert stored.finished_at is None
        assert rt.StructuredResult.model_validate_json(stored.result_payload_json).presentation_pending

    monkeypatch.setattr(
        rt, '_validate_structured_result_contract',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('expression phase must not run semantic result validators')
        ),
    )
    packet = RuntimeWorkPacket(
        workspace_id='candidate', revision=3, phase=RuntimePhase.EXPRESS,
        active_execution_id=41,
    )
    runtime = SimpleNamespace(
        context=CustomContext(username='test', conversation_id=7, turn_id=10,
                              runtime_work_packet=packet.to_dict(), output_contract={'type': 'standard'}),
        tool_call_id='compose',
    )
    result = rt.compose_analysis_presentation.func(
        runtime=runtime, execution_id=41,
        analysis_report='2022 年和 2023 年各有 3 项，并列。',
    )
    assert result.update['analysis_complete'] is True
    with sessions() as session:
        stored = session.query(InsightNsExecution).filter_by(id=41).one()
        finalized = rt.StructuredResult.model_validate_json(stored.result_payload_json)
        assert stored.execution_status == 'success'
        assert stored.analysis_report == '2022 年和 2023 年各有 3 项，并列。'
        assert finalized.presentation_pending is False
        assert finalized.semantic_execution_traces == candidate.semantic_execution_traces
    engine.dispose()


def test_expression_fact_view_preserves_small_ties_and_marks_large_values_as_samples():
    trace = {'execution_id': 'semantic-run', 'result_bindings': [], 'decisions': {}, 'facts': {
        'small': {'state': 'complete', 'operation': 'DistinctCount', 'value_field': 'value',
                  'definition': {'unit': '台', 'group_by': ['year']},
                  'values': [{'year': 2022, 'value': 3}, {'year': 2023, 'value': 3}]},
        'large': {'state': 'complete', 'operation': 'PythonCalculation', 'value_field': 'value',
                  'definition': {}, 'values': [{'value': index} for index in range(150)]},
    }}
    view = rt._build_expression_fact_view([trace])[0]['facts']
    assert view['small']['values'] == [{'year': 2022, 'value': 3}, {'year': 2023, 'value': 3}]
    assert view['small']['values_truncated'] is False
    assert len(view['large']['values']) == 100
    assert view['large']['row_count'] == 150
    assert view['large']['values_truncated'] is True


def test_expression_fact_view_carries_post_filter_input_scope_and_source_grain():
    trace = {
        'execution_id': 'source-scope',
        'result_bindings': [],
        'decisions': {},
        'sources': {
            'profit_march': {
                'query_keys': ['profit_march'],
                'grain': ['公司编码', '车间编码', '产线编码', '产品编码', '年月'],
                'row_count': 36,
                'binding': 'declared',
            },
        },
        'facts': {
            'profit_summary': {
                'state': 'valid',
                'operation': 'PythonCalculation',
                'value_field': '毛利_元',
                'definition': {
                    'definition_id': 'profit_summary',
                    'input_source_ids': ['profit_march'],
                    'output_grain': ['产品编码'],
                },
                'input_rows': 36,
                'input_fact_ids': [],
                'values': [{'产品编码': 'P-STD-001', '毛利_元': 10}],
            },
        },
    }

    view = rt._build_expression_fact_view([trace])[0]

    assert view['source_scopes']['profit_march'] == {
        'query_keys': ['profit_march'],
        'grain': ['公司编码', '车间编码', '产线编码', '产品编码', '年月'],
        'row_count': 36,
        'binding': 'declared',
    }
    assert view['facts']['profit_summary']['input_rows'] == 36
    assert view['facts']['profit_summary']['input_source_scopes']['profit_march']['row_count'] == 36
    assert view['facts']['profit_summary']['row_count'] == 1


def test_presentation_candidate_distinguishes_loaded_rows_from_calculation_population():
    trace = {
        'execution_id': 'source-scope',
        'result_bindings': [],
        'decisions': {},
        'sources': {'profit_march': {'query_keys': ['profit_march'], 'row_count': 36,
                                     'grain': ['产品编码', '年月']}},
        'facts': {'profit_summary': {
            'state': 'valid', 'operation': 'PythonCalculation', 'value_field': '毛利_元',
            'definition': {'definition_id': 'profit_summary', 'input_source_ids': ['profit_march']},
            'input_rows': 36, 'values': [{'产品编码': 'P-STD-001', '毛利_元': 10}],
        }},
    }
    candidate = SimpleNamespace(
        output_format='standard', semantic_execution_traces=[trace], charts=[], tables=[],
        analysis_parameters={}, query_snapshots={'profit_march': {'execution': {
            'status': 'success', 'row_count': 108, 'columns': ['产品编码', '年月'],
        }}},
    )

    message = rt._presentation_candidate_message(tool_call_id='submit', execution_id=41, candidate=candidate)
    payload = __import__('json').loads(message.content)

    assert payload['query_receipts'][0]['loaded_row_count'] == 108
    assert 'row_count' not in payload['query_receipts'][0]
    fact = payload['facts'][0]['facts']['profit_summary']
    assert fact['input_rows'] == 36
    assert fact['input_source_scopes']['profit_march']['row_count'] == 36
    assert 'loaded_row_count' in payload['query_receipt_row_count_semantics']
    assert 'may be broader' in payload['query_receipt_row_count_semantics']
    assert 'rows actually supplied to that calculation' in payload['expression_instruction']


def test_expression_contract_requires_complete_population_facts_for_truncated_claims():
    from pathlib import Path

    root = Path(__file__).parents[1] / 'src' / 'agent' / 'prompts' / 'runtime' / 'contracts' / 'execution'
    definition = (root / 'definition.md').read_text(encoding='utf-8')
    expression = (root / 'expression.md').read_text(encoding='utf-8')
    assert '基于完整总体计算问题所需的汇总事实' in definition
    assert '不要期待表达阶段从截断样本恢复总体统计' in definition
    assert 'use the complete-population summary facts produced by the execution definition' in expression
    assert 'state that boundary instead of calculating it from the sample' in expression


@pytest.mark.parametrize('factor', [2, 3])
def test_python_definition_module_uses_host_lifecycle_real_queries_and_dependency_replay(tmp_path, factor):
    path = tmp_path / 'amounts.csv'
    path.write_text('id,price,qty\n1,2.10,2\n2,3.20,3\n', encoding='utf-8')
    source = f'''analysis_parameters_schema = {{'parameters': [], 'defaults': {{'factor': 1}}}}
def build_analysis(program, parameters):
    def load(parameters):
        return query_local_file(query_key='amounts', file_path={path.as_posix()!r},
            query_constraints={{'time_range': None, 'target_filters': [], 'aggregation': {{'grain': 'detail', 'metric': 'detail'}}}})
    def calculate(inputs, parameters):
        frame = inputs['amount']
        frame['value'] *= parameters['factor']
        return frame
    def present(results, parameters):
        return {{'analysis_report': results.compose_report(['总额：', results.value('scaled', value_format='.2f')]),
                'tables': [build_table_result(data=results.table('scaled'), title='总额', columns=[{{'key': 'value', 'label': '总额'}}])]}}
    program.register_query('rows', load, grain=('id',))
    program.register_calculation(PythonCalculation('scaled', calculate, ('amount',), parameters=parameters), question_id='q1')
    program.register_transform(DecimalColumns('normalized', 'rows', ('price',)))
    program.register_calculation(RowProductSum('amount', 'normalized', 'price', 'qty'), question_id='q1')
    program.register_presentation(present)
'''
    parsed = rt.ExecutableAnalysisProgramInput(definition_source=source, used_datasource_ids=[7])
    code, result = run_worker(parsed.definition_source, {'factor': factor})
    assert rt._validate_generated_code_contract(code, output_format='standard') is None
    assert ('27.60' if factor == 2 else '41.40') in result.analysis_report
    trace = result.semantic_execution_traces[0]
    assert trace['sources']['rows']['query_receipt']['query_key'] == 'amounts'
    assert trace['facts']['scaled']['input_fact_ids'] == ['amount']
    assert trace['facts']['scaled']['definition']['implementation']['source']
    assert result.semantic_execution_coverage['business_status'] == 'not_evaluated'
    assert rt._validate_structured_result_contract(result, generated_code=code) is None


def test_object_dataframe_calculation_freezes_list_inputs_and_keeps_wide_table(tmp_path):
    path = tmp_path / 'assets.csv'
    path.write_text('asset_id,kind\n1,pump\n2,motor\n', encoding='utf-8')
    source = f'''import pandas as pd
def build_analysis(program, parameters):
    def load(parameters):
        return query_local_file(query_key='assets', file_path={path.as_posix()!r},
            query_constraints={{'time_range': None, 'target_filters': [], 'aggregation': {{'grain': 'detail', 'metric': 'detail'}}}})
    def calculate(inputs, parameters):
        frame = inputs['assets']
        return pd.DataFrame([{{'total': len(frame), 'categories': frame['kind'].nunique()}}])
    def present(results, parameters):
        table = results.table('summary')
        return {{'analysis_report': f"总数：{{int(table.iloc[0]['total'])}}，类别：{{int(table.iloc[0]['categories'])}}",
                'tables': [build_table_result(data=table, title='资产摘要', columns=[{{'key': 'total', 'label': '总数'}}, {{'key': 'categories', 'label': '类别'}}])]}}
    program.register_query('assets', load, grain=('asset_id',))
    program.register_calculation(PythonCalculation('summary', calculate, input_source_ids=['assets'],
        parameters=parameters, value_field=None, output_grain=[], value_kind='object'), question_id='q1')
    program.register_presentation(present)
'''
    code, result = run_worker(source)
    assert '总数：2，类别：2' in result.analysis_report
    trace = result.semantic_execution_traces[0]
    assert trace['facts']['summary']['value_field'] is None
    assert trace['facts']['summary']['definition']['input_source_ids'] == ['assets']
    assert trace['facts']['summary']['values'] == [{'total': 2, 'categories': 2}]


def test_model_wire_submits_one_native_definition_module_without_serialized_program_wrapper():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    wire = convert_to_openai_tool(rt.submit_analysis_program.model_copy(update={'args_schema': rt.ExecutableAnalysisProgramInput}))
    parameters = wire['function']['parameters']
    assert parameters['properties']['definition_source']['type'] == 'string'
    assert 'definition_source' in parameters['required']
    assert 'definition' not in parameters['properties']
    assert 'code' not in parameters['properties']
