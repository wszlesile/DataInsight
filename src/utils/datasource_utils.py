import json
from typing import Any


DEFAULT_CONVERSATION_TITLE = '新建洞察'

DATASOURCE_TYPE_MAPPING = {
    'local_file': 'local_file',
    'minio_file': 'minio_file',
    'table': 'table',
    'api': 'api',
}

TABLE_ENGINE_RELATIONAL = 'relational'
TABLE_ENGINE_TIME_SERIES = 'time_series'
TABLE_ENGINE_VALUES = {TABLE_ENGINE_RELATIONAL, TABLE_ENGINE_TIME_SERIES}
TIME_SERIES_BACKEND_VALUES = {'timescale', 'vxbase'}


def safe_json_loads(value: Any, fallback: Any) -> Any:
    """安全反序列化 JSON，同时兼容直接传入 dict/list 的情况。"""
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def dump_json(value: Any) -> str:
    """使用 UTF-8 友好的方式序列化 JSON。"""
    return json.dumps(value, ensure_ascii=False)


def to_int(value: Any, default: int = 0) -> int:
    """把输入尽量转换为整数，失败时返回默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def recommend_local_file_loader(config_json: dict[str, Any], threshold_bytes: int) -> str:
    """根据落库文件大小推荐本地文件加载函数。"""
    file_size_bytes = to_int(config_json.get('file_size_bytes'), 0)
    normalized_threshold = max(to_int(threshold_bytes, 0), 0)
    if normalized_threshold > 0 and file_size_bytes >= normalized_threshold:
        return 'load_local_file_low_memory'
    return 'load_local_file'


def build_conversation_title(user_message: str) -> str:
    """根据最新一轮用户问题生成简短会话标题。"""
    text = (user_message or '').strip().replace('\n', ' ')
    if not text:
        return DEFAULT_CONVERSATION_TITLE
    return text[:40]


def normalize_datasource_type(datasource_type: Any) -> str:
    """
    规范化数据源类型枚举。

    当前仅允许 4 个取值：
    - local_file
    - minio_file
    - table
    - api
    """
    return DATASOURCE_TYPE_MAPPING.get(str(datasource_type or '').strip().lower(), 'unknown')


def resolve_table_engine(config_json: dict[str, Any] | None) -> str:
    """Resolve table sub-type; old table configs default to relational."""
    if not isinstance(config_json, dict):
        return TABLE_ENGINE_RELATIONAL
    value = str(config_json.get('table_engine') or '').strip().lower()
    if value in TABLE_ENGINE_VALUES:
        return value
    return TABLE_ENGINE_RELATIONAL


def normalize_ts_query_source(value: Any) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in TIME_SERIES_BACKEND_VALUES else ''


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        candidates = value
    elif value:
        candidates = [value]
    else:
        candidates = []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or '').strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def normalize_fedquery_database_name(value: Any) -> str:
    normalized = str(value or '').strip()
    if normalized.startswith('feq_'):
        return f"fqe_{normalized[4:]}"
    return normalized


def build_table_runtime_metadata(config_json: dict[str, Any] | None) -> dict[str, Any]:
    """Build prompt-safe metadata for table datasources with backwards compatibility."""
    config = config_json if isinstance(config_json, dict) else {}
    table_engine = resolve_table_engine(config)
    payload: dict[str, Any] = {
        'table_engine': table_engine,
    }
    if table_engine == TABLE_ENGINE_TIME_SERIES:
        backend = normalize_ts_query_source(config.get('ts_query_source'))
        value_fields = _normalize_string_list(config.get('value_fields') or [])
        physical_time_field = 'ts' if backend == 'vxbase' else 'timeStamp'
        payload.update({
            'ts_query_source': backend,
            # Existing datasource records may still carry timeStamp for vxbase.
            # Runtime metadata describes the physical query/result contract.
            'time_field': physical_time_field,
            'value_fields': value_fields,
            'quality_field': str(config.get('quality_field') or 'status').strip(),
        })
    return {key: value for key, value in payload.items() if value != ''}


def extract_datasource_identifier(datasource: Any, config_json: dict[str, Any]) -> str:
    """提取用于 Prompt 注入的数据源定位标识。"""
    datasource_type = normalize_datasource_type(getattr(datasource, 'datasource_type', ''))

    if datasource_type == 'local_file':
        return (
            config_json.get('file_path')
            or config_json.get('path')
            or getattr(datasource, 'datasource_name', '')
        )

    if datasource_type == 'minio_file':
        bucket = config_json.get('bucket') or config_json.get('bucket_name')
        object_name = config_json.get('object_name') or config_json.get('object')
        if bucket and object_name:
            return f"{bucket}/{object_name}"
        return (
            object_name
            or getattr(datasource, 'datasource_name', '')
        )

    if datasource_type == 'table':
        if resolve_table_engine(config_json) == TABLE_ENGINE_TIME_SERIES:
            return (
                config_json.get('uns_alias')
                or config_json.get('table_name')
                or config_json.get('identify')
                or getattr(datasource, 'datasource_name', '')
            )
        database_name = normalize_fedquery_database_name(config_json.get('database_name'))
        table_name = str(config_json.get('table_name') or '').strip()
        if str(config_json.get('uns_data_type') or '').strip() == '7':
            refer_alias = str(config_json.get('refer_alias') or '').strip()
            if refer_alias:
                return f"public.{refer_alias}"
        if str(config_json.get('uns_data_type') or '').strip() == '9' and database_name and table_name:
            return f"{database_name}.{table_name}"
        return (
            table_name
            or config_json.get('identify')
            or getattr(datasource, 'datasource_name', '')
        )

    if datasource_type == 'api':
        return (
            config_json.get('endpoint')
            or config_json.get('url')
            or getattr(datasource, 'datasource_name', '')
        )

    return getattr(datasource, 'datasource_name', '')


def extract_datasource_schema(datasource: Any, config_json: dict[str, Any]) -> dict[str, Any]:
    """优先从主字段读取 metadata_schema，缺失时回退到配置中的 schema。"""
    schema = safe_json_loads(getattr(datasource, 'datasource_schema', ''), {})
    if isinstance(schema, dict) and schema:
        return dict(schema)

    schema_from_config = config_json.get('schema')
    if isinstance(schema_from_config, dict) and schema_from_config:
        return dict(schema_from_config)

    return {}
