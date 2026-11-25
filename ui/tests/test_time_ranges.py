from datetime import UTC, datetime

import pytest

from atropos_ui.time_ranges import PRESETS, TimeRange, minimum_range_step

NOW = datetime(2026, 10, 25, 22, 15, 30, tzinfo=UTC)


def test_relative_range_moves_with_now_and_uses_one_anchor():
    selection = TimeRange('now-15m', 'now')
    first, last = selection.resolve(now=NOW)
    assert last == NOW.timestamp() and last - first == 900
    later = datetime(2026, 10, 25, 23, 15, 30, tzinfo=UTC)
    assert selection.resolve(now=later) == (first + 3600, last + 3600)


def test_calendar_days_follow_selected_timezone():
    today = TimeRange(*PRESETS['Сегодня']).resolve(now=NOW)
    assert today == (datetime(2026, 10, 25, 21, tzinfo=UTC).timestamp(), NOW.timestamp())
    yesterday = TimeRange(*PRESETS['Вчера']).resolve(now=NOW)
    assert yesterday[0] == datetime(2026, 10, 24, 21, tzinfo=UTC).timestamp()
    assert yesterday[1] == pytest.approx(today[0] - 0.000001, abs=0.000001, rel=0)
    assert TimeRange(*PRESETS['Сегодня'], timezone='UTC').resolve(now=NOW)[0] == datetime(2026, 10, 25, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(('expression', 'anchor', 'expected'), [
    ('now-1M', '2024-03-31T10:00:00+00:00', '2024-02-29T10:00:00+00:00'),
    ('now-1y', '2024-02-29T10:00:00+00:00', '2023-02-28T10:00:00+00:00'),
    ('now/w', '2026-10-25T10:00:00+00:00', '2026-10-19T00:00:00+00:00'),
    ('now/M', '2026-10-25T10:00:00+00:00', '2026-10-01T00:00:00+00:00'),
])
def test_calendar_arithmetic(expression, anchor, expected):
    first, _ = TimeRange(expression, 'now', timezone='UTC').resolve(now=datetime.fromisoformat(anchor))
    assert first == datetime.fromisoformat(expected).timestamp()


@pytest.mark.parametrize(('first', 'last'), [
    ('now-15x', 'now'), ('yesterday', 'now'), ('now-1h garbage', 'now'),
    ('now', 'now-1h'), ('now', 'now'), ('now-999999999d', 'now'),
])
def test_invalid_or_reversed_ranges_fail_before_request(first, last):
    with pytest.raises(ValueError):
        TimeRange(first, last).resolve(now=NOW)


def test_absolute_range_uses_timezone_and_preserves_exact_seconds():
    selection = TimeRange('2026-10-25T10:10:23', '2026-10-25T11:20:45', mode='absolute')
    assert selection.resolve() == (
        datetime(2026, 10, 25, 7, 10, 23, tzinfo=UTC).timestamp(),
        datetime(2026, 10, 25, 8, 20, 45, tzinfo=UTC).timestamp(),
    )
    assert selection.resolve(now=NOW) == selection.resolve(now=datetime(2027, 1, 1, tzinfo=UTC))


def test_old_naive_iso_fields_remain_utc():
    selection = TimeRange.from_legacy('2026-10-25T10:10:23', '2026-10-25T11:20:45')
    assert selection.mode == 'absolute' and selection.timezone == 'UTC'
    assert selection.resolve()[0] == datetime(2026, 10, 25, 10, 10, 23, tzinfo=UTC).timestamp()


def test_long_preset_gets_step_below_api_point_limit():
    selection = TimeRange('now-30d', 'now')
    step = minimum_range_step(selection, 1)
    first, last = selection.resolve(now=NOW)
    assert step == 300 and (last - first) / step <= 11000
    assert minimum_range_step(TimeRange(), 60) == 60


def test_unknown_timezone_is_reported():
    with pytest.raises(ValueError, match='часовой пояс'):
        TimeRange(timezone='missing/zone').resolve(now=NOW)
