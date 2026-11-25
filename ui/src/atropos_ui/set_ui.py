import os
from threading import RLock

from atropos_ui.runtime_client import RemoteSetRuntime
from atropos_ui.set_runtime import SetRuntime

_instance = None
_lock = RLock()


def set_runtime():
    global _instance
    with _lock:
        if _instance is None:
            _instance = RemoteSetRuntime() if os.getenv('ATROPOS_RUNTIME_URL') else SetRuntime()
        return _instance
