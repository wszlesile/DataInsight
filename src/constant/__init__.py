from enum import Enum


class SchemaType(Enum):
    """数据源类型标识"""
    LOCAL_FILE = 1  # 本地文件
    MINIO = 2  # MinIO
    DATABASE = 3  # 数据库
    API = 4  # API


class SchemaPropertyType(Enum):
    """字段类型"""
    STRING = "string"
    INTEGER = "integer"
    DOUBLE = "double"
    BOOLEAN = "boolean"