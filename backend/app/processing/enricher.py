import json
import re
from typing import Any

from app.categories import get_main_category_names
from app.discovery.prompts import render_prompt
from app.enums import MAIN_CATEGORIES, Importance, InfoType
from app.llm.client import LlmClient
from app.schemas import EnrichedFields, NormalizedItem

ENRICH_CONTENT_CHAR_LIMIT = 3000

_PROMPT_TEMPLATE = """你是操作系统与 AI 基础设施维护工程师的关键技术新闻与技术情报分析师。阅读下面的技术文章，提取结构化情报。

收录标准分为彼此独立的两条路径：文章只要满足 **OS 路径** 或 **AI 路径** 任一条，即可考虑收录；不要求 AI 内容证明与 OS 直接相关，也不要求 OS 内容涉及 AI。两条都不满足时不收录。
- OS 路径：新兴技术/工具/架构进入可观察阶段，且操作系统、内核、发行版、编译器、包管理、运行时或云原生基础设施出现重要发布、重大更新、性能基准、兼容性变化或技术路线变化，并对 OS 维护、升级、部署或选型有明确价值。
- AI 路径：AI/LLM/Agent 的推理与 serving、训练与评测基础设施、Agent 运行时、模型工具链、部署与可观测性、成本/性能/可靠性或开放生态出现重要发布、重大更新、可复现性能数据、兼容性变化或技术路线变化，并具备可维护、可复用或可操作的工程价值。
Linux 关联要求：针对 OS 路径，Linux 必须是文章的技术主体，或文章中的变更必须对 Linux 内核、发行版、Linux 软件栈、Linux 部署/兼容性/维护产生可验证的实质影响。仅因为文章提到 macOS、Windows 或其他操作系统，或泛泛讨论“跨平台”“操作系统”，不能视为满足 OS 路径；此类内容只有在正文明确说明与 Linux 的实质关联时才可收录。不要把 macOS、Windows 或其他非 Linux 系统自身的版本、产品、生态或使用技巧当作 Linux 情报。
人员变动或公司商业情况不是技术新闻，但可作为有限例外：仅当正文能证明其会实质影响 Linux/开源项目的技术路线、维护者与支持承诺、发行版生命周期、产品可用性、生态合作或用户部署决策时，才可酌情收录；纯高管任免、融资、营收、收购传闻、市场份额或一般商业宣传仍不收录。按此例外收录的消息通常 importance=低；只有同时给出明确、已发生的重大技术或生态影响时，才可评为中，不能仅凭公司规模或职位高低评为高。
安全内容默认从严收录：普通单产品漏洞、例行安全公告和 CVE 罗列通常不收录。只有正文能证明其跨社区/跨发行版/上游生态影响、存在供应链风险，或具有显著的维护与技术决策价值时，才考虑收录。
政策、法律、监管或合规消息本身不是技术新闻：即使标题或正文提到 Linux、操作系统、AI 或开源项目，只要内容主要是在转述法案、监管要求、法律争议或预测潜在影响，而没有已经发生且可验证的技术实现、上游/发行版响应、兼容性变化或部署动作，必须不收录。不得把“可能迫使项目实现某功能”的推测当作技术变更。
论文/学术工作默认更严：只收正文能证明具备工程落地价值的成果——可在真实或接近真实的系统环境复现、能转化为可维护的软件/系统能力，并且满足 OS 路径或 AI 路径之一。纯仿真、纯理论、玩具数据集上的微小增益、缺乏工程落地路径的论文通常不收录。
AI/LLM 内容重点关注但拒绝注水：优先收录推理/serving、训练与评测基础设施、Agent 运行时、模型工具链、与操作系统或云原生结合的重要进展；仅换模型骨架/超参/数据集、只报微小指标提升、缺乏可复用工程价值的“改进型”论文或软文通常不收录。
硬件内容默认不推送：芯片设计、光通信、射频/PCB、服务器整机规格、纯硬件架构介绍、硬件跑分评测等通常不收录；只有正文明确证明其对操作系统、内核、驱动、设备支持、资源调度、兼容性或维护决策有直接影响时，才可例外考虑。
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
  2. 技术价值与变化深度
     - 关键的非硬件版本发布、重大兼容性/ABI 变化、基础设施或工具链突破、具有充分事实支撑的新技术方向，或跨生态的严重安全/供应链风险：3分
     - 重要功能更新、性能显著变化、工具链关键演进、值得维护团队研究的架构路线：2分
     - 普通分析、局部优化、常规更新；或内容以硬件产品、规格、跑分、架构介绍为主：1分
  3. 相关性与维护价值
     - 与 OS 维护或 AI 基础设施/工具链维护的核心关注高度相关，能明显支撑技术探讨、路线判断、选型、升级、部署或维护计划：3分
     - 与近期维护或后续规划有关，具备明确参考价值：2分
     - 参考价值有限，短期通常不影响技术判断：1分
  等级映射规则：
     - 高：高相关性且高技术/维护价值即可，不以“必须立即响应”或“必须高安全严重性”为前提；通常对应关键版本发布、重大兼容性变化、重要基础设施/工具链突破，或事实扎实、值得深入探讨的新技术方向。
     - 中：有明确技术价值和维护价值，但影响范围、变化深度或相关性未达到高；通常对应重要更新、架构演进、AI/工具链进展、较重要性能/适配变化。
     - 低：技术信息真实但影响面或紧迫性有限；通常对应常规性能分析、局部实现解读、一般性架构分析
  额外约束：
     - 对“内核实现分析 / 驱动机制 / 子系统原理 / 教程式内核文章”，除非明确涉及主线版本发布、重大兼容性变化、显著性能影响或维护动作要求，否则 importance 不应高于“中”，通常应为“低”。
     - 对“漏洞 / CVE / 安全公告”，不要仅因出现漏洞编号、安全字样或 CVSS 分数就判为“高”。只有正文明确体现跨社区、多发行版、上游广泛影响、供应链风险，且对维护决策有高价值时，才可判为“高”；普通单产品漏洞或常规公告通常应为“低”或拒收。
     - 对人员变动或公司商业情况，若按有限例外收录，通常 importance 必须为“低”；只有正文明确证明已经产生重大 Linux/开源技术路线、维护、发行版生命周期或生态影响时才可为“中”，不得判为“高”。
  不要只根据标题里的“发布 / 更新 / CVE / AI / 性能”等字样机械判级，必须结合影响范围、技术变化深度、与维护团队的相关性及探讨价值综合判断。
- main_category: 从可选主分类里选一个
- sub_tags: 可聚合的规范标签（字符串数组）
- keywords: 扁平关键词数组，列出文中的关键技术术语和版本细节（如 ["Linux 6.12", "RHEL 10", "systemd 256", "eBPF"]）
- confidence: 0~1 的浮点，表示你对归类与摘要的把握
- should_store: 布尔值。若页面不是新闻/热点/技术更新，或信息不足，则必须为 false
- reject_reason: 当 should_store=false 时必填，简要说明拒收原因；当 should_store=true 时可为 null
- should_fetch_full_text: 布尔值。仅当 should_store=false 且当前标题/正文片段不足以判断、但该条目可能包含符合收录标准的 OS 或 AI 工程情报时为 true；这表示值得访问详情页补齐证据后重新判断。若当前片段已足以确认是营销、活动、招聘、教程、文档/列表页、反爬页或缺乏收录价值，则必须为 false。不得使用固定关键词表作此判断；请根据标题和已给正文的实际语义判断。should_store=true 时必须为 false。
- merge_suggestions: 标签合并建议数组（必须是 JSON 数组，没有建议时输出 []）；如果当前 tag 更通用，可以建议把已有旧 tag 合并到当前 tag
- 即使 should_store=false，也必须输出完整 JSON：title_zh、summary、info_type、importance、main_category 不可省略；数组字段缺失时用 []

should_store 判定规则：
- 只有当内容通过 OS 路径或 AI 路径之一，且对相应维护团队具有明确情报价值、维护价值或近期决策价值时，should_store 才能为 true。不得因为文章同时覆盖两者才提高收录门槛。
- OS 路径必须有 Linux 实质关联：Linux 是技术主体，或正文可验证地说明该变化影响 Linux 内核、发行版、软件栈、部署、兼容性或维护。只涉及 macOS、Windows 或其他系统自身内容，或只有“跨平台/操作系统”泛泛表述而没有 Linux 关联时，必须 should_store=false；不得以非 Linux 系统内容替代 Linux 相关性。
- 人员变动或公司商业情况只有在已发生且可验证地影响 Linux/开源项目的技术路线、维护支持、发行版生命周期、生态合作或用户部署决策时，才可酌情 should_store=true；纯人事新闻、融资营收、市场份额、收购传闻或商业宣传必须 should_store=false。按此例外收录时，默认 importance=低。
- 如果文章主要是教程、入门指引、安装说明、使用演示、机制科普、实现解读、读书笔记、经验分享，即使技术上正确，也通常 should_store=false。
- 如果是内核/驱动/子系统分析类文章，除非明确涉及主线版本发布、ABI/兼容性变化、性能显著变化、维护动作要求或生态影响，否则通常 should_store=false。
- 如果是漏洞/CVE/安全公告，只有当正文明确体现跨社区、多发行版、上游广泛影响、供应链风险，或有显著维护与技术决策价值时，才值得收录；普通单产品漏洞、常规安全公告、普通 CVE 罗列通常 should_store=false。
- 如果内容主要是政策、法律、监管、合规公告或其解读，即使涉及 OS、Linux、AI 或开源，也必须 should_store=false；只有在文章提供已落地的技术实现、上游/发行版正式响应、已发生的兼容性变化或可验证部署影响时，才按 OS 路径或 AI 路径重新判断。
- 如果是常规版本发布、小版本更新、普通 changelog、例行包升级，除非正文明确说明重大兼容性变化、显著性能收益、生态影响或维护动作要求，否则通常 should_store=false。
- 如果内容是论文、预印本、学术会议稿或“我们提出了一种方法”类研究：必须满足“可落地/可工程化”，并满足 OS 路径或 AI 路径之一；纯仿真、仅理论证明、仅实验室玩具环境、无可复现工程路径的成果，必须 should_store=false。
- 如果内容属于 AI/LLM/Agent 方向：重点看是否带来可维护的基础设施、运行时、工具链或系统能力变化；若只是微小精度/分数提升、替换模块后的注水改进、缺乏工程复用价值，必须 should_store=false。
- 如果内容主要讲芯片设计、光通信、射频、PCB、CPU/GPU/NPU/服务器/存储等硬件产品、规格、跑分或硬件架构，而没有明确说明对操作系统、内核、驱动、发行版、设备支持、资源调度、兼容性或维护决策的直接影响，必须 should_store=false。
- 如果标题看起来很重要，但正文为空、过短、只有摘要碎片、站点导航、转载残片，或不足以支撑可靠摘要，则 should_store=false，不要仅凭标题脑补。
- 如果页面更像文档页、仓库首页、列表页、栏目页、登录页、验证码页、反爬挑战页、活动通知页、招聘页、营销页，必须 should_store=false。
- 当你无法确定内容是否满足 OS 路径或 AI 路径、或是否值得相应维护团队关注时，默认 should_store=false，并在 reject_reason 中说明“信息不足”或“维护价值不足”。

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


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_merge_suggestions(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _coerce_info_type(value: Any) -> str:
    text = str(value or "").strip()
    for item in InfoType:
        if text == item.value:
            return item.value
    return InfoType.OTHER.value


def _coerce_importance(value: Any) -> str:
    text = str(value or "").strip()
    for item in Importance:
        if text == item.value:
            return item.value
    return Importance.LOW.value


def _normalize_enrich_payload(
    data: dict[str, Any],
    *,
    categories: list[str],
    title: str,
) -> dict[str, Any]:
    """Coerce incomplete/invalid LLM enrich JSON into EnrichedFields-compatible shape.

    Models often omit required fields or emit non-list merge_suggestions when rejecting;
    without normalization those become enrich_failed instead of enrich_reject.
    """
    normalized = dict(data)

    if "should_store" not in normalized:
        normalized["should_store"] = False
        normalized["reject_reason"] = "模型未明确给出 should_store，按保守策略拒收"
    else:
        normalized["should_store"] = bool(normalized.get("should_store"))

    normalized["should_fetch_full_text"] = bool(normalized.get("should_fetch_full_text"))
    if normalized["should_store"]:
        normalized["should_fetch_full_text"] = False

    if normalized["should_store"]:
        missing_required = [
            key
            for key in ("title_zh", "summary", "info_type", "importance")
            if not str(normalized.get(key) or "").strip()
        ]
        if missing_required:
            normalized["should_store"] = False
            normalized["reject_reason"] = (
                f"模型输出缺少必填字段（{', '.join(missing_required)}），按保守策略拒收"
            )
        elif "reject_reason" not in normalized:
            normalized["reject_reason"] = None
    else:
        if not str(normalized.get("reject_reason") or "").strip():
            normalized["reject_reason"] = "模型判定不收录，但未提供具体原因"

    title_zh = str(normalized.get("title_zh") or "").strip()
    if not title_zh:
        title_zh = (title or "未命名条目").strip()[:20] or "未命名条目"
    normalized["title_zh"] = title_zh

    summary = str(normalized.get("summary") or "").strip()
    if not summary:
        summary = str(normalized.get("reject_reason") or "模型未提供摘要").strip()
    normalized["summary"] = summary

    if normalized.get("main_category") not in categories:
        normalized["main_category"] = categories[-1]

    normalized["info_type"] = _coerce_info_type(normalized.get("info_type"))
    normalized["importance"] = _coerce_importance(normalized.get("importance"))
    normalized["tech_highlights"] = _as_str_list(normalized.get("tech_highlights"))
    normalized["sub_tags"] = _as_str_list(normalized.get("sub_tags"))
    normalized["keywords"] = _as_str_list(normalized.get("keywords"))
    normalized["merge_suggestions"] = _as_merge_suggestions(normalized.get("merge_suggestions"))

    try:
        normalized["confidence"] = float(normalized.get("confidence") or 0.0)
    except (TypeError, ValueError):
        normalized["confidence"] = 0.0

    return normalized


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
                "{content}": item.clean_content[:ENRICH_CONTENT_CHAR_LIMIT],
            },
        )
        raw = self._llm.complete(prompt)
        data = _normalize_enrich_payload(
            _extract_json(raw),
            categories=categories,
            title=item.title,
        )
        return EnrichedFields(**data)
