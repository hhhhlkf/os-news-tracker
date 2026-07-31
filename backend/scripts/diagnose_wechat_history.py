"""只读诊断公众号历史抓取。

从项目已保存的微信登录态读取凭据，但绝不输出 cookie 或 token；不写数据库。

运行（Docker 开发环境）：
    docker compose exec backend python -m scripts.diagnose_wechat_history "Hacking Time"

运行（本机 Python 环境）：
    cd backend && .venv/bin/python -m scripts.diagnose_wechat_history "Hacking Time"

可选的 --include-probe 会额外调用一次当前登录态探测接口。该接口有时会
返回 ret=200002，且会多消耗一次请求额度，因此默认关闭。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.discovery.wechat_tools import (
    wechat_fetch_account_history,
    wechat_resolve_account,
)
from app.wechat_auth import probe_mp_session, profile_public_status, resolve_profile


def _print_result(label: str, payload: dict[str, Any]) -> None:
    """Print only diagnostic fields; credentials and full account lists stay private."""
    safe = {
        key: value
        for key, value in payload.items()
        if key not in {"cookie", "token", "accounts", "items"}
    }
    items = payload.get("items")
    if isinstance(items, list):
        safe["item_count"] = len(items)
        safe["sample_titles"] = [
            str(item.get("title") or "")[:100]
            for item in items[:3]
            if isinstance(item, dict)
        ]
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        safe["account_match_count"] = len(accounts)
    print(f"\n[{label}]")
    print(json.dumps(safe, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断微信公众平台账号解析与历史文章抓取")
    parser.add_argument("account", help="公众号名称或微信号，例如 Hacking Time")
    parser.add_argument("--profile", default="wechat_mp_default", help="微信登录态配置名")
    parser.add_argument("--limit", type=int, default=1, help="历史文章最多取几条（默认 1）")
    parser.add_argument(
        "--include-probe",
        action="store_true",
        help="额外执行登录态探测；会多发一次请求，默认关闭",
    )
    args = parser.parse_args()
    limit = max(1, min(args.limit, 10))

    print(f"诊断公众号：{args.account!r}；profile：{args.profile!r}；history limit：{limit}")
    _print_result("保存的登录态元数据", profile_public_status(args.profile))

    auth = resolve_profile(args.profile)
    _print_result("凭据可用性", auth)
    if auth.get("status") != "ok":
        print("\n结论：本机没有可用登录态；请先在本机环境完成扫码登录。")
        return 2

    if args.include_probe:
        result, reason = probe_mp_session(cookie=str(auth["cookie"]), token=str(auth["token"]))
        _print_result("可选登录态探测", {"result": result, "reason": reason})

    resolved = wechat_resolve_account(args.account, auth_ref=args.profile)
    _print_result("公众号解析（searchbiz）", resolved)
    if resolved.get("status") != "ok":
        print("\n结论：账号解析未成功；请以该段的 status/reason 为准。")
        return 3

    history = wechat_fetch_account_history(
        nickname=args.account,
        account_id=None,
        fakeid=str(resolved.get("fakeid") or "") or None,
        biz=str(resolved.get("__biz") or "") or None,
        limit=limit,
        fetch_content=False,
        auth_ref=args.profile,
    )
    _print_result("历史文章（appmsg）", history)

    status = history.get("status")
    if status == "ok":
        print("\n结论：账号解析和历史文章接口均正常。")
        return 0
    if status == "empty":
        print("\n结论：接口正常返回空列表；这与限流不同。")
        return 4
    if status == "rate_limited":
        print("\n结论：账号解析成功，但历史文章接口触发频率限制。")
        return 5
    print("\n结论：历史文章接口未成功；请保留以上输出用于定位。")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
