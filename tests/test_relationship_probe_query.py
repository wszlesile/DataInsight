import sqlite3

import pandas as pd
import pytest

from agent.tools import _inspect_sql_preparation_relationship


@pytest.mark.parametrize('left,right,fields,expected', [
    ([('A',), ('A',), ('B',), (None,)], [('A',), ('C',), ('C',), (None,)], ['key'],
     (2, 2, 1, 2, 2, .5, .5)),
    ([('A', 1), ('A', 1), ('B', 2), ('C', None), (None, 3)],
     [('A', 1), ('B', 3), ('B', 3), ('C', None)], ['key', 'version'],
     (2, 2, 1, 2, 2, .5, .5)),
    ([], [('A',)], ['key'], (0, 1, 0, 0, 1, 0, 0)),
    ([('A',), ('B',)], [('A',), ('B',), ('B',), ('B',), ('C',)], ['key'],
     (2, 3, 2, 1, 3, 1, 2/3)),
])
def test_relationship_summary_preserves_duplicates_nulls_and_unmatched_population(
        monkeypatch, left, right, fields, expected):
    connection = sqlite3.connect(':memory:')
    pd.DataFrame(left, columns=fields).to_sql('left_source', connection, index=False)
    pd.DataFrame(right, columns=fields).to_sql('right_source', connection, index=False)
    queries = []
    def read(sql, **kwargs):
        queries.append(sql)
        return pd.read_sql_query(sql, connection)
    monkeypatch.setattr('agent.tools.load_data_with_sql', read)
    result = _inspect_sql_preparation_relationship(
        relationship={'relationship_id': 'real_keys'},
        left_source={'datasource_identifier': 'left_source'},
        right_source={'datasource_identifier': 'right_source'},
        left_fields=fields, right_fields=fields)
    facts = result['facts']
    names = ('left_distinct_key_count', 'right_distinct_key_count', 'matched_distinct_key_count',
             'left_max_rows_per_key', 'right_max_rows_per_key',
             'left_match_coverage_ratio', 'right_match_coverage_ratio')
    for name, value in zip(names, expected):
        assert facts[name] == pytest.approx(value)
    assert facts['left_measure_aggregation_safe'] == (expected[4] <= 1)
    assert facts['right_measure_aggregation_safe'] == (expected[3] <= 1)
    assert result['complete'] is True
    assert len(queries) == 1
    connection.close()
