"""Stable time selections, resolved once when a query is executed."""
import calendar
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONES = ('Europe/Moscow', 'UTC')
PRESETS = {
    'Последние 5 минут': ('now-5m', 'now'),
    'Последние 15 минут': ('now-15m', 'now'),
    'Последние 30 минут': ('now-30m', 'now'),
    'Последний час': ('now-1h', 'now'),
    'Последние 3 часа': ('now-3h', 'now'),
    'Последние 6 часов': ('now-6h', 'now'),
    'Последние 12 часов': ('now-12h', 'now'),
    'Последние 24 часа': ('now-24h', 'now'),
    'Последние 2 дня': ('now-2d', 'now'),
    'Последние 7 дней': ('now-7d', 'now'),
    'Последние 30 дней': ('now-30d', 'now'),
    'Сегодня': ('now/d', 'now'),
    'Вчера': ('now-1d/d', 'now-1d/d'),
}
CUSTOM = 'Свой диапазон'
RELATIVE = re.compile(r'now(?:[+-]\d+[smhdwMy])*(?:/[smhdwMy])?')


def utc_now():
    return datetime.now(UTC)


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Неизвестный часовой пояс.') from None


def _months(value, count):
    year, month = divmod(value.year * 12 + value.month - 1 + count, 12)
    month += 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def _shift(value, count, unit):
    if unit in {'M', 'y'}:
        return _months(value, count * (12 if unit == 'y' else 1))
    if unit in {'s', 'm', 'h'}:
        seconds = count * {'s': 1, 'm': 60, 'h': 3600}[unit]
        return (value.astimezone(UTC) + timedelta(seconds=seconds)).astimezone(value.tzinfo)
    return value + timedelta(days=count * (7 if unit == 'w' else 1))


def _round(value, unit, *, end):
    floor = value.replace(microsecond=0)
    if unit != 's':
        floor = floor.replace(second=0)
    if unit in {'h', 'd', 'w', 'M', 'y'}:
        floor = floor.replace(minute=0)
    if unit in {'d', 'w', 'M', 'y'}:
        floor = floor.replace(hour=0)
    if unit == 'w':
        floor -= timedelta(days=floor.weekday())
    elif unit in {'M', 'y'}:
        floor = floor.replace(day=1)
        if unit == 'y':
            floor = floor.replace(month=1)
    return _shift(floor, 1, unit) - timedelta(microseconds=1) if end else floor


def relative_time(expression, anchor, *, end=False):
    if not isinstance(expression, str) or not RELATIVE.fullmatch(expression):
        raise ValueError('Укажите относительное время, например now-15m, now-2h, now-7d или now/d.')
    body, _, rounding = expression.partition('/')
    value = anchor
    for sign, number, unit in re.findall(r'([+-])(\d+)([smhdwMy])', body[3:]):
        value = _shift(value, int(number) * (1 if sign == '+' else -1), unit)
    return _round(value, rounding, end=end) if rounding else value


@dataclass(frozen=True)
class TimeRange:
    start: str = 'now-1h'
    end: str = 'now'
    timezone: str = 'Europe/Moscow'
    mode: str = 'relative'

    def freeze(self, *, now=None):
        """Pin a selection to instants so a historical replay does not move."""
        bounds = self.resolve(now=now)
        tz = zone(self.timezone)
        first, last = (datetime.fromtimestamp(value, tz).isoformat() for value in bounds)
        return type(self)(first, last, self.timezone, 'absolute')

    @classmethod
    def from_text(cls, start, end, timezone='Europe/Moscow', *, now=None):
        """Accept date math or pasted local/ISO dates, using one anchor."""
        start, end = start.strip(), end.strip()
        if RELATIVE.fullmatch(start) and RELATIVE.fullmatch(end):
            return cls(start, end, timezone)
        tz = zone(timezone)
        anchor = utc_now() if now is None else now
        if anchor.tzinfo is None:
            raise ValueError('Текущее время должно содержать часовой пояс.')
        anchor = anchor.astimezone(tz)

        def parse(value, *, is_end):
            if RELATIVE.fullmatch(value):
                try:
                    return relative_time(value, anchor, end=is_end)
                except (OverflowError, OSError):
                    raise ValueError('Выбранная дата выходит за поддерживаемый диапазон.') from None
            try:
                result = datetime.fromisoformat(value)
            except ValueError:
                result = None
                for pattern in ('%d.%m.%Y %H:%M:%S', '%d.%m.%Y %H:%M', '%d.%m.%Y'):
                    try:
                        result = datetime.strptime(value, pattern)
                        break
                    except ValueError:
                        continue
                if result is None:
                    raise ValueError('Укажите дату, например 22.11.2026 17:44:38, или относительное время now-2h.') from None
            return result.replace(tzinfo=tz) if result.tzinfo is None else result

        return cls(parse(start, is_end=False).isoformat(), parse(end, is_end=True).isoformat(),
                   timezone, 'absolute')

    @classmethod
    def from_preset(cls, name, timezone='Europe/Moscow', *, fixed=False, now=None):
        selection = cls(*PRESETS[name], timezone)
        if not fixed:
            return selection
        if name == 'Вчера':
            # Historical source windows are half-open: include the entire day.
            anchor = (utc_now() if now is None else now).astimezone(zone(timezone))
            first = relative_time('now-1d/d', anchor)
            last = relative_time('now/d', anchor)
            return cls(first.isoformat(), last.isoformat(), timezone, 'absolute')
        return selection.freeze(now=now)

    def resolve(self, *, now=None):
        try:
            tz = zone(self.timezone)
            if self.mode == 'relative':
                anchor = utc_now() if now is None else now
                if anchor.tzinfo is None:
                    raise ValueError('Текущее время должно содержать часовой пояс.')
                anchor = anchor.astimezone(tz)
                first = relative_time(self.start, anchor)
                last = relative_time(self.end, anchor, end=True)
            elif self.mode == 'absolute':
                first, last = (datetime.fromisoformat(value) for value in (self.start, self.end))
                first = first.replace(tzinfo=tz) if first.tzinfo is None else first
                last = last.replace(tzinfo=tz) if last.tzinfo is None else last
            else:
                raise ValueError('Выберите относительное или точное время.')
            bounds = first.timestamp(), last.timestamp()
            if not all(math.isfinite(value) for value in bounds) or bounds[1] <= bounds[0]:
                raise ValueError('Конец периода должен быть позже начала.')
            return bounds
        except (OverflowError, OSError):
            raise ValueError('Выбранная дата выходит за поддерживаемый диапазон.') from None

    def label(self):
        if self.mode == 'relative':
            return next((name for name, pair in PRESETS.items() if pair == (self.start, self.end)), f'{self.start} → {self.end}')
        return format_bounds(self.resolve(), self.timezone)

    @classmethod
    def from_legacy(cls, start, end):
        def aware(value):
            parsed = datetime.fromisoformat(value)
            return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).isoformat()
        return cls(aware(start), aware(end), timezone='UTC', mode='absolute')


def format_bounds(bounds, timezone):
    tz = zone(timezone)
    first, last = (datetime.fromtimestamp(value, tz).strftime('%d.%m.%Y %H:%M:%S') for value in bounds)
    return f'{first} → {last}'


def minimum_range_step(selection, minimum_step):
    first, last = selection.resolve()
    required = max(minimum_step, math.ceil((last - first) / 11000))
    if required <= minimum_step:
        return minimum_step
    return next((value for value in (1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 21600, 86400)
                 if value >= required), required)
