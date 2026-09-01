"""Machine-readable connector failures and stable process exit codes."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ConnectorExitCode(IntEnum):
    """Process statuses shared by the runner and the future SandboxRuntime."""

    SUCCESS = 0
    INVALID_INPUT = 10
    INVALID_MANIFEST = 11
    ARTIFACT_INTEGRITY_ERROR = 12
    PLUGIN_LOAD_ERROR = 20
    PLUGIN_EXECUTION_ERROR = 21
    INVALID_OUTPUT = 22
    NETWORK_ERROR = 23
    TIMEOUT = 30
    CANCELLED = 31
    RUNTIME_ERROR = 40


class ConnectorErrorCode(StrEnum):
    """Stable error identifiers persisted in events and returned to callers."""

    INVALID_INPUT = "connector_invalid_input"
    INVALID_MANIFEST = "connector_invalid_manifest"
    ARTIFACT_INTEGRITY_ERROR = "connector_artifact_integrity_error"
    PLUGIN_LOAD_ERROR = "connector_plugin_load_error"
    PLUGIN_EXECUTION_ERROR = "connector_plugin_execution_error"
    INVALID_OUTPUT = "connector_invalid_output"
    NETWORK_ERROR = "connector_network_error"
    TIMEOUT = "connector_timeout"
    CANCELLED = "connector_cancelled"
    RUNTIME_ERROR = "connector_runtime_error"


_EXIT_CODES = {
    ConnectorErrorCode.INVALID_INPUT: ConnectorExitCode.INVALID_INPUT,
    ConnectorErrorCode.INVALID_MANIFEST: ConnectorExitCode.INVALID_MANIFEST,
    ConnectorErrorCode.ARTIFACT_INTEGRITY_ERROR: ConnectorExitCode.ARTIFACT_INTEGRITY_ERROR,
    ConnectorErrorCode.PLUGIN_LOAD_ERROR: ConnectorExitCode.PLUGIN_LOAD_ERROR,
    ConnectorErrorCode.PLUGIN_EXECUTION_ERROR: ConnectorExitCode.PLUGIN_EXECUTION_ERROR,
    ConnectorErrorCode.INVALID_OUTPUT: ConnectorExitCode.INVALID_OUTPUT,
    ConnectorErrorCode.NETWORK_ERROR: ConnectorExitCode.NETWORK_ERROR,
    ConnectorErrorCode.TIMEOUT: ConnectorExitCode.TIMEOUT,
    ConnectorErrorCode.CANCELLED: ConnectorExitCode.CANCELLED,
    ConnectorErrorCode.RUNTIME_ERROR: ConnectorExitCode.RUNTIME_ERROR,
}


class ConnectorProtocolError(RuntimeError):
    """A connector failure carrying a stable code and safe public details."""

    def __init__(
        self,
        code: ConnectorErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    @property
    def exit_code(self) -> ConnectorExitCode:
        return _EXIT_CODES[self.code]
