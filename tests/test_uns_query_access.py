import json
from types import SimpleNamespace

from agent import context_engineering_runtime
from agent.tools import (
    _datasource_relation_tokens,
    _extract_physical_sql_datasource_identifiers,
    _statically_referenced_datasource_ids,
    _validate_uns_get_access_contract,
)
from service.conversation_context_service import ConversationContextService
from utils.datasource_utils import build_table_query_access


def _runtime_payload(message):
    content = message.content
    return json.loads(content[content.index("{"):content.rindex("}") + 1])


def test_native_relational_uns_uses_uns_get_path():
    access = build_table_query_access({
        "table_engine": "relational",
        "uns_data_type": 2,
        "uns_path": "生产数仓/批次质量上下文",
        "database_name": "fqe_uns",
        "table_name": "public._dim_batch",
    })

    assert access == {
        "access_mode": "uns_get",
        "uns_path": "生产数仓/批次质量上下文",
        "relation_expression": "uns_get('生产数仓/批次质量上下文')",
    }


def test_relational_reference_uses_own_uns_path_and_escapes_literal():
    access = build_table_query_access({
        "table_engine": "relational",
        "uns_data_type": 7,
        "uns_path": "业务域/O'Reilly映射",
        "refer_alias": "_physical_target",
    })

    assert access["access_mode"] == "uns_get"
    assert access["uns_path"] == "业务域/O'Reilly映射"
    assert access["relation_expression"] == "uns_get('业务域/O''Reilly映射')"


def test_protocol_and_time_series_keep_their_existing_access_modes():
    assert build_table_query_access({
        "table_engine": "relational",
        "uns_data_type": 9,
        "database_name": "fqe_erp",
        "table_name": "sales.orders",
    }) == {
        "access_mode": "physical_table",
        "relation_expression": "fqe_erp.sales.orders",
    }
    assert build_table_query_access({
        "table_engine": "time_series",
        "uns_data_type": 1,
        "uns_path": "设备/温度",
    }) == {"access_mode": "time_series"}


def test_legacy_relational_config_without_uns_path_uses_qualified_physical_fallback():
    assert build_table_query_access({
        "table_engine": "relational",
        "uns_data_type": 2,
        "database_name": "fqe_uns",
        "table_name": "public._legacy_batch",
    }) == {
        "access_mode": "physical_table",
        "relation_expression": "fqe_uns.public._legacy_batch",
    }


def test_snapshot_persists_query_access_without_reselection():
    datasource = SimpleNamespace(
        id=31,
        datasource_type="table",
        datasource_name="批次质量上下文",
        datasource_schema=json.dumps({"properties": {"batch_id": {"type": "string"}}}),
        datasource_config_json=json.dumps({
            "table_engine": "relational",
            "uns_data_type": 2,
            "uns_path": "生产数仓/批次质量上下文",
            "database_name": "fqe_uns",
            "table_name": "public._dim_batch",
        }),
        uns_node_id="2066811253765967872",
    )

    item = ConversationContextService(session=None)._build_datasource_snapshot_item(datasource)

    assert item["datasource_identifier"] == "public._dim_batch"
    assert item["query_access"]["access_mode"] == "uns_get"
    assert item["query_access"]["uns_path"] == "生产数仓/批次质量上下文"


def test_legacy_snapshot_is_hydrated_from_saved_datasource_config():
    datasource = SimpleNamespace(
        id=31,
        is_deleted=0,
        datasource_config_json=json.dumps({
            "table_engine": "relational",
            "uns_data_type": 2,
            "uns_path": "生产数仓/批次质量上下文",
            "database_name": "fqe_uns",
            "table_name": "public._dim_batch",
        }),
    )

    class _Query:
        def filter(self, *args):
            return self

        def all(self):
            return [datasource]

    class _Session:
        def query(self, model):
            return _Query()

    original = [{
        "datasource_id": 31,
        "datasource_type": "table",
        "datasource_name": "批次质量上下文",
        "datasource_identifier": "public._dim_batch",
    }]
    hydrated = ConversationContextService(
        session=_Session(),
    ).hydrate_datasource_snapshot_query_access(original)

    assert "query_access" not in original[0]
    assert hydrated[0]["query_access"] == {
        "access_mode": "uns_get",
        "uns_path": "生产数仓/批次质量上下文",
        "relation_expression": "uns_get('生产数仓/批次质量上下文')",
    }


def test_runtime_query_contract_exposes_uns_get_as_authoritative_relation(monkeypatch):
    snapshot = {
        "selected_datasource_ids": [31],
        "selected_datasource_snapshot": [{
            "datasource_id": 31,
            "datasource_type": "table",
            "datasource_name": "批次质量上下文",
            "datasource_identifier": "public._dim_batch",
            "table_engine": "relational",
            "query_access": {
                "access_mode": "uns_get",
                "uns_path": "生产数仓/批次质量上下文",
                "relation_expression": "uns_get('生产数仓/批次质量上下文')",
            },
            "metadata_schema": {
                "properties": {"batch_id": {"type": "string"}},
            },
        }],
    }
    monkeypatch.setattr(context_engineering_runtime, "_load_conversation", lambda _: None)

    message = context_engineering_runtime.get_datasource_message(
        namespace_id=1,
        conversation_id=2,
        snapshot_override=snapshot,
    )
    contract = _runtime_payload(message)["datasources"][0]["query_contract"]

    assert contract["access_mode"] == "uns_get"
    assert contract["uns_path"] == "生产数仓/批次质量上下文"
    assert contract["relation_expression"] == "uns_get('生产数仓/批次质量上下文')"
    assert "query_access" not in _runtime_payload(message)["datasources"][0]


def test_uns_get_join_is_bound_back_to_both_datasources():
    sql = """
    SELECT a.batch_id, b.product_name
    FROM uns_get('生产数仓/批次') AS a
    JOIN uns_get('生产数仓/产品') AS b ON a.product_id = b.product_id
    """
    snapshot = [{
        "datasource_id": 31,
        "datasource_identifier": "public._batch",
        "query_access": {"access_mode": "uns_get", "uns_path": "生产数仓/批次"},
    }, {
        "datasource_id": 32,
        "datasource_identifier": "public._product",
        "query_access": {"access_mode": "uns_get", "uns_path": "生产数仓/产品"},
    }]

    assert _extract_physical_sql_datasource_identifiers(sql) == {
        "生产数仓/批次",
        "生产数仓/产品",
    }
    assert "生产数仓/批次" in _datasource_relation_tokens(snapshot[0])
    assert _statically_referenced_datasource_ids(
        f'sql = {sql!r}',
        selected_datasource_snapshot=snapshot,
    ) == {31, 32}


def test_static_contract_rejects_bypassing_uns_node_with_physical_alias():
    snapshot = [{
        "datasource_id": 31,
        "datasource_name": "批次",
        "datasource_identifier": "public._batch",
        "query_access": {
            "access_mode": "uns_get",
            "uns_path": "生产数仓/批次",
            "relation_expression": "uns_get('生产数仓/批次')",
        },
    }]

    retry = _validate_uns_get_access_contract(
        code='sql = "SELECT COUNT(*) FROM fqe_uns.public._batch"',
        selected_datasource_snapshot=snapshot,
    )

    assert retry is not None
    assert retry.diagnostics["violated_contract"] == "uns_get_access_required"
    assert _validate_uns_get_access_contract(
        code='sql = "SELECT COUNT(*) FROM uns_get(\'生产数仓/批次\')"',
        selected_datasource_snapshot=snapshot,
    ) is None


def test_static_contract_rejects_using_datasource_name_as_uns_table():
    snapshot = [{
        "datasource_id": 31,
        "datasource_name": "批次质量上下文",
        "datasource_identifier": "public._batch",
        "metadata_schema": {"name": "batch_quality_context"},
        "query_access": {
            "access_mode": "uns_get",
            "uns_path": "生产数仓/批次质量上下文",
            "relation_expression": "uns_get('生产数仓/批次质量上下文')",
        },
    }]

    retry = _validate_uns_get_access_contract(
        code='sql = "SELECT COUNT(*) FROM 批次质量上下文"',
        selected_datasource_snapshot=snapshot,
    )

    assert retry is not None
    assert retry.diagnostics["findings"][0]["used_bypass_tokens"] == [
        "批次质量上下文"
    ]
    assert _validate_uns_get_access_contract(
        code='analysis_report = "分析批次质量上下文"',
        selected_datasource_snapshot=snapshot,
    ) is None
