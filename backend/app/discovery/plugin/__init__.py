"""Versioned Discovery connector contracts and runtime protocol."""

from app.discovery.plugin.artifact import (
    ConnectorArtifact,
    compute_connector_checksum,
    compute_connector_signature,
    load_connector_artifact,
)
from app.discovery.plugin.contracts import (
    ConnectorContext,
    ConnectorInvocation,
    ConnectorItem,
    ConnectorManifest,
    ConnectorOutput,
    ConnectorRequest,
    ConnectorStats,
)
from app.discovery.plugin.errors import (
    ConnectorErrorCode,
    ConnectorExitCode,
    ConnectorProtocolError,
)

__all__ = [
    "ConnectorArtifact",
    "ConnectorContext",
    "ConnectorErrorCode",
    "ConnectorExitCode",
    "ConnectorInvocation",
    "ConnectorItem",
    "ConnectorManifest",
    "ConnectorOutput",
    "ConnectorProtocolError",
    "ConnectorRequest",
    "ConnectorStats",
    "compute_connector_checksum",
    "compute_connector_signature",
    "load_connector_artifact",
]
