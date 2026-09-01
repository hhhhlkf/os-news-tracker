"""Host-safe structural validation for generated connector source."""

from __future__ import annotations

import ast


def validate_connector_source(source: str) -> None:
    """Reject syntax and entrypoint errors without importing untrusted code."""
    if not source.strip():
        raise ValueError("crawler.py must be non-empty")
    try:
        tree = ast.parse(source, filename="crawler.py")
    except SyntaxError as exc:
        raise ValueError(f"crawler.py syntax error: {exc.msg}") from exc
    crawls = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "crawl"
    ]
    if len(crawls) != 1:
        raise ValueError("crawler.py must define one top-level async def crawl(request, context)")
    if [argument.arg for argument in crawls[0].args.args] != ["request", "context"]:
        raise ValueError("crawler.py crawl entrypoint parameters must be request, context")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else None
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if attribute in {"now", "utcnow", "today", "time"} or name in {"now", "utcnow", "today"}:
            raise ValueError(
                "crawler.py must not generate timestamps locally; use observed page dates or the host snapshot contract"
            )
