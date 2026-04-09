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
        self.base_url = Config.SUPOS_WEB
        self.auth_endpoint = Config.USER_AUTH_ENDPOINT
        self.request_timeout = Config.SUPOS_REQUEST_TIMEOUT

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
                        lake_rds_database_name=self._fetch_lake_rds_database_name(token),
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
        user_context = self.verify_token(auth_header)
        if not user_context:
            raise AuthError("无效的认证凭证")
        return user_context

    def _fetch_lake_rds_database_name(self, token: str) -> str:
        """
        初始化用户上下文时补充 LakeRDS 数据库名。
        这一步仅用于增强 UNS 导入能力；若第三方接口暂时不可用，不阻断认证主流程。
        """
        url = f"{self.base_url}/os/inter-api/fedquery/v1/databases"
        headers = {
            'Authorization': token,
        }
        params = {
            'pageSize': 100000,
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=self.request_timeout)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get('list') or []:
                if item.get('description') == 'LakeRDS' and item.get('name'):
                    return str(item.get('name'))
        except requests.RequestException as exc:
            logger.warn(f"获取 LakeRDS 数据库名失败: {exc}")
        except ValueError as exc:
            logger.warn(f"解析 LakeRDS 数据库响应失败: {exc}")
        return ''


# 全局单例
user_auth_service = UserAuthService()
