import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import service.semantic_datasource_selection_service as semantic  # noqa: E402
import service.insight_ns_rel_datasource_service as datasource_service  # noqa: E402
from service.conversation_context_service import ConversationContextService  # noqa: E402


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Scope:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def test_provider_router_uses_local_when_semantic_center_is_not_installed(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: False,
    )
    assert semantic.DatasourceSelectionProviderRouter.resolve() == 'local'


def test_provider_router_uses_semantic_center_when_application_is_installed(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: True,
    )
    assert semantic.DatasourceSelectionProviderRouter.resolve() == 'semantic_center'


def test_provider_router_allows_semantic_discovery_for_empty_snapshot(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: True,
    )

    assert semantic.DatasourceSelectionProviderRouter.can_discover_without_bound_datasource({})


def test_provider_router_does_not_allow_local_discovery_for_empty_snapshot(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: False,
    )

    assert not semantic.DatasourceSelectionProviderRouter.can_discover_without_bound_datasource({})


def test_provider_router_uses_semantic_center_for_uns_only_snapshot(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: True,
    )

    assert semantic.DatasourceSelectionProviderRouter.resolve({
        'selected_datasource_snapshot': [{
            'datasource_id': 11,
            'datasource_type': 'table',
            'selection_origin': 'uns',
        }],
    }) == 'semantic_center'


def test_provider_router_forces_local_when_external_source_is_associated(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: True,
    )

    assert semantic.DatasourceSelectionProviderRouter.resolve({
        'selected_datasource_snapshot': [
            {
                'datasource_id': 11,
                'datasource_type': 'table',
                'selection_origin': 'uns',
            },
            {
                'datasource_id': 12,
                'datasource_type': 'table',
                'selection_origin': 'external',
            },
        ],
    }) == 'local'


def test_provider_router_treats_legacy_non_table_source_as_external(monkeypatch):
    monkeypatch.setattr(
        semantic.semantic_center_availability,
        'is_installed',
        lambda: True,
    )

    assert semantic.DatasourceSelectionProviderRouter.resolve({
        'selected_datasource_snapshot': [{
            'datasource_id': 12,
            'datasource_type': 'local_file',
        }],
    }) == 'local'


def test_semantic_center_availability_reads_kernel_once_per_cache_window(monkeypatch):
    calls = []
    availability = semantic.SemanticCenterAvailability()
    monkeypatch.setattr(
        semantic.supos_kernel_api,
        'is_platform_application_installed',
        lambda **kwargs: calls.append(kwargs) or True,
    )

    assert availability.is_installed()
    assert availability.is_installed()
    assert calls == [{
        'group': 'toolkit',
        'name': 'supos-rag-semantic-center',
    }]


def test_semantic_center_availability_falls_back_to_local_on_kernel_error(monkeypatch):
    availability = semantic.SemanticCenterAvailability()
    monkeypatch.setattr(
        semantic.supos_kernel_api,
        'is_platform_application_installed',
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError('kernel unavailable')),
    )
    monkeypatch.setattr(
        semantic,
        'semantic_center_availability',
        availability,
    )

    assert semantic.DatasourceSelectionProviderRouter.resolve({}) == 'local'


def test_semantic_selection_uses_platform_service_address(monkeypatch):
    monkeypatch.setattr(
        semantic.Config,
        'SEMANTIC_BASE',
        'http://toolkit-semantic-center:9621',
    )

    service = semantic.SemanticDatasourceSelectionService()

    assert service.base_url == 'http://toolkit-semantic-center:9621'


def test_semantic_selection_accepts_configured_service_address(monkeypatch):
    monkeypatch.setattr(
        semantic.Config,
        'SEMANTIC_BASE',
        'http://127.0.0.1:19621/',
    )

    service = semantic.SemanticDatasourceSelectionService()

    assert service.base_url == 'http://127.0.0.1:19621'


def test_semantic_materialization_uses_node_ids_and_batches_missing_details(monkeypatch):
    existing = SimpleNamespace(uns_node_id='101', id=11)

    class _Query:
        def filter(self, *args):
            return self

        def all(self):
            return [existing]

    class _Session:
        committed = False

        def query(self, *args):
            return _Query()

        def commit(self):
            self.committed = True

    session = _Session()
    service = datasource_service.InsightNsRelDatasourceService(session=session)
    captured = {}

    def fetch_details(node_ids, authorization):
        captured['node_ids'] = list(node_ids)
        captured['authorization'] = authorization
        return [{
            'success': True,
            'id': node_id,
            'detail': {
                'id': node_id,
                'alias': f'alias_{node_id}',
                'name': f'table_{node_id}',
                'dataType': 9,
                'fields': [],
            },
        } for node_id in node_ids]

    monkeypatch.setattr(
        datasource_service,
        'release_clean_session_connection',
        lambda session: None,
    )
    monkeypatch.setattr(service, '_fetch_uns_details_in_parallel', fetch_details)
    monkeypatch.setattr(
        service,
        '_fetch_reference_uns_details_for_import',
        lambda details, authorization: ({}, []),
    )
    monkeypatch.setattr(
        service,
        '_upsert_uns_table_datasource',
        lambda **kwargs: SimpleNamespace(id=int(kwargs['detail']['id'])),
    )
    monkeypatch.setattr(service, '_bind_datasource', lambda **kwargs: None)

    result = service.materialize_semantic_uns_node_ids(
        uns_node_ids=['101', '202', '202', '303'],
        authorization='Bearer test',
    )

    assert captured == {
        'node_ids': ['202', '303'],
        'authorization': 'Bearer test',
    }
    assert result == {'101': 11, '202': 202, '303': 303}
    assert session.committed is True


def test_turn_snapshot_merge_preserves_existing_and_adds_semantic_sources():
    class _Turn:
        selected_datasource_ids_json = '[11]'
        selected_datasource_snapshot_json = '[{"datasource_id":11,"name":"existing"}]'

    class _Query:
        def filter(self, *args):
            return self

        def first(self):
            return turn

    class _Session:
        committed = False

        def query(self, *args):
            return _Query()

        def commit(self):
            self.committed = True

    turn = _Turn()
    session = _Session()
    ConversationContextService(session).merge_turn_datasource_snapshot(
        turn_id=9,
        datasource_snapshot=[
            {'datasource_id': 12, 'name': 'semantic'},
            {'datasource_id': 11, 'name': 'refreshed'},
        ],
    )

    assert json.loads(turn.selected_datasource_ids_json) == [11, 12]
    assert json.loads(turn.selected_datasource_snapshot_json) == [
        {'datasource_id': 11, 'name': 'refreshed'},
        {'datasource_id': 12, 'name': 'semantic'},
    ]
    assert session.committed is True


def test_semantic_retrieve_materializes_node_ids_and_normalizes_relationships(monkeypatch):
    calls = {}

    def fake_post(*args, **kwargs):
        calls['request'] = kwargs
        return _Response({
            'entity_ids': ['10001', '10002'],
            'relationships': [{
                'source_id': '10001',
                'target_id': '10002',
                'description': '订单通过 customer_id 关联客户',
                'field_matches': [{
                    'source_field': 'customer_id',
                    'target_field': 'id',
                }],
            }],
        })

    monkeypatch.setattr(
        semantic.requests,
        'post',
        fake_post,
    )
    monkeypatch.setattr(semantic, 'session_scope', lambda **kwargs: _Scope())

    class _Materializer:
        def __init__(self, session):
            pass

        def materialize_semantic_uns_node_ids(self, **kwargs):
            calls['materialize'] = kwargs
            return {'10001': 101, '10002': 102}

    class _SnapshotBuilder:
        def __init__(self, session):
            pass

        def build_datasource_snapshot_by_ids(self, **kwargs):
            calls['snapshot'] = kwargs
            return [
                {'datasource_id': 101, 'metadata_schema': {}},
                {'datasource_id': 102, 'metadata_schema': {}},
            ]

    monkeypatch.setattr(semantic, 'InsightNsRelDatasourceService', _Materializer)
    monkeypatch.setattr(semantic, 'ConversationContextService', _SnapshotBuilder)

    result = semantic.SemanticDatasourceSelectionService(
        base_url='http://semantic',
        timeout_seconds=3,
    ).retrieve(
        query='分析订单客户情况',
        authorization='Bearer test',
        namespace_id=6,
        database_conn_info={'lake_rds_database_name': 'fqe_uns'},
    )

    assert result.datasource_ids == (101, 102)
    assert calls['request']['json']['top_k'] == 10
    assert calls['materialize']['uns_node_ids'] == ['10001', '10002']
    assert calls['snapshot']['namespace_id'] == 6
    assert result.relationship_hints == ({
        'source_datasource_id': 101,
        'target_datasource_id': 102,
        'source_fields': ['customer_id'],
        'target_fields': ['id'],
        'description': '订单通过 customer_id 关联客户',
        'source': 'semantic_center',
        'verification_status': 'provider_asserted',
    },)


def test_semantic_retrieve_uses_service_credential_without_replacing_uns_user_auth(monkeypatch):
    calls = {}

    def fake_post(*args, **kwargs):
        calls['request'] = kwargs
        return _Response({'entity_ids': ['10001'], 'relationships': []})

    monkeypatch.setattr(semantic.requests, 'post', fake_post)
    monkeypatch.setattr(semantic, 'session_scope', lambda **kwargs: _Scope())
    monkeypatch.setattr(semantic.Config, 'SEMANTIC_DATASOURCE_AUTHORIZATION', 'Bearer semantic-service')

    class _Materializer:
        def __init__(self, session):
            pass

        def materialize_semantic_uns_node_ids(self, **kwargs):
            calls['materialize'] = kwargs
            return {'10001': 101}

    class _SnapshotBuilder:
        def __init__(self, session):
            pass

        def build_datasource_snapshot_by_ids(self, **kwargs):
            return [{'datasource_id': 101, 'metadata_schema': {}}]

    monkeypatch.setattr(semantic, 'InsightNsRelDatasourceService', _Materializer)
    monkeypatch.setattr(semantic, 'ConversationContextService', _SnapshotBuilder)

    semantic.SemanticDatasourceSelectionService(base_url='http://semantic').retrieve(
        query='分析订单客户情况',
        authorization='Bearer platform-user',
        namespace_id=6,
    )

    assert calls['request']['headers']['Authorization'] == 'Bearer semantic-service'
    assert calls['materialize']['authorization'] == 'Bearer platform-user'


@pytest.mark.parametrize('payload', [
    {},
    {'entity_ids': [], 'relationships': []},
    {'entity_ids': '10001', 'relationships': []},
])
def test_semantic_retrieve_rejects_unusable_payload_for_local_fallback(
    monkeypatch,
    payload,
):
    monkeypatch.setattr(
        semantic.requests,
        'post',
        lambda *args, **kwargs: _Response(payload),
    )

    with pytest.raises(semantic.SemanticDatasourceSelectionError):
        semantic.SemanticDatasourceSelectionService(
            base_url='http://semantic',
        ).retrieve(
            query='分析订单',
            authorization='Bearer test',
            namespace_id=6,
        )
