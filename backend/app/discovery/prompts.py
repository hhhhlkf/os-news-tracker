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
4.3 若证据里有 api_reviews，且存在 usable=true、score>=80 的 article_list_api 或 paginated_article_api，应优先选择对应 JSON API；embedded_json 只作为没有高分真实网络 API 时的兜底。
5. 若 success=true 且 source_type=json_api，必须同时给出：list_url、format_locator.value（json path）、fields.title、至少 1 条 sample_items，以及 fields.url 或 fields.id 或 sample_items 中的 path/url。
5.1 对 JSON API：list_url 只放不带 query string 的接口 URL；URL 上的 ?a=b&page=1 等参数必须拆到 fetch.query；POST 请求体参数必须放 fetch.json_body。不要把分页/筛选参数混在 list_url 里。
5.2 对 JSON API：必须认真填写 pagination。若 api_reviews 或证据显示同 endpoint 有分页：
   - page_param：页码型，参数按 1、2、3 递增；
   - offset_limit：偏移型，某个数字参数按每页大小递增，如 offset=0/20/40，或 page_token=12/24/36 且 count=12；
   - cursor：游标型，下一页参数来自响应里的 next_cursor/next_page_token，不能靠加法得到；
   - next_url：响应直接给下一页 URL。
   若判断为 offset_limit，必须填写 offset_param、limit_param、start、size；若响应有 has_more/hasMore，填写 has_more_path。
6. 若 success=true 且 source_type=rss/atom，必须给出 list_url 且 format_locator.kind=feed_entries。
7. 若 success=true 且 source_type=html，必须给出 html_selectors 四项和 sample_items。
8. 若 success=true 且 source_type=embedded_json，必须给出 list_url、format_locator.kind=embedded_json、
   format_locator.value（如 embedded_json:window._ROUTER_DATA:loaderData.xxx.article_list）和字段映射。

输出 schema：
{
  "source_type": "json_api | rss | atom | html | embedded_json | unknown",
  "list_url": "string or null",
  "fetch": {"method": "GET | POST", "transport": "httpx | scrapling", "impersonate": null, "stealthy_headers": true, "headers": {}, "query": {}, "json_body": null},
  "format_locator": {"kind": "json_path | feed_entries | html_selector | embedded_json | unknown", "value": "string"},
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

# These keys remain in the prompt-management HTTP contract so saved prompt
# sets from older deployments still round-trip.  The connector loops never
# execute them; keeping inert templates here avoids importing the retired
# Explorer/Validator/DSL Writer/Auditor implementation.
RETIRED_EXPLORER_PROMPT = "旧网站 Explorer 已停用；普通网站由 Single Agent Loop 探查。"
RETIRED_VALIDATOR_PROMPT = "旧 Validator 已停用。site={site_url} exploration={exploration}"
RETIRED_DSL_WRITER_PROMPT = (
    "旧 DSL Writer 已停用。site={site_url} rule={url_rule} "
    "exploration={exploration} retry={retry_feedback}"
)
RETIRED_AUDITOR_PROMPT = (
    "旧 DSL Auditor 已停用。site={site_url} recipe={recipe_summary} errors={errors} "
    "count={discovered_count} n={n} sample={items_sample}"
)


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
        key="quality_audit",
        label="信息源质量审计",
        description="根据方法实跑样本宽松评估信息源质量分，影响爬取方式库里的质量评级展示。",
        required_tokens=["{source_kind}", "{input_type}", "{items_json}"],
    ),
    StageDef(
        key="wechat_prefetch",
        label="微信正文补抓判断",
        description="只根据微信卡片标题、摘要和 URL 判断是否值得补抓正文，控制公众号历史补抓成本。",
        required_tokens=["{title}", "{summary}", "{url}"],
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
    StageDef(
        key="relevance_filter",
        label="相关性预筛选",
        description="搜索来源及启用相关性过滤的信息源，在富集前判断文章是否继续处理。",
        required_tokens=["{keywords}", "{title}", "{snippet}"],
    ),
]

STAGE_MAP: dict[str, StageDef] = {s.key: s for s in STAGES}


def get_stage_defaults() -> dict[str, str]:
    """返回各发现阶段的内置默认 prompt 模板文本（前端「新建」时的预定模板）。

    功能：从各模块延迟导入默认 prompt 常量，组装成 stage_key→模板文本的字典，避免模块级循环导入。
    谁会调用：prompt 管理接口在返回/初始化默认模板时调用。
    直接调用：
    - 延迟导入各模块 prompt 常量（discovery_routes、graph、quality_audit、wechat_tools、enricher、relevance）。
    输入与结果：无参数；返回阶段 key 到默认模板文本的字典。
    副作用：无（仅导入常量）。
    """
    from app.api.discovery_routes import _NAMING_PROMPT  # noqa: F401 - 仅取常量
    from app.discovery.quality_audit import QUALITY_AUDIT_PROMPT
    from app.discovery.wechat_tools import WECHAT_PREFETCH_PROMPT
    from app.processing.enricher import _PROMPT_TEMPLATE as ENRICH_PROMPT
    from app.processing.relevance import _PROMPT as RELEVANCE_FILTER_PROMPT

    return {
        "explorer_system": RETIRED_EXPLORER_PROMPT,
        "synthesis": DEFAULT_SYNTHESIS,
        "validator": RETIRED_VALIDATOR_PROMPT,
        "dsl_writer": RETIRED_DSL_WRITER_PROMPT,
        "auditor": RETIRED_AUDITOR_PROMPT,
        "quality_audit": QUALITY_AUDIT_PROMPT,
        "wechat_prefetch": WECHAT_PREFETCH_PROMPT,
        "naming": _NAMING_PROMPT,
        "enrich": ENRICH_PROMPT,
        "relevance_filter": RELEVANCE_FILTER_PROMPT,
    }


def validate_prompts(prompts: dict[str, str]) -> list[str]:
    """校验一套 prompt 配置：返回错误信息列表（空=通过）。

    功能：遍历传入的 prompts，未知 stage key 报错；非空文本必须包含该阶段声明的所有必需占位符 token。
    谁会调用：保存 DiscoveryPromptSet 前（discovery_routes 保存接口）调用，拦截非法配置。
    直接调用：
    - STAGE_MAP（查询阶段定义与必需 token）。
    输入与结果：输入 prompts 字典；返回错误字符串列表（空表示通过）。
    副作用：无。
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
    """运行时解析某阶段的 prompt 文本（active 覆盖优先，否则用内置默认）。

    功能：查询当前 active 的 DiscoveryPromptSet，取其对该阶段的覆盖文本（非空则用），否则回退到 fallback；
    任何异常（表未建、DB 不可用）都安全回退，保证发现流程不受配置影响。
    谁会调用：render_prompt 及各阶段取 prompt 处调用。
    直接调用：
    - SQLAlchemy select(...)：查询 active 的 prompt 套。
    - SessionLocal(...)：获取数据库会话。
    - DiscoveryPromptSet：读取 prompts 字段。
    输入与结果：输入 stage_key 与 fallback 文本；返回最终 prompt 文本。
    副作用：只读数据库查询（active prompt 套）；异常时不写库。
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
    """统一渲染某阶段的 prompt：解析文本 → 注入 token → 检查必需 token 是否残留。

    功能：先 resolve 出 prompt 文本，再按 {token} 逐个字符串替换；最后若仍残留未替换的必需 token 则报错，防止漏传。
    谁会调用：explorer/synthesis/validator/dsl_writer/auditor 等各 LLM 阶段在调用模型前调用。
    直接调用：
    - resolve_prompt(...)：取阶段 prompt 文本。
    - STAGE_MAP（查必需 token）。
    - prompt.replace(...)：注入 token 值。
    输入与结果：输入 stage_key、fallback 与 tokens；返回渲染后文本，缺 token 时抛 ValueError。
    副作用：无。
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
