"""Cross-process priority broker for scarce gVisor capacity."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from threading import Condition, Event
from typing import Any

from app.config import get_settings
from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorProtocolError

class SandboxJobPriority(IntEnum):
    SCHEDULED = 0
    MANUAL = 1
    DISCOVERY = 2
    REPAIR = 3

@dataclass(frozen=True)
class _Waiter:
    job_id: str
    priority: SandboxJobPriority
    cancel_event: Event

class CapacityLease:
    def __init__(self, queue: "CapacityQueue", job_id: str, priority: SandboxJobPriority) -> None:
        self._queue, self.job_id, self.priority, self._released = queue, job_id, priority, False
    def release(self) -> None:
        if not self._released:
            self._released = True
            self._queue.release(self.job_id)
    def __enter__(self) -> "CapacityLease": return self
    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None: self.release()

class _HostCapacityBroker:
    """A flock-serialized durable ticket ledger shared by all workers."""
    AGING_INTERVAL_SECONDS = 60.0
    def __init__(self, root: str, *, capacity: int, discovery_capacity: int) -> None:
        self.root, self.capacity, self.discovery_capacity = Path(root), capacity, discovery_capacity
        self.pid, self.start_token = os.getpid(), self._process_start_token(os.getpid())
        self._prepare_root()
        self.lock_path, self.state_path = self.root / "broker.lock", self.root / "tickets.json"

    def register(self, job_id: str, priority: SandboxJobPriority) -> None:
        with self._locked_state() as state:
            tickets = self._clean_dead(state["tickets"])
            if any(ticket["job_id"] == job_id for ticket in tickets):
                raise ValueError(f"duplicate global sandbox job id: {job_id}")
            tickets.append({"job_id": job_id, "priority": int(priority), "enqueued_at": time.time(),
                            "pid": self.pid, "start_token": self.start_token, "state": "pending"})
            state["tickets"] = tickets

    def try_claim(self, job_id: str) -> bool:
        with self._locked_state() as state:
            tickets = self._clean_dead(state["tickets"])
            target = next((ticket for ticket in tickets if ticket["job_id"] == job_id), None)
            if target is None: return False
            if target["state"] == "active": return True
            pending = [ticket for ticket in tickets if ticket["state"] == "pending"]
            active = [ticket for ticket in tickets if ticket["state"] == "active"]
            discovery_active = sum(ticket["priority"] in {2, 3} for ticket in active)
            if len(active) >= self.capacity:
                state["tickets"] = tickets; return False
            eligible = [
                ticket for ticket in pending
                if ticket["priority"] not in {2, 3}
                or discovery_active < self.discovery_capacity
            ]
            if not eligible or min(eligible, key=self._sort_key) is not target:
                state["tickets"] = tickets; return False
            target["state"], target["claimed_at"] = "active", time.time()
            state["tickets"] = tickets
            return True

    def remove(self, job_id: str) -> bool:
        with self._locked_state() as state:
            tickets = self._clean_dead(state["tickets"])
            remaining = [ticket for ticket in tickets if ticket["job_id"] != job_id]
            state["tickets"] = remaining
            return len(remaining) != len(tickets)

    def queue_position(self, job_id: str) -> int | None:
        with self._locked_state() as state:
            tickets = self._clean_dead(state["tickets"]); state["tickets"] = tickets
            pending = sorted((ticket for ticket in tickets if ticket["state"] == "pending"), key=self._sort_key)
            return next((i for i, ticket in enumerate(pending, 1) if ticket["job_id"] == job_id), None)

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        with self._locked_state() as state:
            tickets = self._clean_dead(state["tickets"]); state["tickets"] = tickets
            return {"active": tuple(t["job_id"] for t in tickets if t["state"] == "active"),
                    "waiting": tuple(t["job_id"] for t in sorted((v for v in tickets if v["state"] == "pending"), key=self._sort_key))}

    def _sort_key(self, ticket: dict[str, Any]) -> tuple[int, float, str]:
        promotions = int((time.time() - float(ticket["enqueued_at"])) / self.AGING_INTERVAL_SECONDS)
        return max(0, int(ticket["priority"]) - promotions), float(ticket["enqueued_at"]), str(ticket["job_id"])
    def _clean_dead(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [ticket for ticket in tickets if self._owner_alive(int(ticket["pid"]), str(ticket["start_token"]))]
    @staticmethod
    def _owner_alive(pid: int, token: str) -> bool:
        try:
            os.kill(pid, 0)
            return _HostCapacityBroker._process_start_token(pid) == token
        except (OSError, ValueError): return False
    @staticmethod
    def _process_start_token(pid: int) -> str:
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].strip().split()
        return tail[19]
    def _prepare_root(self) -> None:
        if not self.root.is_absolute(): raise RuntimeError("sandbox capacity broker root must be absolute")
        self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try: self.root.mkdir(mode=0o700)
        except FileExistsError:
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError("sandbox capacity broker root is unsafe")

    class _StateContext:
        def __init__(self, broker: "_HostCapacityBroker") -> None:
            self.broker, self.fd, self.state, self.original = broker, -1, {}, b""
        def __enter__(self) -> dict[str, Any]:
            self.fd = os.open(self.broker.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            try:
                loaded = json.loads(self.broker.state_path.read_text(encoding="utf-8")); tickets = loaded.get("tickets")
                self.state = {"tickets": tickets if isinstance(tickets, list) else []}
            except FileNotFoundError:
                self.state = {"tickets": []}
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("sandbox capacity broker state is unreadable") from exc
            self.original = json.dumps(
                self.state,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            return self.state
        def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
            try:
                if exc_type is None:
                    canonical = json.dumps(
                        self.state,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    if canonical == self.original:
                        return
                    temporary = self.broker.root / f".tickets-{uuid.uuid4().hex}.tmp"
                    data = canonical
                    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    try:
                        pending = memoryview(data)
                        while pending:
                            pending = pending[os.write(out, pending):]
                        os.fsync(out)
                    finally: os.close(out)
                    os.replace(temporary, self.broker.state_path)
                    root_fd = os.open(self.broker.root, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(root_fd)
                    finally: os.close(root_fd)
            finally: fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)
    def _locked_state(self) -> "_HostCapacityBroker._StateContext": return self._StateContext(self)

class CapacityQueue:
    """Process-local cancellation handles backed by one global ticket broker."""
    def __init__(self, *, capacity: int, discovery_capacity: int) -> None:
        if capacity < 1 or discovery_capacity < 1: raise ValueError("sandbox capacities must be positive")
        self.capacity, self.discovery_capacity = min(capacity, 4), min(discovery_capacity, capacity, 3)
        self._condition, self._waiting, self._active = Condition(), {}, {}
        self._broker = _HostCapacityBroker(get_settings().discovery_sandbox_capacity_lock_root,
                                           capacity=self.capacity, discovery_capacity=self.discovery_capacity)
    def acquire(self, job_id: str, priority: SandboxJobPriority, cancel_event: Event) -> CapacityLease:
        waiter = _Waiter(job_id, priority, cancel_event)
        with self._condition:
            if job_id in self._waiting or job_id in self._active: raise ValueError(f"duplicate sandbox execution id: {job_id}")
            self._waiting[job_id] = waiter
        try:
            self._broker.register(job_id, priority)
            while True:
                if cancel_event.is_set(): raise ConnectorProtocolError(ConnectorErrorCode.CANCELLED, "sandbox execution was cancelled while queued")
                if self._broker.try_claim(job_id):
                    with self._condition:
                        self._waiting.pop(job_id, None); self._active[job_id] = priority; self._condition.notify_all()
                    return CapacityLease(self, job_id, priority)
                with self._condition: self._condition.wait(timeout=0.1)
        except BaseException:
            self._broker.remove(job_id)
            with self._condition: self._waiting.pop(job_id, None); self._condition.notify_all()
            raise
    def cancel_waiting(self, job_id: str) -> bool:
        with self._condition:
            waiter = self._waiting.get(job_id)
            if waiter is None: return False
            waiter.cancel_event.set(); self._broker.remove(job_id); self._condition.notify_all(); return True
    def queue_position(self, job_id: str) -> int | None: return self._broker.queue_position(job_id)
    def release(self, job_id: str) -> None:
        self._broker.remove(job_id)
        with self._condition: self._active.pop(job_id, None); self._condition.notify_all()
    def snapshot(self) -> dict[str, object]: return {"capacity": self.capacity, **self._broker.snapshot()}

@lru_cache
def get_sandbox_capacity_queue() -> CapacityQueue:
    settings = get_settings(); total = min(4, max(1, int(settings.discovery_sandbox_max_containers)))
    return CapacityQueue(capacity=total, discovery_capacity=min(3, total, max(1, int(settings.discovery_max_concurrent_runs))))
