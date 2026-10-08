from decimal import Decimal

import pandas as pd
import pytest

from analysis_runtime.calculations import (
    calculate_ratio,
    compare_numeric_values,
    evaluate_numeric_comparison,
    select_extreme_rows,
    summarize_extrema,
)


@pytest.mark.parametrize('values,mode,expected', [
    ([3, 1, 2], 'max', [0]),
    ([3, 3, 1], 'max', [0, 1]),
    ([1, 1, 1], 'max', [0, 1, 2]),
    ([3, 1, 1], 'min', [1, 2]),
    ([-3, -1, -1], 'max', [1, 2]),
    ([None, 0, 0], 'max', [1, 2]),
    ([None, float('nan')], 'max', []),
    ([], 'min', []),
])
def test_extrema_returns_all_ties_without_mutating_input(values, mode, expected):
    frame = pd.DataFrame({'name': [str(i) for i in range(len(values))], 'value': values})
    original = frame.copy(deep=True)
    result = select_extreme_rows(frame, 'value', mode=mode)
    assert result.index.tolist() == expected
    assert result.columns.tolist() == frame.columns.tolist()
    pd.testing.assert_frame_equal(frame, original)
    if not result.empty:
        result.iloc[0, 0] = 'changed'
        pd.testing.assert_frame_equal(frame, original)


def test_extrema_handles_nullable_numbers_duplicate_indices_and_decimal():
    frame = pd.DataFrame({'value': pd.array([1, None, 1], dtype='Int64')}, index=[0, 0, 0])
    assert len(select_extreme_rows(frame, 'value')) == 2
    frame = pd.DataFrame({'value': [Decimal('1.2'), Decimal('1.2'), Decimal('1.1')]})
    assert len(select_extreme_rows(frame, 'value')) == 2


@pytest.mark.parametrize('left,right,expected', [
    (0, 0, 'equal'), (1, 2, 'less'), (2, 1, 'greater'),
    (-1, -2, 'greater'), (None, 1, None), (pd.NA, 1, None),
    (float('nan'), 1, None), (Decimal('1.0'), 1, 'equal'),
])
def test_comparison_distinguishes_equality_and_missing(left, right, expected):
    assert compare_numeric_values(left, right) == expected


@pytest.mark.parametrize('numerator,denominator,expected', [
    (0, 2, 0), (2, 4, 0.5), (2, 0, None), (0, 0, None),
    (None, 2, None), (2, float('nan'), None), (-2, 4, -0.5),
])
def test_ratio_preserves_zero_and_undefined(numerator, denominator, expected):
    assert calculate_ratio(numerator, denominator) == expected


def test_ratio_scale_precision_and_exact_comparison_are_explicit():
    assert calculate_ratio(1, 3, scale=100) == 1 / 3 * 100
    assert calculate_ratio(Decimal('1'), Decimal('4'), scale=100) == Decimal('25')
    assert compare_numeric_values(0.1 + 0.2, 0.3) == 'greater'
    assert compare_numeric_values(round(0.1 + 0.2, 1), 0.3) == 'equal'


@pytest.mark.parametrize('left,right,relation,difference,change', [
    (70, 70, 'equal', 0, 0), (80, 70, 'greater', 10, 10 / 70),
    (60, 70, 'less', -10, -10 / 70), (0, 0, 'equal', 0, None),
    (2, 0, 'greater', 2, None), (None, 1, None, None, None),
    (1, pd.NA, None, None, None),
    (Decimal('0.3'), Decimal('0.2'), 'greater', Decimal('0.1'), Decimal('0.5')),
])
def test_comparison_bundle_has_consistent_predicates_and_calculation_boundaries(left, right, relation, difference, change):
    result = evaluate_numeric_comparison(left, right)
    assert result['relation'] == relation
    assert result['difference'] == difference
    assert result['relative_change'] == change
    for key in ('less', 'equal', 'greater'):
        assert result[key] is (None if relation is None else relation == key)


@pytest.mark.parametrize('values,kind,valid,matched', [
    ([], 'empty', 0, 0), ([None, float('nan')], 'empty', 0, 0),
    ([None, 0], 'single', 1, 1), ([1, 1, 1], 'all_equal', 3, 3),
    ([None, 2, 2], 'all_equal', 2, 2), ([3, 3, 1], 'tied', 3, 2),
    ([3, 2, 1], 'unique', 3, 1),
])
def test_extrema_bundle_distinguishes_all_result_shapes(values, kind, valid, matched):
    frame = pd.DataFrame({'name': list(range(len(values))), 'value': values})
    original = frame.copy(deep=True)
    result = summarize_extrema(frame, 'value')
    assert result['kind'] == kind
    assert result['valid_count'] == valid
    assert result['missing_count'] == len(values) - valid
    assert result['matched_count'] == matched
    assert len(result['rows']) == matched
    assert result['extreme_value'] == (max(v for v in values if v is not None) if matched else None)
    pd.testing.assert_frame_equal(frame, original)
    assert 'risk' not in result and 'concentration' not in result


@pytest.mark.parametrize('value,error', [('1', TypeError), (True, TypeError),
                                         (float('inf'), ValueError), (complex(1, 2), TypeError)])
def test_helpers_reject_invalid_values_instead_of_silently_reinterpreting(value, error):
    with pytest.raises(error):
        compare_numeric_values(value, 1)
    with pytest.raises(error):
        calculate_ratio(value, 1)
    with pytest.raises(error):
        select_extreme_rows(pd.DataFrame({'value': [value]}), 'value')


def test_invalid_helper_options_do_not_fabricate_results():
    with pytest.raises(ValueError):
        select_extreme_rows(pd.DataFrame({'value': [1]}), 'value', mode='first')
    with pytest.raises(KeyError):
        select_extreme_rows(pd.DataFrame({'value': [1]}), 'missing')
    with pytest.raises(ValueError):
        calculate_ratio(1, 2, scale=None)


def test_execution_namespace_and_generated_reference_use_same_functions():
    from agent.tools import _build_execution_namespace
    from agent.runtime_contract_registry import load_runtime_work_contract_text
    from analysis_runtime.expressions import render_numeric_comparison, render_extrema

    namespace = _build_execution_namespace()
    planning = load_runtime_work_contract_text(('execution.semantic_planning',), work='understand')
    implementing = load_runtime_work_contract_text(('execution.implementation',), work='implement')
    numerical = load_runtime_work_contract_text(('execution.numeric_interpretation',), work='implement')
    for helper in (select_extreme_rows, compare_numeric_values, calculate_ratio,
                   evaluate_numeric_comparison, summarize_extrema,
                   render_numeric_comparison, render_extrema):
        assert namespace[helper.__name__] is helper
        assert helper.__name__ not in planning
        assert helper.__name__ in implementing + numerical
    assert 'render_extrema(' in implementing
    for helper in (evaluate_numeric_comparison, summarize_extrema,
                   render_numeric_comparison):
        assert helper.__name__ not in implementing
        assert helper.__name__ in numerical
    assert 'query_local_file(' not in planning
    assert 'save_analysis_result(' not in planning
    assert 'build_chart_result(' not in planning


def test_numerical_api_definitions_are_deduplicated_across_loaded_contracts():
    from agent.runtime_contract_registry import load_runtime_contract_text, runtime_helper_signature

    contract = load_runtime_contract_text(('execution.semantic_planning', 'execution.implementation'))
    for name in ('select_extreme_rows', 'compare_numeric_values', 'calculate_ratio'):
        assert contract.count(f'`{runtime_helper_signature(name)}`') == 1


