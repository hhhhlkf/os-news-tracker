from __future__ import annotations

import re
from urllib.parse import urlparse

MAX_SUGGEST_NAME_LENGTH = 20
MAX_WEBSITE_LABEL_LENGTH = 48
WEBSITE_NAME_PREFIX = "网站："
WECHAT_SEARCH_NAME_PREFIX = "微信搜索："
WECHAT_HISTORY_NAME_PREFIX = "公众号："
INTERNAL_FORUM_NAME_PREFIX = "司内论坛："


def _clean_fragment(value: str | None, *, max_length: int) -> str | None:
    """清理并截断一段文本，作为展示名的基础片段。

    功能：去掉首尾空白与引号，把连续空白压成单空格，截掉句末标点，再按 max_length 截断，结果为空时返回 None。
    谁会调用：本模块内的 normalize_site_name、format_website_display_name、_website_label_from_url 等格式化函数。
    直接调用：
    - re.sub(...)：正则替换空白与句末标点。
    输入与结果：输入待清理字符串与最大长度；返回清理后的字符串或 None。
    副作用：无。
    """
    if not value:
        return None
    cleaned = value.strip().strip("'\"“”‘’`")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[。！？!?,，；;：:]+$", "", cleaned).strip()
    cleaned = cleaned[:max_length].strip()
    return cleaned or None


def normalize_site_name(value: str | None) -> str | None:
    """把站点名规整成简短的展示片段（最多 20 字）。

    功能：调用 _clean_fragment 以建议名称最大长度截断并清理用户输入/LLM 给出的站点名。
    谁会调用：discovery_routes.py、multi_graph.py 在生成来源展示名时调用。
    直接调用：
    - _clean_fragment(...)：完成实际的清理与截断。
    输入与结果：输入任意字符串；返回规整后的名称或 None。
    副作用：无。
    """
    return _clean_fragment(value, max_length=MAX_SUGGEST_NAME_LENGTH)


def format_website_display_name(value: str | None, *, fallback_url: str | None = None) -> str:
    """生成网站来源的展示名，形如「网站：xxx」，可基于 URL 兜底。

    功能：清理名称片段（并去掉已有的「网站：」前缀），名称缺失时用 fallback_url 推导，最后加上统一前缀；
    仍为空时回退为「未命名站点」。
    谁会调用：discovery_routes.py、multi_graph.py 在生成/保存网站抓取方法展示名时调用。
    直接调用：
    - _clean_fragment(...)：清理名称与 URL 推导出的标签。
    - _website_label_from_url(...)：当名称缺失时从 URL 推导标签。
    输入与结果：输入名称（可选）与兜底 URL；返回带前缀的展示名。
    副作用：无。
    """
    base = _clean_fragment(value, max_length=MAX_WEBSITE_LABEL_LENGTH)
    if base and base.startswith(WEBSITE_NAME_PREFIX):
        base = _clean_fragment(base.removeprefix(WEBSITE_NAME_PREFIX), max_length=MAX_WEBSITE_LABEL_LENGTH)
    if not base and fallback_url:
        base = _website_label_from_url(fallback_url)
    if not base:
        base = "未命名站点"
    return f"{WEBSITE_NAME_PREFIX}{base}"


def default_website_display_name(site_url: str) -> str:
    """仅用 URL 生成网站的默认展示名。

    功能：从 URL 推导标签并套上「网站：」前缀，作为用户未提供名称时的兜底展示名。
    谁会调用：discovery_routes.py（未提供 name 时）、multi_graph.py（构造 website 分支展示名时）。
    直接调用：
    - _website_label_from_url(...)：从 URL 解析 host/路径作为标签。
    - format_website_display_name(...)：套前缀并清理。
    输入与结果：输入 site_url；返回默认展示名。
    副作用：无。
    """
    return format_website_display_name(_website_label_from_url(site_url), fallback_url=site_url)


def _website_label_from_url(site_url: str) -> str | None:
    """从 URL 解析出简短标签（host + 路径前两段），用于命名兜底。

    功能：取 URL 的 host（去掉 www.）与路径前两段，去除 feed/rss 等尾部文件段，拼接成标签并清理。
    谁会调用：format_website_display_name、default_website_display_name 在名称缺失时调用。
    直接调用：
    - urlparse(...)：解析 URL 的 host 与 path。
    - re.sub(...)：规范化路径分隔符。
    - _clean_fragment(...)：截断并清理标签。
    输入与结果：输入 URL；返回标签字符串或 None。
    副作用：无。
    """
    parsed = urlparse(site_url.strip())
    host = parsed.netloc.removeprefix("www.").strip()
    path = re.sub(r"/+", "/", parsed.path or "").strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if segments and segments[-1].lower() in {"feed", "rss", "rss.xml", "atom.xml", "index.html"}:
        segments = segments[:-1]
    label_parts = [host] if host else []
    if segments:
        label_parts.append("/".join(segments[:2]))
    label = "/".join(part for part in label_parts if part)
    return _clean_fragment(label or site_url, max_length=MAX_WEBSITE_LABEL_LENGTH)


def format_wechat_search_display_name(value: str) -> str:
    """生成微信搜索来源的展示名，形如「微信搜索：xxx」。

    功能：把查询词规整为短片段，缺失时退回原词，再套上「微信搜索：」前缀。
    谁会调用：discovery_routes.py、multi_graph.py 在生成/保存微信搜索抓取方法展示名时调用。
    直接调用：
    - normalize_site_name(...)：清理查询词片段。
    输入与结果：输入查询词；返回带前缀的展示名。
    副作用：无。
    """
    return f"{WECHAT_SEARCH_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_wechat_history_display_name(value: str) -> str:
    """生成公众号来源的展示名，形如「公众号：xxx」。

    功能：把公众号名称规整为短片段，缺失时退回原词，再套上「公众号：」前缀。
    谁会调用：discovery_routes.py、multi_graph.py 在生成/保存公众号历史抓取方法展示名时调用。
    直接调用：
    - normalize_site_name(...)：清理名称片段。
    输入与结果：输入公众号名称；返回带前缀的展示名。
    副作用：无。
    """
    return f"{WECHAT_HISTORY_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"


def format_internal_forum_display_name(value: str) -> str:
    """生成司内论坛来源的展示名，形如「司内论坛：xxx」。

    功能：把论坛名规整为短片段，缺失时退回原词，再套上「司内论坛：」前缀。
    谁会调用：discovery_routes.py 在生成司内论坛抓取方法展示名时调用。
    直接调用：
    - normalize_site_name(...)：清理名称片段。
    输入与结果：输入论坛名；返回带前缀的展示名。
    副作用：无。
    """
    return f"{INTERNAL_FORUM_NAME_PREFIX}{normalize_site_name(value) or value.strip()}"
