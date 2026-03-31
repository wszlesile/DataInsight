import requests
from typing import Optional

from config.config import Config
from dto import UserContext
from utils import logger


class AuthError(Exception):
    """认证异常"""
    pass


class UserAuthService:
    """用户认证服务"""

    def __init__(self):
        self.base_url = Config.USER_SERVICE_URL
        self.auth_endpoint = Config.USER_AUTH_ENDPOINT

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
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 100000000:
                    data = result.get('data', {})
                    return UserContext.from_auth_response(data, token)
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
        user_context = self.verify_token(auth_header)
        if not user_context:
            raise AuthError("无效的认证凭证")
        return user_context


# 全局单例
user_auth_service = UserAuthService()
