import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from config.config import Config
from utils import logger

KERNEL_TOKEN_PATH = Path("/var/run/secrets/supos.com/serviceaccount/token")


@dataclass
class DatabaseInfo:
    """SUPOS Kernel 返回的数据库连接信息。"""

    host: str = ''
    port: str = ''
    user: str = ''
    password: str = ''
    database: str = ''


class SuposKernelApi:
    """
    SUPOS Kernel Server 访问封装。

    当前职责与 Java 版本保持一致：
    - 从 SUPOS_WEB 解析默认 host
    - 读取 kernel token
    - 访问 kernel server 获取数据库连接信息
    - 通过 SUPOS fedquery 查询字段枚举值
    """

    def __init__(self):
        self.supos_web = Config.SUPOS_WEB
        self.kernel_host = Config.SUPOS_KERNEL_HOST
        self.kernel_port = Config.SUPOS_KERNEL_PORT
        self.kernel_token = Config.SUPOS_KERNEL_TOKEN
        self.timeout = Config.SUPOS_REQUEST_TIMEOUT

    def parse_supos_ip(self) -> str:
        """
        从 SUPOS_WEB 解析 IP 或主机名。

        例如：
        - http://192.168.19.226:30000 -> 192.168.19.226
        - https://supos.example.com -> supos.example.com
        """
        if not self.supos_web:
            return ''

        try:
            match = re.search(r"://([^:/]+)", self.supos_web)
            if match:
                return match.group(1)
        except Exception:
            logger.warn("[SuposKernel] 从 SUPOS_WEB 解析 IP 失败", exc_info=True)
        return ''

    def get_kernel_token(self) -> str:
        """
        获取 Kernel Token。

        优先级：
        1. K8s 挂载 token 文件
        2. 配置中的 SUPOS_KERNEL_TOKEN
        """
        if KERNEL_TOKEN_PATH.exists():
            try:
                token = KERNEL_TOKEN_PATH.read_text(encoding='utf-8').strip()
                if token:
                    logger.debug("[SuposKernel] 从文件读取 kernel token 成功")
                    return token
            except OSError:
                logger.warn("[SuposKernel] 读取 kernel token 文件失败", exc_info=True)

        if self.kernel_token:
            logger.debug("[SuposKernel] 使用配置的 kernel token")
            return self.kernel_token.strip()

        logger.debug("[SuposKernel] 未配置 kernel token")
        return ''

    def get_kernel_server(self) -> str:
        """获取 Kernel Server 基础地址。"""
        if self.kernel_host and self.kernel_port:
            return f"http://{self.kernel_host}:{self.kernel_port}"
        logger.debug("[SuposKernel] Kernel Server 未配置")
        return ''

    def get_database_info(self) -> DatabaseInfo:
        """
        从 Kernel Server 获取数据库连接信息。

        若当前环境没有完整的 kernel 配置，则至少回退返回从 SUPOS_WEB 推断出的 host。
        """
        db_info = DatabaseInfo(host=self.parse_supos_ip())
        kernel_server = self.get_kernel_server()
        token = self.get_kernel_token()

        if not kernel_server or not token:
            logger.info(
                "[SuposKernel] Kernel Server 或 Token 未配置，返回基础 host 信息。kernel_server=%s token_empty=%s",
                "未配置" if not kernel_server else kernel_server,
                not bool(token),
            )
            return db_info

        url = f"{kernel_server}/apis/ns/v1/supbase-ds/SQL/fedquery/fedquery_system"
        params = {"{kinds}": "datasources.supos.com;v1alpha1:DSource"}
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            logger.debug("[SuposKernel] kernel response: %s", data)

            if (
                isinstance(data, dict)
                and data.get("ip")
                and data.get("port")
                and isinstance(data.get("spec"), dict)
            ):
                spec = data["spec"]
                if spec.get("username") and spec.get("password"):
                    db_info.host = str(data.get("ip") or db_info.host or '')
                    db_info.port = str(data.get("port") or '')
                    db_info.user = str(spec.get("username") or '')
                    db_info.password = str(spec.get("password") or '')
                    logger.info("[SuposKernel] 成功获取数据库连接信息")
        except requests.RequestException:
            logger.warn("[SuposKernel] 获取 database_info 失败", exc_info=True)
        except ValueError:
            logger.warn("[SuposKernel] 解析 database_info 响应失败", exc_info=True)

        return db_info

    def query_enum_values(
        self,
        database_name: str,
        schema_name: str,
        table_name: str,
        field_name: str,
        supos_token: str,
    ) -> list[str] | None:
        """
        查询维度字段的枚举值。

        参数语义与 Java 版本保持一致：
        - database_name: 对应 fedquery 中的 database_from
        - schema_name: 对应 fedquery 中的 database_name
        """
        if not self.supos_web:
            logger.warn("[SuposKernel] SUPOS_WEB 未配置")
            return None
        if not supos_token:
            logger.warn("[SuposKernel] SUPOS Token 未提供")
            return None
        if not all([database_name, schema_name, table_name, field_name]):
            logger.warn("[SuposKernel] 查询枚举值参数不完整")
            return None

        sql = (
            f"select distinct {field_name} "
            f"from {database_name}.{schema_name}.{table_name} "
            f"limit 500"
        )
        url = f"{self.supos_web}/os/inter-api/fedquery/v1/command/exucute/"
        headers = {
            "Content-Type": "text/plain",
            "Authorization": f"Bearer {supos_token}",
        }

        try:
            response = requests.post(url, data=sql.encode("utf-8"), headers=headers, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()

            if payload.get("code") != 0:
                logger.warn(
                    "[SuposKernel] 查询枚举值失败，code=%s, message=%s",
                    payload.get("code"),
                    payload.get("message", ""),
                )
                return None

            rows = payload.get("rows") or []
            enum_values: list[str] = []
            for row in rows:
                if isinstance(row, list) and row:
                    enum_values.append(str(row[0]))

            logger.debug(
                "[SuposKernel] 查询到 %s 个枚举值 for %s.%s.%s.%s",
                len(enum_values),
                database_name,
                schema_name,
                table_name,
                field_name,
            )
            return enum_values
        except requests.RequestException:
            logger.warn(
                "[SuposKernel] 查询枚举值请求失败: %s.%s.%s.%s",
                database_name,
                schema_name,
                table_name,
                field_name,
                exc_info=True,
            )
        except ValueError:
            logger.warn(
                "[SuposKernel] 解析枚举值响应失败: %s.%s.%s.%s",
                database_name,
                schema_name,
                table_name,
                field_name,
                exc_info=True,
            )
        return None

    def fetch_fedquery_databases(self, authorization: str) -> list[dict[str, Any]]:
        """获取当前用户在 fedquery 下可见的数据库列表。"""
        if not authorization:
            return []
        url = f"{self.supos_web}/os/inter-api/fedquery/v1/databases"
        headers = {
            "Authorization": authorization,
        }
        params = {
            "pageSize": 100000,
        }
        response = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload.get("list") or []

    def fetch_uns_file_detail(self, alias: str, authorization: str) -> dict[str, Any]:
        """查询单个 UNS 文件节点详情。"""
        if not alias:
            raise ValueError("UNS alias 不能为空")
        if not authorization:
            raise ValueError("SUPOS authorization 不能为空")
        url = f"{self.supos_web}/os/open-api/uns/file/{alias}"
        headers = {
            "Authorization": authorization,
        }
        response = requests.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise ValueError(payload.get("msg") or f"查询 UNS 节点详情失败: {alias}")
        data = payload.get("data") or {}
        if not data:
            raise ValueError(f"未查询到 UNS 节点详情: {alias}")
        return data

    def fetch_uns_tree_nodes(
        self,
        authorization: str,
        parent_id: str = '0',
        page_no: int = 1,
        page_size: int = 100,
        keyword: str = '',
        search_type: int = 1,
    ) -> dict[str, Any]:
        """查询 UNS 树节点列表。"""
        if not authorization:
            raise ValueError("SUPOS authorization 不能为空")
        url = f"{self.supos_web}/inter-api/supos/uns/condition/tree"
        headers = {
            "Authorization": authorization,
        }
        payload = {
            "parentId": str(parent_id or '0'),
            "pageNo": int(page_no or 1),
            "pageSize": int(page_size or 100),
            "keyword": keyword or '',
            "searchType": int(search_type or 1),
        }
        response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 200:
            raise ValueError(data.get("msg") or "查询 UNS 树失败")
        return data


supos_kernel_api = SuposKernelApi()
