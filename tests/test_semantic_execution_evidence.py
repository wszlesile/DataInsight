from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from analysis_runtime.provenance import record_query_frame, reset_capture, execution_traces
from analysis_runtime.semantic_execution import DistinctCount, RowProductSum, SemanticExecution
from analysis_runtime.semantic_evidence import build_semantic_execution_catalog
from service.post_analysis_evidence_service import build_execution_snapshot, LineageIndexer


@pytest.fixture(autouse=True)
def isolated_capture():
    reset_capture()
    yield
    reset_capture()


def capture_source(frame, *, query_key='query', source_id='source', grain=()):
    execution = SemanticExecution('test')
    snapshot = {'query_key': query_key, 'implementation': {'file_filters': []},
                'execution': {'status': 'success', 'columns': list(frame.columns), 'row_count': len(frame)}}
    record_query_frame(query_key, frame, snapshot)
    execution.add_query_source(source_id, frame, grain=grain)
    return execution, snapshot


def test_query_receipt_requires_actual_unchanged_frame_and_rejects_copied_attrs():
    frame = pd.DataFrame({'id': [1, 2], 'category': ['a', 'b']})
    execution, snapshot = capture_source(frame)
    copied = frame.copy()
    copied.attrs['data_insight_query_key'] = 'query'
    with pytest.raises(ValueError, match='not a query frame'):
        execution.add_query_source('copied', copied)
    frame.loc[0, 'category'] = 'edited'
    with pytest.raises(ValueError, match='modified'):
        execution.add_query_source('edited', frame)
    fact = execution.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    assert execution.table(fact)['category'].tolist() == ['a', 'b']
    assert build_semantic_execution_catalog([execution.trace()], {'query': snapshot})[0]['verification'] == 'query_bound'


def test_capture_reset_rejects_frames_from_previous_worker():
    frame = pd.DataFrame({'id': [1]})
    capture_source(frame)
    reset_capture()
    current = SemanticExecution('test')
    with pytest.raises(ValueError, match='not a query frame'):
        current.add_query_source('old', frame)
    assert execution_traces() == []


@pytest.mark.parametrize('change,reason', [
    ('missing', 'successful_query_snapshot_missing'),
    ('failed', 'successful_query_snapshot_missing'),
    ('filter', 'query_snapshot_changed_since_input_capture'),
])
def test_evidence_does_not_accept_missing_failed_or_rebound_queries(change, reason):
    execution, snapshot = capture_source(pd.DataFrame({'id': [1]}))
    execution.execute(DistinctCount('count', 'source', 'id'))
    changed = deepcopy(snapshot)
    if change == 'failed':
        changed['execution']['status'] = 'failed'
    elif change == 'filter':
        changed['implementation']['file_filters'] = [{'field': 'id', 'value': 99}]
    nodes = build_semantic_execution_catalog([execution.trace()], {} if change == 'missing' else {'query': changed})
    assert nodes[0]['verification'] == 'unresolved'
    assert nodes[0]['reason'] == reason
    assert nodes[0]['query_node_ids'] == []


def test_declarations_cannot_certify_transformed_source_even_with_valid_query_key():
    frame = pd.DataFrame({'id': [1, 2], 'price': [2, 7], 'qty': [3, 5]})
    execution, snapshot = capture_source(frame, grain=('id',))
    execution.add_source('joined', frame.copy(), query_key='query', grain=('id',))
    execution.execute(RowProductSum('amount', 'joined', 'price', 'qty', unit='元'))
    node = build_semantic_execution_catalog([execution.trace()], {'query': snapshot})[0]
    assert node['verification'] == 'unresolved'
    assert node['unit_verification'] == 'declared'
    assert node['business_semantics_verification'] == 'not_evaluated'


def test_fresh_execution_recomputes_changed_data_without_mutating_saved_trace():
    first, snapshot = capture_source(pd.DataFrame({'id': [1, 2], 'category': ['a', 'b']}))
    fact = first.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    first.evaluate_extrema('rank', fact)
    saved = deepcopy(first.trace())
    reset_capture()
    second, current_snapshot = capture_source(pd.DataFrame({'id': [1, 2, 3], 'category': ['a', 'a', 'b']}))
    fresh = second.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    second.evaluate_extrema('rank', fresh)
    assert saved['decisions']['rank']['kind'] == 'all_equal'
    assert second.trace()['decisions']['rank']['kind'] == 'unique'
    assert first.trace() == saved
    assert first.trace()['execution_token'] != second.trace()['execution_token']
    assert build_semantic_execution_catalog([saved], {'query': current_snapshot})[0]['verification'] == 'unresolved'


def test_snapshot_persists_traces_and_hashes_them_without_binding_unrelated_report_numbers():
    execution, snapshot = capture_source(pd.DataFrame({'id': [1]}))
    fact = execution.execute(DistinctCount('count', 'source', 'id'))
    execution.evaluate_extrema('rank', fact)
    trace = execution.trace()
    model = SimpleNamespace(id=1, generated_code='result = None', analysis_report='Unrelated 999.',
                            result_payload_json=json.dumps({'query_snapshots': {'query': snapshot},
                                                            'semantic_execution_traces': [trace]}))
    material = build_execution_snapshot(execution=model, artifacts=[], selected_datasource_snapshot=[], user_query='test')
    index = LineageIndexer().build(material)
    nodes = index['semantic_execution_nodes']
    assert len(nodes) == 2
    assert all(node['verification'] == 'query_bound' for node in nodes)
    assert nodes[0]['values'] == [{'value': 1}]
    assert all(not output.get('semantic_fact_id') for output in index['output_nodes'])
    model.result_payload_json = json.dumps({'query_snapshots': {'query': snapshot}})
    legacy = build_execution_snapshot(execution=model, artifacts=[], selected_datasource_snapshot=[], user_query='test')
    assert legacy.semantic_execution_traces == ()
    assert material.result_hash != legacy.result_hash
    assert LineageIndexer().build(replace(material, semantic_execution_traces=()))['semantic_execution_nodes'] == []


def test_result_save_collects_executed_traces_but_does_not_include_empty_sessions():
    import agent.tools as rt
    rt._prime_worker_runtime_context({})
    execution, snapshot = capture_source(pd.DataFrame({'id': [1]}))
    execution.execute(DistinctCount('count', 'source', 'id'))
    SemanticExecution('unused')
    rt.CURRENT_QUERY_SNAPSHOTS.set({'query': snapshot})
    result = rt.save_analysis_result(analysis_report='Test result.')
    assert len(result.semantic_execution_traces) == 1
    assert result.semantic_execution_traces[0]['facts']['count']['values'] == [{'value': 1}]
    result.semantic_execution_traces[0]['facts']['count']['values'][0]['value'] = 999
    assert execution.trace()['facts']['count']['values'] == [{'value': 1}]


@pytest.mark.parametrize('mutation', ['none', 'view', 'copy', 'artifact'])
def test_table_binding_tracks_actual_consumption_and_rejects_later_value_changes(mutation):
    import agent.tools as rt
    execution, snapshot = capture_source(pd.DataFrame({'id': [1, 2], 'category': ['a', 'b']}))
    fact = execution.execute(DistinctCount('counts', 'source', 'id', ('category',)))
    view = execution.table(fact)
    if mutation == 'view':
        view.loc[0, 'value'] = 999
    elif mutation == 'copy':
        view = view.copy()
    table = rt.build_table_result(title='Counts', data=view)['table']
    if mutation == 'artifact':
        table['rows'][0]['value'] = 999
    model = SimpleNamespace(id=1, generated_code='result = None', analysis_report='Counts.',
                            result_payload_json=json.dumps({'query_snapshots': {'query': snapshot},
                                                            'semantic_execution_traces': [execution.trace()]}))
    material = build_execution_snapshot(execution=model,
        artifacts=[{'id': 2, 'artifact_type': 'table', 'content_json': json.dumps(table)}],
        selected_datasource_snapshot=[], user_query='test')
    index = LineageIndexer().build(material)
    outputs = [output for output in index['output_nodes'] if output.get('field') == 'value']
    assert outputs
    if mutation in {'none', 'copy'}:
        assert outputs[0]['semantic_verification'] == 'query_bound'
        assert outputs[0]['semantic_source_query_node_ids'] == ['query']
        assert index['semantic_execution_nodes'][0]['output_node_ids'] == [outputs[0]['output_node_id']]
    elif mutation == 'artifact':
        assert outputs[0]['semantic_verification'] == 'unresolved'
        assert outputs[0]['semantic_unresolved_reason'] == 'table_changed_after_fact_view_consumption'
    else:
        assert '_semantic_execution_binding' not in table
        assert 'semantic_fact_node_id' not in outputs[0]
