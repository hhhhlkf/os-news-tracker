"""Strict data contracts shared by connector packaging and execution."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CONNECTOR_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_RUNTIME_VERSION = re.compile(r"^crawler-runtime:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MAX_CONNECTOR_TEXT_CHARS = 2_000


class ConnectorManifest(BaseModel):
    """Immutable metadata needed to validate and run one connector version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_type: Literal["python_plugin"] = "python_plugin"
    connector_key: str
    version: int = Field(ge=1)
    entry: str
    entrypoint: Literal["crawler:crawl"] = "crawler:crawl"
    runtime_version: str
    checksum: str
    allowed_domains: tuple[str, ...] = Field(min_length=1)
    # Publication timestamps are the normal news contract.  A ranking or
    # changing collection can instead be an observed snapshot: its items do
    # not invent publication dates and the host binds the observation time to
    # the sandbox invocation/attestation.
    time_semantics: Literal["publication", "snapshot"] = "publication"

    @field_validator("connector_key")
    @classmethod
    def validate_connector_key(cls, value: str) -> str:
        if not _CONNECTOR_KEY.fullmatch(value):
            raise ValueError("connector_key must be lowercase snake_case (2-64 characters)")
        return value

    @field_validator("runtime_version")
    @classmethod
    def validate_runtime_version(cls, value: str) -> str:
        if not _RUNTIME_VERSION.fullmatch(value):
            raise ValueError("runtime_version must be an immutable crawler-runtime:<version> tag")
        return value

    @field_validator("checksum")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        normalized = value.lower()
        if not _CHECKSUM.fullmatch(normalized):
            raise ValueError("checksum must be a lowercase SHA-256 hex digest")
        return normalized

    @field_validator("entry")
    @classmethod
    def validate_entry(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("entry must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("entry must not contain credentials")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def validate_allowed_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_value in values:
            value = raw_value.strip().rstrip(".").lower()
            try:
                value = value.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise ValueError("allowed_domains contains an invalid domain") from exc
            labels = value.split(".")
            if (
                len(value) > 253
                or len(labels) < 2
                or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
            ):
                raise ValueError("allowed_domains contains an invalid domain")
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_entry_domain(self) -> ConnectorManifest:
        entry_host = (urlsplit(self.entry).hostname or "").encode("idna").decode("ascii").lower()
        if entry_host not in self.allowed_domains:
            raise ValueError("entry hostname must be listed explicitly in allowed_domains")
        return self


class ConnectorRequest(BaseModel):
    """Public crawl parameters supplied to a connector."""

    model_config = ConfigDict(extra="allow")

    entry: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    target_count: int | None = Field(default=None, ge=1)
    start_at: str | None = None
    end_at: str | None = None


class ConnectorContext(BaseModel):
    """Non-secret, execution-scoped context available inside the sandbox."""

    model_config = ConfigDict(extra="forbid")

    run_id: int | str | None = None
    connector_key: str
    connector_version: int = Field(ge=1)
    allowed_domains: tuple[str, ...] = Field(min_length=1)


class ConnectorInvocation(BaseModel):
    """Single JSON document consumed from the runner's stdin."""

    model_config = ConfigDict(extra="forbid")

    request: ConnectorRequest
    context: ConnectorContext


class ConnectorItem(BaseModel):
    """One candidate news item returned by a connector."""

    model_config = ConfigDict(extra="allow")

    title: str
    url: str
    published_at: str | None = None
    summary: str | None = Field(default=None, max_length=MAX_CONNECTOR_TEXT_CHARS)
    content: str | None = Field(default=None, max_length=MAX_CONNECTOR_TEXT_CHARS)


class ConnectorStats(BaseModel):
    """Execution statistics returned alongside connector items."""

    model_config = ConfigDict(extra="allow")

    discovered_count: int = Field(ge=0)
    # These optional diagnostics intentionally remain extras.  Existing
    # connector output digests must not gain null fields merely because the
    # runtime learned to request better zero-result diagnostics.

    @model_validator(mode="after")
    def validate_optional_diagnostics(self) -> ConnectorStats:
        extras = self.model_extra or {}
        for field_name in ("candidate_count", "rejected_count"):
            if field_name not in extras:
                continue
            value = extras[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"stats.{field_name} must be a non-negative integer")
        if "rejection_reasons" not in extras:
            return self
        value = extras["rejection_reasons"]
        if not isinstance(value, dict) or len(value) > 20:
            raise ValueError("stats.rejection_reasons may contain at most 20 entries")
        normalized: dict[str, int] = {}
        for reason, count in value.items():
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
                raise ValueError("stats.rejection_reasons keys must be bounded non-empty strings")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("stats.rejection_reasons values must be non-negative integers")
            normalized[reason] = count
        extras["rejection_reasons"] = normalized
        return self


class ConnectorOutput(BaseModel):
    """The only JSON shape a successful connector may write to stdout."""

    model_config = ConfigDict(extra="forbid")

    items: list[ConnectorItem]
    stats: ConnectorStats

    @model_validator(mode="after")
    def validate_discovered_count(self) -> ConnectorOutput:
        if self.stats.discovered_count != len(self.items):
            raise ValueError("stats.discovered_count must equal the number of returned items")
        return self
