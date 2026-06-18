from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import ManualNewsRunRequest, ManualNewsRunStatus, RawItem


# ── Request schema ──────────────────────────────────────────────────


def test_manual_news_run_request_accepts_relative_mode():
    req = ManualNewsRunRequest(
        time_mode="relative",
        relative_range="7d",
        target_count=50,
    )
    assert req.time_mode == "relative"
    assert req.relative_range == "7d"
    assert req.target_count == 50


def test_manual_news_run_request_requires_absolute_dates():
    with pytest.raises(ValidationError):
        ManualNewsRunRequest(
            time_mode="absolute",
            target_count=50,
        )


def test_manual_news_run_request_rejects_inverted_absolute_window():
    with pytest.raises(ValidationError):
        ManualNewsRunRequest(
            time_mode="absolute",
            start_at=datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 6, 11, 11, 0, 0, tzinfo=timezone.utc),
            target_count=10,
        )


def test_manual_news_run_request_rejects_naive_absolute_dates():
    with pytest.raises(ValidationError):
        ManualNewsRunRequest(
            time_mode="absolute",
            start_at=datetime(2026, 6, 11, 0, 0, 0),
            end_at=datetime(2026, 6, 11, 23, 59, 59),
            target_count=10,
        )


# ── Controller lifecycle ────────────────────────────────────────────


def test_controller_starts_in_collecting_state():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    assert controller.start(req) is True
    assert controller.status().state == "collecting"


def test_controller_rejects_second_start_while_collecting():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    assert controller.start(req) is True
    assert controller.start(req) is False


def test_controller_rejects_start_while_processing():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    controller.start(req)
    controller.mark_processing()

    assert controller.start(req) is False


def test_controller_transitions_collecting_to_processing():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    controller.start(req)

    controller.mark_processing()
    assert controller.status().state == "processing"


def test_controller_transitions_processing_back_to_collecting():
    """When a new collection round starts mid-run, state goes back to collecting."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    controller.start(req)
    controller.mark_processing()
    assert controller.status().state == "processing"

    controller.mark_collecting()
    assert controller.status().state == "collecting"


# ── Candidate filtering / merging ───────────────────────────────────


def test_filter_candidates_applies_time_window_only_does_not_truncate():
    """filter_candidates filters by time, but does NOT slice to target_count."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=2)
    items = [
        RawItem(source_id=1, title="newest", url="https://x/1", published_at=now - timedelta(hours=1)),
        RawItem(source_id=1, title="middle", url="https://x/2", published_at=now - timedelta(hours=2)),
        RawItem(source_id=1, title="also-recent", url="https://x/3", published_at=now - timedelta(hours=3)),
        RawItem(source_id=1, title="old", url="https://x/4", published_at=now - timedelta(days=3)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)

    # All three in-window items should be present (not truncated by target_count=2).
    assert len(filtered) == 3
    assert [item.title for item in filtered] == ["newest", "middle", "also-recent"]


def test_limit_candidates_per_source_keeps_llm_highest_scored_five_items():
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    low_quality_new_items = [
        RawItem(
            source_id=1,
            title=f"event reminder {idx}",
            url=f"https://x/low-{idx}",
            raw_content="community event reminder and general update",
            published_at=now - timedelta(minutes=idx),
        )
        for idx in range(3)
    ]
    high_quality_old_items = [
        RawItem(
            source_id=1,
            title=f"Linux kernel scheduler performance update {idx}",
            url=f"https://x/high-{idx}",
            raw_content=(
                "Linux Kernel eBPF scheduler benchmark release ABI "
                "compatibility RPM package update"
            ),
            published_at=now - timedelta(hours=idx + 1),
        )
        for idx in range(5)
    ]
    llm_payload = {
        "scores": [
            {"url": f"https://x/low-{idx}", "score": 15, "reason": "community event", "should_keep": False}
            for idx in range(3)
        ] + [
            {"url": f"https://x/high-{idx}", "score": 90 - idx, "reason": "key kernel update", "should_keep": True}
            for idx in range(5)
        ]
    }

    class _StubLlm:
        def __init__(self):
            self.prompts: list[str] = []

        def complete(self, prompt, **kw):
            import json

            self.prompts.append(prompt)
            return json.dumps(llm_payload)

    llm = _StubLlm()
    scorer = CandidateQualityScorer(llm=llm)

    limited = controller.limit_candidates_per_source(
        [*low_quality_new_items, *high_quality_old_items],
        scorer=scorer,
    )

    assert len(limited) == 5
    assert [item.title for item in limited] == [
        "Linux kernel scheduler performance update 0",
        "Linux kernel scheduler performance update 1",
        "Linux kernel scheduler performance update 2",
        "Linux kernel scheduler performance update 3",
        "Linux kernel scheduler performance update 4",
    ]
    assert "score" in llm.prompts[0]
    assert "0-100" in llm.prompts[0]
    assert "should_keep" in llm.prompts[0]


def test_limit_candidates_per_source_excludes_llm_rejected_items():
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"candidate {idx}",
            url=f"https://x/{idx}",
            raw_content="body",
            published_at=now - timedelta(minutes=idx),
        )
        for idx in range(6)
    ]

    class _StubLlm:
        def complete(self, prompt, **kw):
            import json

            return json.dumps(
                {
                    "scores": [
                        {
                            "url": item.url,
                            "score": 100 - idx,
                            "reason": "not key technology news",
                            "should_keep": idx < 2,
                        }
                        for idx, item in enumerate(items)
                    ]
                }
            )

    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=_StubLlm()),
    )

    assert [item.url for item in limited] == ["https://x/0", "https://x/1"]


def test_filter_candidates_excludes_out_of_window():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=5)
    items = [
        RawItem(source_id=1, title="recent", url="https://x/1", published_at=now - timedelta(hours=1)),
        RawItem(source_id=1, title="old", url="https://x/2", published_at=now - timedelta(days=3)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)

    assert [item.title for item in filtered] == ["recent"]


def test_filter_candidates_excludes_missing_published_at(monkeypatch):
    monkeypatch.setenv("MISSING_DATE_POLICY", "exclude")
    # Clear the lru_cache so get_settings() re-reads the env var.
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=5)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="has-date", url="https://x/2", published_at=now - timedelta(hours=1)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)

    assert [item.title for item in filtered] == ["has-date"]


def test_merge_candidates_dedupes_by_url():
    """merge_candidates must skip incoming items whose URL is already present."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    first_batch = [
        RawItem(source_id=1, title="a1", url="https://x/a1", published_at=now - timedelta(hours=1)),
        RawItem(source_id=1, title="a2", url="https://x/a2", published_at=now - timedelta(hours=2)),
    ]
    second_batch = [
        RawItem(source_id=2, title="b1", url="https://x/b1", published_at=now - timedelta(minutes=30)),
        RawItem(source_id=2, title="dup-a1", url="https://x/a1", published_at=now - timedelta(hours=1)),
    ]

    merged = controller.merge_candidates(req, first_batch, second_batch, now=now)

    # b1 + a1 + a2 = 3; the duplicate a1 URL is skipped.
    assert len(merged) == 3
    urls = {item.url for item in merged}
    assert urls == {"https://x/a1", "https://x/a2", "https://x/b1"}


def test_merge_candidates_still_filters_by_time():
    """Time-filter still applies after dedup."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    existing = [
        RawItem(source_id=1, title="recent", url="https://x/r", published_at=now - timedelta(hours=1)),
    ]
    incoming = [
        RawItem(source_id=2, title="old", url="https://x/o", published_at=now - timedelta(days=3)),
    ]

    merged = controller.merge_candidates(req, existing, incoming, now=now)

    # Only the recent one remains.
    assert len(merged) == 1
    assert merged[0].url == "https://x/r"


# ── Counter updates ─────────────────────────────────────────────────


def test_discovered_and_queued_counters_are_independent():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    controller.start(req)

    # Simulate: discovered 15 raw items, but only 8 passed time filter + dedup.
    controller.mark_discovered(15)
    controller.mark_queued(8)

    status = controller.status()
    assert status.discovered_count == 15
    assert status.queued_count == 8


# ── Fulfilment ──────────────────────────────────────────────────────


def test_fulfilled_true_when_saved_count_meets_target():
    """fulfilled should be set to True when saved_count >= target_count."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    controller.start(req)

    # Simulate: after processing, saved 10 items (meets target).
    controller.mark_saved(10)
    controller.set_fulfilled(True)
    controller.set_gap_reason(None)

    status = controller.status()
    assert status.saved_count == 10
    assert status.target_count == 10
    assert status.fulfilled is True
    assert status.gap_reason is None


def test_fulfilled_false_when_saved_count_short_of_target():
    """fulfilled must be False when saved_count < target_count, with gap_reason."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=50)
    controller.start(req)

    # Simulate: only saved 12 items (short of 50).
    controller.mark_saved(12)
    controller.set_fulfilled(False)
    controller.set_gap_reason("入库不足：共新增入库 12 条（目标 50），缺少 38 条")

    status = controller.status()
    assert status.saved_count == 12
    assert status.target_count == 50
    assert status.fulfilled is False
    assert status.gap_reason == "入库不足：共新增入库 12 条（目标 50），缺少 38 条"


def test_fulfilled_is_independent_of_queued_count():
    """Even if many candidates were queued, fulfilled depends on saved_count."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=5)
    controller.start(req)

    # Lots of candidates, but only 2 actually saved.
    controller.mark_queued(100)
    controller.mark_processed(30)
    controller.mark_saved(2)
    controller.set_fulfilled(False)
    controller.set_gap_reason("入库不足：共新增入库 2 条（目标 5），缺少 3 条")

    status = controller.status()
    assert status.queued_count == 100
    assert status.saved_count == 2
    assert status.fulfilled is False


# ── Stop ────────────────────────────────────────────────────────────


def test_stop_marks_controller_stopping_until_complete():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)

    controller.start(req, now=datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc))
    controller.stop()

    status = controller.status()
    assert status.state == "stopping"
    assert controller.should_stop() is True

    controller.complete(state="stopped", now=datetime(2026, 6, 11, 12, 5, 0, tzinfo=timezone.utc))
    assert controller.status().state == "stopped"
    assert controller.should_stop() is False


def test_status_includes_target_count():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=80)
    controller.start(req)

    status = controller.status()
    assert status.target_count == 80


def test_absolute_mode_preserves_dates_in_status():
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 11, 23, 59, 59, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(
        time_mode="absolute",
        start_at=start,
        end_at=end,
        target_count=30,
    )
    controller.start(req)

    status = controller.status()
    assert status.time_mode == "absolute"
    assert status.start_at == start
    assert status.end_at == end
    assert status.target_count == 30


# ── Time filter stats ────────────────────────────────────────────────


def test_time_filter_stats_missing_published_at(monkeypatch):
    """Items with published_at=None are counted as 'missing_published_at' when policy is exclude."""
    monkeypatch.setenv("MISSING_DATE_POLICY", "exclude")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController, TimeFilterStats

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="no-date2", url="https://x/2", published_at=None),
        RawItem(source_id=1, title="has-date", url="https://x/3", published_at=now - timedelta(hours=1)),
    ]

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.missing_published_at == 2
    assert stats.matched == 1
    assert stats.before_start == 0
    assert stats.after_end == 0


def test_time_filter_stats_before_start():
    """Items published before the window start are counted as 'before_start'."""
    from app.manual_news_run import ManualNewsRunController, TimeFilterStats

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    items = [
        RawItem(source_id=1, title="old", url="https://x/1", published_at=now - timedelta(days=10)),
        RawItem(source_id=1, title="recent", url="https://x/2", published_at=now - timedelta(hours=1)),
    ]

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.before_start == 1
    assert stats.matched == 1
    assert stats.missing_published_at == 0
    assert stats.after_end == 0


def test_time_filter_stats_after_end_absolute():
    """In absolute mode, items after end_at are counted as 'after_end'."""
    from app.manual_news_run import ManualNewsRunController, TimeFilterStats

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="absolute", start_at=start, end_at=end, target_count=10)
    items = [
        RawItem(source_id=1, title="in", url="https://x/1", published_at=datetime(2026, 6, 5, 0, 0, 0, tzinfo=timezone.utc)),
        RawItem(source_id=1, title="after", url="https://x/2", published_at=datetime(2026, 6, 11, 0, 0, 0, tzinfo=timezone.utc)),
    ]

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.after_end == 1
    assert stats.matched == 1


def test_time_filter_stats_all_categories(monkeypatch):
    """A single call exercises all stat categories (with exclude policy)."""
    monkeypatch.setenv("MISSING_DATE_POLICY", "exclude")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController, TimeFilterStats

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="absolute", start_at=start, end_at=end, target_count=10)
    items = [
        RawItem(source_id=1, title="missing", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="before", url="https://x/2", published_at=datetime(2026, 5, 20, 0, 0, 0, tzinfo=timezone.utc)),
        RawItem(source_id=1, title="after", url="https://x/3", published_at=datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)),
        RawItem(source_id=1, title="match1", url="https://x/4", published_at=datetime(2026, 6, 5, 0, 0, 0, tzinfo=timezone.utc)),
        RawItem(source_id=1, title="match2", url="https://x/5", published_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)),
    ]

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.missing_published_at == 1
    assert stats.before_start == 1
    assert stats.after_end == 1
    assert stats.matched == 2


# ── Relative vs absolute consistency ─────────────────────────────────


def test_relative_7d_and_equivalent_absolute_produce_same_candidates():
    """Relative 7d and the corresponding absolute UTC window must filter
    identically when fed the same items and now is fixed."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

    # Relative: last 7 days → items from 2026-06-04T12:00:00Z onwards
    rel_req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)

    # Absolute: same window in UTC terms
    abs_start = now - timedelta(days=7)  # 2026-06-04T12:00:00Z
    abs_end = now  # 2026-06-11T12:00:00Z
    abs_req = ManualNewsRunRequest(
        time_mode="absolute", start_at=abs_start, end_at=abs_end, target_count=10,
    )

    items = [
        RawItem(source_id=1, title="too-old", url="https://x/1", published_at=now - timedelta(days=10)),
        RawItem(source_id=1, title="edge-old", url="https://x/2", published_at=now - timedelta(days=7, hours=1)),
        RawItem(source_id=1, title="in-window", url="https://x/3", published_at=now - timedelta(days=3)),
        RawItem(source_id=1, title="in-window-2", url="https://x/4", published_at=now - timedelta(hours=1)),
        RawItem(source_id=1, title="at-boundary", url="https://x/5", published_at=now - timedelta(days=7)),
    ]

    rel_filtered = controller.filter_candidates(rel_req, items, now=now)
    abs_filtered = controller.filter_candidates(abs_req, items, now=now)

    rel_urls = {item.url for item in rel_filtered}
    abs_urls = {item.url for item in abs_filtered}

    assert rel_urls == abs_urls, (
        f"Relative matched: {rel_urls}, absolute matched: {abs_urls}"
    )


def test_different_timezone_inputs_produce_same_filter_result():
    """The same moment expressed in different timezones must produce
    identical filtering results."""
    from datetime import timezone as tz_mod
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

    # Same instant: 2026-06-04T00:00:00Z == 2026-06-04T08:00:00+08:00
    start_utc = datetime(2026, 6, 4, 0, 0, 0, tzinfo=timezone.utc)
    start_cst = datetime(2026, 6, 4, 8, 0, 0, tzinfo=tz_mod(timedelta(hours=8)))
    end_utc = datetime(2026, 6, 11, 23, 59, 59, tzinfo=timezone.utc)
    end_cst = datetime(2026, 6, 12, 7, 59, 59, tzinfo=tz_mod(timedelta(hours=8)))

    req_utc = ManualNewsRunRequest(
        time_mode="absolute", start_at=start_utc, end_at=end_utc, target_count=10,
    )
    req_cst = ManualNewsRunRequest(
        time_mode="absolute", start_at=start_cst, end_at=end_cst, target_count=10,
    )

    items = [
        RawItem(source_id=1, title="in", url="https://x/1", published_at=datetime(2026, 6, 5, 0, 0, 0, tzinfo=timezone.utc)),
        RawItem(source_id=1, title="out", url="https://x/2", published_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)),
    ]

    utc_filtered = controller.filter_candidates(req_utc, items, now=now)
    cst_filtered = controller.filter_candidates(req_cst, items, now=now)

    assert {item.url for item in utc_filtered} == {item.url for item in cst_filtered}


def test_published_at_none_not_counted_as_matched(monkeypatch):
    """Items with published_at=None must not be included when policy is exclude."""
    monkeypatch.setenv("MISSING_DATE_POLICY", "exclude")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="in-window", url="https://x/2", published_at=now - timedelta(hours=1)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)
    assert len(filtered) == 1
    assert filtered[0].url == "https://x/2"


# ── Status includes time filter stats ────────────────────────────────


def test_status_includes_time_filter_stats():
    """status() must expose time_filter_stats when available."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    controller.start(req)

    controller.set_time_filter_stats(missing_pub=3, before=5, after=0, matched=12)

    status = controller.status()
    assert status.time_filter_stats is not None
    assert status.time_filter_stats.missing_published_at == 3
    assert status.time_filter_stats.before_start == 5
    assert status.time_filter_stats.after_end == 0
    assert status.time_filter_stats.matched == 12


def test_status_includes_included_without_date_count():
    """status() must expose included_without_date when available."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    controller.start(req)

    controller.set_time_filter_stats(
        missing_pub=0,
        before=1,
        after=2,
        matched=3,
        included_without_date=4,
    )

    status = controller.status()
    assert status.time_filter_stats is not None
    assert status.time_filter_stats.included_without_date == 4


# ── MissingDatePolicy include_as_now ────────────────────────────────


def test_include_as_now_adds_items_to_matched(monkeypatch):
    """With MISSING_DATE_POLICY=include_as_now, None-date items enter matched."""
    # Restore the default (tests above may have set it to "exclude").
    monkeypatch.delenv("MISSING_DATE_POLICY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="in-window", url="https://x/2", published_at=now - timedelta(hours=1)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)
    assert len(filtered) == 2
    assert {item.title for item in filtered} == {"no-date", "in-window"}


def test_include_as_now_stats_tracks_included_without_date(monkeypatch):
    """With include_as_now, included_without_date tracks the count."""
    monkeypatch.delenv("MISSING_DATE_POLICY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController, TimeFilterStats

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="no-date2", url="https://x/2", published_at=None),
        RawItem(source_id=1, title="has-date", url="https://x/3", published_at=now - timedelta(hours=1)),
    ]

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.included_without_date == 2
    assert stats.missing_published_at == 0
    assert stats.matched == 3


def test_include_as_now_does_not_affect_absolute_ranges(monkeypatch):
    """Absolute date windows must exclude None-date items even with include_as_now."""
    monkeypatch.delenv("MISSING_DATE_POLICY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    start = datetime(2026, 4, 14, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="absolute", start_at=start, end_at=end, target_count=10)
    items = [
        RawItem(source_id=1, title="no-date", url="https://x/1", published_at=None),
        RawItem(source_id=1, title="in-window", url="https://x/2", published_at=datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)),
    ]

    filtered, stats = controller.filter_candidates_with_stats(req, items, now=datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
    assert [item.url for item in filtered] == ["https://x/2"]
    assert stats.matched == 1
    assert stats.included_without_date == 0
    assert stats.missing_published_at == 1


def test_include_as_now_empty_list(monkeypatch):
    """Empty candidate list with include_as_now produces zero stats."""
    monkeypatch.delenv("MISSING_DATE_POLICY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", target_count=10)
    items: list[RawItem] = []

    _filtered, stats = controller.filter_candidates_with_stats(req, items, now=now)
    assert stats.included_without_date == 0
    assert stats.matched == 0
    assert stats.missing_published_at == 0


# ── Status observability fields ───────────────────────────────────────


def test_status_includes_current_source_and_stage():
    """ManualNewsRunStatus must expose current_source_name and current_stage."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    controller.start(req)

    controller.set_current_source("Phoronix", "fetching")
    status = controller.status()

    assert status.current_source_name == "Phoronix"
    assert status.current_stage == "fetching"


def test_current_source_and_stage_are_none_by_default():
    """Before any source is started, the observability fields are None."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    controller.start(req)

    status = controller.status()
    assert status.current_source_name is None
    assert status.current_stage is None


def test_current_source_clears_when_done():
    """After setting, current_source can be cleared back to None."""
    from app.manual_news_run import ManualNewsRunController

    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", target_count=10)
    controller.start(req)

    controller.set_current_source("Example", "time_filtering")
    assert controller.status().current_source_name == "Example"

    controller.set_current_source(None, None)
    assert controller.status().current_source_name is None
    assert controller.status().current_stage is None


# ── limit_candidates_per_source: skip LLM when ≤5 ─────────────────────


def test_limit_candidates_per_source_skips_llm_when_five_or_fewer():
    """When ≤5 candidates, LLM is not called — items are returned as-is."""
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"item {idx}",
            url=f"https://x/{idx}",
            raw_content="body",
            published_at=now - timedelta(hours=idx),
        )
        for idx in range(3)
    ]

    llm_called = []

    class _SpyLlm:
        def complete(self, prompt, **kw):
            llm_called.append(True)
            return "{}"

    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=_SpyLlm()),
    )

    assert len(llm_called) == 0, "LLM should not be called for ≤5 candidates"
    assert len(limited) == 3
    assert {item.url for item in limited} == {item.url for item in items}


def test_limit_candidates_per_source_calls_llm_when_more_than_five():
    """When >5 candidates, LLM is called to score and rank."""
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"item {idx}",
            url=f"https://x/{idx}",
            raw_content="body",
            published_at=now - timedelta(hours=idx),
        )
        for idx in range(7)
    ]

    class _StubLlm:
        def __init__(self):
            self.prompts: list[str] = []

        def complete(self, prompt, **kw):
            import json

            self.prompts.append(prompt)
            return json.dumps({
                "scores": [
                    {"url": item.url, "score": 70 + idx, "reason": "ok", "should_keep": True}
                    for idx, item in enumerate(items)
                ]
            })

    llm = _StubLlm()
    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=llm),
    )

    assert len(llm.prompts) == 1, "LLM should be called for >5 candidates"
    assert len(limited) == 5, "should keep at most 5"


def test_limit_candidates_per_source_empty_list_returns_empty():
    """Empty input produces empty output without LLM call."""
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    llm_called = []

    class _SpyLlm:
        def complete(self, prompt, **kw):
            llm_called.append(True)
            return "{}"

    limited = controller.limit_candidates_per_source(
        [],
        scorer=CandidateQualityScorer(llm=_SpyLlm()),
    )

    assert limited == []
    assert len(llm_called) == 0


def test_limit_candidates_per_source_five_exactly_skips_llm():
    """Exactly 5 candidates — the cap — still skips LLM."""
    from app.manual_news_run import CandidateQualityScorer, ManualNewsRunController

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"item {idx}",
            url=f"https://x/{idx}",
            raw_content="body",
            published_at=now - timedelta(hours=idx),
        )
        for idx in range(5)
    ]

    llm_called = []

    class _SpyLlm:
        def complete(self, prompt, **kw):
            llm_called.append(True)
            return "{}"

    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=_SpyLlm()),
    )

    assert len(llm_called) == 0, "LLM should not be called for exactly 5 candidates"
    assert len(limited) == 5


def test_limit_candidates_per_source_prefilters_before_llm():
    """Large-but-manageable sources send only a prefiltered subset to LLM."""
    from app.manual_news_run import (
        CANDIDATE_LLM_PREFILTER_LIMIT,
        CandidateQualityScorer,
        ManualNewsRunController,
    )

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"Linux kernel scheduler update {idx}",
            url=f"https://x/{idx}",
            raw_content="kernel scheduler performance benchmark RPM compatibility",
            published_at=now - timedelta(minutes=idx),
        )
        for idx in range(CANDIDATE_LLM_PREFILTER_LIMIT + 15)
    ]

    class _SpyLlm:
        def __init__(self):
            self.prompt = ""

        def complete(self, prompt, **kw):
            import json

            self.prompt = prompt
            return json.dumps({
                "scores": [
                    {"url": item.url, "score": 80, "reason": "ok", "should_keep": True}
                    for item in items[:CANDIDATE_LLM_PREFILTER_LIMIT]
                ]
            })

    llm = _SpyLlm()
    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=llm),
    )

    assert len(limited) == 5
    assert llm.prompt.count('"title"') == CANDIDATE_LLM_PREFILTER_LIMIT


def test_limit_candidates_per_source_high_volume_uses_local_fast_path():
    """Very noisy/high-volume sources should not block queueing on LLM."""
    from app.manual_news_run import (
        CANDIDATE_LOCAL_ONLY_THRESHOLD,
        CandidateQualityScorer,
        ManualNewsRunController,
    )

    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    items = [
        RawItem(
            source_id=1,
            title=f"Fedora COPR package build update {idx}",
            url=f"https://x/{idx}",
            raw_content="package build update",
            published_at=now - timedelta(minutes=idx),
        )
        for idx in range(CANDIDATE_LOCAL_ONLY_THRESHOLD + 1)
    ]
    llm_called = []

    class _SpyLlm:
        def complete(self, prompt, **kw):
            llm_called.append(True)
            return "{}"

    limited = controller.limit_candidates_per_source(
        items,
        scorer=CandidateQualityScorer(llm=_SpyLlm()),
    )

    assert len(limited) == 5
    assert len(llm_called) == 0


def test_candidate_quality_prompt_uses_short_snippets():
    from app.manual_news_run import CANDIDATE_SCORE_SNIPPET_LIMIT, CandidateQualityScorer

    item = RawItem(
        source_id=1,
        title="Linux kernel performance update",
        url="https://x/kernel",
        raw_content="x" * (CANDIDATE_SCORE_SNIPPET_LIMIT + 100),
        published_at=datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )

    payload = CandidateQualityScorer(llm=None)._candidate_payload(item)

    assert len(payload["snippet"]) == CANDIDATE_SCORE_SNIPPET_LIMIT


# ── _fetch_source_worker ──────────────────────────────────────────────


def test_fetch_source_worker_uses_own_session():
    """_fetch_source_worker creates and closes its own DB session."""
    from unittest.mock import MagicMock, patch

    from app.manual_news_run import _fetch_source_worker

    mock_source = MagicMock()
    mock_source.id = 1
    mock_source.name = "TestSource"
    mock_source.type = MagicMock()
    mock_source.type.value = "rss"
    mock_source.url = "https://example.com/feed"
    mock_source.stealth = False
    mock_source.enabled = True

    mock_session = MagicMock()
    mock_session.get.return_value = mock_source

    mock_session_factory = MagicMock(return_value=mock_session)

    with patch("app.db.SessionLocal", mock_session_factory), \
         patch("app.extract.scrapling_extractor.ScraplingExtractor"), \
         patch("app.search.base.get_search_provider"), \
         patch("app.scheduler.build_fetcher") as mock_build:
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = []
        mock_build.return_value = mock_fetcher

        result = _fetch_source_worker({
            "source_id": 1,
            "source_name": "TestSource",
            "source_type": "rss",
            "url": "https://example.com/feed",
        })

    # Worker must create its own session.
    mock_session_factory.assert_called_once()
    # Worker must close its session.
    mock_session.close.assert_called_once()
    # Worker must not raise.
    assert result["source_name"] == "TestSource"
    assert result["error"] is None


def test_fetch_source_worker_handles_fetch_failure():
    """A failed fetch returns error info without crashing the worker."""
    from unittest.mock import MagicMock, patch
    from app.manual_news_run import _fetch_source_worker

    mock_source = MagicMock()
    mock_source.id = 1
    mock_source.name = "BrokenSource"
    mock_source.stealth = False
    mock_source.enabled = True

    mock_session = MagicMock()
    mock_session.get.return_value = mock_source
    mock_session_factory = MagicMock(return_value=mock_session)

    with patch("app.db.SessionLocal", mock_session_factory), \
         patch("app.extract.scrapling_extractor.ScraplingExtractor"), \
         patch("app.search.base.get_search_provider"), \
         patch("app.scheduler.build_fetcher") as mock_build:
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.side_effect = RuntimeError("network down")
        mock_build.return_value = mock_fetcher

        result = _fetch_source_worker({
            "source_id": 1,
            "source_name": "BrokenSource",
            "source_type": "rss",
            "url": "https://example.com/feed",
        })

    assert result["error"] is not None
    assert "network down" in result["error"]
    assert result["candidates"] == []
    # Session must still be closed.
    mock_session.close.assert_called_once()
    # On error, session rolls back.
    mock_session.rollback.assert_called_once()


def test_fetch_source_worker_handles_disabled_source():
    """Worker gracefully handles source that was disabled between discovery and fetch."""
    from unittest.mock import MagicMock, patch
    from app.manual_news_run import _fetch_source_worker

    mock_session = MagicMock()
    mock_session.get.return_value = None  # source not found
    mock_session_factory = MagicMock(return_value=mock_session)

    with patch("app.db.SessionLocal", mock_session_factory):
        result = _fetch_source_worker({
            "source_id": 99,
            "source_name": "GhostSource",
            "source_type": "rss",
            "url": "https://example.com/feed",
        })

    assert result["error"] is not None
    assert result["candidates"] == []
    mock_session.close.assert_called_once()
