import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent.tools as tools


@pytest.mark.parametrize('helper', ['query_local_file', 'query_local_file_low_memory'])
def test_selected_workbook_sheet_and_query_owner_survive_real_loading(tmp_path, helper):
    path = tmp_path / 'shared.xlsx'
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({'value': [1]}).to_excel(writer, sheet_name='first', index=False)
        pd.DataFrame({'value': [7]}).to_excel(writer, sheet_name='second', index=False)
    source_token = tools.CURRENT_LOCAL_FILE_DATASOURCES.set({'2': {
        'datasource_type': 'local_file', 'datasource_identifier': str(path), 'sheet_name': 'second'}})
    snapshots_token = tools.CURRENT_QUERY_SNAPSHOTS.set({})
    last_token = tools.CURRENT_LAST_QUERY_KEY.set('')
    try:
        from analysis_runtime.executable_program import ExecutablePythonProgram
        program = ExecutablePythonProgram()
        result = getattr(tools, helper)(datasource_id=2, query_key='selected', query_constraints={
            'time_range': None, 'target_filters': [], 'aggregation': None})
        frame = result if helper == 'query_local_file' else pd.concat(list(result))
        assert frame['value'].tolist() == [7]
        implementation = tools.CURRENT_QUERY_SNAPSHOTS.get()['selected']['implementation']
        assert implementation['datasource_id'] == 2
        from analysis_runtime.provenance import query_receipt, snapshot_digest
        if helper == 'query_local_file':
            receipt = query_receipt(frame)
            assert receipt['snapshot_digest'] == snapshot_digest(tools.CURRENT_QUERY_SNAPSHOTS.get()[receipt['query_key']])
    finally:
        tools.CURRENT_LOCAL_FILE_DATASOURCES.reset(source_token)
        tools.CURRENT_QUERY_SNAPSHOTS.reset(snapshots_token)
        tools.CURRENT_LAST_QUERY_KEY.reset(last_token)


@pytest.mark.parametrize('helper_name,loader_name', [
    ('query_local_file', 'load_local_file'),
    ('query_local_file_low_memory', 'load_local_file_low_memory'),
])
def test_selected_datasource_id_resolves_local_file_path_for_query_helpers(
    monkeypatch, helper_name, loader_name,
):
    data = pd.DataFrame({'value': [1]})
    loaded = {}
    monkeypatch.setattr(
        tools,
        loader_name,
        lambda **kwargs: loaded.update(kwargs) or (data if helper_name == 'query_local_file' else iter([data])),
    )
    monkeypatch.setattr(tools, 'validate_query_constraints', lambda **kwargs: None)
    snapshot = {
        'selected_datasource_snapshot': [{
            'datasource_id': 180,
            'datasource_type': 'local_file',
            'datasource_identifier': 'C:/selected/source.xlsx',
        }],
    }
    context = SimpleNamespace(execution_datasource_snapshot=snapshot)

    payload = tools._serialize_runtime_user_context(context)
    token = tools.CURRENT_LOCAL_FILE_DATASOURCES.set({})
    try:
        tools._prime_worker_runtime_context(payload)
        result = getattr(tools, helper_name)(
            query_key='q_selected_source',
            query_constraints={},
            datasource_id=180,
        )
    finally:
        tools.CURRENT_LOCAL_FILE_DATASOURCES.reset(token)

    assert loaded['file_path'] == 'C:/selected/source.xlsx'
    if helper_name == 'query_local_file':
        assert result.to_dict('records') == [{'value': 1}]
    else:
        assert list(result)[0].to_dict('records') == [{'value': 1}]


def test_datasource_id_path_resolution_rejects_unselected_sources(monkeypatch):
    monkeypatch.setattr(
        tools,
        'load_local_file',
        lambda **kwargs: pytest.fail('An unselected datasource must not be loaded'),
    )
    token = tools.CURRENT_LOCAL_FILE_DATASOURCES.set({})
    try:
        with pytest.raises(ValueError, match='not an available local-file datasource'):
            tools.query_local_file(
                query_key='q_unselected', query_constraints={}, datasource_id=999,
            )
    finally:
        tools.CURRENT_LOCAL_FILE_DATASOURCES.reset(token)


@pytest.mark.parametrize('helper', ['query_local_file', 'query_local_file_low_memory'])
def test_file_constraint_feedback_names_the_actual_api_and_retains_diagnostics(monkeypatch, helper):
    failure = tools.RetryResult(
        retry_type='query_constraint_violation', message='Filter implementation missing',
        diagnostics={'query_constraints': {'target_filters': [{'value': 'A'}]}},
        repair_instructions=['generic SQL instruction'],
    )
    monkeypatch.setattr(tools, 'validate_query_constraints', lambda **kwargs: failure)
    with pytest.raises(tools.QueryConstraintRetrySignal) as raised:
        getattr(tools, helper)(query_key='q', query_constraints={}, file_path='unused')
    feedback = raised.value.retry_result
    assert feedback.diagnostics['query_helper'] == helper
    assert feedback.diagnostics['query_constraints'] == failure.diagnostics['query_constraints']
    assert 'file_filters' in feedback.repair_instructions[0]
    assert 'generic SQL instruction' not in feedback.repair_instructions
    assert feedback.diagnostics['validator_repair_instructions'] == ['generic SQL instruction']
    assert failure.repair_instructions == ['generic SQL instruction']
    assert 'query_helper' not in failure.diagnostics


@pytest.mark.parametrize('loader', ['query_local_file', 'query_local_file_low_memory'])
@pytest.mark.parametrize('filters', [[{'field': 'month', 'op': 'eq', 'value': '2026-03'}], 'month == 3', ['']])
def test_invalid_filter_protocol_fails_before_validation_or_loading(monkeypatch, loader, filters):
    def unexpected(**kwargs):
        pytest.fail('Invalid filter protocol must not reach validation or loading')
    monkeypatch.setattr(tools, 'validate_query_constraints', unexpected)
    with pytest.raises(ValueError, match='does not execute'):
        getattr(tools, loader)(query_key='q', query_constraints={}, file_path='unused',
                               filter_expressions=filters)


def test_expression_protocol_preserves_existing_full_file_behavior(monkeypatch):
    data = pd.DataFrame({'category': ['A', 'B'], 'value': [10, 20]})
    seen = {}
    def validate(**kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(tools, 'validate_query_constraints', validate)
    monkeypatch.setattr(tools, 'load_local_file', lambda **kwargs: data)
    expressions = ["data['category'].eq('A')"]
    actual = tools.query_local_file(query_key='q', query_constraints={}, file_path='unused',
                                   filter_expressions=expressions)
    assert actual is data
    assert len(actual) == 2
    assert seen['filter_expressions'] == expressions


def test_structured_file_filters_are_executed_and_registered(monkeypatch):
    data = pd.DataFrame({'month': ['2026-02', '2026-03', '2026-03'], 'amount': [10, 20, 30]})
    snapshot_token = tools.CURRENT_QUERY_SNAPSHOTS.set({})
    try:
        monkeypatch.setattr(
            tools,
            'load_local_file',
            lambda **kwargs: data,
        )
        actual = tools.query_local_file(
            query_key='q_march',
            query_constraints={
                'time_range': {
                    'start': '2026-03', 'end': '2026-04', 'end_exclusive': True,
                },
                'target_filters': [],
                'aggregation': None,
            },
            file_path='unused',
            file_filters=[{'field': 'month', 'op': 'eq', 'value': '2026-03'}],
        )
        snapshot = tools.CURRENT_QUERY_SNAPSHOTS.get()['q_march']
    finally:
        tools.CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)

    assert actual.to_dict('records') == [
        {'month': '2026-03', 'amount': 20},
        {'month': '2026-03', 'amount': 30},
    ]
    assert snapshot['implementation']['file_filters'] == [
        {'field': 'month', 'op': 'eq', 'value': '2026-03'},
    ]
    assert snapshot['execution']['row_count'] == 2
    assert snapshot['execution']['file_filters_applied'] == [
        {'field': 'month', 'op': 'eq', 'value': '2026-03'},
    ]


def test_month_string_filter_matches_month_form_time_range_without_retry():
    query_constraints = {
        'time_range': {'start': '2026-03', 'end': '2026-04', 'end_exclusive': True},
        'target_filters': [],
        'aggregation': None,
    }
    filter_expression = "data['年月'] == '2026-03'"

    assert tools._check_time_range_constraint(query_constraints, filter_expression) is None
    result = tools.validate_query_constraints(
        query_constraints=query_constraints,
        query_key='q_march',
        filter_expressions=[filter_expression],
        file_filters=[{'field': '年月', 'op': 'eq', 'value': '2026-03'}],
        aggregation_hints=['detail'],
    )
    assert result is None


def test_month_string_filter_does_not_cover_a_multi_month_range():
    query_constraints = {
        'time_range': {'start': '2026-03', 'end': '2026-05', 'end_exclusive': True},
    }
    filter_expression = "data['年月'] == '2026-03'"

    issue = tools._check_time_range_constraint(query_constraints, filter_expression)

    assert issue is not None
    assert issue['diagnostics']['missing_parts'] == ['end']


def test_structured_file_filters_apply_to_each_low_memory_chunk(monkeypatch):
    chunks = iter([
        pd.DataFrame({'month': ['2026-02', '2026-03'], 'amount': [10, 20]}),
        pd.DataFrame({'month': ['2026-03', '2026-04'], 'amount': [30, 40]}),
    ])
    snapshot_token = tools.CURRENT_QUERY_SNAPSHOTS.set({})
    try:
        monkeypatch.setattr(
            tools,
            'validate_query_constraints',
            lambda **kwargs: tools._register_query_snapshot(**kwargs),
        )
        monkeypatch.setattr(tools, 'load_local_file_low_memory', lambda **kwargs: chunks)
        actual = list(tools.query_local_file_low_memory(
            query_key='q_march_stream',
            query_constraints={'time_range': None, 'target_filters': [], 'aggregation': None},
            file_path='unused',
            file_filters=[{'field': 'month', 'op': 'eq', 'value': '2026-03'}],
        ))
        snapshot = tools.CURRENT_QUERY_SNAPSHOTS.get()['q_march_stream']
    finally:
        tools.CURRENT_QUERY_SNAPSHOTS.reset(snapshot_token)

    assert [chunk.to_dict('records') for chunk in actual] == [
        [{'month': '2026-03', 'amount': 20}],
        [{'month': '2026-03', 'amount': 30}],
    ]
    assert snapshot['implementation']['file_filters'] == [
        {'field': 'month', 'op': 'eq', 'value': '2026-03'},
    ]
    assert snapshot['execution']['row_count'] == 2
