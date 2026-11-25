"""Streamlit lifecycle boundary; workers themselves never touch session_state."""
import os
from threading import RLock

import streamlit as st

from atropos_ui.clients import ApiError
from atropos_ui.detector_runtime import ReplayRuntime
from atropos_ui.runtime_client import RemoteReplayRuntime
from atropos_ui.service_api import AccountStore, startup_accounts

_instance = None
_lock = RLock()


def runtime():
    global _instance
    with _lock:
        if _instance is None:
            if os.getenv('ATROPOS_RUNTIME_URL'):
                _instance = RemoteReplayRuntime()
                return _instance
            accounts = startup_accounts()
            _instance = ReplayRuntime()
            for username in accounts:
                store = AccountStore(username)
                if store.metrics() and not store.metric_sets():
                    _instance.start(store)
        return _instance


def on_login(store):
    instance = runtime()
    if (st.session_state.pop('rebuild_after_login', False) and store.preferences()['rebuild_on_login']
            and store.metrics() and not store.metric_sets()):
        try:
            instance.start(store)
        except ApiError:
            st.session_state['rebuild_after_login'] = True
            st.warning('Сервер вычислений недоступен. Пересчёт повторится при обновлении страницы.')
    return instance
