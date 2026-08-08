import pytest

from api.SuposkernelApi import SuposKernelApi
from agent import tools as runtime_tools
from dto import DatabaseConnInfo


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
