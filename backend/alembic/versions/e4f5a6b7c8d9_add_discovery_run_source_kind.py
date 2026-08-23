"""Persist the trusted source kind for Discovery runs.

Revision ID: e4f5a6b7c8d9
Revises: c3d4e5f6a7b8
"""

from collections.abc import Sequence
import json
from typing import Any
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_KINDS = {"website", "wechat", "internal_forum"}
_WECHAT_HOSTS = {"mp.weixin.qq.com", "weixin.qq.com", "weixin.sogou.com"}


def _json_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _explicit_source_kind(value: object) -> str | None:
    """Read a backend-persisted source discriminator, never infer from free text."""
    value = _json_value(value)
    if isinstance(value, dict):
        kind = value.get("source_kind")
        if kind == "internal_mcp":
            return "internal_forum"
        if kind in _SOURCE_KINDS:
            return str(kind)
    elif isinstance(value, list):
        for nested in value:
            found = _explicit_source_kind(nested)
            if found is not None:
                return found
    return None


def _recipe_source_kind(value: object) -> str | None:
    recipe = _json_value(value)
    if not isinstance(recipe, dict):
        return None
    manifest = _json_value(recipe.get("manifest"))
    manifest = manifest if isinstance(manifest, dict) else {}
    connector_key = manifest.get("connector_key") or recipe.get("connector_key")
    connector_kind = recipe.get("connector_kind") or manifest.get("connector_kind")
    if connector_key == "wechat_sogou" or connector_kind == "shared":
        return "wechat" if connector_key == "wechat_sogou" else None
    if recipe.get("recipe_type") == "python_plugin" and connector_kind == "sites":
        return "website"
    return _explicit_source_kind(recipe)


def _historical_url_source_kind(site_url: object) -> str:
    """Conservative final backfill for old rows; runtime never uses this heuristic."""
    if not isinstance(site_url, str):
        return "unknown"
    parsed = urlsplit(site_url.strip())
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if scheme in {"wechat", "weixin"} or host in _WECHAT_HOSTS:
        return "wechat"
    if scheme in {"http", "https"} and host:
        return "website"
    return "unknown"


def upgrade() -> None:
    op.add_column(
        "site_discovery_runs",
        sa.Column("source_kind", sa.String(length=20), server_default="unknown", nullable=False),
    )
    bind = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("site_discovery_runs", metadata, autoload_with=bind)
    methods = sa.Table("crawl_methods", metadata, autoload_with=bind)
    events = sa.Table("discovery_run_events", metadata, autoload_with=bind)

    method_recipes = {
        row.id: row.dsl_recipe
        for row in bind.execute(sa.select(methods.c.id, methods.c.dsl_recipe))
    }
    event_kinds: dict[int, str] = {}
    event_rows = bind.execute(
        sa.select(events.c.run_id, events.c.payload)
        .where(events.c.event_type == "phase_changed")
        .order_by(events.c.run_id, events.c.sequence)
    )
    for row in event_rows:
        explicit = _explicit_source_kind(row.payload)
        if explicit is not None:
            event_kinds[row.run_id] = explicit

    for row in bind.execute(
        sa.select(
            runs.c.id,
            runs.c.site_url,
            runs.c.resulting_method_id,
            runs.c.node_trace,
        )
    ):
        source_kind = None
        if row.resulting_method_id is not None:
            source_kind = _recipe_source_kind(method_recipes.get(row.resulting_method_id))
        source_kind = source_kind or event_kinds.get(row.id)
        source_kind = source_kind or _explicit_source_kind(row.node_trace)
        source_kind = source_kind or _historical_url_source_kind(row.site_url)
        bind.execute(
            sa.update(runs).where(runs.c.id == row.id).values(source_kind=source_kind)
        )


def downgrade() -> None:
    op.drop_column("site_discovery_runs", "source_kind")
