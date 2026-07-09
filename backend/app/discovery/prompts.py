"""站点发现（fetch/生成阶段）LLM prompt 的可配置化支持。

设计：
- 每个"阶段"（stage）对应发现流程里的一次 LLM 调用，有一个内置默认模版。
- 用户可保存多套 prompt（DiscoveryPromptSet），同一时刻最多 1 套 active。
- 各 prompt 使用 ``{token}`` 占位符 + ``str.replace`` 注入（避免 ``.format`` 被 prompt 里
  的 JSON 花括号搞崩），保存时校验必需 token 是否齐全。
- ``resolve_prompt(stage_key, fallback)`` 在运行时读取 active 套的覆盖文本；没有覆盖或
  文本为空则回退到调用点传入的内置默认，保证零配置时行为不变。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Mapping

logger = logging.getLogger(__name__)


# 仅由本模块拥有的新模版（原代码里是内联拼装的，抽成 token 模版供编辑）
DEFAULT_SYNTHESIS = """你是站点探查结果整理器。请根据给定站点探查证据，输出一个 JSON object。要求：
1. 只输出 JSON object，不要解释，不要 Markdown；
2. 所有字符串必须是合法 JSON 字符串；
3. 如果证据不足，如实返回 source_type=unknown, success=false；
4. 尽量保留证据中已经确认的字段和值。
4.1 下面会提供一个程序提取出的候选池，它们只是候选，不是最终答案；由你来选择最像文章列表的那个。
4.2 若候选像 tags/archives/count 统计接口，而不是文章列表，不要选它。
5. 若 success=true 且 source_type=json_api，必须同时给出：list_url、format_locator.value（json path）、fields.title、至少 1 条 sample_items，以及 fields.url 或 fields.id 或 sample_items 中的 path/url。
5.1 对 JSON API：list_url 只放不带 query string 的接口 URL；URL 上的 ?a=b&page=1 等参数必须拆到 fetch.query；POST 请求体参数必须放 fetch.json_body。不要把分页/筛选参数混在 list_url 里。
6. 若 success=true 且 source_type=rss/atom，必须给出 list_url 且 format_locator.kind=feed_entries。
7. 若 success=true 且 source_type=html，必须给出 html_selectors 四项和 sample_items。

输出 schema：
{
  "source_type": "json_api | rss | atom | html | unknown",
  "list_url": "string or null",
  "fetch": {"method": "GET | POST", "transport": "httpx | scrapling", "impersonate": null, "stealthy_headers": true, "headers": {}, "query": {}, "json_body": null},
  "format_locator": {"kind": "json_path | feed_entries | html_selector | unknown", "value": "string"},
  "fields": {"id": null, "title": null, "url": null, "published_at": null, "summary": null, "content": null},
  "html_selectors": {"item_selector": null, "link_selector": null, "title_selector": null, "date_selector": null},
  "sample_items": [{"id": null, "title": null, "raw_url": null, "url": null, "published_at": null, "raw": null}],
  "url_candidates": [],
  "pagination": {"type": "none | page_param | offset_limit | cursor | next_url | html_next | unknown", "page_param": null, "size_param": null, "offset_param": null, "limit_param": null, "cursor_param": null, "next_path": null, "has_more_path": null, "start": 1, "size": null, "notes": ""},
  "evidence": [],
  "notes": [],
  "success": true
}

站点 URL: {site_url}
程序提取的候选池（供你自行选择，不要机械接受）:
{deterministic_candidates}

ReAct 证据轨迹:
{evidence}
"""

DEFAULT_NAMING = """你是一个网站命名助手。请根据给定的网站信息，生成一个适合作为站点名称的短标题。要求：
1. 最终结果不超过20个字符；
2. 可以是中文、英文或中英文混合短语；
3. 像站点名，不要写解释；
4. 只输出名称本身。

URL: {site_url}
域名: {domain}
页面标题: {title}
"""


@dataclass(frozen=True)
class StageDef:
    key: str
    label: str
    description: str
    required_tokens: list[str] = field(default_factory=list)


# 发现流程各 LLM 阶段（顺序即执行顺序）。default_template 由 get_stage_defaults() 动态提供。
STAGES: list[StageDef] = [
    StageDef(
        key="explorer_system",
        label="探查员系统指令",
        description="ReAct 探查员的系统角色与工具使用规则，决定如何摸清站点文章列表来源。",
        required_tokens=[],
    ),
    StageDef(
        key="synthesis",
        label="探查结果整理",
        description="把探查证据整理成结构化 JSON（source_type / list_url / 字段映射等）。",
        required_tokens=["{site_url}", "{deterministic_candidates}", "{evidence}"],
    ),
    StageDef(
        key="validator",
        label="URL 规律推断",
        description="根据探查结果推断列表页/接口的 URL 规律与分页规则。",
        required_tokens=["{site_url}", "{exploration}"],
    ),
    StageDef(
        key="dsl_writer",
        label="爬取配方编写",
        description="根据 URL 规律与探查结果生成可执行的抓取 DSL 配方。",
        required_tokens=["{site_url}", "{url_rule}", "{exploration}", "{retry_feedback}"],
    ),
    StageDef(
        key="auditor",
        label="配方审计",
        description="评判配方实跑结果是否真文章、是否翻页、是否被反爬挡住、是否值得入库。",
        required_tokens=[
            "{site_url}", "{recipe_summary}", "{errors}",
            "{discovered_count}", "{n}", "{items_sample}",
        ],
    ),
    StageDef(
        key="naming",
        label="站点命名",
        description="根据站点信息生成简短的站点显示名称。",
        required_tokens=["{site_url}", "{domain}", "{title}"],
    ),
    StageDef(
        key="enrich",
        label="富集 / 总结 / 筛选",
        description="抓取入库管线：对每条文章做中文摘要、分类、标签、重要性、是否入库（含反爬页判定）。",
        required_tokens=["{categories}", "{existing_tags}", "{title}", "{content}"],
    ),
]

STAGE_MAP: dict[str, StageDef] = {s.key: s for s in STAGES}


def get_stage_defaults() -> dict[str, str]:
    """返回各阶段的内置默认模版文本（前端"新建"时的预定模版）。

    对原本就是内联/常量的 4 个阶段延迟从各自模块导入，避免模块级循环导入。
    """
    from app.api.discovery_routes import _NAMING_PROMPT  # noqa: F401 - 仅取常量
    from app.discovery.graph import (
        DSL_WRITER_PROMPT,
        EXPLORER_SYSTEM_PROMPT,
        VALIDATOR_PROMPT,
        _AUDIT_PROMPT,
        _SYNTHESIS_PROMPT,
    )
    from app.processing.enricher import _PROMPT_TEMPLATE as ENRICH_PROMPT

    return {
        "explorer_system": EXPLORER_SYSTEM_PROMPT,
        "synthesis": _SYNTHESIS_PROMPT,
        "validator": VALIDATOR_PROMPT,
        "dsl_writer": DSL_WRITER_PROMPT,
        "auditor": _AUDIT_PROMPT,
        "naming": _NAMING_PROMPT,
        "enrich": ENRICH_PROMPT,
    }


def validate_prompts(prompts: dict[str, str]) -> list[str]:
    """校验一套 prompt：返回错误信息列表（空=通过）。

    - 未知 stage key → 报错
    - 非空文本必须包含该阶段所有必需 token
    """
    errors: list[str] = []
    for key, text in (prompts or {}).items():
        stage = STAGE_MAP.get(key)
        if stage is None:
            errors.append(f"未知阶段: {key}")
            continue
        if not isinstance(text, str) or not text.strip():
            continue  # 空 = 用默认，不校验
        missing = [tok for tok in stage.required_tokens if tok not in text]
        if missing:
            errors.append(f"「{stage.label}」缺少必需占位符: {', '.join(missing)}")
    return errors


def resolve_prompt(stage_key: str, fallback: str) -> str:
    """运行时解析某阶段 prompt：active 套有非空覆盖则用它，否则用 fallback（内置默认）。

    任何异常（表未建、DB 不可用等）都安全回退到 fallback，保证发现流程不被配置影响。
    """
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import DiscoveryPromptSet

        session = SessionLocal()
        try:
            active = session.scalar(
                select(DiscoveryPromptSet).where(DiscoveryPromptSet.is_active.is_(True)).limit(1)
            )
            if active is not None:
                text = (active.prompts or {}).get(stage_key)
                if isinstance(text, str) and text.strip():
                    return text
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 配置读取失败不能影响发现流程
        logger.debug("resolve_prompt fell back to default for stage=%s", stage_key, exc_info=True)
    return fallback


def render_prompt(stage_key: str, fallback: str, tokens: Mapping[str, object] | None = None) -> str:
    """统一渲染 prompt。

    - 先解析 active/fallback prompt 文本
    - 再按 ``{token}`` 逐个执行字符串替换
    - 最后检查该阶段声明的必需 token 是否仍有残留，防止漏传
    """
    prompt = resolve_prompt(stage_key, fallback)
    for key, value in (tokens or {}).items():
        prompt = prompt.replace(key, "" if value is None else str(value))

    stage = STAGE_MAP.get(stage_key)
    if stage is not None:
        missing = [token for token in stage.required_tokens if token in prompt]
        if missing:
            raise ValueError(f"render_prompt missing required tokens for {stage_key}: {', '.join(missing)}")
    return prompt
