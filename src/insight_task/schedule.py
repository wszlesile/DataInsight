import calendar
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_FREQUENCIES = {'manual', 'daily', 'weekdays', 'weekly', 'monthly'}


def utc_now() -> datetime:
    return datetime.utcnow()


def normalize_schedule(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, dict) else {}
    frequency = str(raw.get('frequency') or 'manual').strip().lower()
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ValueError('任务频率仅支持 manual、daily、weekdays、weekly 或 monthly')
    timezone_name = str(raw.get('timezone') or 'Asia/Shanghai').strip()
    _load_timezone(timezone_name)
    normalized: dict[str, Any] = {'frequency': frequency, 'timezone': timezone_name}
    if frequency == 'manual':
        return normalized

    hour, minute = _parse_clock(str(raw.get('time') or '09:00'))
    normalized['time'] = f'{hour:02d}:{minute:02d}'
    if frequency == 'weekly':
        weekdays = sorted({int(item) for item in (raw.get('weekdays') or []) if str(item).isdigit()})
        if not weekdays or any(item < 1 or item > 7 for item in weekdays):
            raise ValueError('每周任务至少选择一个星期，取值范围为 1 到 7')
        normalized['weekdays'] = weekdays
    elif frequency == 'weekdays':
        normalized['weekdays'] = [1, 2, 3, 4, 5]
    elif frequency == 'monthly':
        raw_day_of_month = raw.get('day_of_month', 1)
        if raw_day_of_month in (None, ''):
            raw_day_of_month = 1
        try:
            day_of_month = int(raw_day_of_month)
        except (TypeError, ValueError) as exc:
            raise ValueError('每月执行日期必须是 1 到 31 之间的整数') from exc
        if day_of_month < 1 or day_of_month > 31:
            raise ValueError('每月执行日期必须是 1 到 31 之间的整数')
        normalized['day_of_month'] = day_of_month
    return normalized


def next_run_after(schedule: dict[str, Any], after_utc: datetime | None = None) -> datetime | None:
    normalized = normalize_schedule(schedule)
    if normalized['frequency'] == 'manual':
        return None
    tz = _load_timezone(normalized['timezone'])
    base_utc = (after_utc or utc_now()).replace(tzinfo=timezone.utc)
    local_after = base_utc.astimezone(tz)
    hour, minute = _parse_clock(normalized['time'])
    weekdays = normalized.get('weekdays')

    if normalized['frequency'] == 'monthly':
        requested_day = normalized['day_of_month']
        for month_offset in range(0, 24):
            absolute_month = local_after.year * 12 + local_after.month - 1 + month_offset
            year, zero_based_month = divmod(absolute_month, 12)
            month = zero_based_month + 1
            last_day = calendar.monthrange(year, month)[1]
            candidate_local = datetime(
                year,
                month,
                min(requested_day, last_day),
                hour,
                minute,
                tzinfo=tz,
            )
            if candidate_local > local_after:
                return candidate_local.astimezone(timezone.utc).replace(tzinfo=None)
        raise ValueError('无法计算下一次任务执行时间')

    for day_offset in range(0, 15):
        candidate_date = local_after.date() + timedelta(days=day_offset)
        if normalized['frequency'] in {'weekly', 'weekdays'} and candidate_date.isoweekday() not in weekdays:
            continue
        candidate_local = datetime.combine(candidate_date, time(hour=hour, minute=minute), tzinfo=tz)
        if candidate_local <= local_after:
            continue
        return candidate_local.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError('无法计算下一次任务执行时间')


def _parse_clock(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.strip().split(':', 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError('执行时间必须使用 HH:mm 格式') from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('执行时间必须使用有效的 HH:mm 格式')
    return hour, minute


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f'不支持的时区: {name}') from exc
