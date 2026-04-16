from uuid import uuid4

from flask import g, jsonify, request

from dto import UserContext
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


def _extract_auth_header() -> tuple[str, str]:
    cookie_token = (request.cookies.get('suposTicket') or '').strip()
    if cookie_token:
        return _normalize_bearer_token(cookie_token), 'cookie:suposTicket'

    header_token = (request.headers.get('Authorization', '') or '').strip()
    if header_token:
        return header_token, 'header:Authorization'

    return '', 'none'


def init_auth_middleware(app):
    """初始化认证中间件。"""

    @app.before_request
    def authenticate():
        """请求前置认证。"""
        g.request_id = request.headers.get('X-Request-ID', '').strip() or uuid4().hex
        auth_header, auth_source = _extract_auth_header()
        logger.info(
            "收到认证信息: path=%s source=%s auth_summary=%s",
            request.path,
            auth_source,
            _describe_auth_header(auth_header),
        )

        def _bind_optional_user_context() -> None:
            """匿名接口允许无 token；若前端带 token，则顺手绑定 UserContext。"""
            if not auth_header.startswith('Bearer '):
                return
            try:
                user_context: UserContext = user_auth_service.get_user_context(auth_header)
                g.user_context = user_context
            except Exception as exc:
                logger.warning("可选认证初始化失败: %s", exc)

        def _bind_required_user_context():
            """非匿名接口必须带合法 token。"""
            if not auth_header.startswith('Bearer '):
                return jsonify({
                    'code': 401,
                    'message': '缺少有效的认证凭证',
                }), 401

            try:
                user_context: UserContext = user_auth_service.get_user_context(auth_header)
                g.user_context = user_context
                logger.info("用户认证成功: %s", user_context.username)
                return None
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
