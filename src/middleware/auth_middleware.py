import hmac
from uuid import uuid4

from flask import g, jsonify, request

from config.config import Config
from dto import UserContext
from service.platform_license_service import (
    LicenseExpiredError,
    LicenseUnauthorizedError,
    LicenseUnavailableError,
    platform_license_service,
)
from service.user_auth_service import AuthError, user_auth_service
from utils import logger


ANONYMOUS_PATHS = ['/health', '/files/']


def _describe_auth_header(auth_header: str) -> str:
    value = (auth_header or '').strip()
    if not value:
        return 'empty'
    prefix = value[:24]
    return (
        f"len={len(value)} "
        f"bearer={value.startswith('Bearer ')} "
        f"prefix={prefix!r}"
    )


def _normalize_bearer_token(raw_token: str) -> str:
    value = (raw_token or '').strip()
    if not value:
        return ''
    return value if value.startswith('Bearer ') else f'Bearer {value}'


def _resolve_local_authorizations(request_authorization: str) -> tuple[str, str] | None:
    if str(Config.PROFILE or '').strip().lower() != 'local':
        return None

    platform_authorization = _normalize_bearer_token(Config.LOCAL_ORIGIN_TOKEN)
    fedquery_authorization = _normalize_bearer_token(Config.LOCAL_GATEWAY_JWT)
    supplied_authorization = _normalize_bearer_token(request_authorization)
    if not platform_authorization or not fedquery_authorization or not supplied_authorization:
        return None
    if not hmac.compare_digest(supplied_authorization, platform_authorization):
        return None
    return fedquery_authorization, platform_authorization


def _extract_authorizations() -> tuple[str, str, str]:
    """Return gateway JWT, original platform ticket, and their source summary."""
    fedquery_authorization = (request.headers.get('Authorization', '') or '').strip()
    platform_authorization = (request.headers.get('origin-token', '') or '').strip()
    if fedquery_authorization and platform_authorization:
        return fedquery_authorization, platform_authorization, 'gateway:Authorization+origin-token'

    local_authorizations = _resolve_local_authorizations(fedquery_authorization)
    if local_authorizations:
        local_fedquery_authorization, local_platform_authorization = local_authorizations
        return local_fedquery_authorization, local_platform_authorization, 'local:configured-dual-token'

    if fedquery_authorization:
        return fedquery_authorization, '', 'header:Authorization'

    cookie_token = (request.cookies.get('suposTicket') or '').strip()
    if cookie_token:
        normalized_cookie_token = _normalize_bearer_token(cookie_token)
        local_authorizations = _resolve_local_authorizations(normalized_cookie_token)
        if local_authorizations:
            local_fedquery_authorization, local_platform_authorization = local_authorizations
            return local_fedquery_authorization, local_platform_authorization, 'local:configured-cookie-token'
        return '', normalized_cookie_token, 'cookie:suposTicket'

    return '', '', 'none'


def init_auth_middleware(app):
    """初始化认证中间件。"""

    @app.before_request
    def authenticate():
        """请求前置认证。"""
        g.request_id = request.headers.get('X-Request-ID', '').strip() or uuid4().hex
        fedquery_authorization, platform_authorization, auth_source = _extract_authorizations()
        logger.info(
            "收到认证信息: path=%s source=%s auth_summary=%s",
            request.path,
            auth_source,
            _describe_auth_header(fedquery_authorization),
        )

        def _bind_optional_user_context() -> None:
            """匿名接口允许无 token；若前端带 token，则顺手绑定 UserContext。"""
            if not platform_authorization.startswith('Bearer '):
                return
            try:
                user_context: UserContext = user_auth_service.get_user_context(
                    platform_authorization,
                    fedquery_authorization,
                )
                g.user_context = user_context
            except Exception as exc:
                logger.warning("可选认证初始化失败: %s", exc)

        def _bind_required_user_context():
            """非匿名接口必须带合法 token。"""
            if not fedquery_authorization.startswith('Bearer ') or not platform_authorization.startswith('Bearer '):
                return jsonify({
                    'code': 401,
                    'message': '缺少有效的认证凭证',
                }), 401

            try:
                platform_license_service.ensure_agent_authorized(platform_authorization)
                user_context: UserContext = user_auth_service.get_user_context(
                    platform_authorization,
                    fedquery_authorization,
                )
                g.user_context = user_context
                logger.info("用户认证成功: %s", user_context.username)
                return None
            except (LicenseUnauthorizedError, LicenseExpiredError) as exc:
                logger.warning("平台授权校验失败: %s", exc)
                return jsonify({
                    'code': 403,
                    'message': str(exc),
                }), 403
            except LicenseUnavailableError as exc:
                logger.error("平台授权服务异常: %s", exc)
                return jsonify({
                    'code': 500,
                    'message': str(exc),
                }), 500
            except AuthError as exc:
                logger.warning("用户认证失败: %s", exc)
                return jsonify({
                    'code': 401,
                    'message': str(exc),
                }), 401
            except Exception as exc:
                logger.error("认证异常: %s", exc)
                return jsonify({
                    'code': 500,
                    'message': '认证服务异常',
                }), 500

        if any(request.path.startswith(path) for path in ANONYMOUS_PATHS):
            _bind_optional_user_context()
            return None

        return _bind_required_user_context()

    @app.after_request
    def attach_request_id(response):
        response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
        return response
