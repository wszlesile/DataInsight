from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from analysis_runtime.provenance import record_query_frame, reset_capture
from analysis_runtime.semantic_execution import SemanticExecution, DistinctCount, RowProductSum, OneToOneJoin, DecimalColumns
from service.post_analysis_evidence_service import PostAnalysisEvidenceCompiler, EvidencePlan, build_execution_snapshot
from service.insight_ns_conversation_service import InsightNsConversationService
from service.evidence_assembly_service import EvidenceAssemblyService
import agent.tools as rt


@pytest.fixture(autouse=True)
def isolated_capture():
    reset_capture()
    yield
    reset_capture()


def material(join=False):
    execution = SemanticExecution('compile-test')
    snapshots = {}
    frames = ({'sales': pd.DataFrame({'key': ['a', 'b', 'c'], 'qty': ['3', '5', '1']}),
               'cost': pd.DataFrame({'key': ['a', 'b', 'd'], 'price': ['2', '7', '9']})} if join else
              {'items': pd.DataFrame({'key': [1, 2, 3], 'category': ['a', 'a', 'b']})})
    for name, frame in frames.items():
        snapshots[name] = {'query_key': name, 'implementation': {'loader': 'load_local_file', 'datasource_identifier': name + '.csv'},
                           'execution': {'status': 'success', 'row_count': len(frame), 'columns': list(frame.columns)}}
        record_query_frame(name, frame, snapshots[name])
        execution.add_query_source(name, frame, grain=('key',))
    if join:
        execution.materialize(OneToOneJoin('matched', 'sales', 'cost', ('key',)))
        execution.materialize(DecimalColumns('numbers', 'matched', ('price', 'qty')))
        fact = execution.execute(RowProductSum('amount', 'numbers', 'price', 'qty', unit='元'))
    else:
        fact = execution.execute(DistinctCount('counts', 'items', 'key', ('category',), unit='台'))
    table = rt.build_table_result(title='统计结果', data=execution.table(fact))['table']
    model = SimpleNamespace(id=1, generated_code='result = None', analysis_report='统计结果。',
        result_payload_json=json.dumps({'query_snapshots': snapshots, 'semantic_execution_traces': [execution.trace()]}, default=str))
    snapshot = build_execution_snapshot(execution=model,
        artifacts=[{'id': 2, 'artifact_type': 'table', 'content_json': json.dumps(table, default=str)}],
        selected_datasource_snapshot=[], user_query='test')
    return snapshot, table


@pytest.mark.parametrize('join', [False, True])
def test_native_table_compiles_in_existing_pipeline_without_model_reinterpreting_algorithm(join):
    snapshot, _ = material(join)
    planner = MagicMock(side_effect=AssertionError('No model planning needed for this bound table'))
    bundle = PostAnalysisEvidenceCompiler(planner=planner, structured_planner=planner).compile(snapshot)
    assert bundle['status'] == 'complete', bundle['diagnostics']
    assert len(bundle['evidence_definitions']) == len(bundle['table_trace_refs']) == 1
    definition = bundle['evidence_definitions'][0]
    assert definition['calculation']['executable_definition']['definition_id'] == ('amount' if join else 'counts')
    assert definition['calculation']['business_semantics_verification'] == 'not_evaluated'
    assert definition['calculation']['unit_verification'] == 'declared'
    assert definition['unit'] == ('元' if join else '台')
    assert planner.call_count == 0
    if join:
        assert set(definition['query_refs']) == {'sales', 'cost'}
        assert definition['calculation']['field_origins']['price'] == [{'query_key': 'cost', 'field': 'price'}]
        assert 'Σ(price × qty)' == definition['calculation']['formula']
        assert any('左侧未匹配 1 条' in note for note in definition['quality_notes'])


def test_changed_table_does_not_get_native_definition_or_complete_status():
    snapshot, _ = material()
    snapshot.tables[0]['rows'][0]['value'] = 999
    planner = MagicMock(return_value=EvidencePlan())
    bundle = PostAnalysisEvidenceCompiler(planner=planner, structured_planner=planner).compile(snapshot)
    assert bundle['status'] != 'complete'
    assert not any(item['calculation'].get('executable_definition') for item in bundle['evidence_definitions'])


def test_compiled_native_table_ref_resolves_through_existing_public_service():
    snapshot, table = material(join=True)
    bundle = PostAnalysisEvidenceCompiler(planner=lambda _: EvidencePlan()).compile(snapshot)
    assert bundle['status'] == 'complete', bundle['diagnostics']
    definition = deepcopy(bundle['evidence_definitions'][0])
    key = definition.pop('evidence_key')
    evidence_id = EvidenceAssemblyService._build_evidence_id(1, key)
    definition['evidence_id'] = evidence_id
    reference = {**bundle['table_trace_refs'][0], 'evidence_id': evidence_id}
    reference.pop('evidence_key')
    artifact_payload = {'id': 2, 'artifact_type': 'table', 'content_json': table,
                        'metadata_json': {'evidence_refs': {'table_trace_refs': [reference]}}}
    artifact = SimpleNamespace(execution_id=1, to_dict=lambda: artifact_payload)
    evidence_row = SimpleNamespace(evidence_map_json=json.dumps({evidence_id: definition}, default=str),
                                   query_snapshots_json=json.dumps(snapshot.query_snapshots))
    session = MagicMock()
    session.query.return_value.filter.return_value.first.side_effect = [artifact, evidence_row, SimpleNamespace(generated_code=snapshot.generated_code)]
    service = InsightNsConversationService(session)
    with patch.object(service, '_get_accessible_conversation', return_value=SimpleNamespace(id=1)), \
         patch.object(service, '_get_turn', return_value=SimpleNamespace(id=1)):
        resolved = service.resolve_evidence('test-user', 1, 1, {'artifact_id': 2, 'evidence_id': evidence_id,
            'detail_level': 'full', 'selection': {'row_index': 0, 'column_key': 'value'}})
    assert resolved['traceability_status'] == 'available'
    assert str(resolved['selected_context']['raw_value']) == '41'
    calculation = next(step for step in resolved['chain_steps'] if step['type'] == 'calculation')
    assert calculation['formula'] == 'Σ(price × qty)'
    assert calculation['details']['calculation']['field_origins']['qty'] == [{'query_key': 'sales', 'field': 'qty'}]
    assert any('数量口径' in str(step) for step in resolved['chain_steps'])
    session.add.assert_not_called()
    session.commit.assert_not_called()
