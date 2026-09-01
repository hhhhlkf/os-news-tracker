"""DEPRECATED / MIGRATION-ONLY Playwright helpers for the old DSL stack.

Active exploration runs through the fixed gVisor Explore session runtime in
``loop.explore_session``.  Keep this module only while stored DSL methods can be
executed for migration or rollback; do not call it from new connector paths.
"""

from __future__ import annotations

from app.discovery.cancel import ensure_not_cancelled


LOAD_MORE_TEXTS = (
    "加载更多",
    "更多",
    "下一页",
    "查看更多",
    "Load more",
    "More",
    "Next",
    "Show more",
)


def exercise_dynamic_page(page, *, scroll_rounds: int = 6, wait_ms: int = 700) -> None:
    """触发动态页面的无限滚动、懒加载列表与「加载更多」式分页。

    功能：先等待，再多次向下滚动以触发懒加载；随后尝试点击常见「加载更多/下一页」文案按钮，让更多内容渲染到页面上。
    谁会调用：tools.py 的抓取类工具（fetch_page 等）与 interpreter.py 的浏览器/抓取动作在需要渲染动态列表时调用。
    直接调用：
    - ensure_not_cancelled(...)：每轮滚动/点击前检查运行是否被取消。
    - Playwright page API（mouse.wheel / get_by_text / click）：执行滚动与点击。
    输入与结果：输入 Playwright page 对象与滚动轮数、等待毫秒；无返回值。
    副作用：对传入的 page 对象进行真实滚动与点击操作（网络/页面副作用），并可能因取消而中断。
    """
    try:
        page.wait_for_timeout(wait_ms)
    except Exception:
        pass
    for _ in range(max(scroll_rounds, 0)):
        ensure_not_cancelled()
        try:
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(wait_ms)
        except Exception:
            break
    for text in LOAD_MORE_TEXTS:
        ensure_not_cancelled()
        try:
            locator = page.get_by_text(text, exact=False)
            count = min(locator.count(), 2)
            for idx in range(count):
                try:
                    locator.nth(idx).click(timeout=1500)
                    page.wait_for_timeout(wait_ms)
                except Exception:
                    continue
        except Exception:
            continue
