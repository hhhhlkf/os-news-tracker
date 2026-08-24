"""Preparation helpers for running stored discovery recipes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.discovery.ingester import parse_published_at

DEFAULT_WECHAT_SEARCH_MAX_PAGES = 6
from app.schemas import ManualNewsRunRequest

# 微信补正文单条方式的抓取上限（封顶），防止被 target_count 放大成几百篇。
WECHAT_ENRICH_MAX_ITEMS = 20
WECHAT_HISTORY_ENRICH_MAX_ITEMS = 8

_RELATIVE_RANGE_TO_DELTA = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def apply_fetch_limits(items: list[dict], request: ManualNewsRunRequest | None) -> list[dict]:
    """按手动抓取请求的时间窗口与条数上限过滤并裁剪结果。

    功能：当 request 为空时原样返回；否则按相对（24h/7d/30d）或绝对时间窗过滤掉过期条目，按时间倒序后取前 target_count 条。
    谁会调用：runner.execute_discovery_fetch、morning_crawl/service.py 在拿到原始产出后做限量裁剪。
    直接调用：
    - parse_published_at(...)：规整每条条目的发布时间。
    - _as_utc(...)：把请求的时间边界转成 UTC。
    输入与结果：输入 items 列表与可选的 ManualNewsRunRequest；返回过滤裁剪后的 items。
    副作用：无。
    """
    if request is None:
        return items

    now = datetime.now(timezone.utc)
    filtered: list[tuple[datetime, dict]] = []
    for item in items:
        published_at = parse_published_at(item.get("published_at"))
        if published_at is None:
            continue
        if request.time_mode == "relative":
            lower_bound = now - _RELATIVE_RANGE_TO_DELTA[request.relative_range]
            if published_at < lower_bound:
                continue
        else:
            start_at = _as_utc(request.start_at)
            end_at = _as_utc(request.end_at)
            if published_at < start_at or published_at > end_at:
                continue
        filtered.append((published_at, item))

    filtered.sort(key=lambda entry: entry[0], reverse=True)
    return [item for _, item in filtered[: request.target_count]]


def prepare_fetch_recipe(recipe: dict[str, Any], request: ManualNewsRunRequest | None) -> dict[str, Any]:
    """运行前对存储的 recipe 做兜底与参数补全。

    功能：深拷贝 recipe 后，确保聚合器 feed 自动补正文动作；对 multi_dsl 补充微信搜索/历史的页面与补抓上限等参数。
    谁会调用：runner.execute_discovery_fetch、morning_crawl/service.py 在执行配方前调用。
    直接调用：
    - ensure_aggregator_feed_article_enrich(...)：为聚合器 feed 补 enrich 动作。
    - ensure_wechat_history_article_enrich(...)：为公众号历史补 enrich 动作。
    输入与结果：输入 recipe 与可选 request；返回补全后的 recipe 字典。
    副作用：无（不修改入参，返回新字典）。
    """
    prepared = json.loads(json.dumps(recipe, ensure_ascii=False, default=str))
    ensure_aggregator_feed_article_enrich(prepared)
    if prepared.get("recipe_type") != "multi_dsl":
        return prepared

    target_count = request.target_count if request else None
    actions = prepared.get("actions") or []
    has_history = any(action.get("op") == "wechat_fetch_account_history" for action in actions)
    ensure_wechat_history_article_enrich(actions)
    for action in actions:
        if action.get("op") == "wechat_search_articles" and action.get("max_pages") is None:
            action["max_pages"] = DEFAULT_WECHAT_SEARCH_MAX_PAGES
        if action.get("op") == "enrich_wechat_articles" and action.get("max_items") is None:
            desired = max(1, target_count) if target_count else 5
            cap = WECHAT_HISTORY_ENRICH_MAX_ITEMS if has_history else WECHAT_ENRICH_MAX_ITEMS
            action["max_items"] = min(desired, cap)
        if has_history and action.get("op") == "enrich_wechat_articles":
            action.setdefault("precheck_topic_with_llm", True)
    return prepared


def ensure_aggregator_feed_article_enrich(recipe: dict[str, Any]) -> None:
    """DEPRECATED / MIGRATION-ONLY mutation for stored legacy DSL recipes.

    Python connectors own article enrichment. This remains only so an old
    HN-style DSL can be replayed during migration or rollback.

    功能：若 recipe 尚无 enrich_article_pages 且首个 feed 动作指向 hnrss/news.ycombinator，则在 dedup_by 之后追加补抓动作。
    谁会调用：prepare_fetch_recipe 在补全 recipe 时调用。
    直接调用：
    - _first_feed_action_url(...)：找首个 feed 动作的 URL 判断 host。
    输入与结果：输入 recipe（原地修改 actions）；无返回值。
    副作用：原地修改传入的 recipe 字典（追加动作）。
    """
    actions = recipe.get("actions")
    if not isinstance(actions, list):
        return
    if any(action.get("op") == "enrich_article_pages" for action in actions if isinstance(action, dict)):
        return
    feed_url = _first_feed_action_url(actions)
    if not feed_url:
        return
    host = urlparse(feed_url).netloc.lower()
    if host not in {"hnrss.org", "news.ycombinator.com"}:
        return
    enrich_action = {
        "op": "enrich_article_pages",
        "fetch_content": True,
        "fill_missing_only": True,
        "max_items": 8,
        "timeout_seconds": 6,
        "content_char_limit": 3000,
    }
    for index, action in enumerate(actions):
        if isinstance(action, dict) and action.get("op") == "dedup_by":
            actions.insert(index + 1, enrich_action)
            return
    actions.append(enrich_action)


def attach_wechat_skip_keys(recipe: dict[str, Any], urls: list[str]) -> dict[str, Any]:
    """DEPRECATED / MIGRATION-ONLY mutation for legacy WeChat DSL recipes.

    The active shared WeChat connector performs deterministic de-duplication
    itself; its ``python_plugin`` recipe returns before importing legacy code.

    功能：从已有 urls 推导出微信文章去重键，合并到 recipe 的 enrich_wechat_articles 动作的 skip_url_keys 中。
    谁会调用：runner.execute_discovery_fetch、morning_crawl/service.py 在补抓前用已存 URL 去重。
    直接调用：
    - wechat_article_key(...)：从 URL 推导微信文章去重键。
    输入与结果：输入 recipe 与已有 urls；返回（原地修改的）recipe。
    副作用：原地修改 recipe 的 skip_url_keys 字段。
    """
    # Python plugins own deterministic de-duplication and never import the
    # deprecated authenticated WeChat/DSL implementation during formal runs.
    if recipe.get("recipe_type") == "python_plugin":
        return recipe

    from app.discovery.wechat_tools import wechat_article_key

    keys = sorted({key for url in urls if (key := wechat_article_key(url))})
    if not keys:
        return recipe
    for action in recipe.get("actions") or []:
        if action.get("op") != "enrich_wechat_articles":
            continue
        existing = {str(key) for key in action.get("skip_url_keys") or [] if key}
        action["skip_url_keys"] = sorted(existing | set(keys))
    return recipe


def ensure_wechat_history_article_enrich(actions: list[dict[str, Any]]) -> None:
    """DEPRECATED / DORMANT mutation for authenticated WeChat-history DSL.

    New WeChat discovery uses the anonymous shared connector and must not
    generate ``wechat_fetch_account_history`` actions.

    功能：当 recipe 含 wechat_fetch_account_history 但还没有 enrich_wechat_articles 时，在 dedup_by 处或末尾插入补抓动作。
    谁会调用：prepare_fetch_recipe 在处理 multi_dsl 时调用。
    直接调用：无（仅遍历 actions 与插入）。
    输入与结果：输入 actions 列表（原地修改）；无返回值。
    副作用：原地修改传入的 actions 列表（追加动作）。
    """
    has_history = any(action.get("op") == "wechat_fetch_account_history" for action in actions)
    has_article_enrich = any(action.get("op") == "enrich_wechat_articles" for action in actions)
    if not has_history or has_article_enrich:
        return

    enrich_action = {
        "op": "enrich_wechat_articles",
        "fetch_content": True,
        "fill_missing_only": True,
        "max_items": None,
    }
    for index, action in enumerate(actions):
        if action.get("op") == "dedup_by":
            actions.insert(index, enrich_action)
            return
    actions.append(enrich_action)


def _as_utc(value: datetime) -> datetime:
    """把日期时间规整为 UTC 感知时间。

    功能：无时区则补 UTC；有时区则转到 UTC，供时间窗比较统一口径。
    谁会调用：apply_fetch_limits 在比较发布时间边界时调用。
    直接调用：无（仅时区处理）。
    输入与结果：输入 datetime；返回 UTC datetime。
    副作用：无。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# DEPRECATED compatibility aliases for historical discovery_routes imports.
# Do not add new callers; use the public functions above where still required.
_apply_fetch_limits = apply_fetch_limits
_prepare_fetch_recipe = prepare_fetch_recipe
_attach_wechat_skip_keys = attach_wechat_skip_keys
_ensure_wechat_history_article_enrich = ensure_wechat_history_article_enrich


def _first_feed_action_url(actions: list[dict[str, Any]]) -> str | None:
    """找出 recipe 中第一个 feed/rss/atom 类型 fetch 动作的 URL。

    功能：用于判断是否为聚合器 feed，以决定是否自动补抓正文。
    谁会调用：ensure_aggregator_feed_article_enrich 在判断 host 时调用。
    直接调用：无（仅遍历 actions 与取字段）。
    输入与结果：输入 actions 列表；返回首个 feed URL 或 None。
    副作用：无。
    """
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("op") == "fetch" and str(action.get("mode") or "").lower() in {"feed", "rss", "atom"}:
            url = str(action.get("url") or "").strip()
            return url or None
    return None
