"""Generic connector process runner used only inside the gVisor runtime image.

The host application must invoke this module through ``SandboxRuntime``. Importing
untrusted connector code into the backend process is deliberately not supported.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import selectors
import signal
import sys
import threading
import traceback
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Coroutine, TextIO

from pydantic import ValidationError

from app.discovery.plugin.artifact import ConnectorArtifact, load_connector_artifact
from app.discovery.plugin.contracts import ConnectorInvocation, ConnectorOutput
from app.discovery.plugin.errors import ConnectorErrorCode, ConnectorExitCode, ConnectorProtocolError


MAX_STDIN_BYTES = 1024 * 1024
MAX_CAPTURED_LOG_BYTES = 1024 * 1024
MAX_CAPTURED_LINE_BYTES = 16 * 1024
TRUNCATION_EVENT_RESERVE_BYTES = 1024


class StructuredStderrWriter:
    """Convert arbitrary plugin stdout/stderr lines into structured log events."""

    def __init__(self, budget: StructuredTextLogBudget, *, stream: str) -> None:
        self.budget = budget
        self.stream = stream

    def write(self, value: str) -> int:
        # Encode in small pieces so this adapter never creates another unbounded
        # copy of a large string supplied by plugin code.
        for offset in range(0, len(value), 4096):
            has_capacity = self.budget.consume(
                self.stream,
                value[offset : offset + 4096].encode("utf-8", errors="replace"),
            )
            if not has_capacity:
                break
        return len(value)

    def flush(self) -> None:
        self.budget.flush(self.stream)


class StructuredTextLogBudget:
    """Shared bounded log sink for the no-file-descriptor fallback path."""

    def __init__(self, destination: TextIO) -> None:
        self.destination = destination
        self._lock = threading.RLock()
        self._buffers = {"stdout": b"", "stderr": b""}
        self._dropping_long_line = {"stdout": False, "stderr": False}
        self._captured_bytes = 0
        self._emitted_bytes = 0
        self._truncation_emitted = False

    def consume(self, stream: str, chunk: bytes) -> bool:
        with self._lock:
            remaining = max(MAX_CAPTURED_LOG_BYTES - self._captured_bytes, 0)
            accepted = chunk[:remaining]
            self._captured_bytes += len(accepted)
            if len(accepted) < len(chunk):
                self._emit_truncation_once("total_log_limit")
            if accepted:
                self._consume_accepted(stream, accepted)
            return self._captured_bytes < MAX_CAPTURED_LOG_BYTES

    def flush(self, stream: str) -> None:
        with self._lock:
            buffer = self._buffers[stream]
            if buffer:
                self._emit_line(stream, buffer)
                self._buffers[stream] = b""
            self.destination.flush()

    def _consume_accepted(self, stream: str, chunk: bytes) -> None:
        buffer = self._buffers[stream]
        dropping = self._dropping_long_line[stream]
        while chunk:
            newline_index = chunk.find(b"\n")
            has_newline = newline_index >= 0
            segment = chunk[:newline_index] if has_newline else chunk
            chunk = chunk[newline_index + 1 :] if has_newline else b""
            if dropping:
                if has_newline:
                    dropping = False
                continue
            remaining = MAX_CAPTURED_LINE_BYTES - len(buffer)
            buffer += segment[:remaining]
            if len(segment) > remaining:
                self._emit_truncation_once("line_log_limit")
                if buffer:
                    self._emit_line(stream, buffer)
                buffer = b""
                dropping = not has_newline
                continue
            if has_newline:
                if buffer:
                    self._emit_line(stream, buffer)
                buffer = b""
        self._buffers[stream] = buffer
        self._dropping_long_line[stream] = dropping

    def _emit_line(self, stream: str, content: bytes) -> None:
        self._emit_payload(
            {
                "type": "connector_event",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "plugin_log",
                "level": "info" if stream == "stdout" else "warning",
                "message": content.decode("utf-8", errors="replace"),
                "stream": stream,
            }
        )

    def _emit_truncation_once(self, reason: str) -> None:
        if self._truncation_emitted:
            return
        self._truncation_emitted = True
        self._emit_payload(
            {
                "type": "connector_event",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "plugin_log_truncated",
                "level": "warning",
                "message": "plugin log output was truncated by the runner",
                "reason": reason,
                "max_line_bytes": MAX_CAPTURED_LINE_BYTES,
                "max_total_bytes": MAX_CAPTURED_LOG_BYTES,
            },
            enforce_log_limit=False,
        )

    def _emit_payload(self, payload: dict[str, Any], *, enforce_log_limit: bool = True) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        ).encode("utf-8")
        if enforce_log_limit:
            available = max(
                MAX_CAPTURED_LOG_BYTES
                - TRUNCATION_EVENT_RESERVE_BYTES
                - self._emitted_bytes,
                0,
            )
            if len(encoded) > available:
                self._emit_truncation_once("structured_log_limit")
                return
            self._emitted_bytes += len(encoded)
        self.destination.write(encoded.decode("utf-8"))


class FileDescriptorCapture(AbstractContextManager[None]):
    """Capture fd1/fd2, including C extensions and inherited subprocess output.

    Two non-blocking pipes are drained concurrently so a noisy plugin cannot fill
    either pipe and deadlock. The original descriptors are restored before drain
    workers receive their stop signal; workers then consume pending bytes and exit
    within a bounded join interval even if a leaked child still owns a pipe writer.
    """

    def __init__(self) -> None:
        self._saved_stderr = -1
        self._saved_stdout = -1
        self._read_fds: dict[str, int] = {}
        self._write_fds: set[int] = set()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._budget_lock = threading.Lock()
        self._captured_bytes = 0
        self._emitted_log_bytes = 0
        self._truncation_emitted = False

    def __enter__(self) -> None:
        _flush_standard_streams()
        try:
            self._saved_stdout = os.dup(1)
            self._saved_stderr = os.dup(2)
            for stream, target_fd in (("stdout", 1), ("stderr", 2)):
                read_fd, write_fd = os.pipe()
                self._read_fds[stream] = read_fd
                self._write_fds.add(write_fd)
                os.set_blocking(read_fd, False)
                thread = threading.Thread(
                    target=self._drain,
                    args=(stream, read_fd),
                    name=f"connector-{stream}-drain",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
                os.dup2(write_fd, target_fd)
                os.close(write_fd)
                self._write_fds.remove(write_fd)
        except BaseException:
            self._restore_descriptors()
            self._close_write_descriptors()
            self._stop_workers()
            self._close_saved_descriptors()
            raise
        return None

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        try:
            _flush_standard_streams()
        finally:
            self._restore_descriptors()
            self._close_write_descriptors()
            self._stop_workers()
            self._close_saved_descriptors()

    def _restore_descriptors(self) -> None:
        if self._saved_stdout >= 0:
            try:
                os.dup2(self._saved_stdout, 1)
            except OSError:
                pass
        if self._saved_stderr >= 0:
            try:
                os.dup2(self._saved_stderr, 2)
            except OSError:
                pass

    def _close_write_descriptors(self) -> None:
        for write_fd in self._write_fds:
            try:
                os.close(write_fd)
            except OSError:
                pass
        self._write_fds.clear()

    def _stop_workers(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        for read_fd in self._read_fds.values():
            try:
                os.close(read_fd)
            except OSError:
                pass
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=1.0)
        self._threads.clear()
        self._read_fds.clear()

    def _close_saved_descriptors(self) -> None:
        if self._saved_stdout >= 0:
            try:
                os.close(self._saved_stdout)
            except OSError:
                pass
            self._saved_stdout = -1
        if self._saved_stderr >= 0:
            try:
                os.close(self._saved_stderr)
            except OSError:
                pass
            self._saved_stderr = -1

    def _drain(self, stream: str, read_fd: int) -> None:
        selector = selectors.DefaultSelector()
        buffer = b""
        dropping_long_line = False
        try:
            selector.register(read_fd, selectors.EVENT_READ)
            while True:
                try:
                    ready = selector.select(timeout=0.05)
                except OSError:
                    break
                if ready:
                    try:
                        chunk = os.read(read_fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                    except OSError:
                        break
                    if chunk:
                        limited_chunk = self._apply_total_log_limit(chunk)
                        if limited_chunk:
                            buffer, dropping_long_line = self._consume_log_bytes(
                                stream,
                                buffer,
                                limited_chunk,
                                dropping_long_line=dropping_long_line,
                            )
                        continue
                    if not chunk and self._stop.is_set():
                        break
                elif self._stop.is_set():
                    break
            if buffer:
                self._emit_captured_line(stream, buffer.decode("utf-8", errors="replace"))
        except BaseException as exc:  # noqa: BLE001 - never leak a plain thread traceback to stderr
            self._emit_captured_line(
                "stderr",
                f"log capture worker failed: {type(exc).__name__}: {exc}",
            )
        finally:
            selector.close()

    def _apply_total_log_limit(self, chunk: bytes) -> bytes:
        with self._budget_lock:
            remaining = max(MAX_CAPTURED_LOG_BYTES - self._captured_bytes, 0)
            accepted = chunk[:remaining]
            self._captured_bytes += len(accepted)
            truncated = len(accepted) < len(chunk)
        if truncated:
            self._emit_truncation_once("total_log_limit")
        return accepted

    def _consume_log_bytes(
        self,
        stream: str,
        buffer: bytes,
        chunk: bytes,
        *,
        dropping_long_line: bool,
    ) -> tuple[bytes, bool]:
        while chunk:
            newline_index = chunk.find(b"\n")
            has_newline = newline_index >= 0
            segment = chunk[:newline_index] if has_newline else chunk
            chunk = chunk[newline_index + 1 :] if has_newline else b""

            if dropping_long_line:
                if has_newline:
                    dropping_long_line = False
                continue

            remaining = MAX_CAPTURED_LINE_BYTES - len(buffer)
            buffer += segment[:remaining]
            if len(segment) > remaining:
                self._emit_truncation_once("line_log_limit")
                if buffer:
                    self._emit_captured_line(stream, buffer.decode("utf-8", errors="replace"))
                buffer = b""
                dropping_long_line = not has_newline
                continue

            if has_newline:
                if buffer:
                    self._emit_captured_line(stream, buffer.decode("utf-8", errors="replace"))
                buffer = b""
        return buffer, dropping_long_line

    def _emit_truncation_once(self, reason: str) -> None:
        with self._budget_lock:
            if self._truncation_emitted:
                return
            self._truncation_emitted = True
        self._emit_structured_payload(
            {
                "type": "connector_event",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "plugin_log_truncated",
                "level": "warning",
                "message": "plugin log output was truncated by the runner",
                "reason": reason,
                "max_line_bytes": MAX_CAPTURED_LINE_BYTES,
                "max_total_bytes": MAX_CAPTURED_LOG_BYTES,
            },
            enforce_log_limit=False,
        )

    def _emit_captured_line(self, stream: str, message: str) -> None:
        payload = {
            "type": "connector_event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "plugin_log",
            "level": "info" if stream == "stdout" else "warning",
            "message": message,
            "stream": stream,
        }
        self._emit_structured_payload(payload)

    def _emit_structured_payload(
        self,
        payload: dict[str, Any],
        *,
        enforce_log_limit: bool = True,
    ) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        ).encode("utf-8")
        if enforce_log_limit:
            with self._budget_lock:
                available = max(
                    MAX_CAPTURED_LOG_BYTES
                    - TRUNCATION_EVENT_RESERVE_BYTES
                    - self._emitted_log_bytes,
                    0,
                )
                if len(encoded) > available:
                    should_truncate = True
                else:
                    self._emitted_log_bytes += len(encoded)
                    should_truncate = False
            if should_truncate:
                self._emit_truncation_once("structured_log_limit")
                return
        with self._write_lock:
            if self._saved_stderr >= 0:
                try:
                    os.write(self._saved_stderr, encoded)
                except OSError:
                    pass


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass


def emit_event(destination: TextIO, *, event: str, level: str, message: str, **fields: Any) -> None:
    """Write exactly one JSON Lines event to the runner's stderr stream."""
    payload = {
        "type": "connector_event",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "level": level,
        "message": message,
        **fields,
    }
    destination.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    )
    destination.flush()


def read_invocation(source: TextIO) -> ConnectorInvocation:
    """Read and validate the runner's single bounded stdin JSON document."""
    payload = source.read(MAX_STDIN_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_INPUT,
            "connector invocation exceeds the 1 MiB input limit",
        )
    try:
        raw_invocation = json.loads(payload)
        return ConnectorInvocation.model_validate(raw_invocation)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        details = (
            {"validation_errors": exc.errors(include_url=False, include_input=False)}
            if isinstance(exc, ValidationError)
            else {}
        )
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_INPUT,
            "stdin does not contain a valid connector invocation",
            details=details,
        ) from exc


def load_entrypoint(artifact: ConnectorArtifact) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Load the fixed ``crawler:crawl`` async entrypoint from a verified artifact."""
    module_name = f"discovery_connector_{artifact.manifest.connector_key}_{artifact.manifest.version}"
    spec = importlib.util.spec_from_file_location(module_name, artifact.connector_path)
    if spec is None or spec.loader is None:
        raise ConnectorProtocolError(
            ConnectorErrorCode.PLUGIN_LOAD_ERROR,
            "unable to create a module loader for crawler.py",
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ConnectorProtocolError(
            ConnectorErrorCode.PLUGIN_LOAD_ERROR,
            f"crawler.py import failed: {type(exc).__name__}: {exc}",
        ) from exc
    return _resolve_crawl(module)


def _resolve_crawl(module: ModuleType) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    crawl = getattr(module, "crawl", None)
    if not callable(crawl) or not inspect.iscoroutinefunction(crawl):
        raise ConnectorProtocolError(
            ConnectorErrorCode.PLUGIN_LOAD_ERROR,
            "crawler:crawl must be declared as async def crawl(request, context)",
        )
    parameters = list(inspect.signature(crawl).parameters.values())
    if len(parameters) != 2:
        raise ConnectorProtocolError(
            ConnectorErrorCode.PLUGIN_LOAD_ERROR,
            "crawler:crawl must accept exactly request and context parameters",
        )
    return crawl


async def execute_connector(
    artifact: ConnectorArtifact,
    invocation: ConnectorInvocation,
    *,
    timeout_seconds: float,
    stderr: TextIO,
    capture_file_descriptors: bool = True,
) -> ConnectorOutput:
    """Execute a verified connector and deterministically validate its result."""
    context = invocation.context
    manifest = artifact.manifest
    if (
        context.connector_key != manifest.connector_key
        or context.connector_version != manifest.version
        or set(context.allowed_domains) != set(manifest.allowed_domains)
    ):
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_INPUT,
            "invocation context does not match the verified connector Manifest",
        )

    text_log_budget = StructuredTextLogBudget(stderr)
    captured_stdout = StructuredStderrWriter(text_log_budget, stream="stdout")
    captured_stderr = StructuredStderrWriter(text_log_budget, stream="stderr")
    capture: AbstractContextManager[Any]
    if capture_file_descriptors:
        capture = FileDescriptorCapture()
    else:
        capture = _TextStreamCapture(captured_stdout, captured_stderr)
    with capture:
        crawl = load_entrypoint(artifact)
        try:
            raw_output = await asyncio.wait_for(
                crawl(
                    invocation.request.model_dump(mode="json", exclude_none=True),
                    invocation.context.model_dump(mode="json", exclude_none=True),
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise ConnectorProtocolError(
                ConnectorErrorCode.TIMEOUT,
                f"connector exceeded its {timeout_seconds:g} second wall-clock limit",
            ) from exc
        except ConnectorProtocolError:
            raise
        except Exception as exc:
            raise ConnectorProtocolError(
                ConnectorErrorCode.PLUGIN_EXECUTION_ERROR,
                f"connector execution failed: {type(exc).__name__}: {exc}",
            ) from exc
        finally:
            captured_stdout.flush()
            captured_stderr.flush()

    try:
        return ConnectorOutput.model_validate(raw_output)
    except ValidationError as exc:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_OUTPUT,
            "connector result does not match the {items, stats} contract",
            details={"validation_errors": exc.errors(include_url=False, include_input=False)},
        ) from exc


def run(
    connector_root: str | Path,
    *,
    kind: str,
    connector_key: str,
    version: int,
    timeout_seconds: float,
    expected_checksum: str,
    expected_signature: str,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> ConnectorExitCode:
    """Run one stdin-to-stdout connector invocation and return its exit code."""
    try:
        if kind not in {"sites", "shared"}:
            raise ConnectorProtocolError(
                ConnectorErrorCode.INVALID_INPUT,
                "connector kind must be sites or shared",
            )
        artifact = load_connector_artifact(
            connector_root,
            kind=kind,
            connector_key=connector_key,
            version=version,
        )
        if artifact.manifest.checksum != expected_checksum:
            raise ConnectorProtocolError(
                ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "artifact checksum differs from the host-approved checksum",
            )
        if artifact.signature != expected_signature:
            raise ConnectorProtocolError(
                ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "artifact signature differs from the host-approved signature",
            )
        invocation = read_invocation(stdin)
        capture_file_descriptors = stdout is sys.stdout and stderr is sys.stderr
        output = asyncio.run(
            execute_connector(
                artifact,
                invocation,
                timeout_seconds=timeout_seconds,
                stderr=stderr,
                capture_file_descriptors=capture_file_descriptors,
            )
        )
        _write_result_document(stdout, output)
        return ConnectorExitCode.SUCCESS
    except ConnectorProtocolError as exc:
        emit_event(
            stderr,
            event="runner_error",
            level="error",
            message=str(exc),
            code=exc.code.value,
            details=exc.details,
        )
        return exc.exit_code
    except BaseException as exc:  # noqa: BLE001 - runner must map all unexpected failures
        emit_event(
            stderr,
            event="runner_error",
            level="error",
            message=f"unexpected runner failure: {type(exc).__name__}: {exc}",
            code=ConnectorErrorCode.RUNTIME_ERROR.value,
            traceback=traceback.format_exc(limit=8),
        )
        return ConnectorExitCode.RUNTIME_ERROR


def _write_result_document(stdout: TextIO, output: ConnectorOutput) -> None:
    """Write the one result document completely across short pipe writes."""
    document = (output.model_dump_json(exclude_none=False) + "\n").encode("utf-8")
    if stdout is not sys.stdout:
        stdout.write(document.decode("utf-8"))
        stdout.flush()
        return
    stdout.flush()
    pending = memoryview(document)
    descriptor = stdout.fileno()
    while pending:
        try:
            written = os.write(descriptor, pending)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("connector stdout pipe accepted no result bytes")
        pending = pending[written:]


def _raise_cancelled(signum: int, _frame: Any) -> None:
    raise ConnectorProtocolError(
        ConnectorErrorCode.CANCELLED,
        f"connector received cancellation signal {signum}",
    )


class _TextStreamCapture(AbstractContextManager[None]):
    """StringIO-compatible fallback used by in-process protocol checks."""

    def __init__(self, stdout: TextIO, stderr: TextIO) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._stdout_redirect: Any = None
        self._stderr_redirect: Any = None

    def __enter__(self) -> None:
        self._stdout_redirect = redirect_stdout(self._stdout)
        self._stderr_redirect = redirect_stderr(self._stderr)
        self._stdout_redirect.__enter__()
        self._stderr_redirect.__enter__()
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._stderr_redirect.__exit__(exc_type, exc, tb)
        self._stdout_redirect.__exit__(exc_type, exc, tb)


class _RunnerArgumentParser(argparse.ArgumentParser):
    """Map CLI mistakes into the same structured runtime protocol."""

    def error(self, message: str) -> None:
        raise ConnectorProtocolError(
            ConnectorErrorCode.INVALID_INPUT,
            f"invalid runner arguments: {message}",
        )


def main() -> int:
    parser = _RunnerArgumentParser(
        description="Run one verified Discovery connector",
        add_help=False,
    )
    parser.add_argument("--connector-root", required=True)
    parser.add_argument("--kind", required=True, choices=("sites", "shared"))
    parser.add_argument("--connector-key", required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--expected-checksum", required=True)
    parser.add_argument("--expected-signature", required=True)
    try:
        args = parser.parse_args()
        if args.timeout_seconds <= 0:
            parser.error("--timeout-seconds must be greater than zero")
        signal.signal(signal.SIGTERM, _raise_cancelled)
        signal.signal(signal.SIGINT, _raise_cancelled)
        return int(
            run(
                args.connector_root,
                kind=args.kind,
                connector_key=args.connector_key,
                version=args.version,
                timeout_seconds=args.timeout_seconds,
                expected_checksum=args.expected_checksum,
                expected_signature=args.expected_signature,
            )
        )
    except ConnectorProtocolError as exc:
        emit_event(
            sys.stderr,
            event="runner_error",
            level="error",
            message=str(exc),
            code=exc.code.value,
            details=exc.details,
        )
        return int(exc.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
