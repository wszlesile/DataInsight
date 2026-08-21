from calendar import monthrange
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


RELATIVE_TIME_RANGE_KIND = 'relative_time_range'
_RELATIVE_TIME_UNITS = {
    'minute': 'minute',
    'minutes': 'minute',
    'min': 'minute',
    '分钟': 'minute',
    'hour': 'hour',
    'hours': 'hour',
    '小时': 'hour',
    '时': 'hour',
    'day': 'day',
    'days': 'day',
    '天': 'day',
    '日': 'day',
    'week': 'week',
    'weeks': 'week',
    '周': 'week',
    '星期': 'week',
    'month': 'month',
    'months': 'month',
    '月': 'month',
    '个月': 'month',
    'year': 'year',
    'years': 'year',
    '年': 'year',
}


def normalize_parameter_schema(value: Any) -> dict[str, Any]:
    schema = deepcopy(value) if isinstance(value, dict) else {}
    parameters = schema.get('parameters')
    defaults = schema.get('defaults')
    schema['parameters'] = parameters if isinstance(parameters, list) else []
    schema['defaults'] = defaults if isinstance(defaults, dict) else {}
    return schema


def strip_generated_runtime_parameter_metadata(
    schema: dict[str, Any],
    values: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove backend-owned runtime bindings from generated-code payloads."""
    normalized_schema = normalize_parameter_schema(schema)
    for item in normalized_schema['parameters']:
        if isinstance(item, dict):
            item.pop('runtime_default', None)
    _strip_nested_runtime_defaults(normalized_schema['defaults'])

    normalized_values = deepcopy(values) if isinstance(values, dict) else {}
    _strip_nested_runtime_defaults(normalized_values)
    return normalized_schema, normalized_values


def validate_analysis_parameters(schema: dict[str, Any], values: dict[str, Any] | None) -> dict[str, Any]:
    if values is not None and not isinstance(values, dict):
        raise ValueError('analysis_parameters 必须是 JSON 对象')
    normalized_schema = deepcopy(normalize_parameter_schema(schema))
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


def build_reusable_parameter_defaults(
    schema: dict[str, Any],
    concrete_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Preserve runtime-bound defaults while retaining ordinary concrete values."""
    normalized_schema = deepcopy(normalize_parameter_schema(schema))
    reusable = validate_analysis_parameters(normalized_schema, concrete_values or {})
    for item in normalized_schema['parameters']:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        runtime_default = item.get('runtime_default')
        if not name or not isinstance(runtime_default, dict):
            continue
        _normalize_relative_time_range(runtime_default, parameter_name=name)
        reusable[name] = dict(runtime_default)
    return reusable


def materialize_analysis_parameters(
    schema: dict[str, Any],
    values: dict[str, Any] | None,
    *,
    anchor_time: datetime | None = None,
) -> dict[str, Any]:
    """Resolve runtime-bound values once before executing persisted analysis code."""
    normalized = validate_analysis_parameters(schema, values)
    return {
        name: _materialize_parameter_value(value, anchor_time=anchor_time)
        for name, value in normalized.items()
    }


def attach_relative_time_runtime_default(
    schema: dict[str, Any],
    *,
    expected_time_range: dict[str, Any] | None,
    concrete_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach a reusable relative default when runtime context proves its semantics."""
    normalized_schema = deepcopy(normalize_parameter_schema(schema))
    expected = expected_time_range if isinstance(expected_time_range, dict) else {}
    if not expected.get('relative_window'):
        return normalized_schema
    amount = expected.get('amount')
    unit = _normalize_relative_unit(expected.get('unit'))
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0 or not unit:
        return normalized_schema

    concrete = concrete_values if isinstance(concrete_values, dict) else {}
    expected_start = str(expected.get('start') or '').strip()
    expected_end = str(expected.get('end') or '').strip()
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in normalized_schema['parameters']:
        if not isinstance(item, dict) or str(item.get('type') or '').strip().lower() != 'time_range':
            continue
        name = str(item.get('name') or '').strip()
        value = concrete.get(name)
        if not name or not isinstance(value, dict):
            continue
        if (
            str(value.get('start') or '').strip() == expected_start
            and str(value.get('end') or '').strip() == expected_end
        ):
            candidates.append((item, value))
    if len(candidates) != 1:
        return normalized_schema

    parameter, concrete_time_range = candidates[0]
    parameter['runtime_default'] = {
        'kind': RELATIVE_TIME_RANGE_KIND,
        'amount': amount,
        'unit': unit,
        'anchor': 'execution_time',
        'timezone': 'Asia/Shanghai',
        'end_exclusive': bool(concrete_time_range.get('end_exclusive', True)),
    }
    return normalized_schema


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


def _strip_nested_runtime_defaults(values: dict[str, Any]) -> None:
    for value in values.values():
        if isinstance(value, dict):
            value.pop('runtime_default', None)


def _validate_type(name: str, value: Any, parameter_type: str) -> None:
    if value is None or not parameter_type:
        return
    if parameter_type in {'string', 'text', 'date', 'datetime'} and not isinstance(value, str):
        raise ValueError(f'分析参数 {name} 必须是字符串')
    if parameter_type == 'time_range':
        if not isinstance(value, dict):
            raise ValueError(f'分析参数 {name} 必须是对象')
        if value.get('kind') == RELATIVE_TIME_RANGE_KIND:
            _normalize_relative_time_range(value, parameter_name=name)
    if parameter_type in {'number', 'integer'} and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ValueError(f'分析参数 {name} 必须是数字')
    if parameter_type in {'boolean', 'bool'} and not isinstance(value, bool):
        raise ValueError(f'分析参数 {name} 必须是布尔值')
    if parameter_type in {'array', 'list', 'multi_select'} and not isinstance(value, list):
        raise ValueError(f'分析参数 {name} 必须是数组')


def _materialize_parameter_value(value: Any, *, anchor_time: datetime | None) -> Any:
    if not isinstance(value, dict) or value.get('kind') != RELATIVE_TIME_RANGE_KIND:
        return value
    binding = _normalize_relative_time_range(value)
    timezone_name = binding['timezone']
    zone = ZoneInfo(timezone_name)
    anchor = anchor_time or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    end = anchor.astimezone(zone).replace(microsecond=0)
    start = _subtract_relative_window(end, binding['amount'], binding['unit'])
    return {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'end_exclusive': binding['end_exclusive'],
    }


def _normalize_relative_time_range(value: dict[str, Any], parameter_name: str = 'time_range') -> dict[str, Any]:
    if str(value.get('anchor') or 'execution_time').strip() != 'execution_time':
        raise ValueError(f'分析参数 {parameter_name} 的相对时间锚点仅支持 execution_time')
    amount = value.get('amount')
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise ValueError(f'分析参数 {parameter_name} 的相对时间长度必须大于 0')
    unit = _normalize_relative_unit(value.get('unit'))
    if not unit:
        raise ValueError(f'分析参数 {parameter_name} 的相对时间单位不受支持')
    timezone_name = str(value.get('timezone') or 'Asia/Shanghai').strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f'分析参数 {parameter_name} 的时区无效') from exc
    return {
        'kind': RELATIVE_TIME_RANGE_KIND,
        'amount': amount,
        'unit': unit,
        'anchor': 'execution_time',
        'timezone': timezone_name,
        'end_exclusive': bool(value.get('end_exclusive', True)),
    }


def _normalize_relative_unit(value: Any) -> str:
    return _RELATIVE_TIME_UNITS.get(str(value or '').strip(), '')


def _subtract_relative_window(anchor: datetime, amount: int | float, unit: str) -> datetime:
    if unit == 'minute':
        return anchor - timedelta(minutes=amount)
    if unit == 'hour':
        return anchor - timedelta(hours=amount)
    if unit == 'day':
        return anchor - timedelta(days=amount)
    if unit == 'week':
        return anchor - timedelta(weeks=amount)
    if not float(amount).is_integer():
        raise ValueError(f'{unit} 相对时间长度必须是整数')
    integer_amount = int(amount)
    if unit == 'month':
        month_index = anchor.year * 12 + anchor.month - 1 - integer_amount
        year, month_offset = divmod(month_index, 12)
        month = month_offset + 1
        return anchor.replace(year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))
    if unit == 'year':
        year = anchor.year - integer_amount
        return anchor.replace(year=year, day=min(anchor.day, monthrange(year, anchor.month)[1]))
    raise ValueError(f'不支持的相对时间单位: {unit}')
