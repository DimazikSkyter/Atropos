from datetime import UTC, date, datetime, time, timedelta

import pytest
from atropos_ui.time_ranges import TimeRange
from streamlit.testing.v1 import AppTest
from test_set_workflow import setup_store
from test_time_picker import PICKER, click

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


def test_pasted_dates_from_reported_dialog_are_accepted(monkeypatch):
    monkeypatch.setattr('atropos_ui.time_ranges.utc_now', lambda: NOW)
    app = AppTest.from_string(PICKER).run()
    app.text_input(key='test_picker_from').set_value('22.11.2026 17:44:38').run()
    app.text_input(key='test_picker_to').set_value('22.11.2026 22:44:38').run()
    assert not app.exception and not app.button(key='test_picker_apply').disabled
    click(app, 'test_picker_apply')
    selection = app.session_state['applied']
    assert selection.mode == 'absolute'
    assert selection.resolve() == (datetime(2026, 11, 22, 14, 44, 38, tzinfo=UTC).timestamp(),
                                   datetime(2026, 11, 22, 19, 44, 38, tzinfo=UTC).timestamp())


def test_fixed_picker_opens_calendar_and_yesterday_stays_fixed(monkeypatch):
    anchor = [NOW]
    monkeypatch.setattr('atropos_ui.time_ranges.utc_now', lambda: anchor[0])
    app = AppTest.from_string(PICKER.replace("_initialize('test', TimeRange())",
                                          "_initialize('test', TimeRange(), fixed=True)")).run()
    assert app.radio(key='test_picker_mode').value == 'Точное время'
    app.selectbox(key='test_picker_preset').set_value('Вчера').run()
    assert app.date_input(key='test_picker_from_date').value == date(2026, 11, 22)
    assert app.date_input(key='test_picker_to_date').value == date(2026, 11, 23)
    assert app.time_input(key='test_picker_from_time').value == time(0, 0)
    click(app, 'test_picker_apply')
    selection = app.session_state['applied']
    assert selection.mode == 'absolute'
    expected = (datetime(2026, 11, 21, 21, tzinfo=UTC).timestamp(),
                datetime(2026, 11, 22, 21, tzinfo=UTC).timestamp())
    assert selection.resolve() == expected
    anchor[0] += timedelta(days=2)
    assert selection.resolve() == expected


def test_historical_bounds_reach_detector_runtime_and_do_not_move(monkeypatch):
    store, _ = setup_store()
    calls = []
    runtime = type('Runtime', (), {
        'start': lambda self, *args: calls.append(args) or True,
        'state': lambda *args: None, 'result': lambda *args: None})()
    monkeypatch.setattr('atropos_ui.detector_workspace.set_runtime', lambda: runtime)
    monkeypatch.setattr('atropos_ui.context.configuration_store', lambda demo: store)
    selection = TimeRange('2026-11-22T17:44:38+03:00', '2026-11-22T22:44:38+03:00', mode='absolute')
    app = AppTest.from_string('''
from atropos_ui.context import context
from atropos_ui.pages.detectors import render
render(context())
''').run()
    app.session_state['detectors_set_window_time_range'] = selection
    for anchor in (NOW, NOW + timedelta(days=2)):
        monkeypatch.setattr('atropos_ui.time_ranges.utc_now', lambda: anchor)
        app.run()
        next(b for b in app.button if b.label == 'Рассчитать сет').click().run()
        assert not app.exception
    expected = tuple(int(round(value * 1e9)) for value in selection.resolve())
    assert len(calls) == 2
    assert all(call[2:4] == expected and call[-1] == 'source' for call in calls)


@pytest.mark.parametrize(('first', 'last'), [
    ('22.11.2026 17:44:38', '22.11.2026 22:44:38'),
    ('2026-11-22T17:44:38+03:00', '2026-11-22T22:44:38+03:00'),
    ('now-1d', '2026-11-23T09:00:00+00:00'),
])
def test_text_range_accepts_local_iso_and_mixed_dates(first, last):
    selection = TimeRange.from_text(first, last, now=NOW)
    start, end = selection.resolve()
    assert selection.mode == 'absolute' and end > start
    assert selection.resolve(now=NOW + timedelta(days=10)) == (start, end)


def test_relative_text_still_rolls_and_freeze_uses_one_anchor():
    selection = TimeRange.from_text('now-2h', 'now')
    fixed = selection.freeze(now=NOW)
    assert selection.mode == 'relative' and fixed.mode == 'absolute'
    assert fixed.resolve() == selection.resolve(now=NOW)
    assert fixed.resolve(now=NOW + timedelta(days=2)) == fixed.resolve()
