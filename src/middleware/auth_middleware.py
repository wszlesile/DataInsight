from uuid import uuid4

from flask import request, g, jsonify

from dto import UserContext
from service.user_auth_service import user_auth_service, AuthError
from utils import logger


# 允许匿名访问的路径
ANONYMOUS_PATHS = ['/health', '/files/', '/api/']


def init_auth_middleware(app):
    """初始化认证中间件"""

    @app.before_request
    def authenticate():
        """请求前置认证"""
        g.request_id = request.headers.get('X-Request-ID', '').strip() or uuid4().hex
        auth_header = request.headers.get('Authorization', '')

        def _bind_optional_user_context() -> None:
            """匿名接口允许无 token；如果前端带了 token，则顺手绑定 UserContext。"""
            if not auth_header.startswith('Bearer '):
                return
            try:
                user_context: UserContext = user_auth_service.get_user_context(auth_header)
                g.user_context = user_context
            except Exception as exc:
                logger.warn(f"可选认证初始化失败: {exc}")

        def _bind_required_user_context():
            """非匿名接口必须有合法 token。"""
            if not auth_header.startswith('Bearer '):
                return jsonify({
                    'code': 401,
                    'message': '缺少有效的认证凭证'
                }), 401

            try:
                user_context: UserContext = user_auth_service.get_user_context(auth_header)
                g.user_context = user_context
                logger.info(f"用户认证成功: {user_context.username}")
                return None
            except AuthError as e:
                logger.warn(f"用户认证失败: {e}")
                return jsonify({
                    'code': 401,
                    'message': str(e)
                }), 401
            except Exception as e:
                logger.error(f"认证异常: {e}")
                return jsonify({
                    'code': 500,
                    'message': '认证服务异常'
                }), 500

        # 匿名路径走可选认证，避免破坏现有 anonymous 访问链路。
        if any(request.path.startswith(path) for path in ANONYMOUS_PATHS):
            _bind_optional_user_context()
            return None

        return _bind_required_user_context()

    @app.after_request
    def attach_request_id(response):
        response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
        return response
