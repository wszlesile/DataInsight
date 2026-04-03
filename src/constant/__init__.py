from enum import Enum


class DatasourceType(Enum):
    """Business-level datasource categories used by the backend."""

    FILE = 'file'
    TABLE = 'table'
    API = 'api'


class DatasourcePropertyType(Enum):
    """Supported metadata-schema property types."""

    STRING = "string"
    INTEGER = "integer"
    DOUBLE = "double"
    BOOLEAN = "boolean"
