from datetime import UTC, date, datetime, time, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from atropos_ui.time_ranges import TimeRange

PICKER = '''
import streamlit as st
from atropos_ui.time_ranges import TimeRange
from atropos_ui.time_picker import _initialize, _dialog_contents
if 'test_time_range' not in st.session_state:
    st.session_state['test_time_range'] = TimeRange()
    _initialize('test', TimeRange())
_dialog_contents('test')
st.session_state['applied'] = st.session_state['test_time_range']
'''
NOW = datetime(2026, 10, 25, 12, 10, 23, tzinfo=UTC)


def click(app, key):
    app.button(key=key).click().run()
    assert not app.exception


@pytest.fixture
def picker(monkeypatch):
    monkeypatch.setattr('atropos_ui.time_ranges.utc_now', lambda: NOW)
    app = AppTest.from_string(PICKER).run()
    assert not app.exception
    return app


def test_relative_preset_is_applied_only_after_confirmation(picker):
    picker.selectbox(key='test_picker_preset').set_value('Последние 15 минут').run()
    assert picker.session_state['applied'] == TimeRange()
    click(picker, 'test_picker_apply')
    assert picker.session_state['applied'] == TimeRange('now-15m', 'now')


def test_period_button_opens_dialog():
    # AppTest reruns whole scripts instead of dialog fragments. Exercise the
    # contents above on full reruns and verify the real dialog opens here.
    app = AppTest.from_string('''
from atropos_ui.time_picker import time_window
time_window('test')
''').run()
    assert not app.radio
    click(app, 'test_open_period')
    assert app.radio(key='test_picker_mode').value == 'Относительное время'
    assert app.button(key='test_picker_apply')


def test_long_calendar_period_increases_step_to_respect_point_limit():
    app = AppTest.from_string('''
import streamlit as st
from atropos_ui.time_ranges import TimeRange
from atropos_ui.time_picker import time_window
st.session_state['test_time_range'] = TimeRange(
    '1970-01-01T00:00:00+00:00', '2100-12-31T00:00:00+00:00', 'UTC', 'absolute')
selection, step = time_window('test')
st.session_state['bounds'] = selection.resolve()
''').run()
    assert not app.exception
    first, last = app.session_state['bounds']
    assert (last - first) / app.number_input(key='test_step').value <= 11000


def test_cancel_keeps_applied_range(picker):
    picker.selectbox(key='test_picker_preset').set_value('Последние 7 дней').run()
    click(picker, 'test_picker_cancel')
    assert picker.session_state['applied'] == TimeRange()


def test_custom_invalid_range_cannot_be_applied(picker):
    picker.text_input(key='test_picker_from').set_value('now-2h').run()
    picker.text_input(key='test_picker_to').set_value('now-3h').run()
    assert picker.selectbox(key='test_picker_preset').value == 'Свой диапазон'
    assert picker.error and picker.button(key='test_picker_apply').disabled


def test_exact_calendar_seconds_and_timezone(picker):
    picker.radio(key='test_picker_mode').set_value('Точное время').run()
    assert not picker.exception
    picker.date_input(key='test_picker_from_date').set_value(date(2026, 10, 20))
    picker.time_input(key='test_picker_from_time').set_value(time(10, 11, 23))
    picker.date_input(key='test_picker_to_date').set_value(date(2026, 10, 21))
    picker.time_input(key='test_picker_to_time').set_value(time(12, 13, 45)).run()
    picker.selectbox(key='test_picker_timezone').set_value('UTC').run()
    assert picker.time_input(key='test_picker_from_time').value == time(7, 11, 23)
    assert picker.time_input(key='test_picker_to_time').value == time(9, 13, 45)
    click(picker, 'test_picker_apply')
    selection = picker.session_state['applied']
    assert selection.mode == 'absolute' and selection.timezone == 'UTC'
    assert selection.resolve() == (
        datetime(2026, 10, 20, 7, 11, 23, tzinfo=UTC).timestamp(),
        datetime(2026, 10, 21, 9, 13, 45, tzinfo=UTC).timestamp(),
    )


def test_modes_can_be_switched_repeatedly_without_losing_relative_draft(picker):
    picker.text_input(key='test_picker_from').set_value('now-2h').run()
    for _ in range(2):
        picker.radio(key='test_picker_mode').set_value('Точное время').run()
        assert not picker.exception
        picker.radio(key='test_picker_mode').set_value('Относительное время').run()
        assert not picker.exception
        assert picker.text_input(key='test_picker_from').value == 'now-2h'
        assert picker.text_input(key='test_picker_to').value == 'now'
    click(picker, 'test_picker_apply')
    assert picker.session_state['applied'].start == 'now-2h'


def test_exact_reversed_calendar_range_is_blocked(picker):
    picker.radio(key='test_picker_mode').set_value('Точное время').run()
    picker.date_input(key='test_picker_from_date').set_value(date(2027, 1, 1)).run()
    assert picker.error and picker.button(key='test_picker_apply').disabled


def test_query_reevaluates_relative_range_only_when_executed(monkeypatch, ui_store):
    from atropos_ui.demo import DemoClient
    calls = []
    anchor = [NOW]

    class Client(DemoClient):
        def range(self, query, first, last, step):
            calls.append((first, last))
            return super().range(query, first, last, step)

    monkeypatch.setenv('ATROPOS_UI_DEMO', '1')
    monkeypatch.setattr('atropos_ui.time_ranges.utc_now', lambda: anchor[0])
    monkeypatch.setattr('atropos_ui.context.Context.source_client', lambda self, source: Client())
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.queries import render
render(context())
''', default_timeout=15).run()
    next(item for item in app.button if item.label == 'Построить временной ряд').click().run()
    assert not app.exception
    assert calls == [(NOW.timestamp() - 3600, NOW.timestamp())]
    anchor[0] += timedelta(minutes=5)
    app.run()
    assert len(calls) == 1 and not app.warning
    next(item for item in app.button if item.label == 'Построить временной ряд').click().run()
    assert not app.exception
    assert calls[-1] == (NOW.timestamp() - 3300, NOW.timestamp() + 300)
