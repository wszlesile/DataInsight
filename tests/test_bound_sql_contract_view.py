from datetime import date

import pytest

import agent.tools as rt


@pytest.mark.parametrize('sql,params', [
    ('SELECT * FROM records WHERE period = %s', ['2026-03']),
    ('SELECT * FROM records WHERE period = %(period)s', {'period': '2026-03'}),
    ('SELECT * FROM records WHERE period >= %s AND period < %s', ['2026-03', '2026-04']),
])
def test_bound_period_predicate_is_visible_to_existing_time_scope_interface(sql, params):
    rt._prime_worker_runtime_context({})
    try:
        result = rt.validate_query_constraints(query_key='period_records',
            query_constraints={'time_range': {'start': '2026-03-01', 'end': '2026-04-01',
                                             'end_exclusive': True},
                               'target_filters': [], 'aggregation': None},
            sql=sql, query_params=params)
        assert result is None
    finally:
        rt._prime_worker_runtime_context({})


def test_binding_view_keeps_literals_comments_order_and_escaped_values():
    sql = "SELECT '%s' AS label, %s FROM records WHERE a=%s AND b=%s -- %s\n/* %s */"
    params = ['second', "O'Reilly", date(2026, 3, 1)]
    view = rt._bound_sql_contract_view(sql, params)
    assert view == ("SELECT '%s' AS label, 'second' FROM records WHERE a='O''Reilly' "
                    "AND b=CAST('2026-03-01' AS DATE) -- %s\n/* %s */")
    assert sql.startswith("SELECT '%s' AS label, %s")
    assert params == ['second', "O'Reilly", date(2026, 3, 1)]


def test_named_bindings_keep_repeated_identity_and_literals():
    sql = "SELECT %(value)s FROM records WHERE a=%(value)s AND label='%(value)s'"
    assert rt._bound_sql_contract_view(sql, {'value': 3}) == (
        "SELECT 3 FROM records WHERE a=3 AND label='%(value)s'")


@pytest.mark.parametrize('sql,params', [
    ('SELECT * FROM records WHERE a=%s', []),
    ('SELECT * FROM records WHERE a=%s', [1, 2]),
    ('SELECT * FROM records WHERE a=%(missing)s', {'other': 1}),
    ('SELECT * FROM records WHERE a=%s', {'value': 1}),
])
def test_incomplete_binding_preserves_original_for_physical_driver(sql, params):
    assert rt._bound_sql_contract_view(sql, params) == sql
