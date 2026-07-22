"""Shared Playwright page actions for discovery and DSL execution."""

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
    """Trigger infinite scroll, lazy-loaded lists, and load-more style pagination."""
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
