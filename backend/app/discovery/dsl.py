"""DSL 规约：9 个原语的 Pydantic 模型 + 结构/语义校验 + 变量替换 + loop 条件求值。

DSL 是受限动作语言，只表达爬取动作序列，不能写文件/执行命令（零沙箱负担）。
Recipe = actions 顺序数组，由 DslInterpreter 解释执行。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


class FetchAction(BaseModel):
    """HTTP 获取原语：静态站（JSON API/RSS/HTML）主力。"""

    op: Literal["fetch"]
    mode: Literal["json", "feed", "html"]  # 决定解析方式：json→dict, feed→feedparser, html→文本
    url: str
    method: str = "GET"
    transport: Literal["httpx", "scrapling"] = "httpx"
    impersonate: str | None = None
    stealthy_headers: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None  # POST 请求体（openEuler 类用）
    as_: str = Field(default="last_fetch", alias="as")  # 结果存入 context 的 key
    model_config = {"populate_by_name": True}  # 允许用 from/as 等关键字别名


class GotoAction(BaseModel):
    """Playwright 导航原语：动态站入口。"""

    op: Literal["goto"]
    url: str
    wait_until: str = "networkidle"


class WaitForAction(BaseModel):
    """等待 selector 出现，动态站同步用。"""

    op: Literal["wait_for"]
    selector: str
    timeout_ms: int = 5000


class ClickAction(BaseModel):
    """点击 selector（如 load_more 按钮）。"""

    op: Literal["click"]
    selector: str
    after_wait_ms: int = 500  # 点击后等待，给页面响应时间


class ExtractAction(BaseModel):
    """提取原语：fields 内嵌声明式字段映射，from 决定数据源。"""

    op: Literal["extract"]
    from_: str = Field(alias="from")  # json path（obj.records）或 selector: 前缀
    fields: dict[str, str | list[str]]  # 字段映射，值可为 template:/attr:/裸字段/候选数组
    into: str = "items"  # 存入 context 的 key
    merge: bool = False  # True=追加到已有 items（翻页用），False=覆盖
    model_config = {"populate_by_name": True}


class SetAction(BaseModel):
    """设置变量，配合 loop 翻页用（如 page+1）。"""

    op: Literal["set"]
    var: str
    value: Any | None = None  # 字面量
    expr: str | None = None  # 表达式（如 {{page}} + 1）


class DedupByAction(BaseModel):
    """按字段去重，通常对 url。"""

    op: Literal["dedup_by"]
    field: str


class EnrichArticlePagesAction(BaseModel):
    """补抓普通网站/RSS 候选详情页正文。"""

    op: Literal["enrich_article_pages"]
    fetch_content: bool = True
    fill_missing_only: bool = True
    max_items: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: float = Field(default=6.0, ge=1.0, le=30.0)
    content_char_limit: int = Field(default=3000, ge=200, le=10000)
    min_existing_chars: int = Field(default=120, ge=0, le=2000)


class EnrichArticleApiAction(BaseModel):
    """用详情 JSON API 补抓候选正文。"""

    op: Literal["enrich_article_api"]
    url_template: str
    fields: dict[str, str]
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    fill_missing_only: bool = True
    max_items: int | None = Field(default=None, ge=1, le=50)
    timeout_seconds: float = Field(default=6.0, ge=1.0, le=30.0)
    content_char_limit: int = Field(default=3000, ge=200, le=10000)
    min_existing_chars: int = Field(default=120, ge=0, le=2000)


class Condition(BaseModel):
    """loop 终止条件：五种取值之一 + 操作符。"""

    count_of: str | None = None  # 数 context 中某 list 长度
    var: str | None = None  # 取变量值
    path: str | None = None  # 从 last_fetch 取 json path
    exists: str | None = None  # 页面 selector 存在
    not_exists: str | None = None  # 页面 selector 消失
    op: str | None = None  # >= > <= < == != (exists/not_exists 时为 None)
    value: Any | None = None

    @model_validator(mode="after")
    def validate_target_presence(self) -> "Condition":
        """校验 Condition 至少定义了一个判定目标（五种取值之一）。

        功能：Pydantic 在构造 Condition 后自动调用，确保 count_of/var/path/exists/not_exists 至少有一个非空，否则报错。
        谁会调用：Pydantic 在构造或解析 Condition 时自动触发（间接来自 DSL 解析与解释器）。
        直接调用：无（仅读取字段并判断）。
        输入与结果：输入 self（Condition 实例）；返回 self，或抛 ValueError。
        副作用：无。
        """
        if any(v is not None for v in (self.count_of, self.var, self.path, self.exists, self.not_exists)):
            return self
        raise ValueError("condition must define one of count_of/var/path/exists/not_exists")


class LoopAction(BaseModel):
    """循环原语：until 终止条件 + max_iters 硬上限（≤20，防死循环）。"""

    op: Literal["loop"]
    until: Condition
    max_iters: int = Field(ge=1, le=20)  # 强制上限，DSL 无 while(true)
    body: list["Action"]  # 循环体
    on_each: list["Action"] = Field(default_factory=list)  # 每轮后置动作（如 page+1）


# discriminated union：Pydantic 按 op 字段自动分发到对应原语模型
Action = Annotated[
    FetchAction | GotoAction | WaitForAction | ClickAction
    | ExtractAction | LoopAction | SetAction | DedupByAction | EnrichArticlePagesAction | EnrichArticleApiAction,
    Field(discriminator="op"),
]


class DslRecipe(BaseModel):
    """DSL Recipe：站点爬取方式的统一表达，存 crawl_methods.dsl_recipe。"""

    recipe_type: Literal["dsl"] = "dsl"
    entry_url: str
    actions: list[Action]
    notes: list[str] = Field(default_factory=list)  # dsl_writer 诊断说明（执行器忽略）


# 变量替换正则：匹配 {{var}} 或 {{obj.field}}
_VAR_RE = re.compile(r"\{\{(\w+(?:\.\w+)?)\}\}")
_PATHISH_TEMPLATE_RE = re.compile(r"\{item\.(path|href)\}")


def render_vars(text: str, ctx: dict) -> str:
    """把模板里的 {{var}} / {{last_fetch.field}} 占位符替换为 context 中的实际值。

    功能：用正则匹配占位符，按路径从 ctx["vars"]（或对象属性）取值替换；变量未定义时替换为空串，支持一层点号取字段。
    谁会调用：DslInterpreter 在渲染 URL、请求头、字段模板等时调用（如 _fetch、_browser_action、_render_item_template）。
    直接调用：
    - _VAR_RE.sub(...)：正则替换占位符。
    - repl（嵌套）：按路径从 ctx 取值。
    输入与结果：输入模板文本与上下文 ctx；返回替换后的字符串。
    副作用：无。
    """

    def repl(m: "re.Match") -> str:
        """正则替换回调：按点号路径从 ctx["vars"] 取值，未定义返回空串。

        功能：取匹配到的占位符名，按 "." 拆成路径，从 ctx["vars"]（dict 或对象属性）逐层取值，
        未定义/取到 None 时返回空串，否则返回字符串。
        谁会调用：render_vars 在 _VAR_RE.sub 时调用。
        直接调用：无（仅字典/属性取值）。
        输入与结果：输入正则匹配对象；返回替换字符串。
        副作用：无。
        """
        path = m.group(1).split(".")
        val = ctx.get("vars", {})
        for p in path:
            val = val.get(p) if isinstance(val, dict) else getattr(val, p, None)
        return "" if val is None else str(val)

    return _VAR_RE.sub(repl, text)


def eval_condition(cond: dict, ctx: dict) -> bool:
    """求值 loop 的 until 终止条件（供解释器判断是否继续循环）。

    功能：把条件 dict 构造成 Condition，按 count_of（数 list 长度）/var（取变量）/path（取 last_fetch 字段）/exists/not_exists
    取出实际值，再用 op 与期望值比较；selector 类条件需 Playwright，在此处占 False。
    谁会调用：DslInterpreter._loop 在每轮循环开始时调用，判断是否满足终止条件。
    直接调用：
    - Condition(...)：构造条件对象以便读取字段。
    输入与结果：输入条件 dict 与上下文 ctx；返回布尔（True 表示条件成立，应终止循环）。
    副作用：无。
    """
    c = Condition(**cond)
    if c.count_of:
        actual = len(ctx.get(c.count_of, []))
    elif c.var:
        actual = ctx.get("vars", {}).get(c.var)
    elif c.path:
        actual = ctx.get("last_fetch", {})
        for p in c.path.split("."):
            actual = actual.get(p) if isinstance(actual, dict) else None
    elif c.exists is not None or c.not_exists is not None:
        return False  # selector 类条件在解释器里求值（需 Playwright），此处占 False
    else:
        return False
    expected = c.value
    if c.op == "!=":
        return actual != expected
    if c.op == "==":
        return actual == expected
    if actual is None or expected is None:
        return False
    if c.op == ">=":
        return actual >= expected
    if c.op == ">":
        return actual > expected
    if c.op == "<=":
        return actual <= expected
    if c.op == "<":
        return actual < expected
    return False


def validate_semantics(recipe: DslRecipe) -> list[str]:
    """跨 action 的语义约束校验（结构校验管不了的部分）。

    功能：检查 extract.from 是否与最近 fetch.mode 匹配、extract.fields 能否产出 url、浏览器动作须在 goto 之后、
    scrapling transport 仅支持 GET；递归检查 loop 的 body/on_each。
    谁会调用：website_workflow 在保存 DSL 前、website/recipe_audit.auditor 在审计时调用，做静态结构合理性审查。
    直接调用：
    - check_fetch_transport（嵌套）：递归检查每个 fetch 的 transport 与 method 约束。
    输入与结果：输入 DslRecipe；返回错误字符串列表（空列表表示通过）。
    副作用：无。
    """
    errors: list[str] = []
    last_mode: str | None = None
    has_browser = False

    def check_fetch_transport(actions: list[Action], *, path: str = "action") -> None:
        """递归检查 fetch 动作的 transport 约束：scrapling 仅支持 GET。

        功能：遍历动作列表，若 FetchAction 使用 scrapling 但 method 非 GET，追加错误；
        遇到 LoopAction 则递归检查其 body 与 on_each（带路径前缀）。
        谁会调用：validate_semantics 在顶层与递归 loop 体时调用。
        直接调用：无（仅类型判断与向 errors 列表追加）。
        输入与结果：输入动作列表与路径前缀；无返回值（错误写入外层 errors）。
        副作用：无。
        """
        for idx, action in enumerate(actions):
            action_path = f"{path} {idx}" if path == "action" else f"{path}.{idx}"
            if (
                isinstance(action, FetchAction)
                and action.transport == "scrapling"
                and action.method.upper() != "GET"
            ):
                errors.append(f"{action_path}: scrapling transport only supports GET fetch actions")
            if isinstance(action, LoopAction):
                check_fetch_transport(action.body, path=f"{action_path}.loop.body")
                check_fetch_transport(action.on_each, path=f"{action_path}.loop.on_each")

    check_fetch_transport(recipe.actions)

    for i, a in enumerate(recipe.actions):
        if isinstance(a, FetchAction):
            last_mode = a.mode
        if isinstance(a, GotoAction):
            has_browser = True
        if isinstance(a, ExtractAction):
            # 规则 2：from 与 mode 匹配
            if last_mode == "json" and a.from_.startswith("selector:"):
                errors.append(f"action {i}: extract.from selector: 与 fetch.mode=json 不匹配")
            if (
                last_mode in ("html",)
                and not a.from_.startswith("selector:")
                and not a.from_.startswith("embedded_json:")
                and not a.from_.startswith("feed")
            ):
                errors.append(f"action {i}: extract.from 须为 selector: 或 embedded_json: 前缀（mode=html）")
            # 规则 3：至少有一个能产 url（裸 url 字段 或 template:{item.}）
            has_url = any(
                k == "url" or (isinstance(v, str) and v.startswith("template:") and "{item." in v)
                for k, v in a.fields.items()
            )
            if not has_url:
                errors.append(f"action {i}: extract.fields 须含 url 字段或 template:{{item.}}")
            url_spec = a.fields.get("url")
            if isinstance(url_spec, str) and url_spec.startswith("template:"):
                tmpl = url_spec.removeprefix("template:")
                if (
                    tmpl.startswith(("http://", "https://"))
                    and _PATHISH_TEMPLATE_RE.search(tmpl)
                    and "?" not in tmpl
                    and "#" not in tmpl
                    and not re.search(r"/\{item\.(path|href)\}", tmpl)
                ):
                    errors.append(
                        f"action {i}: url template 疑似错误，相对路径字段前缺少 '/' 分隔符"
                    )
        if isinstance(a, (GotoAction, ClickAction, WaitForAction)) and not has_browser:
            errors.append(f"action {i}: {a.op} 须在 goto 之后")
    return errors


# 解析 LoopAction.body / on_each 里的 Action 前向引用
LoopAction.model_rebuild()
