import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent.tools as tools


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
            'validate_query_constraints',
            lambda **kwargs: tools._register_query_snapshot(**kwargs),
        )
        monkeypatch.setattr(tools, 'load_local_file', lambda **kwargs: data)
        actual = tools.query_local_file(
            query_key='q_march',
            query_constraints={'time_range': None, 'target_filters': [], 'aggregation': None},
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
