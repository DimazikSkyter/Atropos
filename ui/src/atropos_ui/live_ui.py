"""One process-wide runtime, shared by pages and browser sessions."""
import os
from threading import RLock

from atropos_ui.live_runtime import LiveRuntime
from atropos_ui.runtime_client import RemoteLiveRuntime

_instance = None
_lock = RLock()


def live_runtime():
    global _instance
    with _lock:
        if _instance is None:
            _instance = RemoteLiveRuntime() if os.getenv('ATROPOS_RUNTIME_URL') else LiveRuntime()
        return _instance
