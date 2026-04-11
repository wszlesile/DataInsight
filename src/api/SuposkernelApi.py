import os
import re
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

import psycopg2
import requests
from sqlalchemy.pool import QueuePool

from config.config import Config
from dto import DatabaseContext
from utils import logger

KERNEL_TOKEN_PATH = Path("/var/run/secrets/supos.com/serviceaccount/token")

class SuposKernelApi:

    def __init__(self):
        self.supos_web = Config.SUPOS_WEB
        self.timeout = Config.SUPOS_REQUEST_TIMEOUT
        self._database_context = DatabaseContext()
        self._database_context_lock = Lock()
        self._database_pool: QueuePool | None = None
        self._database_pool_lock = Lock()

    def get_database_context(self, token: str | None = None) -> DatabaseContext:
        """
        获取系统级数据库连接上下文单例。

        第一次在拿到用户 token 后初始化；后续所有用户上下文都复用同一个实例。
        """
        if self._database_context.is_ready():
            return self._database_context
        if not token:
            return self._database_context

        with self._database_context_lock:
            if self._database_context.is_ready():
                return self._database_context
            self._initialize_database_context(token)
            return self._database_context

    def get_database_pool(self, token: str | None = None) -> QueuePool | None:
        """
        获取系统级 SQLAlchemy QueuePool 单例。

        这里复用 SQLAlchemy 的连接池能力，但不使用 PostgreSQL dialect 初始化探测，
        避免 FedQuery 方言与 `pg_catalog.version()` 等标准探测 SQL 不兼容。
        """
        database_context = self.get_database_context(token)
        if not database_context.is_ready():
            return None
        if self._database_pool is not None:
            return self._database_pool

        with self._database_pool_lock:
            if self._database_pool is not None:
                return self._database_pool
            self._database_pool = self._create_database_pool(database_context)
            return self._database_pool

    def query_dataframe(self, sql: str, params: list[Any] | None = None):
        """
        使用系统级连接池执行一条 FedQuery SQL，并返回 pandas DataFrame。

        这里统一收口连接借还、游标执行与结果装配逻辑，避免散落到工具层。
        """
        import pandas as pd

        columns, rows = self.query_rows(sql=sql, params=params)
        return pd.DataFrame(rows, columns=columns)

    def query_rows(self, sql: str, params: list[Any] | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
        """
        使用系统级连接池执行 SQL，并返回列名与结果行。

        时间类字段会在驱动层统一按字符串返回，确保上层 DataFrame 行为稳定一致。
        """
        database_context = self.get_database_context()
        if not database_context.is_ready():
            raise ValueError("数据库上下文尚未初始化，请先完成一次用户认证")

        with self.borrow_database_connection() as dbapi_connection:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(sql, params or None)
                rows = cursor.fetchall()
                columns = [desc[0] for desc in (cursor.description or [])]
                return columns, rows

    @contextmanager
    def borrow_database_connection(self):
        """从系统级连接池借出一个 DBAPI 连接，并在使用后归还。"""
        database_pool = self.get_database_pool()
        if database_pool is None:
            raise ValueError("数据库连接池尚未初始化，请先完成一次用户认证")

        pooled_connection = database_pool.connect()
        try:
            self._ping_dbapi_connection(pooled_connection)
            yield pooled_connection
        finally:
            pooled_connection.close()

    def _initialize_database_context(self, token: str) -> None:
        db_info = DatabaseContext()
        if Config.PROFILE == 'local':
            db_info.host = '192.168.19.228'
            db_info.port = '31432'
            db_info.user = 'fedquery'
            db_info.password = 'fedquery'
            db_info.lake_rds_database_name = 'fqe_ddc0ef5614'
        else:
            self._fill_kernel_database_context(token, db_info)
            db_info.lake_rds_database_name = self._fetch_lake_rds_database_name(token)

        self._database_context = db_info
        self._database_pool = None

    def _fill_kernel_database_context(self, token: str, db_info: DatabaseContext) -> None:
        url = f"{self.supos_web}/apis/ns/v1/supbase-ds/SQL/fedquery/fedquery_system"
        params = {"{kinds}": "datasources.supos.com;v1alpha1:DSource"}
        headers = {"Authorization": token}
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

    def _fetch_lake_rds_database_name(self, token: str) -> str:
        """补充当前系统默认使用的 LakeRDS 数据库名称。"""
        url = f"{self.supos_web}/os/inter-api/fedquery/v1/databases"
        headers = {
            "Authorization": token,
        }
        params = {
            "pageSize": 100000,
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("list") or []:
                if item.get("description") == "LakeRDS" and item.get("name"):
                    return str(item.get("name"))
        except requests.RequestException:
            logger.warn("[SuposKernel] 获取 LakeRDS 数据库名失败", exc_info=True)
        except ValueError:
            logger.warn("[SuposKernel] 解析 LakeRDS 数据库响应失败", exc_info=True)
        return ''

    def _create_database_pool(self, database_context: DatabaseContext) -> QueuePool:
        return QueuePool(
            creator=lambda: self._create_dbapi_connection(database_context),
            pool_size=5,
            max_overflow=10,
            recycle=1800,
            pre_ping=False,
        )

    def _create_dbapi_connection(self, database_context: DatabaseContext):
        dbapi_connection = psycopg2.connect(
            host=database_context.host,
            user=database_context.user,
            password=database_context.password,
            database=database_context.lake_rds_database_name,
            port=int(database_context.port),
        )
        self._register_time_text_casts(dbapi_connection)
        return dbapi_connection

    def _register_time_text_casts(self, dbapi_connection) -> None:
        """
        FedQuery 返回的时间列在驱动层解析不稳定，统一按字符串读取。

        这样上层工具总能拿到稳定的字符串列，再按业务需要显式转换。
        """
        try:
            from psycopg2.extensions import new_type, register_type

            time_as_text = new_type(
                (1082, 1083, 1114, 1115, 1184, 1185, 1266, 1270),
                "FEDQUERY_TIME_AS_TEXT",
                lambda value, _cursor: value,
            )
            register_type(time_as_text, dbapi_connection)
        except Exception:
            logger.warn("[SuposKernel] 注册时间字段文本转换器失败", exc_info=True)

    def _ping_dbapi_connection(self, dbapi_connection) -> None:
        """
        对池中连接执行一次轻量探活。

        这里不依赖 SQLAlchemy dialect，因此显式用 `SELECT 1` 代替 `pre_ping`。
        """
        cursor = None
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            if cursor is not None:
                cursor.close()

    def fetch_uns_instance_detail(self, node_id: str, authorization: str) -> dict[str, Any]:
        """查询单个 UNS 实例详情。"""
        if not node_id:
            raise ValueError("UNS node id 不能为空")
        if not authorization:
            raise ValueError("SUPOS authorization 不能为空")
        url = f"{self.supos_web}/inter-api/supos/uns/instance"
        headers = {
            "Authorization": authorization,
        }
        response = requests.get(url, headers=headers, params={"id": node_id}, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, 200):
            raise ValueError(payload.get("msg") or f"查询 UNS 实例详情失败: {node_id}")
        data = payload.get("data") or {}
        if not data:
            raise ValueError(f"未查询到 UNS 实例详情: {node_id}")
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
