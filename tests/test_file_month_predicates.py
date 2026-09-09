import pytest

from agent.tools import _month_grain_filter_satisfies_time_range


@pytest.mark.parametrize('expression', [
    "data['month'].eq('2026-03')",
    "data['month'].isin(['2026-03'])",
    "(data['month'].eq('2026-03')) & (data['region'].eq('A'))",
])
def test_documented_month_expressions_match_full_month(expression):
    assert _month_grain_filter_satisfies_time_range(
        '2026-03-01', '2026-04-01', {'end_exclusive': True}, expression)


@pytest.mark.parametrize('expression', [
    "data['month'].eq('2026-02')",
    "data['month'].ne('2026-03')",
    "data['month'].isin(['2026-03', '2026-04'])",
    "data['month'].eq('2026-03') | data['region'].eq('A')",
    "~data['month'].eq('2026-03')",
    '"data[\'month\'].eq(\'2026-03\')"',
])
def test_non_equivalent_month_expressions_are_not_accepted(expression):
    assert not _month_grain_filter_satisfies_time_range(
        '2026-03-01', '2026-04-01', {'end_exclusive': True}, expression)


@pytest.mark.parametrize('start,end,exclusive', [
    ('2026-03-02', '2026-04-01', True),
    ('2026-03-01', '2026-05-01', True),
    ('2026-03-01', '2026-04-01', False),
])
def test_month_equality_does_not_prove_other_intervals(start, end, exclusive):
    assert not _month_grain_filter_satisfies_time_range(
        start, end, {'end_exclusive': exclusive}, "data['month'].eq('2026-03')")
