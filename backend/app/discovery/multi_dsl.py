from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class McpCallAction(BaseModel):
    op: Literal["mcp_call"]
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}


class WechatSearchArticlesAction(BaseModel):
    op: Literal["wechat_search_articles"]
    query: str
    limit: int | None = Field(default=None, ge=1, le=100)
    max_pages: int | None = Field(default=10, ge=1, le=20)
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}


class WechatFetchAccountHistoryAction(BaseModel):
    op: Literal["wechat_fetch_account_history"]
    nickname: str | None = None
    account_id: str | None = None
    fakeid: str | None = None
    biz: str | None = Field(default=None, alias="__biz")
    limit: int = Field(default=100, ge=1, le=100)
    fetch_content: bool = False
    auth_ref: str = "wechat_mp_default"
    as_: str = Field(default="last_fetch", alias="as")
    model_config = {"populate_by_name": True}


class MultiDslRecipe(BaseModel):
    recipe_type: Literal["multi_dsl"] = "multi_dsl"
    source_kind: Literal["website", "wechat", "internal_mcp"]
    entry: str
    auth_ref: str | None = None
    requires_auth: bool = False
    actions: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)
