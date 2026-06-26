"""PlanAgent — LLM 驱动的 URL 发现与规划（Pre-Act + DFSDT 角色）。

从源站首页提取所有出站链接，由 LLM 根据用户关注领域进行语义过滤和排序，
选出最有价值的候选 URL。随后经过确定性验证层（同域、http 协议、SiteMemory 跳过）
做安全兜底，确保 LLM 的不可预测输出不会污染下游阶段。

这是 Handoff Chain 的第①阶段。PlanAgent 是链中第一个也是唯一的
"会看站点结构" 的组件 — 它只审查首页的链接文本和 URL pattern，
不深入抓取具体页面内容。
"""

import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from app.agent.schemas import AgentSourceConfig, CrawlPlan, PlanUrl
from app.agent.site_memory import SiteMemory
from app.models import Source
from app.run_logs import append_run_log

logger = logging.getLogger(__name__)

# ── URL 规划 prompt ──────────────────────────────────────────────
# 风格对齐 Enricher：段落式标准 + URL 启发式规则 + 明确任务目的。
# 链接数据升级为 {"url": ..., "text": ...} 格式，让 LLM 能读到链接文字。
_PROMPT = """你是操作系统维护工程师的 URL 发现与规划助手。给定一个技术网站首页的链接列表，从 OS maintainer 的视角筛选出最可能包含有价值技术内容的页面 URL，并按优先级排序。选出的 URL 将进入全文深度抓取，请优先选内容最丰富的页面，而不只是标题最吸引眼球的。

优先选择：版本发布公告（Release、Announcement、Changelog、包含版本号）；操作系统、内核、驱动、文件系统、网络栈、虚拟化的技术博客；性能基准测试报告与横向对比；安全公告、CVE 报告、漏洞修复说明；新工具/新项目介绍（编译器、调试器、包管理器、容器运行时）；技术讨论、RFC、设计文档。
不选：首页、关于页、联系页、赞助页；招聘与 HR 相关；活动通知、meetup、会议征稿、直播预告；用户文档、入门教程；社交媒体与 RSS/Atom feed 链接；登录/注册页面；已知低质量 URL（见跳过列表）。

URL 路径规律参考：路径中包含年份（/2026/）、月份或日期数字通常是文章；包含版本号（v1.2、6.12、2026.1）通常是发布公告；/about/、/contact/、/tag/、/category/、/page/、/feed/、/archive/ 通常跳过。链接文字（text 字段）比 URL 路径更能说明内容，优先参考链接文字做判断。

严格输出 JSON，不要多余文字：
{{"urls": [{{"url": "https://blog.example.com/2026/06/linux-6.12-released", "guessed_topic": "Linux Kernel"}}, {{"url": "https://blog.example.com/2026/06/ebpf-verifier-improvements", "guessed_topic": "eBPF"}}]}}

- url：完整绝对 URL
- guessed_topic：英文短标注，不超过 3 个词（如 "Linux Kernel"、"systemd"、"KVM"）
- 按优先级从高到低排序，最多返回 {max_urls} 个

用户关注领域：{focus_areas}
页面 URL：{source_url}
候选链接（共 {total_count} 个，格式为 url + 链接文字）：
{links_json}
已知低质量 URL（请跳过）：{skip_patterns}
"""


class _LinkExtractor(HTMLParser):
    """从 HTML 中提取所有 <a href> 链接及其链接文字的 HTMLParser 子类。

    同时捕获 URL（href）和链接文字（anchor text），提供更丰富的链接上下文，
    让 LLM 可以通过文字而非仅靠 URL pattern 做出判断。
    自动处理相对 URL（通过 urljoin 拼接 base_url）。
    保持链接出现的顺序，自动去重。
    """

    def __init__(self, base_url: str):
        """初始化链接提取器。

        Args:
            base_url: 用于解析相对 URL 的基础 URL。
        """
        super().__init__()
        self._base_url = base_url
        self._links: list[dict] = []          # {"url": ..., "text": ...}
        self._seen: set[str] = set()
        self._current_url: str | None = None  # 当前 <a> 标签的 href
        self._text_buf: list[str] = []        # 累积当前 <a> 内的文字

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """遇到 <a> 开始标签时，记录 href 并开始捕获链接文字。"""
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                # 跳过页面内锚点
                if value.startswith("#"):
                    return
                # 跳过 javascript: / mailto: 等非 http 协议
                if ":" in value and not value.startswith(("http://", "https://")):
                    return
                # 解析相对 URL
                absolute = urljoin(self._base_url, value)
                if absolute not in self._seen:
                    self._current_url = absolute
                    self._text_buf = []
                break

    def handle_data(self, data: str) -> None:
        """在 <a>…</a> 内部遇到文本节点时，追加到文字缓冲。"""
        if self._current_url is not None:
            stripped = data.strip()
            if stripped:
                self._text_buf.append(stripped)

    def handle_endtag(self, tag: str) -> None:
        """遇到 </a> 时提交 {url, text} 对并重置状态。"""
        if tag == "a" and self._current_url is not None:
            text = " ".join(self._text_buf).strip()
            if self._current_url not in self._seen:
                self._seen.add(self._current_url)
                self._links.append({"url": self._current_url, "text": text})
            self._current_url = None
            self._text_buf = []


def _extract_json(text: str) -> dict:
    """从 LLM 原始响应中提取 JSON 对象。

    支持两种格式：
    1. ```json { ... } ``` 围栏代码块
    2. 裸 JSON { ... }

    Args:
        text: LLM 原始响应文本。

    Returns:
        解析后的 dict。

    Raises:
        ValueError: 响应中找不到有效 JSON。
    """
    # 优先匹配围栏代码块
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    # 回退：匹配裸 JSON
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in plan response: {text[:200]}")


def _normalize_link_records(links: list[dict] | list[str]) -> list[dict[str, str]]:
    """Normalize legacy string links and rich link records to {url, text}."""
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        if isinstance(link, str):
            url = link
            text = ""
        elif isinstance(link, dict):
            raw_url = link.get("url", "")
            if not isinstance(raw_url, str):
                continue
            url = raw_url
            raw_text = link.get("text", "")
            text = raw_text if isinstance(raw_text, str) else ""
        else:
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append({"url": url, "text": text})
    return normalized


class PlanAgent:
    """LLM 驱动的 URL 发现与规划器。

    职责：
    1. 从源站首页提取所有出站链接（确定性 _fetch_links）
    2. 调用 LLM 按用户关注领域进行语义筛选和排序（随机性）
    3. 经过确定性验证层过滤非法 URL（同域、http 协议、SiteMemory 跳过）

    用法：
        agent = PlanAgent(llm=my_llm, memory=my_memory)
        plan = agent.plan(source, config, db=db)
    """

    def __init__(self, llm=None, memory: SiteMemory | None = None):
        """初始化 PlanAgent。

        Args:
            llm: 可选的自定义 LLM 客户端，默认使用 LlmClient。
            memory: 可选的 SiteMemory 实例，默认创建新实例。
        """
        from app.llm.client import LlmClient

        self._llm = llm or LlmClient()
        self._memory = memory or SiteMemory()

    def _fetch_links(self, url: str) -> list[dict]:
        """从指定页面提取所有出站链接及其链接文字。

        使用 httpx 获取页面 HTML，然后用 _LinkExtractor（基于 stdlib HTMLParser）
        同时提取 <a href> 和链接文字。最多返回 100 个去重链接。

        Args:
            url: 要提取链接的页面 URL。

        Returns:
            去重后的链接列表，每项格式为 {"url": ..., "text": ...}。
            如果请求失败则返回空列表。
        """
        import httpx

        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("plan_agent: 获取页面失败 %s: %s", url, e)
            return []

        # 使用最终重定向后的 URL 作为 base（处理短链接等跳转）
        base_url = str(resp.url) if resp.url != url else url

        parser = _LinkExtractor(base_url)
        try:
            parser.feed(resp.text)
        except Exception as e:
            logger.warning("plan_agent: HTML 解析失败 %s: %s", url, e)
            return []

        # 最多返回 100 个链接
        links = parser._links[:100]
        logger.debug("plan_agent: 从 %s 提取了 %d 个链接", url, len(links))
        return links

    def plan(self, source: Source, config: AgentSourceConfig, *, db: Session) -> CrawlPlan:
        """为给定源规划本轮抓取的 URL 列表。

        流程：
        1. 从 source.url 提取所有链接
        2. 过滤已知 discard URL（SiteMemory.should_skip）
        3. 将候选链接发送给 LLM 进行语义筛选和排序
        4. 确定性验证：同域检查、http 协议检查、SiteMemory 最终检查
        5. 截断到 max_urls_per_run

        Args:
            source: 源记录（ORM 对象），需包含 id 和 url 属性。
            config: Agent 源配置。
            db: 数据库会话（传递给 SiteMemory）。

        Returns:
            规划好的 CrawlPlan，包含经过验证的 URL 列表。
        """
        root_url = source.url
        base_domain = urlparse(root_url).netloc
        source_name = source.name

        append_run_log("plan", "开始规划 URL", source=source_name, url=root_url)

        # ── 1. 提取链接 ──
        links = _normalize_link_records(self._fetch_links(root_url))
        if not links:
            append_run_log(
                "plan",
                "首页未提取到任何链接，返回空计划",
                source=source_name,
                level="warning",
                url=root_url,
            )
            logger.warning(
                "plan_agent: %s 未找到任何链接，返回空计划", root_url,
            )
            return CrawlPlan(source_id=config.source_id, urls=[])

        # ── 2. 过滤已知 discard（按 URL 检查）──
        skip_urls = {
            link["url"] for link in links
            if self._memory.should_skip(
                db=db, source_id=config.source_id, url=link["url"],
            )
        }
        candidate_links = [l for l in links if l["url"] not in skip_urls]
        append_run_log(
            "plan",
            "首页链接提取完成",
            source=source_name,
            total_links=len(links),
            skipped_known=len(skip_urls),
            candidates=len(candidate_links),
        )

        # ── 3. LLM 语义筛选 ──
        prompt = _PROMPT.format(
            focus_areas=", ".join(config.focus_areas),
            source_url=root_url,
            total_count=len(candidate_links),
            links_json=json.dumps(candidate_links[:50], ensure_ascii=False),
            skip_patterns=json.dumps(list(skip_urls)[:10], ensure_ascii=False) if skip_urls else "无",
            max_urls=config.max_urls_per_run,
        )

        raw = self._llm.complete(prompt)

        # ── 4. 解析 LLM 响应 ──
        try:
            data = _extract_json(raw)
            raw_urls = data.get("urls", [])
        except Exception as e:
            logger.warning("plan_agent: LLM 响应解析失败: %s", e)
            raw_urls = []

        # ── 5. 确定性验证 ──
        validated: list[PlanUrl] = []
        dropped_invalid = 0
        dropped_cross_domain = 0
        dropped_memory = 0
        for entry in raw_urls:
            url = entry.get("url", "")
            if not isinstance(url, str) or not url:
                dropped_invalid += 1
                continue
            # 必须是 http/https 协议
            if not url.startswith(("http://", "https://")):
                dropped_invalid += 1
                continue
            # 必须与源站同域（防止 LLM 幻觉跨域 URL）
            if urlparse(url).netloc != base_domain:
                dropped_cross_domain += 1
                append_run_log(
                    "plan",
                    "过滤跨域 URL",
                    source=source_name,
                    level="warning",
                    url=url,
                    reason=f"非同域（期望 {base_domain}）",
                )
                continue
            # 不能在 SiteMemory 跳过列表中
            if self._memory.should_skip(db=db, source_id=config.source_id, url=url):
                dropped_memory += 1
                append_run_log(
                    "plan",
                    "过滤已知低质量 URL",
                    source=source_name,
                    url=url,
                    reason="SiteMemory 标记为 discard",
                )
                continue
            validated.append(PlanUrl(
                url=url,
                guessed_topic=entry.get("guessed_topic", ""),
            ))
            # 截断到 max_urls_per_run
            if len(validated) >= config.max_urls_per_run:
                break

        append_run_log(
            "plan",
            "URL 规划完成",
            source=source_name,
            plan_urls=len(validated),
            llm_returned=len(raw_urls),
            dropped_cross_domain=dropped_cross_domain,
            dropped_memory=dropped_memory,
            dropped_invalid=dropped_invalid,
        )
        logger.info(
            "plan_agent: 为源 %d 规划了 %d 个 URL（候选 %d，skip %d）",
            config.source_id, len(validated), len(candidate_links), len(skip_urls),
        )
        return CrawlPlan(source_id=config.source_id, urls=validated)
