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

logger = logging.getLogger(__name__)

# ── URL 规划 prompt ──────────────────────────────────────────────
# 参考 Enricher 的 prompt 风格：角色定位、筛选标准、输出格式约束。
_PROMPT = """你是操作系统维护团队的信息采集规划助手。你的任务是从网页链接列表中，
筛选出最可能包含 OS maintainer 关心的技术内容的 URL，并按优先级排序。

## 角色定位

你面对的是一个技术新闻/博客/发布页的链接列表。用户需要从中选出最有价值的页面
进入后续的深度抓取和摘要流程。你需要根据 URL pattern 和链接上下文做出判断，
就像一个有经验的 OS 工程师在浏览页面时会点击哪些链接。

## 筛选标准（高优先级）

以下类型的链接值得优先选择：
- 版本发布公告（Release、Announcement、Changelog）
- 技术博客文章（内核、驱动、文件系统、网络栈、虚拟化等）
- 性能基准测试报告、基准对比
- 安全公告、CVE 报告、漏洞修复说明
- 新工具/新项目介绍（编译器、调试器、包管理器、容器运行时）
- 技术讨论、RFC、设计文档

## 排除标准（低优先级或直接跳过）

以下类型应降低优先级或排除：
- 首页、关于页、联系页、赞助页
- 招聘、求职、HR 相关
- 活动通知、meetup、会议征稿、直播预告
- 用户文档、入门教程、"Getting Started"
- 社交媒体链接（Twitter、LinkedIn、YouTube）
- RSS/Atom feed 链接
- 登录/注册页面
- 已知的低质量 URL（见跳过列表）

## 输出格式

严格输出 JSON，不要多余文字：
```json
{{
  "urls": [
    {{"url": "https://blog.example.com/2026/06/linux-6.12-released", "guessed_topic": "Linux Kernel"}},
    {{"url": "https://blog.example.com/2026/06/ebpf-verifier-improvements", "guessed_topic": "eBPF"}}
  ]
}}
```

- url: 完整的绝对 URL
- guessed_topic: 用英文简短标注该页面可能的技术主题（如 "Linux Kernel"、"systemd"、"KVM"），不超过 3 个词
- 按优先级从高到低排序
- 最多返回 {max_urls} 个 URL

用户关注领域：{focus_areas}
页面 URL：{source_url}
候选链接（共 {total_count} 个）：{links_json}
已知低质量 URL（请跳过）：{skip_patterns}
"""


class _LinkExtractor(HTMLParser):
    """从 HTML 中提取所有 <a href> 链接的 HTMLParser 子类。

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
        self._links: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """遇到开始标签时回调，提取 <a href> 属性。"""
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                # 跳过页面内锚点
                if value.startswith("#"):
                    continue
                # 跳过 javascript: / mailto: 等非 http 协议
                if ":" in value and not value.startswith(("http://", "https://")):
                    continue
                # 解析相对 URL
                absolute = urljoin(self._base_url, value)
                # 去重
                if absolute not in self._seen:
                    self._seen.add(absolute)
                    self._links.append(absolute)
                break


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

    def _fetch_links(self, url: str) -> list[str]:
        """从指定页面提取所有出站链接。

        使用 httpx 获取页面 HTML，然后用 _LinkExtractor（基于 stdlib HTMLParser）
        提取所有 <a href> 链接。最多返回 100 个去重链接。

        Args:
            url: 要提取链接的页面 URL。

        Returns:
            去重后的绝对 URL 列表。如果请求失败则返回空列表。
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

        # ── 1. 提取链接 ──
        links = self._fetch_links(root_url)
        if not links:
            logger.warning(
                "plan_agent: %s 未找到任何链接，返回空计划", root_url,
            )
            return CrawlPlan(source_id=config.source_id, urls=[])

        # ── 2. 过滤已知 discard ──
        skip_patterns = [
            link for link in links
            if self._memory.should_skip(db=db, source_id=config.source_id, url=link)
        ]
        candidate_links = [l for l in links if l not in skip_patterns]

        # ── 3. LLM 语义筛选 ──
        prompt = _PROMPT.format(
            focus_areas=", ".join(config.focus_areas),
            source_url=root_url,
            total_count=len(candidate_links),
            links_json=json.dumps(candidate_links[:50], ensure_ascii=False),
            skip_patterns=json.dumps(skip_patterns[:10], ensure_ascii=False) if skip_patterns else "无",
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
        for entry in raw_urls:
            url = entry.get("url", "")
            if not isinstance(url, str) or not url:
                continue
            # 必须是 http/https 协议
            if not url.startswith(("http://", "https://")):
                continue
            # 必须与源站同域（防止 LLM 幻觉跨域 URL）
            if urlparse(url).netloc != base_domain:
                logger.debug("plan_agent: 跳过跨域 URL %s", url)
                continue
            # 不能在 SiteMemory 跳过列表中
            if self._memory.should_skip(db=db, source_id=config.source_id, url=url):
                logger.debug("plan_agent: 跳过 SiteMemory discard URL %s", url)
                continue
            validated.append(PlanUrl(
                url=url,
                guessed_topic=entry.get("guessed_topic", ""),
            ))
            # 截断到 max_urls_per_run
            if len(validated) >= config.max_urls_per_run:
                break

        logger.info(
            "plan_agent: 为源 %d 规划了 %d 个 URL（候选 %d，skip %d）",
            config.source_id, len(validated), len(candidate_links), len(skip_patterns),
        )
        return CrawlPlan(source_id=config.source_id, urls=validated)
