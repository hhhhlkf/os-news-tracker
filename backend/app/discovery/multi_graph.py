"""Stable multi-source Discovery facade.

Ordinary websites and WeChat are delegated exclusively to the connector loops.
The internal-forum branch intentionally remains an interface/routing seam; this
refactor does not implement its transport.
"""

from __future__ import annotations

from typing import Any

from app.discovery.contracts import SourceRoute
from app.discovery.multi_routes import apply_route_choice, route_input
from app.discovery.naming import (
    default_website_display_name,
    format_wechat_history_display_name,
    format_wechat_search_display_name,
)


# Historical callers import this name. Keep the routing contract without
# retaining the website/WeChat DSL graph behind it.
source_router_for_input = route_input


def start_website_discovery_run(
    site_url: str,
    *,
    force: bool = True,
    name: str | None = None,
) -> int:
    """Start the in-process Single Agent Loop for an ordinary website."""
    from app.discovery.loop.engine import start_website_loop_run

    return start_website_loop_run(site_url, force=force, name=name)


def start_multi_discovery_run(
    raw_input: str,
    *,
    force: bool = True,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
    selected_route_type: str | None = None,
    route_source: str = "inferred",
) -> dict[str, Any]:
    """Route public Discovery requests without importing legacy DSL code."""
    route = source_router_for_input(raw_input, hints)
    apply_route_choice(route, selected_route_type)

    if route.kind == "website":
        display_name = name or default_website_display_name(route.normalized_input)
        run_id = start_website_discovery_run(
            route.normalized_input,
            force=force,
            name=display_name,
        )
        return _started_result(
            run_id,
            route,
            resolved_route_type="website",
            route_source=route_source,
            delegated="website_discovery",
        )

    if route.kind == "wechat":
        from app.discovery.wechat_plugin import start_wechat_discovery_run

        display_name = name or (
            format_wechat_search_display_name(route.normalized_input)
            if route.input_type == "wechat_search"
            else format_wechat_history_display_name(route.normalized_input)
        )
        run_id = start_wechat_discovery_run(
            route.normalized_input,
            input_type=route.input_type,
            force=force,
            name=display_name,
            hints=hints,
        )
        return _started_result(
            run_id,
            route,
            resolved_route_type=selected_route_type or route.input_type,
            route_source=route_source,
            delegated="wechat_shared_plugin",
        )

    # Internal forum is deliberately a compatibility contract only. It does
    # not share the website/WeChat connector or their retired generators.
    return {
        "status": "accepted",
        "run_id": None,
        "route": route.model_dump(),
        "branch_artifact": _internal_forum_contract(route),
        "resolved_route_type": selected_route_type or route.input_type,
        "route_source": route_source,
    }


def _started_result(
    run_id: int,
    route: SourceRoute,
    *,
    resolved_route_type: str,
    route_source: str,
    delegated: str,
) -> dict[str, Any]:
    return {
        "status": "started",
        "run_id": run_id,
        "route": route.model_dump(),
        "resolved_route_type": resolved_route_type,
        "route_source": route_source,
        "delegated": delegated,
    }


def _internal_forum_contract(route: SourceRoute) -> dict[str, Any]:
    if route.kind == "internal_mcp":
        return {
            "source_kind": "internal_mcp",
            "status": "needs_implementation",
            "markers": route.markers,
            "query": route.normalized_input,
            "mcp_action_contract": {
                "op": "mcp_call",
                "server": "km | iwiki",
                "tool": "search_articles",
                "args": {"query": route.normalized_input, "author": None},
                "as": "last_fetch",
            },
        }
    return {
        "source_kind": route.kind,
        "status": "unsupported",
        "reason": route.reason,
    }
