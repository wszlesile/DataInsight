from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import agent.tools as rt
from analysis_runtime.provenance import record_query_frame, set_delivery_requirements
from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount
from service.post_analysis_evidence_service import (
    build_execution_snapshot, LineageIndexer, PostAnalysisEvidenceCompiler, EvidencePlan,
)
from service.insight_ns_conversation_service import InsightNsConversationService
from service.evidence_assembly_service import EvidenceAssemblyService


@pytest.fixture(autouse=True)
def isolated():
    rt._prime_worker_runtime_context({})
    yield
    rt._prime_worker_runtime_context({})


def material(extrema=False, value_field='value', chart_kind='bar'):
    execution = SemanticExecution('presentation-evidence')
    data = pd.DataFrame({'id': [1, 2, 3], 'group': ['A', 'A', 'B']})
    query = {'query_key': 'items', 'implementation': {'loader': 'load_local_file', 'datasource_identifier': 'items.csv'},
             'execution': {'status': 'success', 'row_count': 3, 'columns': list(data.columns)}}
    record_query_frame('items', data, query)
    execution.add_query_source('input', data, grain=('id',))
    fact = execution.execute(DistinctCount('counts', 'input', 'id', ('group',), value_field=value_field, unit='台'))
    execution.bind_result('q1', 'counts', fact)
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'counts'}])
    chart = execution.chart(fact, chart_kind=chart_kind, title='分布', category_field='group', value_field=value_field)
    fragment = (execution.render_extrema(execution.evaluate_extrema('rank', fact), 'group') if extrema else
                execution.render_value(fact, value_format='.2f', prefix='A组设备数量为', suffix='台。'))
    report = execution.compose_report(['## 分析\n\n', fragment])
    rt.CURRENT_QUERY_SNAPSHOTS.set({'items': query})
    result = rt.save_analysis_result(analysis_report=report, charts=[chart])
    model = SimpleNamespace(id=1, generated_code='result = None', analysis_report=result.analysis_report,
                            result_payload_json=result.model_dump_json())
    snapshot = build_execution_snapshot(execution=model, artifacts=[
        {'id': 2, 'artifact_type': 'chart', 'content_json': json.dumps(result.charts[0])}],
        selected_datasource_snapshot=[], user_query='test')
    return snapshot


@pytest.mark.parametrize('value_field', ['value', 'metric_total', '数量'])
def test_native_chart_and_exact_report_value_compile_without_model_reinterpreting_formula(value_field):
    snapshot = material(value_field=value_field)
    planner = MagicMock(side_effect=AssertionError('Native consumption should not need model recovery'))
    bundle = PostAnalysisEvidenceCompiler(planner=planner, structured_planner=planner).compile(snapshot)
    assert bundle['status'] == 'complete', bundle['diagnostics']
    assert len(bundle['evidence_definitions']) == 1
    assert len(bundle['chart_trace_refs']) == 1
    assert len(bundle['report_trace_refs']) == 1
    assert bundle['report_trace_refs'][0]['text'] == '2.00'
    assert bundle['report_trace_refs'][0]['presentation_binding']['value_format'] == '.2f'
    assert bundle['evidence_definitions'][0]['calculation']['formula'] == '去重计数(id)'
    planner.assert_not_called()


def test_extrema_only_primary_value_is_bound_not_the_equal_comparable_group_count():
    snapshot = material(extrema=True)
    index = LineageIndexer().build(snapshot)
    reports = [output for output in index['output_nodes'] if output['artifact_type'] == 'report']
    assert len(reports) == 2
    assert [output.get('semantic_verification') for output in reports].count('query_bound') == 1
    bound = next(output for output in reports if output.get('semantic_verification') == 'query_bound')
    assert bound['semantic_presentation_binding']['kind'] == 'extrema_value'
    assert bound['semantic_presentation_binding']['decision_id'] == 'rank'
    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan(), structured_planner=lambda _: EvidencePlan()).compile(snapshot)
    assert bundle['status'] != 'complete'
    assert len(bundle['report_trace_refs']) == 1


def test_stale_chart_and_report_receipts_do_not_get_native_bindings():
    snapshot = material()
    original = LineageIndexer().build(snapshot)
    assert all(output.get('semantic_verification') == 'query_bound' for output in original['output_nodes'])
    changed = deepcopy(snapshot.charts)
    changed[0]['title'] = '其他口径'
    index = LineageIndexer().build(replace(snapshot, charts=changed,
                                        analysis_report=snapshot.analysis_report.replace('2.00', '9.00')))
    assert all(output.get('semantic_verification') == 'unresolved' for output in index['output_nodes'])


def test_coverage_changes_snapshot_hash_and_survives_roundtrip():
    snapshot = material()
    assert snapshot.semantic_execution_coverage['report_digest']
    row = snapshot.semantic_execution_coverage['results'][0]
    assert row['report_spans'][0]['value_format'] == '.2f'
    payload = {'query_snapshots': snapshot.query_snapshots, 'semantic_execution_traces': snapshot.semantic_execution_traces,
               'semantic_execution_coverage': deepcopy(snapshot.semantic_execution_coverage)}
    model = SimpleNamespace(id=1, generated_code=snapshot.generated_code, analysis_report=snapshot.analysis_report,
                            result_payload_json=json.dumps(payload))
    payload['semantic_execution_coverage']['report_digest'] = 'changed'
    model.result_payload_json = json.dumps(payload)
    changed = build_execution_snapshot(execution=model, artifacts=[{
        'id': 2, 'artifact_type': 'chart', 'content_json': json.dumps(snapshot.charts[0])}],
        selected_datasource_snapshot=[], user_query='test')
    assert changed.result_hash != snapshot.result_hash


@pytest.mark.parametrize('kind', ['chart', 'report'])
def test_existing_public_resolver_returns_display_value_and_same_native_formula(kind):
    snapshot = material()
    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)
    definition = deepcopy(bundle['evidence_definitions'][0])
    key = definition.pop('evidence_key')
    evidence_id = EvidenceAssemblyService._build_evidence_id(1, key)
    definition['evidence_id'] = evidence_id
    ref = {**bundle[f'{kind}_trace_refs'][0], 'evidence_id': evidence_id}
    ref.pop('evidence_key')
    content = snapshot.charts[0] if kind == 'chart' else {'report_markdown': snapshot.analysis_report}
    artifact_view = {'id': 2, 'artifact_type': kind, 'content_json': content,
                     'metadata_json': {'evidence_refs': {f'{kind}_trace_refs': [ref]}}}
    artifact = SimpleNamespace(execution_id=1, to_dict=lambda: artifact_view)
    evidence = SimpleNamespace(evidence_map_json=json.dumps({evidence_id: definition}),
                               query_snapshots_json=json.dumps(snapshot.query_snapshots))
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = [artifact, evidence, SimpleNamespace(generated_code=snapshot.generated_code)]
    service = InsightNsConversationService(session)
    selection = ({'data_index': 0, 'series_index': 0, 'value_field': 'value'} if kind == 'chart' else
                 {'start': ref['start'], 'end': ref['end']})
    with patch.object(service, '_get_accessible_conversation', return_value=SimpleNamespace(id=1)), \
         patch.object(service, '_get_turn', return_value=SimpleNamespace(id=1)):
        result = service.resolve_evidence('test-user', 1, 1, {'artifact_id': 2, 'evidence_id': evidence_id,
            'detail_level': 'full', 'selection': selection})
    assert result['traceability_status'] == 'available'
    assert str(result['selected_context']['raw_value']) == ('2' if kind == 'chart' else '2.00')
    calculation = next(step for step in result['chain_steps'] if step['type'] == 'calculation')
    assert calculation['formula'] == '去重计数(id)'
    assert calculation['details']['calculation']['business_semantics_verification'] == 'not_evaluated'
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_nested_extrema_field_offsets_exclude_numbers_in_labels_and_count_fields():
    from analysis_runtime.provenance import execution_delivery_coverage
    execution = SemanticExecution('nested-rank')
    data = pd.DataFrame({'id': [1, 2, 3], 'group': ['A2026', 'A2026', 'B2025']})
    execution.add_source('input', data, query_key='declared')
    fact = execution.execute(DistinctCount('count', 'input', 'id', ('group',)))
    execution.bind_result('q1', 'count', fact)
    set_delivery_requirements([{'question_id': 'q1', 'result_id': 'count'}])
    fragment = execution.render_extrema(execution.evaluate_extrema('rank', fact), 'group')
    section = execution.compose_report(['章节：', fragment])
    report = execution.compose_report(['\n引言。\n', section, '\n'])
    row = execution_delivery_coverage(report=report)['results'][0]
    span = row['report_spans'][0]['value_spans'][0]
    assert report[span['start']:span['end']] == '2'
    assert span['start'] > report.index('2026')


def test_display_aggregation_keeps_chart_but_does_not_certify_underlying_fact_row_formula():
    snapshot = material(chart_kind='pie')
    index = LineageIndexer().build(snapshot)
    chart = next(output for output in index['output_nodes'] if output['artifact_type'] == 'chart')
    assert chart['semantic_verification'] == 'execution_only'
    assert chart['semantic_unresolved_reason'] == 'display_transform_not_proven_as_fact_row'
    assert snapshot.charts
    assert chart['semantic_presentation_binding']['metric_mapping'] == 'display_transform'


def test_duplicate_category_guard_is_preserved_and_explicit_series_keeps_direct_rows():
    execution = SemanticExecution('category-collapse')
    data = pd.DataFrame({'id': [1, 2], 'group': ['A', 'A'], 'partition': ['X', 'Y']})
    execution.add_source('input', data, query_key='declared')
    fact = execution.execute(DistinctCount('counts', 'input', 'id', ('group', 'partition')))
    with pytest.raises(ValueError, match='duplicate plotting keys'):
        execution.chart(fact, chart_kind='bar', title='分类', category_field='group', value_field='value')
    chart = execution.chart(fact, chart_kind='bar', title='分类', category_field='group',
                            series_field='partition', value_field='value')
    assert chart['_semantic_execution_binding']['metric_mapping'] == 'direct_rows'
