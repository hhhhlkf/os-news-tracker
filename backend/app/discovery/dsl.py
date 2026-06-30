"""DSL 规约：8 个原语的 Pydantic 模型 + 结构/语义校验 + 变量替换 + loop 条件求值。

DSL 是受限动作语言，只表达爬取动作序列，不能写文件/执行命令（零沙箱负担）。
Recipe = actions 顺序数组，由 DslInterpreter 解释执行。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class FetchAction(BaseModel):
    """HTTP 获取原语：静态站（JSON API/RSS/HTML）主力。"""

    op: Literal["fetch"]
    mode: Literal["json", "feed", "html"]  # 决定解析方式：json→dict, feed→feedparser, html→文本
    url: str
    method: str = "GET"
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


class Condition(BaseModel):
    """loop 终止条件：五种取值之一 + 操作符。"""

    count_of: str | None = None  # 数 context 中某 list 长度
    var: str | None = None  # 取变量值
    path: str | None = None  # 从 last_fetch 取 json path
    exists: str | None = None  # 页面 selector 存在
    not_exists: str | None = None  # 页面 selector 消失
    op: str | None = None  # >= > <= < == != (exists/not_exists 时为 None)
    value: Any | None = None


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
    | ExtractAction | LoopAction | SetAction | DedupByAction,
    Field(discriminator="op"),
]


class DslRecipe(BaseModel):
    """DSL Recipe：站点爬取方式的统一表达，存 crawl_methods.dsl_recipe。"""

    recipe_type: Literal["dsl"] = "dsl"
    entry_url: str
    actions: list[Action]


# 变量替换正则：匹配 {{var}} 或 {{obj.field}}
_VAR_RE = re.compile(r"\{\{(\w+(?:\.\w+)?)\}\}")


def render_vars(text: str, ctx: dict) -> str:
    """把 {{var}} / {{last_fetch.field}} 替换为 context 中的实际值。

    支持一层点号取字段。变量未定义时替换为空串。
    """

    def repl(m: "re.Match") -> str:
        path = m.group(1).split(".")
        val = ctx.get("vars", {})
        for p in path:
            val = val.get(p) if isinstance(val, dict) else getattr(val, p, None)
        return "" if val is None else str(val)

    return _VAR_RE.sub(repl, text)


def eval_condition(cond: dict, ctx: dict) -> bool:
    """求值 loop 的 until 终止条件。

    五种取值：count_of（数 list 长度）/var（取变量）/path（取 last_fetch 字段）
    /exists/not_exists（页面 selector，需 Playwright，此处占 False）。
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
    return {"!=": actual != expected, "==": actual == expected,
            ">=": actual >= expected, ">": actual > expected,
            "<=": actual <= expected, "<": actual < expected}.get(c.op, False)


def validate_semantics(recipe: DslRecipe) -> list[str]:
    """语义校验：跨 action 的约束，结构校验（Pydantic）管不了的部分。

    规则：from 与最近 fetch.mode 匹配；extract.fields 须能产 url；
    goto/click/wait_for 须在 goto 打开浏览器之后。
    """
    errors: list[str] = []
    last_mode: str | None = None
    has_browser = False
    for i, a in enumerate(recipe.actions):
        if isinstance(a, FetchAction):
            last_mode = a.mode
        if isinstance(a, GotoAction):
            has_browser = True
        if isinstance(a, ExtractAction):
            # 规则 2：from 与 mode 匹配
            if last_mode == "json" and a.from_.startswith("selector:"):
                errors.append(f"action {i}: extract.from selector: 与 fetch.mode=json 不匹配")
            if last_mode in ("html",) and not a.from_.startswith("selector:") and not a.from_.startswith("feed"):
                errors.append(f"action {i}: extract.from 须为 selector: 前缀（mode=html）")
            # 规则 3：至少有一个能产 url（裸 url 字段 或 template:{item.}）
            has_url = any(
                k == "url" or (isinstance(v, str) and v.startswith("template:") and "{item." in v)
                for k, v in a.fields.items()
            )
            if not has_url:
                errors.append(f"action {i}: extract.fields 须含 url 字段或 template:{{item.}}")
        if isinstance(a, (GotoAction, ClickAction, WaitForAction)) and not has_browser:
            errors.append(f"action {i}: {a.op} 须在 goto 之后")
    return errors


# 解析 LoopAction.body / on_each 里的 Action 前向引用
LoopAction.model_rebuild()
