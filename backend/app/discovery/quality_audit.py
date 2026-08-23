"""Information-source quality audit for discovery methods."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.discovery.ingester import parse_published_at

QUALITY_AUDIT_SAMPLE_LIMIT = 12
QUALITY_LLM_TIMEOUT_SECONDS = 60.0

QUALITY_AUDIT_PROMPT = """你是 OS 技术情报系统的信息源质量审计员。

请只根据真实抓取到的条目样本，宽松评估这个信息源是否值得继续观察或长期收录。

评分重点：
- 质量还可以或较高：样本中只要有一部分条目明显涉及 OS/Linux/发行版/内核/编译器/工具链/RISC-V/CXL/性能/安全/版本/兼容性/云原生基础设施等具体技术信息，就不要给低分。
- AI Infra 也是重点技术方向：GPU/NPU/加速器、分布式训练、推理引擎与推理服务、模型部署、调度与资源管理、可观测性、向量检索等包含工程细节的内容应加分。
- Agent 技术也是重点方向：Agent Runtime、Agentic System、Context Engineering、MCP/A2A、工具调用、记忆/RAG、Agent Evals、可观测性、安全隔离和生产部署等包含架构、实现、评测或性能数据的内容应加分。
- 不要仅因文章出现 AI、Agent、LLM 等热词就加分；产品发布软文、融资宣传和缺少实现细节的泛泛观点仍按低质量处理。
- 低质量：活动通知、会议报名、社区运营报告、营销宣传、招聘、纯观点但缺少技术细节、正文空泛。
- 宽松原则：不要因为样本中混有活动、月报或宣传内容就整体打低分；只要能稳定抓到若干相关技术内容，quality_score 至少应在 60 分左右。
- 只有当样本几乎全是活动、营销、招聘、空泛宣传，且没有明显技术内容时，才给 50 分以下。

输出严格 JSON：
{{"quality_score": 0-100 的整数, "reason": "一句话原因"}}

source_kind: {source_kind}
input_type: {input_type}
items: {items_json}
"""

_TECH_KEYWORDS = (
    "linux",
    "kernel",
    "内核",
    "发行版",
    "risc-v",
    "gcc",
    "llvm",
    "编译器",
    "工具链",
    "cxl",
    "性能",
    "调优",
    "漏洞",
    "cve",
    "安全",
    "补丁",
    "版本",
    "兼容",
    "容器",
    "kubernetes",
    "云原生",
    "ai agent",
    "ai infra",
    "ai infrastructure",
    "agent runtime",
    "agentic system",
    "agentic workflow",
    "context engineering",
    "model context protocol",
    "mcp server",
    "a2a protocol",
    "tool calling",
    "function calling",
    "agent eval",
    "agent observability",
    "agent security",
    "推理引擎",
    "推理服务",
    "模型部署",
    "模型服务",
    "分布式训练",
    "显存优化",
    "gpu 调度",
    "npu",
    "cuda",
    "vllm",
    "tensorrt",
    "triton inference",
    "ray serve",
    "向量检索",
    "上下文工程",
    "智能体运行时",
    "智能体评测",
    "智能体安全",
    "openEuler".lower(),
    "openanolis",
)
_LOW_QUALITY_KEYWORDS = (
    "报名",
    "会议",
    "活动",
    "meetup",
    "峰会",
    "议程",
    "招聘",
    "直播",
    "抽奖",
    "运营报告",
    "月报",
    "周报",
    "宣传",
)


@dataclass(frozen=True)
class SourceQualityAudit:
    quality_score: int
    quality_grade: str
    quality_reason: str
    quality_sample_count: int
    density_score: int
    density_daily_avg: float
    density_weekly_avg: float
    quality_audit_status: str

    @property
    def overall_score(self) -> int:
        """综合分 = 质量分与密度分加权后的总分。

        功能：调用 calculate_overall_score 把质量分与密度分合并为 0-100 的综合分，供等级与展示使用。
        谁会调用：as_update_values、外部读取审计结果时调用。
        直接调用：
        - calculate_overall_score(...)：计算综合分。
        输入与结果：无参数（读取自身字段）；返回 0-100 整数。
        副作用：无。
        """
        return calculate_overall_score(self.quality_score, self.density_score)

    def as_update_values(self) -> dict[str, Any]:
        """把审计结果打包成可直接写库的字段字典。

        功能：汇总 overall_score、质量分、密度分、评语、采样数等字段，并附上审计时间，供落库使用。
        谁会调用：website_workflow、multi_graph 在保存方法后写库时调用。
        直接调用：
        - self.overall_score（属性）：取综合分。
        输入与结果：无参数；返回字段名到值的字典。
        副作用：无。
        """
        return {
            "overall_score": self.overall_score,
            "quality_score": self.quality_score,
            "quality_grade": self.quality_grade,
            "quality_reason": self.quality_reason,
            "quality_sample_count": self.quality_sample_count,
            "density_score": self.density_score,
            "density_daily_avg": self.density_daily_avg,
            "density_weekly_avg": self.density_weekly_avg,
            "quality_audit_status": self.quality_audit_status,
            "quality_audited_at": datetime.now(timezone.utc),
        }


def audit_source_quality(
    *,
    items: list[dict],
    source_kind: str,
    input_type: str,
    llm: Any | None = None,
) -> SourceQualityAudit:
    """根据真实抓取的条目样本评估信息源质量与发布密度。

    功能：采样条目、统计发布密度，优先用 LLM 打质量分（失败回退启发式），再算综合分、等级与评语，封装为 SourceQualityAudit。
    谁会调用：website_workflow、multi_graph 在保存爬取方法后对实跑样本做质量审计时调用。
    直接调用：
    - _sample_items(...)：取样本。
    - _density_stats(...)：统计发布密度。
    - _llm_quality_score(...)：LLM 打分（失败返回 None）。
    - _fallback_quality_score(...)：启发式打分兜底。
    - _density_score(...)：密度分映射。
    - _overall_score(...)/_grade(...)/_quality_reason(...)：综合分、等级、评语。
    输入与结果：输入 items、source_kind、input_type 与可选 llm；返回 SourceQualityAudit。
    副作用：可能调用 LLM（网络请求）。
    """
    sample = _sample_items(items)
    density = _density_stats(items)
    quality_score = _llm_quality_score(sample=sample, source_kind=source_kind, input_type=input_type, llm=llm)
    if quality_score is None:
        quality_score = _fallback_quality_score(sample)
    density_score = _density_score(density["weekly_avg"])
    overall = _overall_score(quality_score, density_score)
    status = "passed" if quality_score >= 70 else ("weak" if quality_score >= 50 else "failed")
    reason = _quality_reason(
        quality_score=quality_score,
        density_score=density_score,
        sample_count=len(sample),
        dated_count=density["dated_count"],
    )
    return SourceQualityAudit(
        quality_score=quality_score,
        quality_grade=_grade(overall),
        quality_reason=reason,
        quality_sample_count=len(sample),
        density_score=density_score,
        density_daily_avg=density["daily_avg"],
        density_weekly_avg=density["weekly_avg"],
        quality_audit_status=status,
    )


def audit_plugin_source_quality(
    *,
    items: list[dict[str, Any]],
    now: datetime | None = None,
) -> SourceQualityAudit:
    """Deterministically audit real plugin output without Agent, RAG or LLM.

    The existing quality-audit result fields and scoring rules are retained,
    while the plugin publishing path is kept independent from the legacy LLM
    audit implementation.
    """
    sample = _sample_items(items)
    density = _density_stats(items, now=now)
    quality_score = _fallback_quality_score(sample)
    density_score = _density_score(density["weekly_avg"])
    overall = _overall_score(quality_score, density_score)
    status = "passed" if quality_score >= 70 else ("weak" if quality_score >= 50 else "failed")
    return SourceQualityAudit(
        quality_score=quality_score,
        quality_grade=_grade(overall),
        quality_reason=_quality_reason(
            quality_score=quality_score,
            density_score=density_score,
            sample_count=len(sample),
            dated_count=density["dated_count"],
        ),
        quality_sample_count=len(sample),
        density_score=density_score,
        density_daily_avg=density["daily_avg"],
        density_weekly_avg=density["weekly_avg"],
        quality_audit_status=status,
    )


def audit_plugin_trial_quality(
    *,
    items_by_run: list[list[dict[str, Any]]],
) -> tuple[SourceQualityAudit, list[dict[str, Any]]]:
    """Audit both complete trials and use the most conservative field result."""
    if len(items_by_run) != 2:
        raise ValueError("plugin quality audit requires exactly two complete trials")
    evaluation_time = datetime.now(timezone.utc)
    audits = [
        audit_plugin_source_quality(items=items, now=evaluation_time)
        for items in items_by_run
    ]
    per_run = [
        {
            "run": index,
            "item_count": len(items_by_run[index - 1]),
            "evaluation_time": evaluation_time.isoformat(),
            **{
                key: (value.isoformat() if isinstance(value, datetime) else value)
                for key, value in audit.as_update_values().items()
            },
        }
        for index, audit in enumerate(audits, start=1)
    ]
    worst = min(audits, key=lambda audit: (audit.quality_score, audit.density_score))
    status = (
        "failed" if any(audit.quality_audit_status == "failed" for audit in audits)
        else "weak" if any(audit.quality_audit_status == "weak" for audit in audits)
        else "passed"
    )
    combined_score = min(audit.quality_score for audit in audits)
    combined_density = min(audit.density_score for audit in audits)
    combined_overall = calculate_overall_score(combined_score, combined_density)
    combined = SourceQualityAudit(
        quality_score=combined_score,
        quality_grade=_grade(combined_overall),
        quality_reason=(
            f"双试跑保守质量结论：run1={audits[0].quality_audit_status}/{audits[0].quality_score}，"
            f"run2={audits[1].quality_audit_status}/{audits[1].quality_score}；{worst.quality_reason}"
        ),
        quality_sample_count=min(audit.quality_sample_count for audit in audits),
        density_score=combined_density,
        density_daily_avg=min(audit.density_daily_avg for audit in audits),
        density_weekly_avg=min(audit.density_weekly_avg for audit in audits),
        quality_audit_status=status,
    )
    return combined, per_run


def apply_quality_audit_to_method(method: Any, audit: SourceQualityAudit) -> None:
    """把审计结果写回爬取方法对象的对应字段。

    功能：遍历审计结果字段字典，逐个 setattr 到 method 对象上，供随后落库保存质量信息。
    谁会调用：website_workflow、multi_graph 在质量审计后调用。
    直接调用：
    - audit.as_update_values(...)：取字段字典。
    - setattr(...)：写字段。
    输入与结果：输入 method 对象与审计结果；无返回值。
    副作用：修改 method 对象的字段（内存中）。
    """
    for key, value in audit.as_update_values().items():
        setattr(method, key, value)


def _sample_items(items: list[dict]) -> list[dict[str, Any]]:
    """取前 N 条条目并规整成供 LLM 评估的精简样本。

    功能：截取样本上限内的条目，压缩标题/正文空白并截断，仅保留 title/url/published_at/content_snippet。
    谁会调用：audit_source_quality 在评估前调用。
    直接调用：无（仅字典构造与字符串处理）。
    输入与结果：输入 items；返回精简样本列表。
    副作用：无。
    """
    sample: list[dict[str, Any]] = []
    for item in items[:QUALITY_AUDIT_SAMPLE_LIMIT]:
        title = " ".join(str(item.get("title") or "").split())
        content = item.get("content") or item.get("summary") or ""
        sample.append(
            {
                "title": title[:240],
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "content_snippet": " ".join(str(content).split())[:360],
            }
        )
    return sample


def _llm_quality_score(
    *,
    sample: list[dict[str, Any]],
    source_kind: str,
    input_type: str,
    llm: Any | None,
) -> int | None:
    """用 LLM 对样本打信息源质量分，失败则回退到 None。

    功能：渲染质量审计 prompt 并调用 LLM 取 JSON 中的 quality_score，解析并夹到 0-100；任何异常都记警告并返回 None 触发启发式兜底。
    谁会调用：audit_source_quality 在打质量分时调用。
    直接调用：
    - render_prompt(...)：渲染 prompt 模板。
    - LlmClient().complete(...)/llm.complete(...)：调用 LLM。
    - _extract_json(...)：解析返回 JSON。
    - _clamp_score(...)：夹分。
    输入与结果：输入样本、source_kind、input_type 与可选 llm；返回质量分或 None。
    副作用：调用 LLM（网络请求），并记录日志。
    """
    if not sample:
        return 0
    import logging

    from app.discovery.prompts import render_prompt

    logger = logging.getLogger(__name__)
    prompt = render_prompt(
        "quality_audit",
        QUALITY_AUDIT_PROMPT,
        {
            "{source_kind}": source_kind,
            "{input_type}": input_type,
            "{items_json}": json.dumps(sample, ensure_ascii=False),
        },
    )
    try:
        if llm is None:
            from app.llm.client import LlmClient

            raw = LlmClient().complete(
                prompt,
                temperature=0.0,
                response_format={"type": "json_object"},
                timeout=QUALITY_LLM_TIMEOUT_SECONDS,
            )
        else:
            raw = llm.complete(
                prompt,
                temperature=0.0,
                response_format={"type": "json_object"},
                timeout=QUALITY_LLM_TIMEOUT_SECONDS,
            )
        data = _extract_json(raw)
        return _clamp_score(data.get("quality_score"))
    except Exception as exc:
        logger.warning(
            "quality audit LLM failed, falling back to heuristic · error=%s",
            str(exc)[:300],
        )
        return None


def _fallback_quality_score(sample: list[dict[str, Any]]) -> int:
    """LLM 不可用时的关键词启发式质量打分（宽松策略）。

    功能：对每条样本统计技术关键词/低质量关键词命中与长度加成，按相关条目数量采用宽松加权，避免混有活动/宣传内容被整体打低。
    谁会调用：audit_source_quality 在 LLM 打分失败时调用。
    直接调用：无（关键词匹配与数值运算）。
    输入与结果：输入样本；返回 0-100 整数。
    副作用：无。
    """
    if not sample:
        return 0
    scores: list[int] = []
    for item in sample:
        text = f"{item.get('title') or ''}\n{item.get('content_snippet') or ''}".lower()
        tech_hits = sum(1 for keyword in _TECH_KEYWORDS if keyword in text)
        low_hits = sum(1 for keyword in _LOW_QUALITY_KEYWORDS if keyword in text)
        length_bonus = min(len(text) // 160, 3) * 5
        score = 45 + min(tech_hits, 4) * 12 + length_bonus - min(low_hits, 3) * 18
        scores.append(max(0, min(100, score)))
    best = max(scores)
    average = sum(scores) / len(scores)
    relevant_count = sum(1 for score in scores if score >= 60)
    # 宽松策略：信息源只要已经抓到比较相关的技术条目，就不按全样本平均值严惩。
    if relevant_count >= 3:
        return int(round(best * 0.55 + average * 0.45))
    if relevant_count >= 1:
        return int(round(max(60, best * 0.65 + average * 0.35)))
    return int(round(average))


def _density_stats(
    items: list[dict],
    *,
    now: datetime | None = None,
) -> dict[str, float | int]:
    """统计条目的发布时间密度（近 7 天 / 30 天）。

    功能：解析每条发布时间，统计近一周与近一月条数，估算周均/日均密度，无时间则按总条数兜底。
    谁会调用：audit_source_quality 在评估发布密度时调用。
    直接调用：
    - parse_published_at(...)：解析发布时间。
    输入与结果：输入 items；返回含 dated_count/weekly_avg/daily_avg 的字典。
    副作用：无。
    """
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    dated: list[datetime] = []
    for item in items:
        dt = parse_published_at(item.get("published_at"))
        if dt is not None:
            dated.append(dt)
    if dated:
        weekly_count = sum(1 for dt in dated if dt >= week_ago)
        monthly_count = sum(1 for dt in dated if dt >= month_ago)
        weekly_avg = weekly_count if weekly_count else monthly_count / 4.0
    else:
        weekly_avg = float(min(len(items), 7))
    return {
        "dated_count": len(dated),
        "weekly_avg": round(float(weekly_avg), 2),
        "daily_avg": round(float(weekly_avg) / 7.0, 2),
    }


def _density_score(weekly_avg: float) -> int:
    """把周均发布条数映射成 0-100 的密度分。

    功能：按周均条数分档（≥10→100，≥5→80，≥2→55，≥1→35，否则 0）给出密度评分。
    谁会调用：audit_source_quality 在算综合分时调用。
    直接调用：无（仅区间判断）。
    输入与结果：输入周均条数；返回 0-100 整数。
    副作用：无。
    """
    if weekly_avg >= 10:
        return 100
    if weekly_avg >= 5:
        return 80
    if weekly_avg >= 2:
        return 55
    if weekly_avg >= 1:
        return 35
    return 0


def _quality_reason(*, quality_score: int, density_score: int, sample_count: int, dated_count: int) -> str:
    """拼出一句信息源质量评语。

    功能：依据质量分、密度分、样本数与时间充足情况，组合成「质量高/一般/不足，信息密度高/一般/低，发布时间充足/不足，样本 N 条」的摘要。
    谁会调用：audit_source_quality 在构造审计结果时调用。
    直接调用：无（仅字符串拼接）。
    输入与结果：输入各分数与计数；返回评语字符串。
    副作用：无。
    """
    if sample_count == 0:
        return "未抓到可评估样本"
    quality_text = "质量高" if quality_score >= 70 else ("质量一般" if quality_score >= 50 else "质量不足")
    density_text = "信息密度高" if density_score >= 80 else ("信息密度一般" if density_score >= 35 else "信息密度低")
    date_text = "发布时间充足" if dated_count else "发布时间不足"
    return f"{quality_text}，{density_text}，{date_text}，样本 {sample_count} 条"


def calculate_overall_score(quality_score: int, density_score: int) -> int:
    """综合质量分与密度分算综合分（含加减分规则）。

    功能：以质量分 0.75、密度分 0.25 加权，并按质量/密度组合追加或扣减分值（如高质量低密度加分），最后夹到 0-100。
    谁会调用：SourceQualityAudit.overall_score、discovery_routes、mail/service、review 在计算综合评级时调用。
    直接调用：无（仅数值运算）。
    输入与结果：输入质量分与密度分；返回 0-100 整数。
    副作用：无。
    """
    score = quality_score * 0.75 + density_score * 0.25
    if quality_score >= 85 and density_score < 55:
        score += 10
    if quality_score >= 80 and density_score >= 80:
        score += 5
    if quality_score < 50 and density_score >= 80:
        score -= 15
    if quality_score < 50 and density_score < 35:
        score -= 10
    return max(0, min(100, int(round(score))))


def _overall_score(quality_score: int, density_score: int) -> int:
    """转发调用 calculate_overall_score 计算综合分（内部封装）。

    功能：仅是对综合分计算函数的封装，便于审计内部统一调用。
    谁会调用：audit_source_quality 在算综合分时调用。
    直接调用：
    - calculate_overall_score(...)：实际计算综合分。
    输入与结果：输入质量分与密度分；返回 0-100 整数。
    副作用：无。
    """
    return calculate_overall_score(quality_score, density_score)


def _grade(score: int) -> str:
    """把综合分映射成等级字母 A/B/C/D。

    功能：按区间（≥85→A，≥70→B，≥50→C，否则 D）给出质量等级。
    谁会调用：audit_source_quality 在构造审计结果时调用。
    直接调用：无（仅区间判断）。
    输入与结果：输入综合分；返回等级字母。
    副作用：无。
    """
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _clamp_score(value: Any) -> int:
    """把任意值夹成 0-100 的整数分数。

    功能：尝试把输入转成浮点并四舍五入，再夹到 0-100；转换失败返回 0。
    谁会调用：_llm_quality_score 在解析 LLM 返回分时使用。
    直接调用：无（int/round/max/min）。
    输入与结果：输入任意值；返回 0-100 整数。
    副作用：无。
    """
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, number))


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 返回文本中解析出 JSON 对象（容错去多余文本）。

    功能：先直接 json.loads；失败则用正则截取第一个 {...} 再解析，用于兼容模型在 JSON 外附加说明的情况。
    谁会调用：_llm_quality_score 在解析 LLM 返回时调用。
    直接调用：
    - json.loads(...)：解析 JSON。
    - re.search(...)：截取 JSON 片段。
    输入与结果：输入文本；返回字典（解析失败则抛异常）。
    副作用：无。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))
