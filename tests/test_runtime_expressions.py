from decimal import Decimal

import pandas as pd
import pytest

from analysis_runtime.expressions import (
    render_extrema, render_numeric_comparison,
)


COMPARISONS = dict(
    less='{left_label} {left:g} < {right_label} {right:g}',
    equal='{left_label} {left:g} = {right_label} {right:g}',
    greater='{left_label} {left:g} > {right_label} {right:g}',
    missing='Comparison unavailable',
)
EXTREMA = dict(
    empty='No comparable values',
    single='Only {labels}: {value:g}; no between-object ranking',
    unique='{labels} is the unique extreme: {value:g}',
    tied='{labels} are tied: {value:g}',
    all_equal='All {valid_count} objects ({labels}) have the same value: {value:g}',
)


@pytest.mark.parametrize('left,right,expected', [
    (70, 70, 'A 70 = B 70'), (80, 70, 'A 80 > B 70'),
    (60, 70, 'A 60 < B 70'), (0, 0, 'A 0 = B 0'),
    (None, 1, 'Comparison unavailable'), (pd.NA, 1, 'Comparison unavailable'),
    (Decimal('0.3'), Decimal('0.2'), 'A 0.3 > B 0.2'),
])
def test_comparison_selects_exactly_the_actual_case(left, right, expected):
    assert render_numeric_comparison(left, right, left_label='A', right_label='B', **COMPARISONS) == expected


def test_difference_and_relative_change_keep_separate_units():
    cases = {**COMPARISONS, 'greater': 'Difference {difference:g}; change {relative_change:.1%}'}
    assert render_numeric_comparison(80, 70, **cases) == 'Difference 10; change 14.3%'
    cases['greater'] = 'Difference {difference:g}; no relative baseline'
    assert render_numeric_comparison(2, 0, **cases) == 'Difference 2; no relative baseline'
    cases['greater'] = '{relative_change:.1%}'
    assert render_numeric_comparison(2, 0, **cases) == 'N/A'
    assert render_numeric_comparison(2, 0, unavailable='undefined', **cases) == 'undefined'


@pytest.mark.parametrize('values,expected', [
    ([], 'No comparable values'), ([None, None], 'No comparable values'),
    ([None, 0], 'Only B: 0; no between-object ranking'),
    ([3, 2, 1], 'A is the unique extreme: 3'),
    ([3, 3, 1], 'A, B are tied: 3'),
    ([1, 1, 1], 'All 3 objects (A, B, C) have the same value: 1'),
    ([None, 2, 2], 'All 2 objects (B, C) have the same value: 2'),
])
def test_extrema_retains_all_objects_and_renders_every_state(values, expected):
    data = pd.DataFrame({'name': list('ABC')[:len(values)], 'value': values})
    original = data.copy(deep=True)
    assert render_extrema(data, 'value', 'name', **EXTREMA) == expected
    pd.testing.assert_frame_equal(data, original)


def test_extrema_minimum_and_duplicates_are_not_silently_collapsed():
    data = pd.DataFrame({'name': ['A', 'A', 'B'], 'value': [1, 1, 2]})
    assert render_extrema(data, 'value', 'name', mode='min', **EXTREMA) == 'A, A are tied: 1'
    assert render_extrema(data, 'value', 'name', mode='min', separator=' / ', **EXTREMA) == 'A / A are tied: 1'


@pytest.mark.parametrize('values,expected', [
    ([], '没有可比较的数量记录。'),
    ([1], '仅有 A 一个可比较项，数量为 1个，无法作项间比较。'),
    ([3, 2, 1], '在 3 个可比较项中，A的数量最高，为 3个。'),
    ([3, 3, 1], '在 3 个可比较项中，A、B的数量并列最高，各为 3个。'),
    ([1, 1, 1], '3 个可比较项的数量均为 1个：A、B、C。'),
    ([0, 0, 0], '3 个可比较项的数量均为 0个：A、B、C。'),
    ([None, 2, 2], '2 个可比较项的数量均为 2个：B、C。 另有 1 条记录的数量不可比较。'),
    ([None, None], '没有可比较的数量记录。 另有 2 条记录的数量不可比较。'),
])
def test_default_extrema_fact_covers_result_states_without_caller_branches(values, expected):
    frame = pd.DataFrame({'name': list('ABC')[:len(values)], 'value': values})
    original = frame.copy(deep=True)
    assert render_extrema(frame, 'value', 'name', value_label='数量', unit='个', separator='、') == expected
    pd.testing.assert_frame_equal(frame, original)


def test_default_extrema_minimum_and_english_keep_the_full_comparison_population():
    frame = pd.DataFrame({'name': ['A', 'B', 'C'], 'value': [1, 1, 2]})
    assert render_extrema(frame, 'value', 'name', mode='min', language='en',
                         value_label='count') == \
        'Among 3 comparable items, A, B are tied for the lowest count: 1 each.'
    with pytest.raises(ValueError, match='language'):
        render_extrema(frame, 'value', 'name', language='invalid')


def test_custom_extrema_expression_overrides_only_its_own_result_state():
    frame = pd.DataFrame({'name': ['A', 'B'], 'value': [2, 2]})
    assert render_extrema(frame, 'value', 'name', unique='Custom {labels}', language='en') == \
        'All 2 comparable items have value 2: A, B.'
    assert render_extrema(frame, 'value', 'name', all_equal='Custom {labels}', language='en') == 'Custom A, B'


@pytest.mark.parametrize('template', ['', ' ', '{unknown}', '{left.real}', '{left[0]}', '{left:{right}}'])
def test_expression_protocol_rejects_incomplete_or_unsupported_fields(template):
    with pytest.raises(ValueError):
        render_numeric_comparison(1, 1, **{**COMPARISONS, 'greater': template})


def test_all_cases_are_required_and_business_text_is_not_semantically_certified():
    with pytest.raises(TypeError):
        render_numeric_comparison(1, 2, less='lower', equal='same', greater='higher')
    # Deliberately demonstrates the limit: the API chooses the right branch,
    # but cannot turn caller-invented business text into a verified assessment.
    assert render_numeric_comparison(2, 1, **{**COMPARISONS, 'greater': 'healthy'}) == 'healthy'


def test_missing_values_survive_conversion_and_only_selected_case_is_formatted():
    assert render_numeric_comparison(None, 1, **{
        **COMPARISONS, 'missing': 'Missing {left!s:.1f}',
    }) == 'Missing N/A'
    assert render_numeric_comparison(2, 0, **{
        **COMPARISONS, 'equal': 'Never rendered {relative_change:.1%}',
    }) == ' 2 >  0'
    assert render_numeric_comparison(70, 70, **{**COMPARISONS, 'equal': '{{equal}} {left:g}'}) == '{equal} 70'


