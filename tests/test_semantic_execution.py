from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from analysis_runtime.semantic_execution import DistinctCount, PythonCalculation, RowProductSum, SemanticExecution
from analysis_runtime.calculations import summarize_extrema


def session(frame, *, grain=()):
    execution = SemanticExecution('test-execution')
    execution.add_source('source', frame, query_key='physical-query', grain=grain)
    return execution


@pytest.mark.parametrize('labels,kind,state,matched', [
    ([], 'empty', 'unknown', []),
    (['a'], 'single', 'not_applicable', ['a']),
    (['a', 'b'], 'all_equal', 'false', ['a', 'b']),
    (['a', 'a', 'b', 'b', 'c'], 'tied', 'false', ['a', 'b']),
    (['a', 'a', 'b'], 'unique', 'true', ['a']),
])
def test_saved_counts_drive_complete_extrema_and_display_without_recalculation(labels, kind, state, matched):
    execution = session(pd.DataFrame({'id': range(len(labels)), 'category': labels}))
    definition = DistinctCount('counts', 'source', 'id', ('category',), unit='台')
    with patch('analysis_runtime.semantic_execution.summarize_extrema', wraps=summarize_extrema) as evaluate:
        fact = execution.execute(definition)
        decision = execution.evaluate_extrema('ranking', fact)
        table = execution.table(fact)
        table['value'] = 999
        execution.execute(definition)
        execution.evaluate_extrema('ranking', fact)
        text = execution.render_extrema(decision, 'category')
        assert evaluate.call_count == 1
    assert execution.decision_state(decision)['kind'] == kind
    assert execution.decision_state(decision)['state'] == state
    assert execution.matches(decision)['category'].tolist() == matched
    assert '999' not in text
    assert execution.trace()['facts']['counts']['query_keys'] == ('physical-query',)


def test_source_table_and_matched_rows_are_detached_from_the_execution_snapshot():
    original = pd.DataFrame({'id': [1, 2], 'category': ['a', 'b']})
    execution = session(original)
    original.loc[0, 'category'] = 'changed'
    fact = execution.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    decision = execution.evaluate_extrema('ranking', fact)
    rows = execution.matches(decision)
    rows.loc[0, 'category'] = 'changed'
    assert execution.table(fact)['category'].tolist() == ['a', 'b']
    assert execution.matches(decision)['category'].tolist() == ['a', 'b']
    trace = execution.trace()
    trace['facts']['counts']['state'] = 'fabricated'
    assert execution.fact_state(fact)['state'] == 'valid'


def test_receipts_cannot_reuse_previous_execution_even_with_same_execution_label():
    first = session(pd.DataFrame({'id': [1], 'category': ['a']}))
    second = session(pd.DataFrame({'id': [2], 'category': ['b']}))
    fact = first.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    decision = first.evaluate_extrema('rank', fact)
    with pytest.raises(ValueError, match='another execution'):
        second.table(fact)
    with pytest.raises(ValueError, match='another execution'):
        second.matches(decision)


def test_formula_and_condition_cannot_overwrite_an_existing_producer():
    execution = session(pd.DataFrame({'id': [1, 2], 'category': ['a', 'b']}))
    fact = execution.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    with pytest.raises(ValueError, match='different producer'):
        execution.execute(DistinctCount('counts', 'source', 'id'))
    execution.evaluate_extrema('rank', fact)
    with pytest.raises(ValueError, match='another producer'):
        execution.evaluate_extrema('rank', fact, mode='min')


def test_variable_prices_are_multiplied_at_checked_row_grain_before_aggregation():
    frame = pd.DataFrame({'key': ['a', 'b'], 'product': ['p', 'p'],
                          'price': [Decimal('2'), Decimal('7')], 'qty': [3, 5]}, index=[0, 0])
    execution = session(frame, grain=('key',))
    definition = RowProductSum('amount', 'source', 'price', 'qty', ('product',), unit='元')
    fact = execution.execute(definition)
    assert execution.table(fact)['value'].tolist() == [Decimal('41')]
    assert execution.fact_state(fact)['unit'] == '元'
    assert execution.trace()['facts']['amount']['definition']['value_field'] == 'price'
    assert execution.trace()['facts']['amount']['input_rows'] == 2


@pytest.mark.parametrize('frame,grain', [
    (pd.DataFrame({'key': ['a', 'a'], 'price': [2, 7], 'qty': [3, 5]}), ('key',)),
    (pd.DataFrame({'key': ['a', None], 'price': [2, 7], 'qty': [3, 5]}), ('key',)),
])
def test_duplicate_or_null_grain_is_rejected_before_amount_expansion(frame, grain):
    with pytest.raises(ValueError, match='grain'):
        session(frame, grain=grain)


def test_amount_requires_checked_grain_and_no_silent_missing_value_drop():
    frame = pd.DataFrame({'key': [1, 2], 'price': [Decimal('2'), None], 'qty': [3, 5]})
    with pytest.raises(ValueError, match='grain'):
        session(frame).execute(RowProductSum('amount', 'source', 'price', 'qty'))
    execution = session(frame, grain=('key',))
    propagated = execution.execute(RowProductSum('amount', 'source', 'price', 'qty'))
    excluded = execution.execute(RowProductSum('observed_amount', 'source', 'price', 'qty', missing_policy='exclude'))
    assert execution.table(propagated)['value'].iloc[0] is None
    assert execution.fact_state(propagated)['state'] == 'missing'
    assert execution.table(excluded)['value'].iloc[0] == Decimal('6')
    assert execution.fact_state(excluded)['state'] == 'partial'
    assert execution.fact_state(excluded)['missing_count'] == 1


def test_zero_is_valid_empty_is_not_a_phantom_zero_and_all_missing_is_unknown():
    execution = session(pd.DataFrame({'key': [1], 'price': [Decimal('0')], 'qty': [3]}), grain=('key',))
    fact = execution.execute(RowProductSum('amount', 'source', 'price', 'qty'))
    assert execution.table(fact)['value'].iloc[0] == Decimal('0')
    assert execution.fact_state(fact)['state'] == 'valid'
    empty = session(pd.DataFrame(columns=['key', 'price', 'qty']), grain=('key',))
    fact = empty.execute(RowProductSum('amount', 'source', 'price', 'qty'))
    assert empty.table(fact).empty
    assert empty.fact_state(fact)['state'] == 'empty'
    assert empty.decision_state(empty.evaluate_extrema('rank', fact))['state'] == 'unknown'


def test_large_integer_products_and_grouped_missing_values_do_not_lose_precision():
    frame = pd.DataFrame({'key': [1, 2], 'group': ['a', 'b'], 'price': [2**62 + 1, None], 'qty': [3, 1]}, dtype=object)
    execution = session(frame, grain=('key',))
    fact = execution.execute(RowProductSum('amount', 'source', 'price', 'qty', ('group',)))
    assert execution.table(fact)['value'].iloc[0] == 3 * (2**62 + 1)
    assert execution.table(fact)['value'].iloc[1] is None


@pytest.mark.parametrize('bad', [True, '2', float('inf')])
def test_amount_does_not_coerce_labels_booleans_or_infinity_into_money(bad):
    frame = pd.DataFrame({'key': [1], 'price': [bad], 'qty': [2]})
    with pytest.raises((ValueError, TypeError)):
        session(frame, grain=('key',)).execute(RowProductSum('amount', 'source', 'price', 'qty'))


def test_grouped_missing_values_disclose_the_incomplete_comparison_scope():
    frame = pd.DataFrame({'key': [1, 2, 3], 'label': ['a', 'a', 'b'],
                          'price': [Decimal('2'), None, Decimal('3')], 'qty': [2, 1, 1]})
    execution = session(frame, grain=('key',))
    fact = execution.execute(RowProductSum('amount', 'source', 'price', 'qty', ('label',), missing_policy='exclude'))
    decision = execution.evaluate_extrema('rank', fact)
    assert not execution.decision_state(decision)['complete_inputs']
    assert '排除了 1 条缺失输入' in execution.render_extrema(decision, 'label')


def test_multi_source_amount_keeps_both_query_references_without_claiming_join_proof():
    execution = SemanticExecution('joined')
    frame = pd.DataFrame({'key': [1], 'price': [Decimal('2')], 'qty': [3]})
    execution.add_source('joined', frame, query_keys=('prices-query', 'quantity-query'), grain=('key',))
    execution.execute(RowProductSum('amount', 'joined', 'price', 'qty'))
    assert execution.trace()['facts']['amount']['query_keys'] == ('prices-query', 'quantity-query')


def test_multifield_python_result_reports_the_object_table_contract_at_the_producer():
    execution = session(pd.DataFrame({'id': [1]}))
    definition = PythonCalculation(
        'summary', lambda inputs, parameters: pd.DataFrame([{'total': 1, 'average': 1.0}]), ('source',)
    )
    with pytest.raises(ValueError, match="value_field='value'.*no primary numeric measure exists"):
        execution.execute(definition)


def test_numeric_measure_in_multifield_result_table_supports_extrema():
    execution = session(pd.DataFrame({'id': [1]}))
    definition = PythonCalculation(
        'distribution',
        lambda inputs, parameters: pd.DataFrame({
            'category': ['A', 'B', 'C'], 'count': [4, 7, 7], 'share': [20.0, 35.0, 35.0],
        }),
        ('source',), value_field='count', output_grain=('category',),
    )
    execution.define(definition, question_id='q1', result_id='distribution')
    execution.define_extrema('largest_category', 'distribution')

    result = execution.result('distribution')
    decision = execution.decision('largest_category')

    assert execution.table(result).to_dict('records') == [
        {'category': 'A', 'count': 4, 'share': 20.0},
        {'category': 'B', 'count': 7, 'share': 35.0},
        {'category': 'C', 'count': 7, 'share': 35.0},
    ]
    assert execution.matches(decision).to_dict('records') == [
        {'category': 'B', 'count': 7, 'share': 35.0},
        {'category': 'C', 'count': 7, 'share': 35.0},
    ]
