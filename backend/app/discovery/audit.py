"""Unified audit entrypoint for discovery recipe probes.

This module unifies the audit interface without forcing every source kind to
share the same audit strategy. Website discovery can keep its graph/LLM audit,
while WeChat and other deterministic multi discovery branches use lightweight
run-result audits.
"""

from __future__ import annotations

from typing import Any


def audit_discovery_recipe(
    *,
    source_kind: str,
    input_type: str,
    recipe: dict[str, Any],
    items: list,
    status: str | None = None,
    website_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit a discovery recipe result through one public interface."""
    discovered_count = len(items)
    if source_kind == "website":
        return _finalize_website_audit(
            website_result or {},
            input_type=input_type,
            discovered_count=discovered_count,
        )
    return _audit_lightweight_result(
        source_kind=source_kind,
        input_type=input_type,
        recipe=recipe,
        discovered_count=discovered_count,
        status=status,
    )


def _finalize_website_audit(
    result: dict[str, Any],
    *,
    input_type: str,
    discovered_count: int,
) -> dict[str, Any]:
    passed = bool(result.get("passed"))
    reason = "website_graph_passed" if passed else "website_graph_rejected"
    if result.get("errors"):
        reason = "website_graph_errors"
    elif result.get("test", {}).get("error"):
        reason = "website_graph_runtime_error"
    elif result.get("decision") == "rewrite":
        reason = "website_graph_rewrite"
    elif result.get("decision") == "reexplore":
        reason = "website_graph_reexplore"

    return {
        **result,
        "audit_kind": "website_graph_audit",
        "input_type": input_type,
        "passed": passed,
        "method_status": "active" if passed else "failed",
        "required_count": 1,
        "discovered_count": discovered_count,
        "reason": result.get("reason") or reason,
    }


def _audit_lightweight_result(
    *,
    source_kind: str,
    input_type: str,
    recipe: dict[str, Any],
    discovered_count: int,
    status: str | None,
) -> dict[str, Any]:
    required_count = _required_count(recipe)
    audit_kind = _audit_kind(source_kind=source_kind, input_type=input_type)
    if status in {"pending_auth", "auth_invalid"}:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": False,
            "method_status": status,
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": status,
        }
    if status in {"rate_limited", "captcha_required"}:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": False,
            "method_status": "retry_later",
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": status,
        }
    if discovered_count >= required_count:
        return {
            "audit_kind": audit_kind,
            "input_type": input_type,
            "passed": True,
            "method_status": "active",
            "required_count": required_count,
            "discovered_count": discovered_count,
            "reason": "enough_items",
        }
    return {
        "audit_kind": audit_kind,
        "input_type": input_type,
        "passed": False,
        "method_status": "failed",
        "required_count": required_count,
        "discovered_count": discovered_count,
        "reason": "insufficient_items",
    }


def _audit_kind(*, source_kind: str, input_type: str) -> str:
    if source_kind == "wechat" and input_type == "wechat_search":
        return "wechat_search_audit"
    if source_kind == "wechat" and input_type in {"wechat_history", "wechat_history_url"}:
        return "wechat_history_audit"
    if source_kind == "website":
        return "website_graph_audit"
    return f"{source_kind}_audit"


def _required_count(recipe: dict[str, Any]) -> int:
    limit = 20
    for action in recipe.get("actions") or []:
        if action.get("op") in {"wechat_fetch_account_history", "wechat_search_articles"}:
            if action.get("limit") is not None:
                limit = int(action["limit"])
            break
    return min(5, max(1, int(limit * 0.25)))
