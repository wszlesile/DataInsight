from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest

from analysis_runtime.provenance import record_query_frame, reset_capture
from analysis_runtime.semantic_execution import SemanticExecution, OneToOneJoin, DecimalColumns, RowProductSum
from analysis_runtime.semantic_evidence import build_semantic_execution_catalog


@pytest.fixture(autouse=True)
def isolated_capture():
    reset_capture()
    yield
    reset_capture()


def sources(left, right):
    execution = SemanticExecution('join-test')
    snapshots = {}
    for name, frame in [('sales', left), ('cost', right)]:
        snapshot = {'query_key': name, 'execution': {'status': 'success', 'row_count': len(frame), 'columns': list(frame.columns)}}
        record_query_frame(name, frame, snapshot)
        execution.add_query_source(name, frame)
        snapshots[name] = snapshot
    return execution, snapshots


def test_join_decimal_and_rowwise_amount_have_exact_executed_field_origins():
    execution, snapshots = sources(pd.DataFrame({'id': ['a', 'b', 'c'], 'qty': ['3', '5', '4']}),
        pd.DataFrame({'id': ['a', 'b', 'd'], 'price': ['2', '7', '9']}))
    join = OneToOneJoin('matched', 'sales', 'cost', ('id',))
    execution.materialize(join)
    execution.materialize(DecimalColumns('numeric', 'matched', ('qty', 'price')))
    fact = execution.execute(RowProductSum('amount', 'numeric', 'price', 'qty', unit='元'))
    assert execution.table(fact)['value'].tolist() == [Decimal('41')]
    before = deepcopy(execution.trace())
    assert execution.materialize(join) == 'matched'
    assert before == execution.trace()
    assert before['sources']['matched']['observations'] == {'both': 2, 'left_only': 1, 'right_only': 1, 'output_rows': 2}
    node = build_semantic_execution_catalog([before], snapshots)[0]
    assert node['verification'] == 'query_bound'
    assert node['field_origins']['price'] == [{'query_key': 'cost', 'field': 'price'}]
    assert node['field_origins']['qty'] == [{'query_key': 'sales', 'field': 'qty'}]
    assert [item['kind'] for item in node['source_transformations']] == ['OneToOneJoin', 'DecimalColumns']
    changed = deepcopy(snapshots)
    changed['cost']['execution']['status'] = 'failed'
    assert build_semantic_execution_catalog([before], changed)[0]['verification'] == 'unresolved'


@pytest.mark.parametrize('side,bad', [('sales', ['a', 'a']), ('cost', ['a', 'a']),
                                      ('sales', ['a', None]), ('cost', ['a', None])])
def test_join_rejects_duplicate_or_null_actual_keys_before_multiplication(side, bad):
    left = pd.DataFrame({'id': ['a', 'b'], 'qty': [3, 5]})
    right = pd.DataFrame({'id': ['a', 'b'], 'price': [2, 7]})
    (left if side == 'sales' else right)['id'] = bad
    execution, _ = sources(left, right)
    with pytest.raises(ValueError, match='non-null and unique'):
        execution.materialize(OneToOneJoin('matched', 'sales', 'cost', ('id',)))
    assert 'matched' not in execution.trace()['sources']


@pytest.mark.parametrize('how,rows', [('inner', 1), ('left', 2), ('outer', 3)])
def test_explicit_join_population_and_missing_input_do_not_become_zero(how, rows):
    large = 2**62 + 1
    execution, snapshots = sources(pd.DataFrame({'id': ['a', 'b'], 'qty': [1, 1]}),
        pd.DataFrame({'id': ['a', 'c'], 'price': [large, large]}))
    execution.materialize(OneToOneJoin('joined', 'sales', 'cost', ('id',), how=how))
    fact = execution.execute(RowProductSum('amount', 'joined', 'price', 'qty'))
    trace = execution.trace()
    assert trace['sources']['joined']['row_count'] == rows
    assert trace['sources']['joined']['observations']['both'] == 1
    if how == 'inner':
        assert execution.table(fact)['value'].iloc[0] == large
    else:
        assert execution.table(fact)['value'].iloc[0] is None
        assert execution.fact_state(fact)['state'] == 'missing'
    assert build_semantic_execution_catalog([trace], snapshots)[0]['verification'] == 'query_bound'


def test_empty_join_is_empty_fact_and_ambiguous_columns_cannot_overwrite_values():
    execution, _ = sources(pd.DataFrame({'id': ['a'], 'qty': [1]}), pd.DataFrame({'id': ['b'], 'price': [2]}))
    execution.materialize(OneToOneJoin('matched', 'sales', 'cost', ('id',)))
    fact = execution.execute(RowProductSum('amount', 'matched', 'price', 'qty'))
    assert execution.table(fact).empty
    assert execution.fact_state(fact)['state'] == 'empty'
    with pytest.raises(ValueError, match='another producer'):
        execution.materialize(OneToOneJoin('matched', 'sales', 'cost', ('id',), how='outer'))
    execution, _ = sources(pd.DataFrame({'id': ['a'], 'value': [1]}), pd.DataFrame({'id': ['a'], 'value': [2]}))
    with pytest.raises(ValueError):
        execution.materialize(OneToOneJoin('ambiguous', 'sales', 'cost', ('id',), suffixes=('', '')))


@pytest.mark.parametrize('invalid', ['invalid', True, 'Infinity'])
def test_decimal_conversion_rejects_invalid_values_without_partial_source(invalid):
    execution, _ = sources(pd.DataFrame({'id': [1], 'qty': [invalid]}), pd.DataFrame({'id': [1], 'price': [2]}))
    with pytest.raises(ValueError):
        execution.materialize(DecimalColumns('numeric', 'sales', ('qty',)))
    assert 'numeric' not in execution.trace()['sources']


def test_overlapping_join_fields_keep_both_original_query_identities():
    execution, snapshots = sources(pd.DataFrame({'id': ['a'], 'value': [3]}), pd.DataFrame({'id': ['a'], 'value': [7]}))
    execution.materialize(OneToOneJoin('joined', 'sales', 'cost', ('id',)))
    fact = execution.execute(RowProductSum('amount', 'joined', 'value_y', 'value_x'))
    node = build_semantic_execution_catalog([execution.trace()], snapshots)[0]
    assert node['field_origins']['value_x'] == [{'query_key': 'sales', 'field': 'value'}]
    assert node['field_origins']['value_y'] == [{'query_key': 'cost', 'field': 'value'}]
    assert execution.table(fact)['value'].iloc[0] == 21
