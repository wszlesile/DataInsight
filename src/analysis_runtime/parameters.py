from typing import Any


def normalize_parameter_schema(value: Any) -> dict[str, Any]:
    schema = dict(value) if isinstance(value, dict) else {}
    parameters = schema.get('parameters')
    defaults = schema.get('defaults')
    schema['parameters'] = parameters if isinstance(parameters, list) else []
    schema['defaults'] = defaults if isinstance(defaults, dict) else {}
    return schema


def validate_analysis_parameters(schema: dict[str, Any], values: dict[str, Any] | None) -> dict[str, Any]:
    if values is not None and not isinstance(values, dict):
        raise ValueError('analysis_parameters 必须是 JSON 对象')
    normalized_schema = normalize_parameter_schema(schema)
    normalized = dict(normalized_schema['defaults'])
    normalized.update(values or {})

    for item in normalized_schema['parameters']:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        if bool(item.get('required')) and name not in normalized:
            raise ValueError(f'缺少必填分析参数: {name}')
        if name not in normalized:
            continue
        value = normalized[name]
        parameter_type = str(item.get('type') or '').strip().lower()
        _validate_type(name, value, parameter_type)
        options = item.get('options')
        if isinstance(options, list) and options:
            _validate_options(name, value, parameter_type, options)
    return normalized


def _validate_options(name: str, value: Any, parameter_type: str, options: list[Any]) -> None:
    if parameter_type in {'array', 'list', 'multi_select'}:
        if not isinstance(value, list):
            raise ValueError(f'分析参数 {name} 必须是数组')
        invalid_values = [item for item in value if item not in options]
        if invalid_values:
            raise ValueError(f'分析参数 {name} 不在允许选项内: {invalid_values[0]}')
        return
    if value not in options:
        raise ValueError(f'分析参数 {name} 不在允许选项内')


def _validate_type(name: str, value: Any, parameter_type: str) -> None:
    if value is None or not parameter_type:
        return
    if parameter_type in {'string', 'text', 'date', 'datetime'} and not isinstance(value, str):
        raise ValueError(f'分析参数 {name} 必须是字符串')
    if parameter_type == 'time_range' and not isinstance(value, dict):
        raise ValueError(f'分析参数 {name} 必须是对象')
    if parameter_type in {'number', 'integer'} and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ValueError(f'分析参数 {name} 必须是数字')
    if parameter_type in {'boolean', 'bool'} and not isinstance(value, bool):
        raise ValueError(f'分析参数 {name} 必须是布尔值')
    if parameter_type in {'array', 'list', 'multi_select'} and not isinstance(value, list):
        raise ValueError(f'分析参数 {name} 必须是数组')
