import pytest

from api.SuposkernelApi import SuposKernelApi
from agent import tools as runtime_tools
from dto import DatabaseConnInfo


@pytest.mark.parametrize('failure_phase', [None, 'connection', 'execute', 'fetch'])
def test_query_timing_preserves_results_errors_and_hides_query_material(monkeypatch, failure_phase):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import importlib
    module = importlib.import_module('api.SuposkernelApi')
    api = SuposKernelApi()
    monkeypatch.setattr(api, 'get_database_conn_info', lambda: _conn_info())
    error = RuntimeError('original failure')
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [('amount',)]
    cursor.fetchall.return_value = [(7,)]
    if failure_phase == 'execute':
        cursor.execute.side_effect = error
    if failure_phase == 'fetch':
        cursor.fetchall.side_effect = error
    @contextmanager
    def borrowed():
        if failure_phase == 'connection':
            raise error
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(api, 'borrow_database_connection', borrowed)
    log = MagicMock()
    monkeypatch.setattr(module.logger, 'info', log)
    sql = "SELECT private_column FROM secret_table WHERE account = %s"
    if failure_phase:
        with pytest.raises(RuntimeError) as caught:
            api.query_rows(sql, ['private account'])
        assert caught.value is error
    else:
        assert api.query_rows(sql, ['private account']) == (['amount'], [(7,)])
    template, *arguments = log.call_args.args
    rendered = template % tuple(arguments)
    assert 'status=' + ('failed' if failure_phase else 'success') in rendered
    assert 'phase=' + (failure_phase or 'fetch') in rendered
    assert 'sql_hash=' in rendered
    assert 'secret_table' not in rendered
    assert 'private account' not in rendered
    assert 'original failure' not in rendered


class FakeHttpResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeCursor:
    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor, commit_error=None):
        self._cursor = cursor
        self._commit_error = commit_error
        self.closed = False
        self.commit_count = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commit_count += 1
        if self._commit_error is not None:
            raise self._commit_error

    def close(self):
        self.closed = True


def _conn_info(token=""):
    return DatabaseConnInfo(
        host="fedquery",
        port="15432",
        user="fedquery",
        password="fedquery",
        lake_rds_database_name="fqe_uns",
        authorization_token=token,
    )


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("jwt-token", "Bearer jwt-token"),
        ("Bearer jwt-token", "Bearer jwt-token"),
    ],
)
def test_database_connection_sets_request_authorization(
    monkeypatch,
    token,
    expected,
):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(
        "api.SuposkernelApi.psycopg2.connect",
        lambda **kwargs: connection,
    )
    api = SuposKernelApi()
    monkeypatch.setattr(api, "_register_time_text_casts", lambda conn: None)

    result = api._create_dbapi_connection(_conn_info(token))

    assert result is connection
    assert cursor.calls == [
        ("SET supos_authorization TO %s", (expected,)),
    ]
    assert cursor.closed
    assert connection.commit_count == 1
    assert not connection.closed


def test_database_connection_without_authorization_skips_set(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(
        "api.SuposkernelApi.psycopg2.connect",
        lambda **kwargs: connection,
    )
    api = SuposKernelApi()
    monkeypatch.setattr(api, "_register_time_text_casts", lambda conn: None)

    api._create_dbapi_connection(_conn_info())

    assert cursor.calls == []
    assert connection.commit_count == 0


def test_local_database_connection_uses_configured_fedquery_target(monkeypatch):
    monkeypatch.setattr('api.SuposkernelApi.Config.PROFILE', 'local')
    monkeypatch.setattr('api.SuposkernelApi.Config.SUPOS_FEDQUERY_HOST', '127.0.0.1')
    monkeypatch.setattr('api.SuposkernelApi.Config.SUPOS_FEDQUERY_PORT', '16432')
    monkeypatch.setattr('api.SuposkernelApi.Config.SUPOS_FEDQUERY_USERNAME', 'local-user')
    monkeypatch.setattr('api.SuposkernelApi.Config.SUPOS_FEDQUERY_PASSWORD', 'local-password')
    monkeypatch.setattr('api.SuposkernelApi.Config.SUPOS_FEDQUERY_DATABASE', 'postgres')
    monkeypatch.setenv('SUPOS_TS_QUERY_SOURCE', 'vxbase')
    api = SuposKernelApi()

    api._initialize_database_conn_info('Bearer platform-ticket')

    info = api._database_conn_info
    assert info.host == '127.0.0.1'
    assert info.port == '16432'
    assert info.user == 'local-user'
    assert info.password == 'local-password'
    assert info.lake_rds_database_name == 'postgres'
    assert info.ts_query_source == 'vxbase'


def test_platform_application_installation_uses_exact_kernel_resource(monkeypatch):
    captured = {}

    def fake_get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return FakeHttpResponse(200, {
            '$kind': 'apps.supos.com;v1alpha1:Application',
            '$metadata': {
                'name': 'supos-rag-semantic-center',
                'namespace': '/system/apps/toolkit.supos-rag-semantic-center',
            },
            'group': 'toolkit',
        })

    monkeypatch.setattr('api.SuposkernelApi.requests.get', fake_get)
    api = SuposKernelApi()
    api.supos_gateway = 'http://platform-base-gateway:8000'

    assert api.is_platform_application_installed(
        group='toolkit',
        name='supos-rag-semantic-center',
        authorization='service-app-key',
    )
    assert captured['url'].endswith(
        '/apis/ns/v1/system/apps/'
        'toolkit.supos-rag-semantic-center/supos-rag-semantic-center'
    )
    assert captured['headers'] == {'Authorization': 'Bearer service-app-key'}


def test_platform_application_installation_treats_not_found_as_not_installed(monkeypatch):
    monkeypatch.setattr(
        'api.SuposkernelApi.requests.get',
        lambda *args, **kwargs: FakeHttpResponse(404),
    )

    assert not SuposKernelApi().is_platform_application_installed(
        group='toolkit',
        name='supos-rag-semantic-center',
        authorization='Bearer service-app-key',
    )


def test_database_authorization_failure_closes_connection(monkeypatch):
    cursor = FakeCursor(RuntimeError("invalid authorization"))
    connection = FakeConnection(cursor)
    monkeypatch.setattr(
        "api.SuposkernelApi.psycopg2.connect",
        lambda **kwargs: connection,
    )
    api = SuposKernelApi()
    monkeypatch.setattr(api, "_register_time_text_casts", lambda conn: None)

    with pytest.raises(RuntimeError, match="invalid authorization"):
        api._create_dbapi_connection(_conn_info("jwt-token"))

    assert cursor.closed
    assert connection.closed


def test_database_authorization_commit_failure_closes_connection(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor, commit_error=RuntimeError("commit failed"))
    monkeypatch.setattr(
        "api.SuposkernelApi.psycopg2.connect",
        lambda **kwargs: connection,
    )
    api = SuposKernelApi()
    monkeypatch.setattr(api, "_register_time_text_casts", lambda conn: None)

    with pytest.raises(RuntimeError, match="commit failed"):
        api._create_dbapi_connection(_conn_info("jwt-token"))

    assert cursor.closed
    assert connection.closed


def test_worker_runtime_binds_current_request_token(monkeypatch):
    original_info = runtime_tools.supos_kernel_api._database_conn_info
    original_pool = runtime_tools.supos_kernel_api._database_pool
    monkeypatch.setattr(
        runtime_tools.supos_kernel_api,
        "_database_conn_info",
        original_info,
    )
    monkeypatch.setattr(runtime_tools.supos_kernel_api, "_database_pool", original_pool)

    runtime_tools._prime_worker_runtime_context({
        "token": "Bearer request-jwt",
        "database_conn_info": {
            "host": "fedquery",
            "port": "15432",
            "user": "fedquery",
            "password": "fedquery",
            "lake_rds_database_name": "fqe_uns",
        },
    })

    assert (
        runtime_tools.supos_kernel_api._database_conn_info.authorization_token
        == "Bearer request-jwt"
    )
    assert runtime_tools.supos_kernel_api._database_pool is None
