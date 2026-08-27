from contextvars import ContextVar
from typing import Callable


ProgressCallback = Callable[[str, str], None]

_progress_callback: ContextVar[ProgressCallback | None] = ContextVar("progress_callback", default=None)


def set_progress_callback(callback: ProgressCallback | None):
    return _progress_callback.set(callback)


def reset_progress_callback(token):
    _progress_callback.reset(token)


def report_progress(message: str, phase: str = "running"):
    callback = _progress_callback.get()
    if callback:
        callback(phase, message)
