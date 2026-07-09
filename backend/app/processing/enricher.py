import json
import re

from app.categories import get_main_category_names
from app.discovery.prompts import render_prompt
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
- importance: 从 [高, 中, 低] 选一个。
  请先按下面维度做综合判断，再输出最终等级：
  1. 影响范围
     - 影响多个社区、多个发行版、广泛基础设施或上游生态：3分
     - 影响单一重要项目、单一主流发行版或关键组件：2分
     - 影响范围较窄或局部：1分
  2. 技术严重性
     - 涉及严重安全漏洞、供应链风险、ABI/兼容性破坏、关键基础组件重大变更：3分
     - 涉及重要功能更新、性能显著变化、工具链关键演进：2分
     - 普通分析、局部优化、常规更新：1分
  3. 时效与行动价值
     - 需要 maintainer 立即关注，可能触发修复、适配、回滚、升级评估等动作：3分
     - 值得近期关注，可能影响后续路线、选型或维护计划：2分
     - 参考价值为主，短期通常不需要动作：1分
  等级映射规则：
     - 高：总体现为高影响 + 高严重性，或明显需要立即关注；通常对应严重安全事件、关键版本发布、重大兼容性变化、重要基础设施/工具链突破
     - 中：有明确技术价值和维护价值，但不属于立即响应级；通常对应重要更新、架构演进、AI/工具链进展、较重要性能/适配变化
     - 低：技术信息真实但影响面或紧迫性有限；通常对应常规性能分析、局部实现解读、一般性架构分析
  额外约束：
     - 对“内核实现分析 / 驱动机制 / 子系统原理 / 教程式内核文章”，除非明确涉及主线版本发布、重大兼容性变化、显著性能影响或维护动作要求，否则 importance 不应高于“中”，通常应为“低”。
     - 对“漏洞 / CVE / 安全公告”，不要仅因出现漏洞编号或安全字样就判为“高”。只有当正文明确体现跨社区、多发行版、上游广泛影响、供应链风险或需要 maintainer 立即响应时，才可判为“高”；普通单产品漏洞或常规公告通常应为“低”或拒收。
  不要只根据标题里的“发布 / 更新 / CVE / AI / 性能”等字样机械判级，必须结合影响范围、技术严重性、维护动作价值综合判断。
- main_category: 从可选主分类里选一个
- sub_tags: 可聚合的规范标签（字符串数组）
- keywords: 扁平关键词数组，列出文中的关键技术术语和版本细节（如 ["Linux 6.12", "RHEL 10", "systemd 256", "eBPF"]）
- confidence: 0~1 的浮点，表示你对归类与摘要的把握
- should_store: 布尔值。若页面不是新闻/热点/技术更新，或信息不足，则必须为 false
- reject_reason: 当 should_store=false 时必填，简要说明拒收原因；当 should_store=true 时可为 null
- merge_suggestions: 标签合并建议数组；如果当前 tag 更通用，可以建议把已有旧 tag 合并到当前 tag

should_store 判定规则：
- 只有当内容对 OS maintainer 具有明确情报价值、维护价值或近期决策价值时，should_store 才能为 true。
- 如果文章主要是教程、入门指引、安装说明、使用演示、机制科普、实现解读、读书笔记、经验分享，即使技术上正确，也通常 should_store=false。
- 如果是内核/驱动/子系统分析类文章，除非明确涉及主线版本发布、ABI/兼容性变化、性能显著变化、维护动作要求或生态影响，否则通常 should_store=false。
- 如果是漏洞/CVE/安全公告，只有当正文明确体现跨社区、多发行版、上游广泛影响、供应链风险或需要 maintainer 立即响应时，才值得收录；普通单产品漏洞、常规安全公告、普通 CVE 罗列通常 should_store=false。
- 如果是常规版本发布、小版本更新、普通 changelog、例行包升级，除非正文明确说明重大兼容性变化、显著性能收益、生态影响或维护动作要求，否则通常 should_store=false。
- 如果标题看起来很重要，但正文为空、过短、只有摘要碎片、站点导航、转载残片，或不足以支撑可靠摘要，则 should_store=false，不要仅凭标题脑补。
- 如果页面更像文档页、仓库首页、列表页、栏目页、登录页、验证码页、反爬挑战页、活动通知页、招聘页、营销页，必须 should_store=false。
- 当你无法确定内容是否值得维护团队关注时，默认 should_store=false，并在 reject_reason 中说明“信息不足”或“维护价值不足”。

标签聚合规则：
- sub_tags 控制在 2-4 个，优先选择能跨多篇文章复用的 canonical 名称；每条最终最多保留 5 个标签（含 main_category），把一次性细节放入 keywords。
- 使用稳定、短小的标签：厂商/发行版/项目/组件/包名/主题，如 openEuler、OpenAnolis、Fedora、RHEL、Ubuntu、Linux Kernel、RPM、Koji、glibc、systemd、CVE、安全更新、性能优化、软件包更新。
- 合并同义写法和大小写变体，例如 OpenEuler/openEuler/欧拉 统一写作 openEuler；OpenAnolis/Anolis/龙蜥 统一写作 OpenAnolis；kernel/Linux kernel/内核 统一写作 Linux Kernel。
- 不要把完整版本号、补丁号、CVE 编号、公告编号、日期、URL 片段、过长短语放入 sub_tags；这些细节放入 keywords。
- 避免近义重复：同一篇文章不要同时输出 openEuler 和 欧拉、Linux Kernel 和 kernel、RPM 和 rpm package。
- keywords 可以保留更细的原文术语、版本、包名组合和 CVE 编号，但仍应去重，避免同义写法重复。
- existing_tags 是数据库中已有标签，格式为 {"id": 数字, "name": 标签名, "usage_count": 使用次数}；优先复用 existing_tags 中适合描述当前文章的标签。
- 如果 existing_tags 里没有合适标签，才创建新的 sub_tags。
- 如果当前新增或选用的 tag 比已有旧 tag 更通用，可以输出 merge_suggestions；系统会默认 approved 写入 tag_aliases 表。
- merge_suggestions 格式：{"child_tag_id": 旧标签id, "parent_tag_id": 可选父标签id, "parent_tag_name": "父标签名", "reason": "原因", "confidence": 0.0-1.0}。

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
        categories = get_main_category_names() or list(MAIN_CATEGORIES)
        prompt = render_prompt(
            "enrich",
            _PROMPT_TEMPLATE,
            {
                "{categories}": ", ".join(categories),
                "{existing_tags}": json.dumps(existing_tags or [], ensure_ascii=False),
                "{title}": item.title,
                "{content}": item.clean_content[:6000],
            },
        )
        raw = self._llm.complete(prompt)
        data = _extract_json(raw)
        if data.get("main_category") not in categories:
            data["main_category"] = categories[-1]
        if "should_store" not in data:
            data["should_store"] = False
            data["reject_reason"] = "模型未明确给出 should_store，按保守策略拒收"
        elif not data.get("should_store") and "reject_reason" not in data:
            data["reject_reason"] = "模型判定不收录，但未提供具体原因"
        if data.get("should_store") and "reject_reason" not in data:
            data["reject_reason"] = None
        return EnrichedFields(**data)
