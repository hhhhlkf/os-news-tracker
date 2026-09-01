"""key ordinary website crawl methods by normalized full entry URL

Revision ID: full_url_method_keys_20260831
Revises: agent_memory_cap_20260831
Create Date: 2026-08-31
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable, Sequence, Union
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import sqlalchemy as sa
from alembic import op


revision: str = "full_url_method_keys_20260831"
down_revision: Union[str, Sequence[str], None] = "agent_memory_cap_20260831"
branch_labels = None
depends_on = None


_ACTIVE_MIGRATION_STATES = {
    "shadow_pending",
    "shadow_running",
    "shadow_passed",
    "blocked",
    "ready",
    "cutover",
    "eligible",
}
_PLUGIN_PUBLISHED_MIGRATION_STATES = {"cutover", "eligible"}
_MIGRATION_STATE_RANK = {
    "shadow_pending": 0,
    "shadow_running": 1,
    "shadow_passed": 2,
    "blocked": 3,
    "ready": 4,
    "cutover": 5,
    "eligible": 6,
}


def _value(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _iter_actions(actions: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(actions, list):
        return
    for action in actions:
        if not isinstance(action, dict):
            continue
        yield action
        if action.get("op") == "loop":
            yield from _iter_actions(action.get("body"))
            yield from _iter_actions(action.get("on_each"))


def _is_feed_recipe(recipe: Any) -> bool:
    if not isinstance(recipe, dict):
        return False
    for action in _iter_actions(recipe.get("actions")):
        if action.get("op") == "fetch" and str(action.get("mode") or "").lower() in {
            "feed",
            "rss",
            "atom",
        }:
            return True
    return False


def _is_ordinary_website(row: dict[str, Any]) -> bool:
    domain = str(row.get("domain") or "")
    recipe = row.get("dsl_recipe")
    if domain.startswith(("feed:", "wechat_")) or _is_feed_recipe(recipe):
        return False
    source_kind = str(_value(recipe, "source_kind") or "").lower()
    if source_kind in {"wechat", "wechat_history", "internal_forum", "internal_mcp"}:
        return False
    if _value(recipe, "connector_kind") == "shared":
        return False
    parsed = urlparse(str(row.get("entry_url") or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _normalized_url(value: str) -> str:
    parsed = urlparse(value.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def _website_key(value: str) -> str:
    normalized = _normalized_url(value)
    if len(normalized) <= 255:
        return normalized
    parsed = urlparse(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"site:{parsed.netloc[:210]}:{digest}"[:255]


def _method_recency_key(row: dict[str, Any]) -> tuple[bool, str, int]:
    """Order reviewed methods deterministically by review time, then ID."""
    reviewed_at = row.get("reviewed_at")
    return reviewed_at is not None, str(reviewed_at or ""), int(row["id"])


def _tables() -> tuple[sa.Table, sa.Table, sa.Table, sa.Table, sa.Table]:
    metadata = sa.MetaData()
    methods = sa.Table(
        "crawl_methods",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("domain", sa.String(255)),
        sa.Column("entry_url", sa.String(1000)),
        sa.Column("dsl_recipe", sa.JSON()),
        sa.Column("status", sa.String(20)),
        sa.Column("review_status", sa.String(20)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("source_id", sa.Integer()),
    )
    mappings = sa.Table(
        "crawl_method_domains",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("domain", sa.String(255)),
        sa.Column("method_id", sa.Integer()),
    )
    migrations = sa.Table(
        "discovery_method_migrations",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("domain", sa.String(255)),
        sa.Column("legacy_method_id", sa.Integer()),
        sa.Column("plugin_method_id", sa.Integer()),
        sa.Column("state", sa.String(30)),
        sa.Column("last_error", sa.Text()),
    )
    comparisons = sa.Table(
        "discovery_migration_comparisons",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("migration_id", sa.Integer()),
        sa.Column("status", sa.String(20)),
        sa.Column("error_message", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    sources = sa.Table(
        "sources",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("enabled", sa.Boolean()),
    )
    return methods, mappings, migrations, comparisons, sources


def upgrade() -> None:
    bind = op.get_bind()
    methods, mappings, migrations, comparisons, sources = _tables()
    method_rows = [dict(row) for row in bind.execute(sa.select(methods)).mappings()]
    methods_by_id = {int(row["id"]): row for row in method_rows}
    affected = [row for row in method_rows if _is_ordinary_website(row)]
    if not affected:
        return

    new_keys = {int(row["id"]): _website_key(str(row["entry_url"])) for row in affected}
    affected_ids = set(new_keys)
    old_mappings = [
        dict(row)
        for row in bind.execute(sa.select(mappings)).mappings()
        if int(row["method_id"]) in affected_ids
    ]
    published_ids = {int(row["method_id"]) for row in old_mappings}
    published_keys_by_old_domain: dict[str, set[str]] = defaultdict(set)
    for row in old_mappings:
        method_id = int(row["method_id"])
        owner = methods_by_id[method_id]
        published_keys_by_old_domain[str(owner["domain"])].add(new_keys[method_id])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in affected:
        grouped[new_keys[int(row["id"])]].append(row)
    published_winner_by_key: dict[str, dict[str, Any]] = {}
    for key, rows in grouped.items():
        published = [row for row in rows if int(row["id"]) in published_ids]
        if published:
            published_winner_by_key[key] = max(
                published,
                key=lambda row: (row["status"] == "active", *_method_recency_key(row)),
            )

    # Remove only mappings owned by affected ordinary-site methods. Feed,
    # WeChat and the internal-forum seam retain their existing identities.
    if published_ids:
        bind.execute(sa.delete(mappings).where(mappings.c.method_id.in_(published_ids)))

    # A migration can only follow the plugin to its normalized URL identity
    # when its rollback method has the same identity. Historical hostname
    # collisions could pair unrelated entry paths; terminate those records
    # rather than leaving a stranded active handover on either new mapping.
    migration_rows = [dict(row) for row in bind.execute(sa.select(migrations)).mappings()]
    migration_keys: dict[int, str] = {}
    valid_active_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    superseded: dict[int, str] = {}
    for row in migration_rows:
        migration_id = int(row["id"])
        plugin_id = int(row["plugin_method_id"])
        if plugin_id not in new_keys:
            continue
        plugin_key = new_keys[plugin_id]
        migration_keys[migration_id] = plugin_key
        legacy_id = row.get("legacy_method_id")
        legacy = methods_by_id.get(int(legacy_id)) if legacy_id is not None else None
        legacy_key = (
            new_keys.get(int(legacy_id), str(legacy["domain"]))
            if legacy is not None and legacy_id is not None
            else None
        )
        if row["state"] not in _ACTIVE_MIGRATION_STATES:
            continue
        if legacy_key != plugin_key:
            superseded[migration_id] = (
                "superseded while normalizing full entry URL method keys: "
                "legacy and plugin entry URLs differ"
            )
            continue
        expected = published_winner_by_key.get(plugin_key)
        published_owner_id = (
            plugin_id
            if row["state"] in _PLUGIN_PUBLISHED_MIGRATION_STATES
            else int(legacy_id) if legacy_id is not None else None
        )
        if expected is None or published_owner_id != int(expected["id"]):
            superseded[migration_id] = (
                "superseded while normalizing full entry URL method keys: "
                "migration state owner is not the reconstructed published mapping owner"
            )
            continue
        valid_active_by_key[plugin_key].append(row)

    # Even malformed historical data may contain more than one active record
    # for an equivalent normalized identity. Retain at most one, and only
    # after the published mapping owner check above succeeds.
    for rows in valid_active_by_key.values():
        winner = max(
            rows,
            key=lambda row: (_MIGRATION_STATE_RANK[str(row["state"])], int(row["id"])),
        )
        for row in rows:
            if row["id"] != winner["id"]:
                superseded[int(row["id"])] = (
                    "superseded while normalizing full entry URL method keys: "
                    "another migration owns the reconstructed published mapping"
                )

    for migration_id, error in superseded.items():
        bind.execute(
            sa.update(migrations)
            .where(migrations.c.id == migration_id)
            .values(
                state="shadow_failed",
                legacy_method_id=None,
                last_error=error,
            )
        )
        bind.execute(
            sa.update(comparisons)
            .where(
                comparisons.c.migration_id == migration_id,
                comparisons.c.status.in_(("queued", "running")),
            )
            .values(
                status="failed",
                error_message=error,
                completed_at=sa.func.now(),
            )
        )

    # Rekey terminal history and the single valid active survivor. Mismatched
    # terminal history keeps its original audit domain.
    for row in migration_rows:
        migration_id = int(row["id"])
        key = migration_keys.get(migration_id)
        if key is None or migration_id in superseded:
            continue
        legacy_id = row.get("legacy_method_id")
        legacy = methods_by_id.get(int(legacy_id)) if legacy_id is not None else None
        legacy_key = (
            new_keys.get(int(legacy_id), str(legacy["domain"]))
            if legacy is not None and legacy_id is not None
            else None
        )
        if legacy_key is not None and legacy_key != key:
            continue
        bind.execute(
            sa.update(migrations)
            .where(migrations.c.id == migration_id)
            .values(domain=key)
        )

    for method_id, key in new_keys.items():
        bind.execute(
            sa.update(methods).where(methods.c.id == method_id).values(domain=key)
        )

    sources_to_reconcile: set[int] = set()
    for key, rows in grouped.items():
        published = [row for row in rows if int(row["id"]) in published_ids]
        if published:
            # Preserve the status (including explicit disabled) and Source
            # enabled flag of the pre-migration published mapping. Only an
            # abnormal duplicate mapping needs a deterministic loser.
            winner = published_winner_by_key[key]
            for loser in published:
                if loser["id"] == winner["id"]:
                    continue
                if loser["status"] == "active":
                    bind.execute(
                        sa.update(methods)
                        .where(methods.c.id == int(loser["id"]))
                        .values(status="inactive")
                    )
                sources_to_reconcile.add(int(loser["source_id"]))
            bind.execute(sa.insert(mappings).values(domain=key, method_id=int(winner["id"])))
            continue

        # No old mapping owned this new URL identity. The old hostname
        # collision proves that it needs an independent mapping, but does not
        # prove that an inactive/disabled method was safe to enable. Publish
        # the newest approved candidate while preserving its status; a user
        # can explicitly enable this now-addressable URL afterward.
        collision_domains = {
            old_domain
            for old_domain, published_keys in published_keys_by_old_domain.items()
            if key not in published_keys
        }
        suppressed = [
            row
            for row in rows
            if (
                row["review_status"] == "approved"
                and row["status"] in {"inactive", "disabled"}
                and str(row["domain"]) in collision_domains
            )
        ]
        if not suppressed:
            continue
        winner = max(suppressed, key=_method_recency_key)
        bind.execute(
            sa.insert(mappings).values(domain=key, method_id=int(winner["id"]))
        )
        sources_to_reconcile.add(int(winner["source_id"]))

    # Mapping losers and newly split inactive/disabled winners no longer make
    # their Source runnable. Preserve a shared Source only if another active
    # published method still owns it.
    for source_id in sources_to_reconcile:
        active_mapping = bind.execute(
            sa.select(mappings.c.id)
            .select_from(mappings.join(methods, mappings.c.method_id == methods.c.id))
            .where(methods.c.source_id == source_id, methods.c.status == "active")
            .limit(1)
        ).first()
        bind.execute(
            sa.update(sources)
            .where(sources.c.id == source_id)
            .values(enabled=active_mapping is not None)
        )


def downgrade() -> None:
    raise RuntimeError(
        "full URL method keys cannot be safely collapsed back to hostname keys without losing distinct active sources"
    )
