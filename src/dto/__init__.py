from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class PropertySchema(BaseModel):
    """描述单个元数据字段的结构定义。"""

    # 字段类型，例如 string、number、integer。
    type: str
    # 字段的业务含义说明，供 LLM 理解该字段用途。
    description: str
    # 可选示例值，用于帮助 LLM 更直观地理解字段数据形态。
    example: Any | None = None


class DataSourceSchema(BaseModel):
    """描述注入给 LLM 的数据源 metadata_schema 结构。"""

    # 当前元数据模型名称。
    name: str
    # 当前数据源结构的整体说明。
    description: str = ''
    # 字段定义集合，key 为字段名，value 为字段结构说明。
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    # 必填字段列表，帮助 LLM 识别关键分析字段。
    required: list[str] = Field(default_factory=list)


@dataclass
class DatabaseConnInfo:
    """Table/UNS 查询共用的数据库连接信息。"""

    host: str = ''
    port: str = ''
    user: str = ''
    password: str = ''
    lake_rds_database_name: str = ''

    def is_ready(self) -> bool:
        """是否已经拿到完整可用的数据库连接信息。"""
        return bool(
            self.host and self.port and self.user and self.password and self.lake_rds_database_name
        )


@dataclass
class UserContext:
    """当前请求对应的用户上下文。"""

    # 用户主键 ID。
    user_id: str
    # 登录用户名。
    username: str
    # 员工主键 ID。
    staff_id: str
    # 员工姓名。
    staff_name: str
    # 用户编码。
    user_code: str
    # 当前认证 token。
    token: str
    # 当前请求可见的数据库连接信息快照。
    database_conn_info: DatabaseConnInfo = field(default_factory=DatabaseConnInfo)

    @classmethod
    def from_auth_response(
        cls,
        data: dict,
        token: str,
        database_conn_info: DatabaseConnInfo | None = None,
    ) -> 'UserContext':
        """从认证接口响应中构造用户上下文。"""
        user_session = data.get('userSessionInfo', {})
        return cls(
            user_id=str(user_session.get('userId', '')),
            username=user_session.get('username', ''),
            staff_id=str(user_session.get('staffId', '')),
            staff_name=user_session.get('staffName', ''),
            user_code=data.get('userCode', ''),
            token=token,
            database_conn_info=database_conn_info or DatabaseConnInfo(),
        )


def get_current_user_context():
    """获取当前 Flask 请求绑定的用户上下文。"""
    from flask import g

    return getattr(g, 'user_context', None)


__all__ = [
    'DatabaseConnInfo',
    'UserContext',
    'get_current_user_context',
    'PropertySchema',
    'DataSourceSchema',
]
