import json
import re

from app.enums import MAIN_CATEGORIES
from app.llm.client import LlmClient
from app.schemas import EnrichedFields, NormalizedItem

_PROMPT_TEMPLATE = """你是操作系统维护工程师的关键技术新闻与技术情报分析师。阅读下面的技术文章，为 OS maintainer 提取结构化情报。

只收录关键技术新闻：新兴技术/工具/架构进入可观察阶段，操作系统、内核、发行版、编译器、包管理、云原生基础设施、AI agent/LLM 工具链出现重要发布、重大更新、性能基准、兼容性变化或技术路线变化。
安全内容只收录会影响多个社区、多个发行版、上游项目或广泛生态的严重漏洞/供应链问题；厂商自身的小范围漏洞、普通 CVE 罗列、只影响单一产品的常规安全公告，应拒收，除非正文明确说明跨社区影响。
不收录：社区活动通知、招聘信息、用户入门教程、市场营销材料、非技术性公告、单纯文档页、仓库首页、SIG 介绍页、列表页、登录页、验证码页、反爬挑战页。
如果正文为空、只有站点导航、只是文档首页/仓库首页/SIG 介绍页，或者信息不足以支撑真实技术摘要，则不要收录。
如果页面标题或正文出现“确保您不是机器人 / Making sure you're not a bot / Anubis / Proof-of-Work / Hashcash / enable JavaScript / browser verification”等反爬挑战页特征，必须 should_store=false；不要把反爬工具或挑战页本身当作技术新闻总结。

输出严格 JSON（不要多余文字）。

可选主分类（必须选一个最贴切的）：{categories}

字段要求：
- title_zh: 中文翻译标题（准确翻译原标题，不是概括），20字以内
- summary: 2-4句核心摘要
- tech_highlights: 3-5条技术要点（字符串数组），每条格式为「[关键词] 具体说明」，如「[内核版本] Linux 6.12 引入了 sched_ext 调度器框架」
- info_type: 从 [发布, 更新, 性能数据, 适配, 观点/分析, 其他] 选一个
- importance: 从 [高, 中, 低] 选一个。高=版本发布/安全公告/重大新工具；中=技术更新/AI工具链；低=性能测试/架构分析
- main_category: 从可选主分类里选一个
- sub_tags: 可聚合的规范标签（字符串数组）
- keywords: 扁平关键词数组，列出文中的关键技术术语和版本细节（如 ["Linux 6.12", "RHEL 10", "systemd 256", "eBPF"]）
- confidence: 0~1 的浮点，表示你对归类与摘要的把握
- should_store: 布尔值。若页面不是新闻/热点/技术更新，或信息不足，则必须为 false
- reject_reason: 当 should_store=false 时必填，简要说明拒收原因；当 should_store=true 时可为 null
- merge_suggestions: 标签合并建议数组；如果当前 tag 更通用，可以建议把已有旧 tag 合并到当前 tag

标签聚合规则：
- sub_tags 控制在 2-4 个，优先选择能跨多篇文章复用的 canonical 名称；每条最终最多保留 5 个标签（含 main_category），把一次性细节放入 keywords。
- 使用稳定、短小的标签：厂商/发行版/项目/组件/包名/主题，如 openEuler、OpenAnolis、Fedora、RHEL、Ubuntu、Linux Kernel、RPM、Koji、glibc、systemd、CVE、安全更新、性能优化、软件包更新。
- 合并同义写法和大小写变体，例如 OpenEuler/openEuler/欧拉 统一写作 openEuler；OpenAnolis/Anolis/龙蜥 统一写作 OpenAnolis；kernel/Linux kernel/内核 统一写作 Linux Kernel。
- 不要把完整版本号、补丁号、CVE 编号、公告编号、日期、URL 片段、过长短语放入 sub_tags；这些细节放入 keywords。
- 避免近义重复：同一篇文章不要同时输出 openEuler 和 欧拉、Linux Kernel 和 kernel、RPM 和 rpm package。
- keywords 可以保留更细的原文术语、版本、包名组合和 CVE 编号，但仍应去重，避免同义写法重复。
- existing_tags 是数据库中已有标签，格式为 {{"id": 数字, "name": 标签名, "usage_count": 使用次数}}；优先复用 existing_tags 中适合描述当前文章的标签。
- 如果 existing_tags 里没有合适标签，才创建新的 sub_tags。
- 如果当前新增或选用的 tag 比已有旧 tag 更通用，可以输出 merge_suggestions；系统会默认 approved 写入 tag_aliases 表。
- merge_suggestions 格式：{{"child_tag_id": 旧标签id, "parent_tag_id": 可选父标签id, "parent_tag_name": "父标签名", "reason": "原因", "confidence": 0.0-1.0}}。

已有标签 existing_tags：
{existing_tags}

标题：{title}
正文：
{content}

只输出 JSON：
"""


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in LLM output: {text[:200]}")


class Enricher:
    def __init__(self, llm: LlmClient | None = None):
        self._llm = llm or LlmClient()

    def enrich(self, item: NormalizedItem, *, existing_tags: list[dict] | None = None) -> EnrichedFields:
        prompt = _PROMPT_TEMPLATE.format(
            categories=", ".join(MAIN_CATEGORIES),
            existing_tags=json.dumps(existing_tags or [], ensure_ascii=False),
            title=item.title,
            content=item.clean_content[:6000],
        )
        raw = self._llm.complete(prompt)
        data = _extract_json(raw)
        if data.get("main_category") not in MAIN_CATEGORIES:
            data["main_category"] = MAIN_CATEGORIES[-1]
        if "should_store" not in data:
            data["should_store"] = True
        if data.get("should_store") and "reject_reason" not in data:
            data["reject_reason"] = None
        return EnrichedFields(**data)
