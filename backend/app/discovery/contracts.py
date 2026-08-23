"""智能探查各阶段共用的数据契约。

功能：集中定义网页探查、多来源路由和 DSL 审计在模块之间传递的状态与结构化数据，
避免执行图之间通过彼此的实现文件引用类型。
谁会调用：``graph``、``multi_graph`` 及其调用方通过这些类型构造、校验或标注探查数据。
直接调用：无；本模块只声明数据结构，不执行探查、网络请求或数据库操作。
输入与结果：无（模块级，仅声明类型）。
副作用：无。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


BranchKind = Literal["website", "wechat", "internal_mcp", "unsupported"]


class DiscoveryState(TypedDict, total=False):
    """智能探查主图的可选状态字段。

    功能：承载网页探查各节点在同一次运行中交接的输入、中间结果与最终结果。
    谁会调用：``graph`` 的节点和 ``multi_graph`` 的网站分支读写此状态。
    直接调用：无（数据契约，由调用方构造并读写字段）。
    输入与结果：无（TypedDict 类型，字段见类定义）。
    副作用：无。
    """

    site_url: str
    homepage: dict
    network_captures: list
    exploration: dict
    url_rule: dict | None
    dsl_recipe: dict | None
    audit_result: dict | None
    attempt: int
    dsl_cycle_attempt: int
    verdict: str | None
    method_id: int | None
    token_used: int
    force: bool
    error: str | None
    name: str | None
    explorer_agent_output: str | None
    explorer_synthesis_output: str | None
    explorer_parse_error: str | None
    explorer_trace_logs: list[dict] | None
    api_reviews: list[dict] | None
    validator_llm_output: str | None
    dsl_writer_llm_output: str | None
    auditor_llm_output: str | None
    audit_input: dict | None
    retry_feedback: dict | None
    dsl_sanitize_warnings: list[str] | None
    run_id: int | None
    log_source: str | None


class ExplorationFetch(BaseModel):
    """探查阶段识别出的列表请求方式。

    功能：描述请求列表数据时要使用的 HTTP 方法、传输方式和请求参数。
    谁会调用：``graph.explorer`` 产出，``graph.dsl_writer`` 读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    method: str = "GET"
    transport: str = "httpx"
    impersonate: str | None = None
    stealthy_headers: bool = True
    headers: dict = Field(default_factory=dict)
    query: dict = Field(default_factory=dict)
    json_body: dict | None = None


class ExplorationFormatLocator(BaseModel):
    """探查结果中新闻列表所在位置的定位信息。

    功能：记录列表数据是 JSON、HTML 等何种形式，以及该列表的定位值。
    谁会调用：``graph.explorer`` 产出，``graph.dsl_writer`` 读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    kind: str = "unknown"
    value: str = ""


class ExplorationFields(BaseModel):
    """探查识别出的新闻字段映射。

    功能：把来源中的标识、标题、链接、时间和正文等字段名统一记录下来。
    谁会调用：``graph.explorer`` 产出，验证与 DSL 编写节点读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    id: str | None = None
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    content: str | None = None


class ExplorationHtmlSelectors(BaseModel):
    """HTML 列表页的 CSS 选择器集合。

    功能：保存 HTML 来源提取新闻卡片及其链接、标题、日期所需的选择器。
    谁会调用：``graph.explorer`` 产出，``graph.dsl_writer`` 读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    item_selector: str | None = None
    link_selector: str | None = None
    title_selector: str | None = None
    date_selector: str | None = None


class ExplorationSampleItem(BaseModel):
    """探查阶段保留的一条新闻样本。

    功能：保存原始新闻数据和已解析的关键字段，供 URL 验证与 DSL 编写核对。
    谁会调用：``graph.explorer`` 产出，验证与 DSL 编写节点读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    raw: dict | str | None = None
    id: str | None = None
    title: str | None = None
    raw_url: str | None = None
    url: str | None = None
    published_at: str | None = None


class ExplorationUrlCandidate(BaseModel):
    """探查阶段提出但尚未最终确认的详情页链接规则。

    功能：记录候选链接模式、相关字段和初步验证结论，交给验证节点最终裁定。
    谁会调用：``graph.explorer`` 产出，``graph.validator`` 读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    mode: str = "unknown"
    url_field: str | None = None
    id_field: str | None = None
    template: str | None = None
    verification: str | None = None


class ExplorationPagination(BaseModel):
    """新闻列表的翻页规则。

    功能：描述页码、偏移量或游标翻页所需的参数和下一页判断字段。
    谁会调用：``graph.explorer`` 产出，``graph.dsl_writer`` 读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    type: str = "unknown"
    page_param: str | None = None
    size_param: str | None = None
    offset_param: str | None = None
    limit_param: str | None = None
    cursor_param: str | None = None
    next_path: str | None = None
    has_more_path: str | None = None
    start: int = 1
    size: int | None = None
    notes: str = ""


class ExplorationEvidence(BaseModel):
    """探查结论的工具证据摘要。

    功能：记录哪个工具观察到了什么事实，供后续节点和日志解释结论来源。
    谁会调用：``graph.explorer`` 产出，验证与审计节点读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    tool: str
    summary: str


class ExplorationResult(BaseModel):
    """网页探查阶段的完整结构化结果。

    功能：组合请求方式、列表定位、字段、样本、链接候选、翻页和证据。
    谁会调用：``graph.explorer`` 创建，``graph.validator`` 与 ``graph.dsl_writer`` 使用。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    source_type: str = "unknown"
    list_url: str | None = None
    fetch: ExplorationFetch = Field(default_factory=ExplorationFetch)
    format_locator: ExplorationFormatLocator = Field(default_factory=ExplorationFormatLocator)
    fields: ExplorationFields = Field(default_factory=ExplorationFields)
    html_selectors: ExplorationHtmlSelectors = Field(default_factory=ExplorationHtmlSelectors)
    sample_items: list[ExplorationSampleItem] = Field(default_factory=list)
    url_candidates: list[ExplorationUrlCandidate] = Field(default_factory=list)
    pagination: ExplorationPagination = Field(default_factory=ExplorationPagination)
    evidence: list[ExplorationEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    success: bool = False


class UrlRule(BaseModel):
    """验证节点确认后的详情页链接规则。

    功能：定义详情链接的生成模式、字段、样本验证结果及正文获取策略。
    谁会调用：``graph.validator`` 创建，``graph.dsl_writer`` 与审计辅助逻辑读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    mode: str = "unknown"
    template: str | None = None
    base_url: str | None = None
    path_field: str | None = None
    id_field: str | None = None
    url_field: str | None = None
    sample_items: list[dict] = Field(default_factory=list)
    validation_samples: list[dict] = Field(default_factory=list)
    content_strategy: str | None = None
    content_verified: bool = False
    content_chars: int | None = None
    detail_api: dict | None = None
    confidence: str = "low"
    reason: str = ""


class AuditVerdict(BaseModel):
    """DSL 实跑结果的结构化质量判定。

    功能：记录配方是否可保存、内容真实性、翻页能力、拦截情况及修复建议。
    谁会调用：``graph.auditor`` 创建，``graph.supervisor_route`` 与 DSL 重写流程读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    passed: bool
    is_real_content: bool
    has_pagination: bool
    not_blocked: bool
    value_assessment: str = ""
    issues: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None


class SourceRoute(BaseModel):
    """多来源探查选择分支后的路由结果。

    功能：标记输入属于网站、微信公众号、内部 MCP 或不支持类型，并解释原因。
    谁会调用：``multi_routes.route_input`` 创建，``multi_graph`` 的各分支读取。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（Pydantic 数据模型，字段见类定义）。
    副作用：无。
    """

    kind: BranchKind
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_input: str
    input_type: str
    markers: list[str] = Field(default_factory=list)
    reason: str
    suggested_branch: BranchKind


class MultiDiscoveryState(DiscoveryState, total=False):
    """多来源探查在主图状态上附加的路由和分支结果。

    功能：保存原始输入、路由判断、分支执行痕迹及多 DSL 的配方和审计结果。
    谁会调用：``multi_graph`` 的路由节点和来源分支读写。
    直接调用：无（数据契约，由调用方构造并读取字段）。
    输入与结果：无（TypedDict 类型，字段见类定义）。
    副作用：无。
    """

    raw_input: str
    normalized_input: str
    source_route: dict[str, Any]
    branch_kind: str
    branch_artifact: dict[str, Any]
    branch_trace_logs: list[dict[str, Any]]
    multi_dsl_recipe: dict[str, Any] | None
    multi_audit_result: dict[str, Any] | None
