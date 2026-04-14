import time
from typing import Optional

import requests

from api import supos_kernel_api
from config.config import Config
from dto import UserContext
from utils import logger


class AuthError(Exception):
    """认证异常。"""


class UserAuthService:
    """用户认证服务。"""

    def __init__(self):
        self.base_url = Config.SUPOS_WEB
        self.auth_endpoint = Config.USER_AUTH_ENDPOINT
        self.request_timeout = Config.SUPOS_REQUEST_TIMEOUT
        self.cache_ttl_seconds = Config.USER_CONTEXT_CACHE_TTL_SECONDS
        self._context_cache: dict[str, tuple[float, UserContext]] = {}

    def verify_token(self, token: str) -> Optional[UserContext]:
        """验证 Authorization 头并构造用户上下文。"""
        if not token:
            return None

        url = f"{self.base_url}{self.auth_endpoint}"
        headers = {'Authorization': token}
        logger.info(
            "开始认证校验: url=%s auth_summary=%s",
            url,
            self._describe_auth_header(token),
        )

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

                logger.warning(
                    "认证失败: auth_summary=%s message=%s",
                    self._describe_auth_header(token),
                    result.get('message', '未知错误'),
                )
                return None

            logger.warning(
                "认证请求失败: url=%s status=%s auth_summary=%s response_preview=%s",
                url,
                response.status_code,
                self._describe_auth_header(token),
                self._preview_response_text(response.text),
            )
            return None
        except requests.RequestException as exc:
            logger.error(
                "认证请求异常: url=%s auth_summary=%s error=%s",
                url,
                self._describe_auth_header(token),
                exc,
            )
            return None

    def get_user_context(self, auth_header: str) -> UserContext:
        """获取用户上下文；失败抛出认证异常。"""
        user_context = self._get_cached_user_context(auth_header)
        if user_context:
            return user_context

        user_context = self.verify_token(auth_header)
        if not user_context:
            raise AuthError("无效的认证凭证")
        self._set_cached_user_context(auth_header, user_context)
        return user_context

    def _get_cached_user_context(self, auth_header: str) -> UserContext | None:
        """按 Authorization 头缓存用户上下文，避免重复访问认证接口。"""
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

    @staticmethod
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

    @staticmethod
    def _preview_response_text(text: str, max_length: int = 200) -> str:
        normalized = (text or '').replace('\r', ' ').replace('\n', ' ').strip()
        if len(normalized) <= max_length:
            return normalized
        return f"{normalized[:max_length]}..."


user_auth_service = UserAuthService()
