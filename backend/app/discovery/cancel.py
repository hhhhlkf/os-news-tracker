"""Discovery 运行取消控制：per-run event + 当前线程上下文。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from threading import Event, Lock


class DiscoveryCancelled(RuntimeError):
    """当前 discovery run 被用户取消。"""


_current_run_id: ContextVar[int | None] = ContextVar("discovery_current_run_id", default=None)
_events: dict[int, Event] = {}
_lock = Lock()


def register_run(run_id: int) -> None:
    with _lock:
        _events[run_id] = Event()


def unregister_run(run_id: int) -> None:
    with _lock:
        _events.pop(run_id, None)


def activate_run(run_id: int) -> Token:
    return _current_run_id.set(run_id)


def deactivate_run(token: Token) -> None:
    _current_run_id.reset(token)


def request_cancel(run_id: int) -> bool:
    with _lock:
        event = _events.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def is_cancelled(run_id: int | None = None) -> bool:
    rid = run_id if run_id is not None else _current_run_id.get()
    if rid is None:
        return False
    with _lock:
        event = _events.get(rid)
    return bool(event and event.is_set())


def ensure_not_cancelled() -> None:
    if is_cancelled():
        raise DiscoveryCancelled("用户已取消智能探查")
