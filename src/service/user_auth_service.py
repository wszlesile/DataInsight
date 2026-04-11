import requests
import time
from typing import Optional

from api import supos_kernel_api
from config.config import Config
from dto import UserContext
from utils import logger


class AuthError(Exception):
    """认证异常"""
    pass


class UserAuthService:
    """用户认证服务"""

    def __init__(self):
        self.base_url = Config.SUPOS_WEB
        self.auth_endpoint = Config.USER_AUTH_ENDPOINT
        self.request_timeout = Config.SUPOS_REQUEST_TIMEOUT
        self.cache_ttl_seconds = Config.USER_CONTEXT_CACHE_TTL_SECONDS
        self._context_cache: dict[str, tuple[float, UserContext]] = {}

    def verify_token(self, token: str) -> Optional[UserContext]:
        """
        验证token并获取用户信息

        Args:
            token: 完整的Authorization头 (Bearer <token>)

        Returns:
            UserContext: 用户上下文，验证失败返回None
        """
        if not token:
            return None

        url = f"{self.base_url}{self.auth_endpoint}"
        headers = {
            'Authorization': token
        }

        try:
            response = requests.get(url, headers=headers, timeout=self.request_timeout)
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 100000000:
                    data = result.get('data', {})
                    return UserContext.from_auth_response(
                        data,
                        token,
                        database_context=supos_kernel_api.get_database_context(token),
                    )
                else:
                    logger.warn(f"认证失败: {result.get('message', '未知错误')}")
                    return None
            else:
                logger.warn(f"认证请求失败: HTTP {response.status_code}")
                return None
        except requests.RequestException as e:
            logger.error(f"认证请求异常: {e}")
            return None

    def get_user_context(self, auth_header: str) -> UserContext:
        """
        获取用户上下文，失败抛出AuthError

        Args:
            auth_header: 完整的Authorization头 (Bearer <token>)

        Returns:
            UserContext: 用户上下文

        Raises:
            AuthError: 认证失败
        """
        user_context = self._get_cached_user_context(auth_header)
        if user_context:
            return user_context

        user_context = self.verify_token(auth_header)
        if not user_context:
            raise AuthError("无效的认证凭证")
        self._set_cached_user_context(auth_header, user_context)
        return user_context

    def _get_cached_user_context(self, auth_header: str) -> UserContext | None:
        """按 Authorization 头缓存用户上下文，避免每次请求都访问 SUPOS 用户接口。"""
        if not auth_header or self.cache_ttl_seconds <= 0:
            return None

        cached = self._context_cache.get(auth_header)
        if not cached:
            return None

        expires_at, user_context = cached
        if expires_at <= time.time():
            self._context_cache.pop(auth_header, None)
            return None
        return user_context

    def _set_cached_user_context(self, auth_header: str, user_context: UserContext) -> None:
        if not auth_header or self.cache_ttl_seconds <= 0:
            return
        self._context_cache[auth_header] = (time.time() + self.cache_ttl_seconds, user_context)


# 全局单例
user_auth_service = UserAuthService()
