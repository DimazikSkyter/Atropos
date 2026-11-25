"""One date dialog for query, catalog and server pages. No database writes."""
from datetime import date, datetime

import streamlit as st

from atropos_ui.time_ranges import (
    CUSTOM,
    PRESETS,
    TIMEZONES,
    TimeRange,
    format_bounds,
    minimum_range_step,
    zone,
)

RELATIVE = 'Относительное время'
ABSOLUTE = 'Точное время'


def _key(prefix, suffix):
    return prefix + '_picker_' + suffix


def _set_calendar(prefix, bounds, timezone):
    tz = zone(timezone)
    for side, bound in zip(('from', 'to'), bounds):
        value = datetime.fromtimestamp(bound, tz).replace(microsecond=0)
        st.session_state[_key(prefix, side + '_date')] = value.date()
        st.session_state[_key(prefix, side + '_time')] = value.time().replace(tzinfo=None)


def _initialize(prefix, selection, *, fixed=False):
    st.session_state[_key(prefix, 'fixed')] = fixed
    st.session_state[_key(prefix, 'mode')] = RELATIVE if selection.mode == 'relative' and not fixed else ABSOLUTE
    st.session_state[_key(prefix, 'previous_mode')] = st.session_state[_key(prefix, 'mode')]
    st.session_state[_key(prefix, 'timezone')] = selection.timezone
    st.session_state[_key(prefix, 'previous_timezone')] = selection.timezone
    first, last = (selection.start, selection.end) if selection.mode == 'relative' else ('now-1h', 'now')
    st.session_state[_key(prefix, 'from')] = first
    st.session_state[_key(prefix, 'to')] = last
    st.session_state[_key(prefix, 'relative_draft')] = (first, last)
    st.session_state[_key(prefix, 'preset')] = next(
        (name for name, pair in PRESETS.items() if selection.mode == 'relative' and pair == (first, last)), CUSTOM)
    _set_calendar(prefix, selection.resolve(), selection.timezone)


def _choose_preset(prefix):
    selected = st.session_state[_key(prefix, 'preset')]
    if selected in PRESETS:
        first, last = PRESETS[selected]
        st.session_state[_key(prefix, 'from')] = first
        st.session_state[_key(prefix, 'to')] = last
        st.session_state[_key(prefix, 'relative_draft')] = (first, last)
        if st.session_state[_key(prefix, 'mode')] == ABSOLUTE:
            timezone = st.session_state[_key(prefix, 'timezone')]
            selection = TimeRange.from_preset(selected, timezone, fixed=True)
            _set_calendar(prefix, selection.resolve(), timezone)


def _custom_preset(prefix):
    st.session_state[_key(prefix, 'preset')] = CUSTOM


def _absolute(prefix, timezone):
    values = [st.session_state.get(_key(prefix, side + '_' + part))
              for side in ('from', 'to') for part in ('date', 'time')]
    if any(value is None for value in values):
        raise ValueError('Выберите обе даты и время начала и конца.')
    first = datetime.combine(values[0], values[1], tzinfo=zone(timezone))
    last = datetime.combine(values[2], values[3], tzinfo=zone(timezone))
    return TimeRange(first.isoformat(), last.isoformat(), timezone, 'absolute')


def _change_zone(prefix):
    previous = st.session_state[_key(prefix, 'previous_timezone')]
    current = st.session_state[_key(prefix, 'timezone')]
    if st.session_state[_key(prefix, 'mode')] == ABSOLUTE:
        try:
            _set_calendar(prefix, _absolute(prefix, previous).resolve(), current)
        except ValueError:
            pass  # Incomplete dates stay visible until the user finishes editing.
    st.session_state[_key(prefix, 'previous_timezone')] = current
    if st.session_state[_key(prefix, 'mode')] == ABSOLUTE:
        _custom_preset(prefix)


def _change_mode(prefix):
    current = st.session_state[_key(prefix, 'mode')]
    previous = st.session_state[_key(prefix, 'previous_mode')]
    if current == ABSOLUTE and previous == RELATIVE:
        first = st.session_state[_key(prefix, 'from')]
        last = st.session_state[_key(prefix, 'to')]
        st.session_state[_key(prefix, 'relative_draft')] = (first, last)
        try:
            tz = st.session_state[_key(prefix, 'timezone')]
            preset = st.session_state[_key(prefix, 'preset')]
            selection = (TimeRange.from_preset(preset, tz, fixed=True) if preset in PRESETS
                         else TimeRange.from_text(first, last, tz))
            _set_calendar(prefix, selection.resolve(), tz)
        except ValueError:
            pass
    elif current == RELATIVE:
        first, last = st.session_state[_key(prefix, 'relative_draft')]
        st.session_state[_key(prefix, 'from')] = first
        st.session_state[_key(prefix, 'to')] = last
        st.session_state[_key(prefix, 'preset')] = next(
            (name for name, pair in PRESETS.items() if pair == (first, last)), CUSTOM)
    st.session_state[_key(prefix, 'previous_mode')] = current


def _dialog_contents(prefix):
    mode = st.radio('Режим выбора', [RELATIVE, ABSOLUTE], horizontal=True,
                    key=_key(prefix, 'mode'), on_change=_change_mode, args=(prefix,))
    timezone = st.selectbox('Часовой пояс', TIMEZONES, key=_key(prefix, 'timezone'),
                            on_change=_change_zone, args=(prefix,))
    st.selectbox('Быстрый выбор', [*PRESETS, CUSTOM], key=_key(prefix, 'preset'),
                 on_change=_choose_preset, args=(prefix,))
    if mode == RELATIVE:
        a, b = st.columns(2)
        first = a.text_input('От', key=_key(prefix, 'from'), on_change=_custom_preset, args=(prefix,))
        last = b.text_input('До', key=_key(prefix, 'to'), on_change=_custom_preset, args=(prefix,))
        st.caption('Можно ввести now-2h, now/d или точную дату: 22.11.2026 17:44:38.')
        try:
            candidate = TimeRange.from_text(first, last, timezone)
        except ValueError as exc:
            candidate = None
            st.error(str(exc))
    else:
        a, b = st.columns(2)
        a.date_input('Дата начала', key=_key(prefix, 'from_date'), format='DD.MM.YYYY',
                     min_value=date(1970, 1, 1), max_value=date(2100, 12, 31),
                     on_change=_custom_preset, args=(prefix,))
        a.time_input('Время начала', key=_key(prefix, 'from_time'), step=1,
                     on_change=_custom_preset, args=(prefix,))
        b.date_input('Дата конца', key=_key(prefix, 'to_date'), format='DD.MM.YYYY',
                     min_value=date(1970, 1, 1), max_value=date(2100, 12, 31),
                     on_change=_custom_preset, args=(prefix,))
        b.time_input('Время конца', key=_key(prefix, 'to_time'), step=1,
                     on_change=_custom_preset, args=(prefix,))
        st.caption('Нажмите на дату, чтобы открыть календарь. Время можно указать с точностью до секунды.')
        try:
            candidate = _absolute(prefix, timezone)
        except ValueError as exc:
            candidate = None
            st.error(str(exc))
    valid = False
    if candidate is not None:
        try:
            if st.session_state.get(_key(prefix, 'fixed')):
                preset = st.session_state[_key(prefix, 'preset')]
                candidate = (TimeRange.from_preset(preset, timezone, fixed=True)
                             if mode == RELATIVE and preset in PRESETS else candidate.freeze())
            bounds = candidate.resolve()
            st.caption(format_bounds(bounds, timezone) + ' · ' + timezone)
            valid = True
        except ValueError as exc:
            st.error(str(exc))
    apply, cancel = st.columns(2)
    if apply.button('Применить период', type='primary', key=_key(prefix, 'apply'), disabled=not valid):
        st.session_state[prefix + '_time_range'] = candidate
        st.rerun()
    if cancel.button('Отмена', key=_key(prefix, 'cancel')):
        st.rerun()


@st.dialog('Выбор периода', width='large')
def _dialog(prefix):
    _dialog_contents(prefix)


def restore_window(prefix, window):
    """Keep an existing view when moving between pages or upgrading an open UI."""
    if isinstance(window, dict) and isinstance(window.get('range'), TimeRange):
        st.session_state.setdefault(prefix + '_time_range', window['range'])
        st.session_state.setdefault(prefix + '_step', window['step'])
    elif isinstance(window, (tuple, list)) and len(window) == 3:
        try:
            selection = TimeRange.from_legacy(window[0], window[1])
            selection.resolve()
            st.session_state.setdefault(prefix + '_time_range', selection)
            st.session_state.setdefault(prefix + '_step', window[2])
        except (ValueError, TypeError):
            pass


def time_window(prefix: str, *, minimum_step: int = 1, fixed: bool = False):
    key = prefix + '_time_range'
    if key not in st.session_state:
        legacy = [st.session_state.get(prefix + '_' + side) for side in ('start', 'end')]
        if all(isinstance(value, str) for value in legacy):
            restore_window(prefix, (*legacy, st.session_state.get(prefix + '_step', max(30, minimum_step))))
        st.session_state.setdefault(key, TimeRange())
    selection = st.session_state[key]
    a, b = st.columns([4, 1], vertical_alignment='bottom')
    if a.button('🕒 ' + selection.label(), key=prefix + '_open_period', use_container_width=True):
        _initialize(prefix, selection, fixed=fixed)
        _dialog(prefix)
    required = minimum_range_step(selection, minimum_step)
    step_key = prefix + '_step'
    previous = st.session_state.get(step_key, max(30, minimum_step))
    if previous < required:
        st.session_state[step_key] = required
    step = b.number_input('Шаг, секунд', minimum_step, max(86400, required, previous),
                           max(30, minimum_step), key=step_key)
    st.caption('Часовой пояс: ' + selection.timezone)
    if fixed:
        st.caption('Выберите любой период, включая прошлые даты. При применении его границы фиксируются для повторного расчёта.')
    if previous < required:
        st.caption(f'Шаг увеличен до {required} с для выбранного периода.')
    return selection, step
