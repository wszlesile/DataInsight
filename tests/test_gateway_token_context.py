import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from flask import Flask, jsonify


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dto import UserContext  # noqa: E402
from middleware.auth_middleware import init_auth_middleware  # noqa: E402
from api.SuposkernelApi import SuposKernelApi  # noqa: E402
from dto import DatabaseConnInfo  # noqa: E402
from service.user_auth_service import UserAuthService  # noqa: E402


def _user_context() -> UserContext:
    return UserContext(
        user_id='1',
        username='tester',
        staff_id='2',
        staff_name='Tester',
        user_code='tester',
        platform_authorization='Bearer platform-ticket',
        fedquery_authorization='Bearer fedquery-jwt',
    )


def _app() -> Flask:
    app = Flask(__name__)
    init_auth_middleware(app)

    @app.get('/api/protected')
    def protected():
        return jsonify({'ok': True})

    @app.get('/health')
    def health():
        return jsonify({'ok': True})

    return app


def test_protected_api_uses_origin_token_for_platform_and_authorization_for_fedquery():
    app = _app()
    with (
        patch('middleware.auth_middleware.platform_license_service.ensure_agent_authorized') as license_check,
        patch('middleware.auth_middleware.user_auth_service.get_user_context', return_value=_user_context()) as get_context,
    ):
        response = app.test_client().get(
            '/api/protected',
            headers={
                'Authorization': 'Bearer fedquery-jwt',
                'origin-token': 'Bearer platform-ticket',
            },
        )

    assert response.status_code == 200
    license_check.assert_called_once_with('Bearer platform-ticket')
    get_context.assert_called_once_with('Bearer platform-ticket', 'Bearer fedquery-jwt')


def test_protected_api_rejects_a_request_without_gateway_origin_token():
    app = _app()
    with patch('middleware.auth_middleware.user_auth_service.get_user_context') as get_context:
        response = app.test_client().get(
            '/api/protected',
            headers={'Authorization': 'Bearer fedquery-jwt'},
        )

    assert response.status_code == 401
    get_context.assert_not_called()


def test_local_api_uses_configured_gateway_jwt_for_matching_platform_ticket():
    app = _app()
    with (
        patch('middleware.auth_middleware.Config.PROFILE', 'local'),
        patch('middleware.auth_middleware.Config.LOCAL_ORIGIN_TOKEN', 'Bearer platform-ticket'),
        patch('middleware.auth_middleware.Config.LOCAL_GATEWAY_JWT', 'Bearer fedquery-jwt'),
        patch('middleware.auth_middleware.platform_license_service.ensure_agent_authorized') as license_check,
        patch('middleware.auth_middleware.user_auth_service.get_user_context', return_value=_user_context()) as get_context,
    ):
        response = app.test_client().get(
            '/api/protected',
            headers={'Authorization': 'Bearer platform-ticket'},
        )

    assert response.status_code == 200
    license_check.assert_called_once_with('Bearer platform-ticket')
    get_context.assert_called_once_with('Bearer platform-ticket', 'Bearer fedquery-jwt')


def test_local_api_rejects_a_ticket_that_does_not_match_local_configuration():
    app = _app()
    with (
        patch('middleware.auth_middleware.Config.PROFILE', 'local'),
        patch('middleware.auth_middleware.Config.LOCAL_ORIGIN_TOKEN', 'Bearer configured-ticket'),
        patch('middleware.auth_middleware.Config.LOCAL_GATEWAY_JWT', 'Bearer fedquery-jwt'),
        patch('middleware.auth_middleware.user_auth_service.get_user_context') as get_context,
    ):
        response = app.test_client().get(
            '/api/protected',
            headers={'Authorization': 'Bearer another-ticket'},
        )

    assert response.status_code == 401
    get_context.assert_not_called()


def test_production_api_does_not_use_local_dual_token_configuration():
    app = _app()
    with (
        patch('middleware.auth_middleware.Config.PROFILE', 'prod'),
        patch('middleware.auth_middleware.Config.LOCAL_ORIGIN_TOKEN', 'Bearer platform-ticket'),
        patch('middleware.auth_middleware.Config.LOCAL_GATEWAY_JWT', 'Bearer fedquery-jwt'),
        patch('middleware.auth_middleware.user_auth_service.get_user_context') as get_context,
    ):
        response = app.test_client().get(
            '/api/protected',
            headers={'Authorization': 'Bearer platform-ticket'},
        )

    assert response.status_code == 401
    get_context.assert_not_called()


def test_health_remains_anonymous_without_gateway_headers():
    app = _app()
    with patch('middleware.auth_middleware.user_auth_service.get_user_context') as get_context:
        response = app.test_client().get('/health')

    assert response.status_code == 200
    get_context.assert_not_called()


def test_database_connection_sets_only_fedquery_authorization():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    api = SuposKernelApi()
    conn_info = DatabaseConnInfo(
        host='127.0.0.1',
        port='5432',
        user='fedquery',
        password='secret',
        lake_rds_database_name='fqe_uns',
        authorization_token='Bearer fedquery-jwt',
    )

    with (
        patch('api.SuposkernelApi.psycopg2.connect', return_value=connection) as connect,
        patch.object(api, '_register_time_text_casts'),
    ):
        api._create_dbapi_connection(conn_info)

    connect.assert_called_once()
    cursor.execute.assert_called_once_with(
        'SET supos_authorization TO %s',
        ('Bearer fedquery-jwt',),
    )


def test_auth_transport_error_keeps_the_original_authorization_summary():
    service = UserAuthService()
    with patch('service.user_auth_service.requests.get', side_effect=requests.RequestException('offline')):
        assert service.verify_token('Bearer platform-ticket', 'Bearer fedquery-jwt') is None
