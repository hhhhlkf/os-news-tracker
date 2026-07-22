"""Information-source quality audit for discovery methods."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.discovery.ingester import parse_published_at

QUALITY_AUDIT_SAMPLE_LIMIT = 12
QUALITY_LLM_TIMEOUT_SECONDS = 20.0

QUALITY_AUDIT_PROMPT = """你是 OS 技术情报系统的信息源质量审计员。

请只根据真实抓取到的条目样本，宽松评估这个信息源是否值得继续观察或长期收录。

评分重点：
- 质量还可以或较高：样本中只要有一部分条目明显涉及 OS/Linux/发行版/内核/编译器/工具链/RISC-V/CXL/性能/安全/版本/兼容性/云原生基础设施等具体技术信息，就不要给低分。
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
        return calculate_overall_score(self.quality_score, self.density_score)

    def as_update_values(self) -> dict[str, Any]:
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
    """Evaluate source quality and publishing density from real fetched items."""
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


def apply_quality_audit_to_method(method: Any, audit: SourceQualityAudit) -> None:
    for key, value in audit.as_update_values().items():
        setattr(method, key, value)


def _sample_items(items: list[dict]) -> list[dict[str, Any]]:
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
    if not sample:
        return 0
    from app.discovery.prompts import render_prompt

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
            raw = llm.complete(prompt, temperature=0.0, response_format={"type": "json_object"})
        data = _extract_json(raw)
        return _clamp_score(data.get("quality_score"))
    except Exception:
        return None


def _fallback_quality_score(sample: list[dict[str, Any]]) -> int:
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


def _density_stats(items: list[dict]) -> dict[str, float | int]:
    now = datetime.now(timezone.utc)
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
    if sample_count == 0:
        return "未抓到可评估样本"
    quality_text = "质量高" if quality_score >= 70 else ("质量一般" if quality_score >= 50 else "质量不足")
    density_text = "信息密度高" if density_score >= 80 else ("信息密度一般" if density_score >= 35 else "信息密度低")
    date_text = "发布时间充足" if dated_count else "发布时间不足"
    return f"{quality_text}，{density_text}，{date_text}，样本 {sample_count} 条"


def calculate_overall_score(quality_score: int, density_score: int) -> int:
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
    return calculate_overall_score(quality_score, density_score)


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _clamp_score(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, number))


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))
