from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def crawl_method_domain_key(entry_url: str, recipe: Any) -> str:
    """计算一次已发现爬取方法的去重 domain key。

    功能：feed 类方法保留带 `feed:` 前缀的完整 URL 键；普通网站按规范化完整入口 URL 去重。
    谁会调用：website_workflow（legacy recipe 去重 / check_existing_method）、multi_graph（_run_and_save_multi_recipe 落库前去重）。
    直接调用：
    - _first_feed_url(...)：取出 recipe 中的 feed URL。
    - _feed_domain_key(...)：把 feed URL 转成 `feed:` 前缀 key。
    - normalized_website_domain_key(...)：非 feed 时生成完整入口 URL 键。
    输入与结果：输入入口 URL 与 recipe；返回去重 key 字符串。
    副作用：无。
    """
    feed_url = _first_feed_url(recipe)
    if feed_url:
        return _feed_domain_key(feed_url)

    if _uses_legacy_domain_scope(recipe):
        parsed = urlparse(entry_url)
        return parsed.netloc or entry_url
    return normalized_website_domain_key(entry_url)


def crawl_method_domain_key_for_input_url(entry_url: str) -> str:
    """探查前对「看起来像 feed」的直接 URL 估算去重 key。

    功能：若 URL 像 feed 则按 feed 规则键，否则按规范化完整入口 URL 键。
    谁会调用：website_workflow.check_existing_method 在探查前做去重判断。
    直接调用：
    - _looks_like_feed_url(...)：判断 URL 是否像 feed。
    - _feed_domain_key(...)：feed 情形生成 key。
    - normalized_website_domain_key(...)：非 feed 生成完整入口 URL 键。
    输入与结果：输入入口 URL；返回去重 key 字符串。
    副作用：无。
    """
    if _looks_like_feed_url(entry_url):
        return _feed_domain_key(entry_url)
    return normalized_website_domain_key(entry_url)


def normalized_website_domain_key(entry_url: str) -> str:
    """把普通网站入口 URL 规范化为去重键。

    保留 scheme、path 和 query 值，仅统一 scheme/host 大小写、末尾斜杠和 query
    顺序，并移除 fragment。超过数据库 255 字符限制时使用带 host 的稳定哈希键。
    """
    normalized = _normalize_website_full_url(entry_url)
    if len(normalized) <= 255:
        return normalized
    parsed = urlparse(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    host = parsed.netloc[:210]
    return f"site:{host}:{digest}"[:255]


def normalized_feed_domain_key(feed_url: str) -> str:
    """把 feed URL 规范化为去重 key（统一套 `feed:` 前缀）。

    功能：仅是对 _feed_domain_key 的封装，供已有 feed URL 直接求 key 使用。
    谁会调用：website_workflow.check_existing_method 做 key 比对。
    直接调用：
    - _feed_domain_key(...)：生成规范化 feed key。
    输入与结果：输入 feed_url；返回 key 字符串。
    副作用：无。
    """
    return _feed_domain_key(feed_url)


def is_feed_recipe(recipe: Any) -> bool:
    """判断 recipe 是否为 feed 类（含 feed/rss/atom 抓取动作）。

    功能：通过是否存在 feed 型 fetch 动作来判定，用于决定采用 host 还是 feed 级去重。
    谁会调用：website_workflow.check_existing_method 在比对已有方法时调用。
    直接调用：
    - _first_feed_url(...)：查找 recipe 里的 feed URL。
    输入与结果：输入 recipe；返回布尔值。
    副作用：无。
    """
    return _first_feed_url(recipe) is not None


def _first_feed_url(recipe: Any) -> str | None:
    """找出 recipe 中第一个 feed 型 fetch 动作的完整 URL。

    功能：递归遍历所有动作（含 loop 子动作），返回首个 mode 为 feed/rss/atom 的 fetch URL（已合并 query），找不到则返回 None。
    谁会调用：crawl_method_domain_key、is_feed_recipe 用于判定/取 feed URL。
    直接调用：
    - _iter_actions(...)：遍历（含递归）全部动作。
    - _value(...)：读取动作的 op/mode/url/query 字段。
    - _url_with_action_query(...)：把 query 合并进 URL。
    输入与结果：输入 recipe；返回 feed URL 或 None。
    副作用：无。
    """
    for action in _iter_actions(_recipe_actions(recipe)):
        if _value(action, "op") != "fetch":
            continue
        if str(_value(action, "mode") or "").lower() not in {"feed", "rss", "atom"}:
            continue
        url = _url_with_action_query(_value(action, "url"), _value(action, "query"))
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _uses_legacy_domain_scope(recipe: Any) -> bool:
    """保留微信共享 connector 与 internal-forum 兼容缝的既有键语义。"""
    source_kind = str(_value(recipe, "source_kind") or "").lower()
    if source_kind in {"wechat", "wechat_history", "internal_forum", "internal_mcp"}:
        return True
    return _value(recipe, "connector_kind") == "shared"


def _iter_actions(actions: Any):
    """递归遍历动作列表，展开 loop 的 body/on_each 子动作。

    功能：把嵌套在 loop 中的子动作也一并产出，便于统一检查所有动作。
    谁会调用：_first_feed_url 在查找 feed 动作时调用。
    直接调用：
    - _value(...)：读取动作的 op 字段判断是否为 loop。
    输入与结果：输入 actions（列表或任意）；产出动作生成器（非列表则无产出）。
    副作用：无。
    """
    if not isinstance(actions, list):
        return
    for action in actions:
        yield action
        if _value(action, "op") == "loop":
            yield from _iter_actions(_value(action, "body") or [])
            yield from _iter_actions(_value(action, "on_each") or [])


def _recipe_actions(recipe: Any) -> Any:
    """兼容 dict 与对象两种 recipe，取出其 actions 字段。

    功能：统一两类 recipe 表示，避免后续遍历时反复判断类型。
    谁会调用：_first_feed_url 在取动作列表时调用。
    直接调用：无（仅字典 get / getattr）。
    输入与结果：输入 recipe；返回 actions（列表或 None）。
    副作用：无。
    """
    if isinstance(recipe, dict):
        return recipe.get("actions")
    return getattr(recipe, "actions", None)


def _value(obj: Any, key: str) -> Any:
    """兼容 dict 与对象读取字段，并处理 `as`→`as_` 别名。

    功能：统一从动作对象中取字段值，对 Pydantic 的 `as_` 别名做兼容映射。
    谁会调用：_first_feed_url、_iter_actions 等读取动作字段时调用。
    直接调用：无（仅字典 get / getattr）。
    输入与结果：输入对象与键名；返回对应值或 None。
    副作用：无。
    """
    if isinstance(obj, dict):
        return obj.get(key)
    if key == "as":
        return getattr(obj, "as_", None)
    return getattr(obj, key, None)


def _feed_domain_key(feed_url: str) -> str:
    """规范化 feed URL 并生成 `feed:` 前缀的去重 key，超长则截断为 host+hash。

    功能：把 feed URL 规范化后加 `feed:` 前缀；若长度超过 255，则用 host 加短哈希兜底，避免 key 过长。
    谁会调用：crawl_method_domain_key、crawl_method_domain_key_for_input_url、normalized_feed_domain_key。
    直接调用：
    - _normalize_full_url(...)：规范化 URL。
    - hashlib.sha256(...)：超长 key 时计算短哈希。
    - urlparse(...)：取 host。
    输入与结果：输入 feed_url；返回 key 字符串。
    副作用：无。
    """
    normalized = _normalize_full_url(feed_url)
    key = f"feed:{normalized}"
    if len(key) <= 255:
        return key
    parsed = urlparse(normalized)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"feed:{parsed.netloc}:{digest}"


def _url_with_action_query(url: Any, query: Any) -> str | None:
    """把动作的 query 字典合并进 URL 的查询参数。

    功能：将 action.query 中的非空参数追加到原 URL 查询串（排序后拼接）；无 query 时原样返回。
    谁会调用：_first_feed_url 在构造 feed URL 时调用。
    直接调用：
    - urlparse/parse_qsl/urlencode/urlunparse(...)：解析与重组 URL。
    输入与结果：输入 url 与 query 字典；返回合并后的 URL 或 None（url 非法时）。
    副作用：无。
    """
    if not isinstance(url, str) or not url.strip():
        return None
    if not isinstance(query, dict) or not query:
        return url
    parsed = urlparse(url.strip())
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params.extend((str(k), str(v)) for k, v in query.items() if v is not None)
    merged_query = urlencode(sorted(params), doseq=True)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, merged_query, parsed.fragment)
    )


def _looks_like_feed_url(url: str) -> bool:
    """判断 URL 路径是否像 feed（.rss/.atom/.xml 或含 feed/rss/atom 段）。

    功能：用于探查前快速识别 feed 型入口，决定采用 feed 级去重。
    谁会调用：crawl_method_domain_key_for_input_url 在估算 key 前判断。
    直接调用：
    - urlparse(...)：解析路径。
    输入与结果：输入 url；返回布尔值。
    副作用：无。
    """
    parsed = urlparse(url.strip())
    path = parsed.path.lower().rstrip("/")
    if path.endswith((".rss", ".atom", ".xml")):
        return True
    return any(part in {"feed", "feeds", "rss", "atom"} for part in path.split("/"))


def _normalize_full_url(url: str) -> str:
    """规范化完整 URL：scheme 默认 https、host 小写、去末斜杠、查询参数排序。

    功能：消除等价 URL 的表面差异，使相同 feed 的不同写法得到一致 key。
    谁会调用：_feed_domain_key 在生成 key 前调用。
    直接调用：
    - urlparse/parse_qsl/urlencode/urlunparse(...)：解析、排序并重组 URL。
    输入与结果：输入 url；返回规范化后的 URL 字符串。
    副作用：无。
    """
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    return urlunparse((scheme, netloc, path, "", query, ""))


def _normalize_website_full_url(url: str) -> str:
    """规范化普通网站 URL，保留 path parameters；feed 键继续使用原有规则。"""
    parsed = urlparse(url.strip())
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
