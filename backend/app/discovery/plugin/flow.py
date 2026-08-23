"""Safe, read-only execution-flow summaries derived from connector source AST."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.discovery.plugin.artifact import load_connector_artifact
from app.discovery.plugin.recipe import resolve_plugin_recipe


def describe_plugin_execution(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a plugin artifact and summarize its crawler without executing it."""
    artifact = _load_plugin_artifact(recipe)
    source = artifact.connector_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename="crawler.py")
    calls = {_call_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    strings = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    request_fields = sorted(_request_fields(tree))
    steps: list[dict[str, Any]] = [{
        "icon": "⇥",
        "title": "读取运行参数",
        "detail": "、".join(request_fields) if request_fields else "entry、config",
        "tags": ["条目上限可控" if "target_count" in request_fields else "运行后统一限量"],
    }]
    if any("sitemap" in value for value in strings):
        steps.append({"icon": "⌘", "title": "读取 Sitemap", "detail": "从站点地图发现候选文章 URL"})
    if any(name.startswith("feedparser.") for name in calls):
        steps.append({"icon": "≋", "title": "解析 RSS / Atom", "detail": "读取订阅条目及发布时间"})
    transport_tags: list[str] = []
    if any(name.startswith("httpx.") or name.endswith(".get") or name.endswith(".request") for name in calls):
        transport_tags.append("HTTP")
    if any("playwright" in name for name in calls):
        transport_tags.append("浏览器渲染")
    if transport_tags:
        steps.append({
            "icon": "↓", "title": "请求公开页面",
            "detail": "仅访问 Manifest 声明域名", "tags": transport_tags,
        })
    parse_tags: list[str] = []
    if any("fromstring" in name for name in calls):
        parse_tags.append("HTML/XML")
    xpath_count = sum(1 for name in calls if name.endswith(".xpath"))
    if xpath_count:
        parse_tags.append("XPath")
    if parse_tags:
        steps.append({"icon": "⊞", "title": "解析列表与文章字段", "detail": "提取标题、链接、时间和正文", "tags": parse_tags})
    semaphore = _integer_call_argument(tree, "Semaphore")
    if any(name.endswith(".gather") for name in calls) or semaphore is not None:
        steps.append({
            "icon": "⇉", "title": "并发抓取详情页",
            "detail": "并发数由 Connector 代码限制",
            "tags": [f"最多 {semaphore} 并发"] if semaphore is not None else [],
        })
    if {"start_at", "end_at"} & set(request_fields):
        steps.append({"icon": "◷", "title": "按发布时间过滤", "detail": "应用本次查询的起止时间范围"})
    if "page" in strings or any("next_page" in value or "has_more" in value for value in strings):
        steps.append({"icon": "↻", "title": "按需循环翻页", "detail": "达到目标条数、没有下一页或页数上限时停止"})
    if any("text_content" in name for name in calls) or any("clean" in name for name in calls):
        steps.append({"icon": "▤", "title": "清洗并限制正文", "detail": "输出纯文本，合同强制每项最多 2000 字"})
    steps.extend((
        {"icon": "#", "title": "限制返回条目", "detail": "Connector 优先按 target_count 抓取，Runner 再统一裁剪"},
        {"icon": "✓", "title": "返回标准结果", "detail": "校验 items 与 stats.discovered_count 后进入新闻 Pipeline"},
    ))
    return steps[:10]


def read_plugin_source(recipe: dict[str, Any]) -> str:
    """Return checksum-validated connector source for an authorized reviewer."""
    return _load_plugin_artifact(recipe).connector_path.read_text(encoding="utf-8")


def _load_plugin_artifact(recipe: dict[str, Any]):
    if recipe.get("recipe_type") != "python_plugin":
        raise ValueError("method is not a Python connector")
    resolved = resolve_plugin_recipe(recipe)
    settings = get_settings()
    root = Path(settings.discovery_connector_root)
    version_dir = root / resolved.connector_kind / resolved.manifest.connector_key / f"v{resolved.manifest.version}"
    if not version_dir.exists() and resolved.connector_kind == "shared":
        from app.discovery.wechat_plugin import bundled_connector_root

        root = bundled_connector_root()
    return load_connector_artifact(
        root,
        kind=resolved.connector_kind,
        connector_key=resolved.manifest.connector_key,
        version=resolved.manifest.version,
    )


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _request_fields(tree: ast.AST) -> set[str]:
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "request" and node.func.attr == "get":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    fields.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "request":
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                fields.add(node.slice.value)
    return fields


def _integer_call_argument(tree: ast.AST, suffix: str) -> int | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _call_name(node.func).endswith(suffix) or not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return value.value
    return None
