"""Reviewed plugin recipe envelope, including per-method public configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.discovery.plugin.artifact import compute_connector_signature
from app.discovery.plugin.contracts import ConnectorManifest, ConnectorOutput


WECHAT_CHECKPOINT_MAX_ITEMS = 50
WECHAT_CHECKPOINT_MAX_TITLE_CHARS = 2_000
WECHAT_CHECKPOINT_MAX_URL_CHARS = 8_000
WECHAT_CHECKPOINT_MAX_DATE_CHARS = 200
WECHAT_CHECKPOINT_MAX_TEXT_CHARS = 100_000


class WechatCheckpointOutput(BaseModel):
    """All-or-incomplete snapshot; a sample can never masquerade as a full run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: bool
    item_count: int = Field(ge=0)
    output: ConnectorOutput | None = None
    incomplete_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_complete_snapshot(self) -> "WechatCheckpointOutput":
        if not self.complete:
            if self.output is not None or not self.incomplete_reason:
                raise ValueError("incomplete checkpoint output must omit output and include a reason")
            return self
        if self.output is None or len(self.output.items) != self.item_count:
            raise ValueError("complete checkpoint output must contain every reported item")
        if self.item_count > WECHAT_CHECKPOINT_MAX_ITEMS:
            raise ValueError("complete checkpoint output exceeds the 50-item restore contract")
        for item in self.output.items:
            fields = {
                "title": (item.title, WECHAT_CHECKPOINT_MAX_TITLE_CHARS),
                "url": (item.url, WECHAT_CHECKPOINT_MAX_URL_CHARS),
                "published_at": (item.published_at or "", WECHAT_CHECKPOINT_MAX_DATE_CHARS),
                "summary": (item.summary or "", WECHAT_CHECKPOINT_MAX_TEXT_CHARS),
                "content": (item.content or "", WECHAT_CHECKPOINT_MAX_TEXT_CHARS),
            }
            for name, (value, maximum) in fields.items():
                if len(value) > maximum:
                    raise ValueError(f"checkpoint item {name} exceeds {maximum} characters")
        return self


class WechatSogouConfig(BaseModel):
    """Only public, deterministic inputs accepted by the shared connector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_name: str = Field(default="", max_length=120)
    keywords: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    max_pages: int = Field(default=6, ge=1, le=10)
    limit: int = Field(default=120, ge=5, le=120)

    @field_validator("account_name")
    @classmethod
    def normalize_account_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in values:
            value = " ".join(raw.strip().split())[:120]
            if value and value not in normalized:
                normalized.append(value)
        return tuple(normalized)

    def model_post_init(self, __context: Any) -> None:
        if not self.account_name and not self.keywords:
            raise ValueError("account_name or keywords is required")


class ConfiguredPluginRecipe(BaseModel):
    """A reviewed Manifest plus non-secret invocation configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_type: Literal["python_plugin"] = "python_plugin"
    connector_kind: Literal["shared"] = "shared"
    manifest: ConnectorManifest
    config: WechatSogouConfig


@dataclass(frozen=True)
class ResolvedPluginRecipe:
    manifest: ConnectorManifest
    connector_kind: Literal["sites", "shared"]
    config: dict[str, Any]
    reviewed_signature: str


def configured_plugin_signature(recipe: ConfiguredPluginRecipe) -> str:
    """Bind shared code identity and each account's reviewed public configuration."""
    document = {
        "artifact_signature": compute_connector_signature(recipe.manifest),
        "connector_kind": recipe.connector_kind,
        "config": recipe.config.model_dump(mode="json"),
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def configured_plugin_config_hash(config: WechatSogouConfig | dict[str, Any]) -> str:
    """Canonical public-config binding used by trial and human review evidence."""
    normalized = (
        config.model_dump(mode="json")
        if isinstance(config, WechatSogouConfig)
        else WechatSogouConfig.model_validate(config).model_dump(mode="json")
    )
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_plugin_recipe(raw: dict[str, Any]) -> ResolvedPluginRecipe:
    """Accept historical website Manifests and the strict shared connector envelope."""
    if "manifest" not in raw:
        manifest = ConnectorManifest.model_validate(raw)
        return ResolvedPluginRecipe(
            manifest=manifest,
            connector_kind="sites",
            config={},
            reviewed_signature=compute_connector_signature(manifest),
        )
    recipe = ConfiguredPluginRecipe.model_validate(raw)
    if recipe.manifest.connector_key != "wechat_sogou":
        raise ValueError("only wechat_sogou may use the shared configured connector recipe")
    return ResolvedPluginRecipe(
        manifest=recipe.manifest,
        connector_kind=recipe.connector_kind,
        config=recipe.config.model_dump(mode="json"),
        reviewed_signature=configured_plugin_signature(recipe),
    )
