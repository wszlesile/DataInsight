import ast
from datetime import timedelta
import inspect
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from agent import tools
from agent.runtime_contract_registry import (
    RUNTIME_CONTRACT_PACKS,
    load_runtime_contract_text,
    runtime_helper_signature,
)
from agent.runtime_prompt_loader import load_runtime_prompt


CONTRACTS = Path(__file__).resolve().parents[1] / 'src/agent/prompts/runtime/contracts'


@pytest.fixture(autouse=True)
def isolated_execution_context():
    values = (
        (tools.CURRENT_QUERY_SNAPSHOTS, {}),
        (tools.CURRENT_LAST_QUERY_KEY, ''),
        (tools.CURRENT_EXPECTED_TIME_RANGE, {}),
        (tools.CURRENT_TIME_RANGE_GUARD_ACTIVE, False),
        (tools.CURRENT_EXECUTION_INTENT, 'analysis'),
        (tools.CURRENT_ANALYSIS_PARAMETERS_OVERRIDE, {}),
        (tools.CURRENT_CONFIRMED_NO_DATA_RESULT, False),
        (tools.CURRENT_OUTPUT_CONTRACT, {'type': 'standard'}),
    )
    tokens = [(variable, variable.set(value)) for variable, value in values]
    yield
    for variable, token in reversed(tokens):
        variable.reset(token)


def example(filename, name, marker='runtime-behavior-example'):
    body = (CONTRACTS / filename).read_text(encoding='utf-8')
    match = re.search(rf'<!-- {marker}: {name} -->\s*```python\s*(.*?)\s*```', body, re.S)
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize('name', sorted({name for pack in RUNTIME_CONTRACT_PACKS for name in pack.helpers}))
def test_published_signature_matches_the_callable_including_keyword_only_parameters(name):
    source = runtime_helper_signature(name)
    namespace = {}
    exec(f'def {source}:\n    pass\n', namespace)
    actual = inspect.signature(tools._build_execution_namespace()[name])
    documented = inspect.signature(namespace[name])
    assert list(documented.parameters) == list(actual.parameters)
    for key, parameter in actual.parameters.items():
        assert documented.parameters[key].kind == parameter.kind
        assert documented.parameters[key].default == parameter.default
    prompt = load_runtime_contract_text(pack.contract_id for pack in RUNTIME_CONTRACT_PACKS)
    assert prompt.count(f'`{source}`') == 1


def test_helper_coverage_is_exhaustive_without_exposing_backend_only_helpers():
    exposed = set(tools._build_execution_namespace()) - {'pd'}
    published = {name for pack in RUNTIME_CONTRACT_PACKS for name in pack.helpers}
    assert exposed - published == {'build_chart_document', 'save_empty_analysis_result'}
    mixed = load_runtime_prompt(('sql', 'time_series', 'local_file'))
    assert 'query_data_with_sql(*,' in mixed
    assert 'query_local_file(*,' in mixed
    assert 'query_local_file_low_memory(*,' in mixed
    assert 'load_data_with_api(' not in mixed
    assert 'load_minio_file(' not in mixed
    assert '<!-- runtime-helper-reference -->' not in mixed


def test_known_day_range_behavior_is_not_changed_to_match_incorrect_documentation():
    start, end = tools.get_day_range(1)
    assert start.tzinfo is not None and end.tzinfo == start.tzinfo
    assert start.hour == start.minute == start.second == start.microsecond == 0
    assert end.date() == start.date()
    assert end.hour == 23 and end.minute == 59 and end.second == 59 and end.microsecond == 999999
    assert start + timedelta(days=1) == end + timedelta(microseconds=1)
    assert '包含式终点' in load_runtime_contract_text(('execution.core',))


@pytest.mark.parametrize('mode', ['analysis', 'probe'])
@pytest.mark.parametrize('source', [
    'result = execution_intent\n',
    '# header\r\nresult = execution_intent\r\n',
    '"""module doc"""\nfrom __future__ import annotations\nresult = execution_intent',
    '"""中文说明"""; result = execution_intent',
    '"""module doc"""',
    '@(lambda function: function)\ndef report():\n    return execution_intent\nresult = report()',
    '"""module doc"""\n@(lambda cls: cls)\nclass Report:\n    mode = execution_intent\nresult = Report.mode',
])
def test_tool_mode_is_persisted_as_valid_standalone_source(source, mode):
    materialized = tools._bind_program_execution_mode(source, mode)
    namespace = {}
    exec(compile(materialized, '<analysis>', 'exec'), namespace)
    assert namespace['execution_intent'] == mode
    if 'result =' in source:
        assert namespace['result'] == mode
    assert tools._bind_program_execution_mode(materialized, mode) == materialized
    assert ast.get_docstring(ast.parse(source)) == ast.get_docstring(ast.parse(materialized))


def test_existing_declarations_and_syntax_error_source_are_not_rewritten():
    source = 'execution_intent: str = "analysis"\nresult = 1\n'
    assert tools._bind_program_execution_mode(source, 'analysis') == source
    invalid = 'result = (\n'
    assert tools._bind_program_execution_mode(invalid, 'analysis') == invalid
    assert tools._bind_program_execution_mode('', 'analysis') == ''
    with pytest.raises(ValueError, match='match the selected tool'):
        tools._bind_program_execution_mode(source, 'probe')
    with pytest.raises(ValueError, match='dynamic assignment'):
        tools._bind_program_execution_mode('if True:\n    execution_intent = "analysis"', 'analysis')


@pytest.mark.parametrize('tool,mode', [(tools.probe_analysis_data, 'probe'), (tools.submit_analysis_program, 'analysis')])
def test_public_tool_supplies_mode_without_adding_a_model_argument(monkeypatch, tool, mode):
    executor = Mock(return_value='sentinel')
    monkeypatch.setattr(tools.execute_python, 'func', executor)
    runtime = SimpleNamespace(context=SimpleNamespace(), tool_call_id='test')
    assert tool.func(runtime=runtime, code='result = None', used_datasource_ids=[1]) == 'sentinel'
    assert executor.call_args.kwargs['execution_mode'] == mode
    assert 'execution_mode' not in tool.args_schema.model_fields
    assert '无需重复声明' in tool.args_schema.model_fields['code'].description


@pytest.mark.parametrize('clarification_mode', ['same_turn', 'disabled'])
def test_real_agent_graph_keeps_observations_and_repair_history_until_success(monkeypatch, clarification_mode):
    import agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from pydantic import Field

    class ScriptedModel(FakeMessagesListChatModel):
        seen: list = Field(default_factory=list)

        def bind_tools(self, tool_list, **kwargs):
            return self

        def _generate(self, messages, **kwargs):
            self.seen.append(list(messages))
            return super()._generate(messages, **kwargs)

    calls = ['probe_analysis_data', 'probe_analysis_data', 'submit_analysis_program', 'submit_analysis_program']
    responses = [AIMessage(content='', tool_calls=[{
        'id': str(index), 'name': name, 'args': {'code': 'result = None', 'used_datasource_ids': [1]},
    }]) for index, name in enumerate(calls)]
    llm = ScriptedModel(responses=responses)
    monkeypatch.setattr(agent, 'create_data_insight_model', lambda config=None: llm)
    count = 0

    def execute(*, runtime, **kwargs):
        nonlocal count
        count += 1
        message = ToolMessage(content=f'fact-{count}', tool_call_id=runtime.tool_call_id)
        return tools._terminal_tool_success(message) if count == 4 else message

    monkeypatch.setattr(tools.execute_python, 'func', execute)
    graph = agent.create_data_insight_agent(clarification_mode=clarification_mode)
    result = graph.invoke({'messages': [HumanMessage('Analyze the complete scope')]},
                          context=agent.CustomContext(username='test'))
    assert count == len(llm.seen) == 4
    assert [m.content for m in llm.seen[-1] if isinstance(m, ToolMessage)] == ['fact-1', 'fact-2', 'fact-3']
    assert [m.content for m in result['messages'] if isinstance(m, ToolMessage)] == ['fact-1', 'fact-2', 'fact-3', 'fact-4']
    assert all(sum(isinstance(m, HumanMessage) for m in messages) == 1 for messages in llm.seen)


def test_probe_cannot_supply_backend_execution_facts_without_real_queries():
    feedback = tools.request_retry('probe_feedback', 'Observed', diagnostics={
        '_query_execution_outcomes': [{'query_key': 'invented', 'status': 'success'}],
    })
    assert '_query_execution_outcomes' not in feedback.diagnostics


def test_probe_feedback_keeps_real_query_failure_when_model_only_copies_row_count(monkeypatch):
    monkeypatch.setattr(tools, '_load_data_with_fed_query', Mock(side_effect=RuntimeError('query result exceeds 10000 rows')))
    tools.CURRENT_EXECUTION_INTENT.set('probe')
    response = tools.probe_data_with_sql('SELECT amount FROM observations')
    assert response['status'] == 'query_error'
    assert response['row_count'] is None
    feedback = tools.request_retry('probe_feedback', 'Observed', diagnostics={'observations': {'rows': response['row_count']}})
    outcomes = feedback.diagnostics['_query_execution_outcomes']
    assert len(outcomes) == 1
    assert outcomes[0]['status'] == 'failed'
    assert outcomes[0]['error_type'] == 'query_result_too_large'
    assert outcomes[0]['error_message'] == 'query result exceeds 10000 rows'
    assert 'row_count' not in outcomes[0]
    assert outcomes[0]['query_key'] in tools.CURRENT_QUERY_SNAPSHOTS.get()


@pytest.mark.parametrize('inspect', [
    lambda outcome: tools.probe_distinct_values(outcome, 'status'),
    lambda outcome: tools.describe_time_coverage(outcome, 'recorded_at'),
    lambda outcome: tools.probe_text_candidates(outcome, 'status', 'active'),
    lambda outcome: tools._ensure_probe_dataframe(outcome),
    lambda outcome: tools._ensure_probe_dataframe(dict(outcome)),
    lambda outcome: outcome.columns,
    lambda outcome: outcome.empty,
    lambda outcome: outcome.to_dict('records'),
])
@pytest.mark.parametrize('error_type,message', [
    ('query_result_too_large', 'query result exceeds 10000 rows'),
    ('data_source_unavailable', 'upstream query unavailable'),
])
def test_probe_inspection_cannot_turn_query_failure_into_empty_or_missing_column(inspect, error_type, message):
    outcome = tools.ProbeQueryOutcome({
        'status': 'query_error', 'data': None, 'row_count': None,
        'error_type': error_type, 'error_message': message,
    })
    with pytest.raises(tools.ProbeQueryFailure) as caught:
        inspect(outcome)
    assert str(caught.value) == message
    assert tools._classify_execution_error(caught.value, str(caught.value)) == error_type
    assert outcome['status'] == 'query_error'
    assert not outcome.success
    assert not outcome.no_data


def test_probe_no_data_remains_distinct_from_query_failure():
    outcome = tools.ProbeQueryOutcome({
        'status': 'no_data', 'data': None, 'row_count': 0,
        'error_type': 'no_data_found', 'error_message': 'empty path',
    })
    assert outcome.no_data
    assert outcome['row_count'] == 0
    assert outcome.data is None
    assert tools._ensure_probe_dataframe(outcome).empty


def test_failed_probe_envelope_keeps_normal_mapping_copy_protocol():
    from copy import deepcopy

    outcome = tools.ProbeQueryOutcome({
        'status': 'query_error', 'data': None, 'row_count': None,
        'error_type': 'query_result_too_large', 'error_message': 'query result exceeds 10000 rows',
    })
    assert dict(deepcopy(outcome)) == dict(outcome)


def test_probe_execution_outcomes_distinguish_empty_success_and_failed_paths():
    tools.CURRENT_QUERY_SNAPSHOTS.set({
        'q_rows': {'execution': {'status': 'success', 'row_count': 4}},
        'q_empty': {'execution': {'status': 'no_data', 'row_count': 0}},
        'q_error': {'execution': {'status': 'failed', 'error_type': 'schema_or_column_mismatch', 'error_message': 'Unknown field'}},
        'q_not_run': {'execution': {'status': 'running'}},
    })
    feedback = tools.request_retry('probe_feedback', 'Observed', diagnostics={'_query_execution_outcomes': [{'query_key': 'invented'}]})
    outcomes = {item['query_key']: item for item in feedback.diagnostics['_query_execution_outcomes']}
    assert set(outcomes) == {'q_rows', 'q_empty', 'q_error'}
    assert outcomes['q_rows']['row_count'] == 4
    assert outcomes['q_empty']['status'] == 'no_data'
    assert outcomes['q_error']['status'] == 'failed'
    assert 'row_count' not in outcomes['q_error']


def test_source_is_materialized_before_record_validation_and_patch_feedback(monkeypatch):
    recorded = []
    monkeypatch.setattr(tools, '_raise_if_runtime_task_cancelled', lambda *a, **kw: None)
    monkeypatch.setattr(tools, '_get_stream_emitter', lambda: lambda *a, **kw: None)
    monkeypatch.setattr(tools, '_create_execution_record', lambda **kw: recorded.append(kw) or 42)
    monkeypatch.setattr(tools, '_update_execution_record', lambda *a, **kw: None)
    monkeypatch.setattr(tools, '_turn_has_prior_time_range_guard_signal', lambda **kw: False)
    monkeypatch.setattr(tools, '_validate_tool_used_datasource_ids', lambda **kw: None)
    monkeypatch.setattr(tools, '_build_turn_failure_memory', lambda **kw: {})
    runtime = SimpleNamespace(context=SimpleNamespace(conversation_id=1, turn_id=2), tool_call_id='test')
    message = tools.submit_analysis_program.func(runtime=runtime, code='value = 1\n', used_datasource_ids=[1])
    persisted = recorded[0]['code']
    assert persisted == 'execution_intent = "analysis"\nvalue = 1\n'
    assert json.loads(message.content)['error_type'] != 'invalid_execution_intent'
    repaired = tools._apply_source_code_patches(
        persisted, [tools.SourceCodePatch(start_line=2, end_line=2, replacement='value = 2')],
    )
    assert tools._bind_program_execution_mode(repaired, 'analysis') == repaired


def recorded_loader(frame):
    def load(sql, params=None, **kwargs):
        tools._mark_latest_query_snapshot_execution(
            query_key=tools.CURRENT_LAST_QUERY_KEY.get(), status='success',
            columns=list(frame.columns), row_count=len(frame),
        )
        return frame.copy()
    return load


def test_parent_scope_example_returns_detail_rows_without_claiming_aggregation(monkeypatch):
    parent = pd.DataFrame({'batch_id': ['A', 'B']})
    details = pd.DataFrame({'batch_id': ['A', 'A', 'B'], 'event_type': ['x', 'y', 'x']})
    scope = {'time_range': None, 'target_filters': [], 'aggregation': None}
    monkeypatch.setattr(tools, 'load_data_with_sql', recorded_loader(parent))
    parent_data = tools.query_data_with_sql(
        query_key='q_risk_batches', query_constraints=scope, sql='SELECT batch_id FROM parent_records',
    )
    loader = Mock(side_effect=recorded_loader(details))
    monkeypatch.setattr(tools, 'load_data_with_sql', loader)
    namespace = {**tools._build_execution_namespace(), 'parent_data': parent_data}
    exec(example('execution/core.md', 'parent-detail-scope'), namespace)
    assert namespace['child_data'].equals(details)
    assert loader.call_args.kwargs['params'] == ['A', 'B']
    snapshots = tools.CURRENT_QUERY_SNAPSHOTS.get()
    assert snapshots['q_batch_events']['query_constraints']['aggregation'] is None
    assert snapshots['q_batch_events']['analysis_scope'] == snapshots['q_risk_batches']['analysis_scope']


def test_parent_scope_example_does_not_query_an_empty_id_set(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(tools, 'load_data_with_sql', loader)
    namespace = {**tools._build_execution_namespace(), 'parent_data': pd.DataFrame({'batch_id': []})}
    with pytest.raises(tools.NoDataFoundError):
        exec(example('execution/core.md', 'parent-detail-scope'), namespace)
    loader.assert_not_called()


@pytest.mark.parametrize('filename,name', [('query/relational.md', 'relational'), ('query/local_file.md', 'local_file')])
def test_complete_examples_execute_real_validation_helpers_and_build_real_artifacts(monkeypatch, filename, name):
    data = pd.DataFrame({'category': ['类别A', '类别B'], 'metric_value': [30.0, 70.0]})
    monkeypatch.setattr(tools, 'load_data_with_sql', recorded_loader(data))
    monkeypatch.setattr(tools, 'load_local_file', lambda *a, **kw: recorded_loader(data)(''))
    namespace = tools._build_execution_namespace()
    exec(example(filename, name, 'canonical-runtime-example'), namespace)
    result = namespace['result']
    assert result.tables and result.charts
    assert result.query_snapshots
    assert '100.00' in result.analysis_report
    assert '类别B' in result.analysis_report
    table = result.tables[0]
    assert table['columns'][0]['label'] == '业务类别'
    assert table['rows'][0]['category'] == '类别B'


@pytest.mark.parametrize('values,expected,unexpected', [
    ([30.0, 70.0], ['比第二名 类别A高 40.00', '70.0%'], ['集中度较高', '相对分散']),
    ([50.0, 50.0], ['并列最高', '50.0%'], ['比第二名']),
    ([5.0], ['只有一个类别', '100.0%'], ['比第二名']),
    ([0.0, 0.0], ['并列最高', '不将其解释为贡献份额'], ['nan', 'inf', '0.0%']),
    ([-5.0, 10.0], ['高 15.00', '不将其解释为贡献份额'], ['200.0%']),
])
def test_reference_example_computes_comparisons_without_invented_thresholds(monkeypatch, values, expected, unexpected):
    data = pd.DataFrame({'category': ['类别A', '类别B'][:len(values)], 'metric_value': values})
    monkeypatch.setattr(tools, 'load_data_with_sql', recorded_loader(data))
    namespace = tools._build_execution_namespace()
    exec(example('query/relational.md', 'relational', 'canonical-runtime-example'), namespace)
    report = namespace['result'].analysis_report
    assert all(value in report for value in expected)
    assert all(value not in report for value in unexpected)


def test_query_helpers_work_in_functions_and_loops_without_losing_snapshots(monkeypatch):
    data = pd.DataFrame({'value': [1, 2]})
    monkeypatch.setattr(tools, 'load_data_with_sql', recorded_loader(data))
    def load_one(key):
        return tools.query_data_with_sql(
            query_key=key, query_constraints={'time_range': None, 'target_filters': [], 'aggregation': None},
            sql='SELECT value FROM measurements',
        )
    results = [load_one(key) for key in ('q_one', 'q_two')]
    assert sum(len(frame) for frame in results) == 4
    assert set(tools.CURRENT_QUERY_SNAPSHOTS.get()) == {'q_one', 'q_two'}
    assert all(s['execution']['row_count'] == 2 for s in tools.CURRENT_QUERY_SNAPSHOTS.get().values())


def test_function_wrapped_query_remains_valid_in_a_complete_analysis_program(monkeypatch):
    tree = ast.parse(example('query/relational.md', 'relational', 'canonical-runtime-example'))
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'query_data_with_sql':
            function = ast.parse('def read_summary():\n    return None').body[0]
            function.body[0].value = node.value
            node.value = ast.Call(func=ast.Name(id='read_summary', ctx=ast.Load()), args=[], keywords=[])
            tree.body.insert(index, function)
            break
    source = ast.unparse(ast.fix_missing_locations(tree))
    assert tools._validate_generated_code_contract(source) is None
    monkeypatch.setattr(tools, 'load_data_with_sql', recorded_loader(pd.DataFrame({
        'category': ['A', 'B'], 'metric_value': [30.0, 70.0],
    })))
    namespace = tools._build_execution_namespace()
    exec(source, namespace)
    assert namespace['result'].query_snapshots['q_category_metric_summary']['execution']['row_count'] == 2
    assert '100.00' in namespace['result'].analysis_report


def test_low_memory_file_query_keeps_all_rows_and_records_actual_file_scope(tmp_path):
    path = tmp_path / 'measurements.csv'
    pd.DataFrame({'amount': [1, 2, 3, 4]}).to_csv(path, index=False)
    stream = tools.query_local_file_low_memory(
        query_key='q_file', query_constraints={'time_range': None, 'target_filters': [], 'aggregation': None},
        file_path=str(path), chunk_size=2,
    )
    total = sum(float(chunk['amount'].sum()) for chunk in stream)
    assert total == 10
    snapshot = tools.CURRENT_QUERY_SNAPSHOTS.get()['q_file']
    assert snapshot['execution']['row_count'] == 4


def test_invalid_query_contract_never_reaches_the_loader(monkeypatch):
    loader = Mock()
    monkeypatch.setattr(tools, 'load_data_with_sql', loader)
    with pytest.raises(tools.QueryConstraintRetrySignal):
        tools.query_data_with_sql(query_key='q_invalid', query_constraints={'target_filters': [{}]}, sql='SELECT 1')
    loader.assert_not_called()


@pytest.mark.parametrize('output_type', ['text', 'json'])
def test_nonstandard_output_still_uses_the_actual_save_api_without_artifacts(output_type):
    contract = {'type': output_type}
    if output_type == 'json':
        contract['schema'] = {'type': 'object', 'properties': {'count': {'type': 'integer'}}, 'required': ['count']}
    tools.CURRENT_OUTPUT_CONTRACT.set(contract)
    kwargs = {'output_text': 'There are 2 records.'} if output_type == 'text' else {'output_json': {'count': 2}}
    result = tools.save_analysis_result(**kwargs)
    assert result.output_format == output_type
    assert not result.charts and not result.tables
    assert 'build_chart_result(*,' not in load_runtime_prompt(('sql',), output_format=output_type)
