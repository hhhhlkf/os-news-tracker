"""多来源探查的输入分流。

功能：把用户输入规范化，并确定应进入网站、微信或内部来源分支。
由谁调用：``multi_graph`` 在启动和执行多来源探查时调用。
会调用谁：仅调用 URL 解析与 ``SourceRoute`` 数据合同；不执行抓取、不写数据库。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.discovery.contracts import SourceRoute


def normalize_input(raw_input: str) -> str:
    """整理用户输入中的空白。

    功能：去掉首尾空白，并把连续空白压成一个空格，供后续路由与去重稳定使用。
    谁会调用：``route_input`` 和 ``multi_graph`` 的启动流程。
    直接调用：无，仅做字符串处理。
    输入与结果：输入原始文本；返回规范化文本。
    副作用：无。
    """
    return " ".join((raw_input or "").strip().split())


def route_input(raw_input: str, hints: dict[str, Any] | None = None) -> SourceRoute:
    """判断输入应走哪一种探查分支。

    功能：根据内部标记、显式 hints、URL 特征和纯文本兜底规则，返回网站、微信或内部来源的路由结果。
    谁会调用：``multi_graph`` 的同步分派、后台运行和启动入口。
    直接调用：
    - ``normalize_input(...)``：先统一输入格式。
    - ``_wechat_input_type(...)``：区分微信历史地址与搜索词。
    - ``urlparse(...)``：识别普通 URL 和微信文章 URL。
    输入与结果：输入原始文本和可选 hints；返回 ``SourceRoute``。
    副作用：无。
    """
    normalized = normalize_input(raw_input)
    hints = hints or {}
    hinted_kind = str(hints.get("source_kind") or "").strip().lower()
    lower = normalized.lower()
    markers = [marker for marker, token in (("KM", "[km]"), ("iWiki", "[iwiki]")) if token in lower]

    if markers:
        return SourceRoute(
            kind="internal_mcp", confidence=1.0, normalized_input=normalized,
            input_type="internal_query", markers=markers,
            reason="input contains internal source marker", suggested_branch="internal_mcp",
        )

    if hinted_kind in {"wechat", "wechat_search", "wechat_history"}:
        input_type = _wechat_input_type(normalized)
        if hinted_kind == "wechat_search":
            input_type = "wechat_search"
        elif hinted_kind == "wechat_history":
            input_type = "wechat_history"
        return SourceRoute(
            kind="wechat", confidence=1.0, normalized_input=normalized, input_type=input_type,
            reason=f"source_kind hint requested {hinted_kind}", suggested_branch="wechat",
        )

    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.lower().endswith("mp.weixin.qq.com"):
            return SourceRoute(
                kind="wechat", confidence=1.0, normalized_input=normalized,
                input_type="wechat_history_url", reason="mp.weixin.qq.com URL", suggested_branch="wechat",
            )
        return SourceRoute(
            kind="website", confidence=1.0, normalized_input=normalized,
            input_type="url", reason="ordinary URL", suggested_branch="website",
        )

    return SourceRoute(
        kind="wechat", confidence=0.6, normalized_input=normalized, input_type="wechat_search",
        reason="plain text defaults to wechat_search when source_kind is omitted", suggested_branch="wechat",
    )


def apply_route_choice(route: SourceRoute, selected_route_type: str | None) -> None:
    """把前端明确选择的来源类型应用到已有路由。

    功能：前端手动指定类型时，覆盖自动推断的 kind、input_type 和分支名称。
    谁会调用：``multi_graph`` 在实际分派前调用。
    直接调用：无，仅修改传入的 ``SourceRoute``。
    输入与结果：输入路由对象和可选类型；无返回值。
    副作用：修改 ``route`` 的内存字段。
    """
    selected = {
        "website": ("website", "url", "website"),
        "wechat_search": ("wechat", "wechat_search", "wechat"),
        "wechat_history": ("wechat", "wechat_history", "wechat"),
        "internal_forum": ("internal_mcp", "internal_query", "internal_mcp"),
    }.get(selected_route_type or "")
    if selected is None:
        return
    route.kind, route.input_type, route.suggested_branch = selected
    route.reason = f"explicit {selected_route_type} route from frontend"


def _wechat_input_type(value: str) -> str:
    """区分微信历史地址和关键词搜索。

    功能：带 HTTP(S) scheme 的输入视为历史入口，其余视为搜索词。
    谁会调用：``route_input`` 在处理微信 hints 时调用。
    直接调用：``urlparse(...)``，判断是否为 URL。
    输入与结果：输入文本；返回微信输入类型。
    副作用：无。
    """
    return "wechat_history_url" if urlparse(value).scheme in {"http", "https"} else "wechat_search"
