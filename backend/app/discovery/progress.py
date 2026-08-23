"""Progress-event logging helpers for discovery recipe execution."""

from __future__ import annotations

from typing import Any, Callable

LogEmitter = Callable[..., None]


_WECHAT_PROGRESS_MESSAGES = {
    "wechat_search_started": "开始执行微信搜索 DSL",
    "wechat_search_page_started": "微信搜索开始抓取分页",
    "wechat_search_page_finished": "微信搜索分页抓取完成",
    "wechat_search_page_empty": "微信搜索当前分页未提取到结果",
    "wechat_search_rate_limited": "微信搜索触发限流",
    "wechat_search_captcha_required": "微信搜索触发验证码",
    "wechat_search_failed": "微信搜索执行失败",
    "wechat_search_finished": "微信搜索执行完成",
    "wechat_enrich_started": "开始补抓微信文章内容",
    "wechat_enrich_topic_skipped": "微信文章补抓主题预筛跳过",
    "wechat_enrich_item_started": "微信文章补抓进行中",
    "wechat_article_fetch_request_started": "微信文章页面请求开始",
    "wechat_article_fetch_response_received": "微信文章页面响应返回",
    "wechat_article_fetch_finished": "微信文章页面解析完成",
    "wechat_article_fetch_failed": "微信文章页面请求失败",
    "wechat_enrich_item_finished": "微信文章补抓完成",
    "wechat_enrich_finished": "微信文章补抓阶段完成",
    "article_enrich_started": "开始补抓文章详情页",
    "article_enrich_item_started": "文章详情页补抓进行中",
    "article_page_fetch_request_started": "文章详情页请求开始",
    "article_page_fetch_response_received": "文章详情页响应返回",
    "article_page_fetch_finished": "文章详情页解析完成",
    "article_page_fetch_failed": "文章详情页请求失败",
    "article_enrich_item_finished": "文章详情页补抓完成",
    "article_enrich_finished": "文章详情页补抓阶段完成",
}

_FETCH_PROGRESS_FIELDS = {
    "wechat_search_started": ("query", "max_pages", "limit", "resolve_final_urls_limit"),
    "wechat_search_page_started": ("query", "page_no", "total_pages", "fetched_count", "transport"),
    "wechat_search_page_finished": (
        "query",
        "page_no",
        "page_items",
        "fetched_count",
        "resolved_count",
        "transport",
    ),
    "wechat_search_page_empty": ("query", "page_no", "fetched_count", "transport"),
    "wechat_search_rate_limited": ("query", "page_no", "raw_status", "fetched_count", "transport"),
    "wechat_search_captcha_required": ("query", "page_no", "fetched_count", "transport"),
    "wechat_search_failed": ("query", "fetched_count", "transport", "error"),
    "wechat_search_finished": ("query", "fetched_count", "status", "transport"),
    "wechat_enrich_started": (
        "total_items",
        "selected_items",
        "skipped_existing",
        "skipped_topic",
        "topic_check_failed",
        "max_items",
        "fetch_content",
    ),
    "wechat_enrich_topic_skipped": ("title", "url", "reason"),
    "wechat_enrich_item_started": ("attempted_count", "max_items", "title", "url"),
    "wechat_article_fetch_request_started": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_chars",
        "published_at_ms",
        "meta_ms",
        "js_content_ms",
        "strip_ms",
        "error",
    ),
    "wechat_article_fetch_response_received": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_chars",
        "published_at_ms",
        "meta_ms",
        "js_content_ms",
        "strip_ms",
        "error",
    ),
    "wechat_article_fetch_finished": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_chars",
        "published_at_ms",
        "meta_ms",
        "js_content_ms",
        "strip_ms",
        "error",
    ),
    "wechat_article_fetch_failed": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_chars",
        "published_at_ms",
        "meta_ms",
        "js_content_ms",
        "strip_ms",
        "error",
    ),
    "wechat_enrich_item_finished": ("attempted_count", "enriched_count", "title", "url", "status"),
    "wechat_enrich_finished": (
        "status",
        "attempted_count",
        "enriched_count",
        "total_items",
        "selected_items",
        "skipped_existing",
        "skipped_topic",
        "topic_check_failed",
    ),
    "article_enrich_started": (
        "total_items",
        "selected_items",
        "max_items",
        "fetch_content",
        "fill_missing_only",
    ),
    "article_enrich_item_started": ("attempted_count", "max_items", "title", "url"),
    "article_page_fetch_request_started": ("attempted_count", "max_items", "title", "url"),
    "article_page_fetch_response_received": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_type",
    ),
    "article_page_fetch_finished": (
        "attempted_count",
        "max_items",
        "title",
        "url",
        "final_url",
        "status",
        "status_code",
        "duration_ms",
        "body_chars",
        "content_chars",
        "parse_ms",
    ),
    "article_page_fetch_failed": ("attempted_count", "max_items", "title", "url", "error"),
    "article_enrich_item_finished": ("attempted_count", "enriched_count", "title", "url", "status"),
    "article_enrich_finished": (
        "status",
        "attempted_count",
        "enriched_count",
        "total_items",
        "selected_items",
        "fetch_content",
    ),
}


def log_discovery_progress(
    event: str,
    payload: dict[str, Any],
    *,
    emit: LogEmitter,
    stage: str,
    source: str,
    method_id: int,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """统一上报一次 discovery 进度事件。

    功能：根据事件名翻译出中文提示语、挑选出该事件关心的字段，再调用外部传入的 emit 回调输出进度。
    谁会调用：morning_crawl/service.py、discovery/runner.py 在抓取与文章补抓流程中调用，用于实时日志。
    直接调用：
    - _select_fields(...)：按事件名选出要上报的字段。
    - emit(...)：外部传入的进度回调（用于写日志/推送 SSE）。
    输入与结果：输入事件名、payload 字典、emit 回调、stage/source/method_id；无返回值。
    副作用：通过 emit 回调产生日志/进度输出，不写数据库。
    """
    message = _WECHAT_PROGRESS_MESSAGES.get(event, event)
    fields = _select_fields(event, payload)
    fields.update(extra_fields or {})
    emit(stage, message, source=source, method_id=method_id, **fields)


def _select_fields(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按事件名挑选出需要上报的进度字段。

    功能：已知事件从预设字段表取对应字段；未知事件则透传除 stage/message/source/level 外的全部字段。
    谁会调用：log_discovery_progress 在组装进度事件时调用。
    直接调用：无（仅查表与字典推导）。
    输入与结果：输入事件名与 payload；返回字段名到值的字典。
    副作用：无。
    """
    if event not in _FETCH_PROGRESS_FIELDS:
        return {k: v for k, v in payload.items() if k not in {"stage", "message", "source", "level"}}
    return {field: payload.get(field) for field in _FETCH_PROGRESS_FIELDS[event]}
