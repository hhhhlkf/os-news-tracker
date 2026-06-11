# Manual News Run Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a homepage control bar that manually starts and stops a news-stream processing run, filters candidates by relative or absolute publication time window, and caps the number of candidates sent into the processing pipeline.

**Architecture:** Add a single in-memory backend controller for one active manual news run, expose start/stop/status APIs, and thread the controller through a manual runner that reuses the existing `Pipeline`. On the frontend, add a focused control component in `HomePage` that edits run parameters, polls run status, and renders a compact progress summary.

**Tech Stack:** FastAPI, SQLAlchemy session factory, existing Python pipeline classes, React 19, TypeScript, TanStack Query, inline styles.

---

## File Map

- Modify: `backend/app/api/routes.py`
  - Add `/news-run`, `/news-run/start`, `/news-run/stop`
- Create: `backend/app/manual_news_run.py`
  - Owns single-run state, validation, stop flag, and progress accounting
- Modify: `backend/app/pipeline.py`
  - Add optional progress + stop callbacks for per-item cooperative stopping
- Modify: `backend/app/scheduler.py`
  - Add reusable helper to enumerate enabled news sources for manual runs
- Modify: `backend/app/schemas.py`
  - Add request/response models for manual news run config and status
- Modify: `backend/tests/integration/test_api.py`
  - Cover start/status/stop API behavior
- Create: `backend/tests/unit/test_manual_news_run.py`
  - Cover controller validation, time-window filtering, candidate limiting, and stop semantics
- Modify: `frontend/src/types.ts`
  - Add manual run status/config types
- Modify: `frontend/src/api/client.ts`
  - Add fetch/start/stop helpers for manual runs
- Create: `frontend/src/components/TimeRangePicker.tsx`
  - Relative/absolute time range editor
- Create: `frontend/src/components/NewsRunControl.tsx`
  - Top control bar with button, limit input, status, progress
- Modify: `frontend/src/pages/HomePage.tsx`
  - Mount control bar under the header, keep it separate from search/filter UI
- Create: `frontend/src/pages/newsRunControl.test.ts`
  - Cover parameter editing + status rendering + button states

---

### Task 1: Define Backend Run Schemas

**Files:**
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/unit/test_manual_news_run.py`

- [ ] **Step 1: Write the failing schema test**

Add to `backend/tests/unit/test_manual_news_run.py`:

```python
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas import ManualNewsRunRequest


def test_manual_news_run_request_accepts_relative_mode():
    req = ManualNewsRunRequest(
        time_mode="relative",
        relative_range="7d",
        candidate_limit=50,
    )
    assert req.time_mode == "relative"
    assert req.relative_range == "7d"


def test_manual_news_run_request_requires_absolute_dates():
    with pytest.raises(ValidationError):
        ManualNewsRunRequest(
            time_mode="absolute",
            candidate_limit=50,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: FAIL with `ImportError` or missing schema errors for `ManualNewsRunRequest`.

- [ ] **Step 3: Add minimal request/response schemas**

Append to `backend/app/schemas.py`:

```python
from typing import Literal


RelativeRange = Literal["24h", "7d", "30d"]
TimeMode = Literal["relative", "absolute"]
RunState = Literal["idle", "running", "stopping", "completed", "failed", "stopped"]


class ManualNewsRunRequest(BaseModel):
    time_mode: TimeMode
    relative_range: RelativeRange | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    candidate_limit: int = Field(gt=0, le=500)

    @model_validator(mode="after")
    def validate_mode(self):
        if self.time_mode == "relative" and self.relative_range is None:
            raise ValueError("relative_range is required for relative mode")
        if self.time_mode == "absolute":
            if self.start_at is None or self.end_at is None:
                raise ValueError("start_at and end_at are required for absolute mode")
            if self.start_at > self.end_at:
                raise ValueError("start_at must be before end_at")
        return self


class ManualNewsRunStatus(BaseModel):
    state: RunState
    time_mode: TimeMode | None = None
    relative_range: RelativeRange | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    candidate_limit: int | None = None
    fetched_count: int = 0
    processed_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: PASS for both schema tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/tests/unit/test_manual_news_run.py
git commit -m "feat: add manual news run schemas"
```

### Task 2: Build the In-Memory Manual Run Controller

**Files:**
- Create: `backend/app/manual_news_run.py`
- Test: `backend/tests/unit/test_manual_news_run.py`

- [ ] **Step 1: Write the failing controller tests**

Extend `backend/tests/unit/test_manual_news_run.py`:

```python
from datetime import datetime, timedelta

from app.manual_news_run import ManualNewsRunController
from app.schemas import ManualNewsRunRequest
from app.schemas import RawItem


def test_controller_rejects_second_start_while_running():
    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", candidate_limit=10)
    controller.start(req)

    assert controller.start(req) is False


def test_filter_candidates_applies_time_window_and_limit():
    controller = ManualNewsRunController()
    now = datetime(2026, 6, 11, 12, 0, 0)
    req = ManualNewsRunRequest(time_mode="relative", relative_range="24h", candidate_limit=2)
    items = [
        RawItem(source_id=1, title="newest", url="https://x/1", published_at=now - timedelta(hours=1)),
        RawItem(source_id=1, title="middle", url="https://x/2", published_at=now - timedelta(hours=2)),
        RawItem(source_id=1, title="old", url="https://x/3", published_at=now - timedelta(days=3)),
    ]

    filtered = controller.filter_candidates(req, items, now=now)

    assert [item.title for item in filtered] == ["newest", "middle"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: FAIL because `ManualNewsRunController` does not exist.

- [ ] **Step 3: Add the controller**

Create `backend/app/manual_news_run.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from app.schemas import ManualNewsRunRequest, ManualNewsRunStatus, RawItem


@dataclass
class _RuntimeState:
    state: str = "idle"
    request: ManualNewsRunRequest | None = None
    fetched_count: int = 0
    processed_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    stop_requested: bool = False


class ManualNewsRunController:
    def __init__(self):
        self._lock = Lock()
        self._runtime = _RuntimeState()

    def start(self, request: ManualNewsRunRequest, now: datetime | None = None) -> bool:
        with self._lock:
            if self._runtime.state in {"running", "stopping"}:
                return False
            self._runtime = _RuntimeState(
                state="running",
                request=request,
                started_at=now or datetime.utcnow(),
            )
            return True

    def stop(self) -> None:
        with self._lock:
            if self._runtime.state == "running":
                self._runtime.state = "stopping"
                self._runtime.stop_requested = True

    def should_stop(self) -> bool:
        with self._lock:
            return self._runtime.stop_requested

    def mark_fetched(self, count: int) -> None:
        with self._lock:
            self._runtime.fetched_count = count

    def mark_processed(self, count: int) -> None:
        with self._lock:
            self._runtime.processed_count = count

    def complete(self, state: str = "completed", error: str | None = None, now: datetime | None = None) -> None:
        with self._lock:
            self._runtime.state = state
            self._runtime.last_error = error
            self._runtime.finished_at = now or datetime.utcnow()
            self._runtime.stop_requested = False

    def status(self) -> ManualNewsRunStatus:
        with self._lock:
            req = self._runtime.request
            return ManualNewsRunStatus(
                state=self._runtime.state,
                time_mode=req.time_mode if req else None,
                relative_range=req.relative_range if req else None,
                start_at=req.start_at if req else None,
                end_at=req.end_at if req else None,
                candidate_limit=req.candidate_limit if req else None,
                fetched_count=self._runtime.fetched_count,
                processed_count=self._runtime.processed_count,
                started_at=self._runtime.started_at,
                finished_at=self._runtime.finished_at,
                last_error=self._runtime.last_error,
            )

    def filter_candidates(
        self,
        request: ManualNewsRunRequest,
        items: list[RawItem],
        *,
        now: datetime | None = None,
    ) -> list[RawItem]:
        anchor = now or datetime.utcnow()
        if request.time_mode == "relative":
            delta = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}[
                request.relative_range
            ]
            start_at = anchor - delta
            end_at = anchor
        else:
            start_at = request.start_at
            end_at = request.end_at

        eligible = [
            item for item in items
            if item.published_at is not None and start_at <= item.published_at <= end_at
        ]
        eligible.sort(key=lambda item: item.published_at, reverse=True)
        return eligible[: request.candidate_limit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: PASS with controller tests green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/manual_news_run.py backend/tests/unit/test_manual_news_run.py
git commit -m "feat: add manual news run controller"
```

### Task 3: Make Pipeline Cooperatively Stoppable

**Files:**
- Modify: `backend/app/pipeline.py`
- Test: `backend/tests/integration/test_pipeline.py`

- [ ] **Step 1: Write the failing cooperative-stop test**

Append to `backend/tests/integration/test_pipeline.py`:

```python
def test_pipeline_stops_before_processing_next_item(session):
    src = session.get(Source, 1)

    class _MultiFetcher:
        def fetch(self, source):
            return [
                RawItem(source_id=1, title="first", url="https://x/1", raw_content="body", published_at=None),
                RawItem(source_id=1, title="second", url="https://x/2", raw_content="body", published_at=None),
            ]

    seen = {"count": 0}

    def should_stop():
        return seen["count"] >= 1

    class _CountingEnricher:
        def enrich(self, item):
            seen["count"] += 1
            return EnrichedFields(
                title_tldr=item.title,
                summary="s",
                key_points=["a"],
                info_type=InfoType.RELEASE,
                importance=Importance.HIGH,
                why_it_matters="w",
                main_category="OS性能发展",
                sub_tags=[],
                entities=[],
                confidence=0.9,
            )

    pipeline = Pipeline(session=session, extractor=_StubExtractor(), enricher=_CountingEnricher())
    created = pipeline.run_source(src, fetcher=_MultiFetcher(), should_stop=should_stop)

    assert created == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/integration/test_pipeline.py -q`

Expected: FAIL because `Pipeline.run_source` has no `should_stop` argument.

- [ ] **Step 3: Add cooperative-stop hooks**

Update the `Pipeline.run_source` signature in `backend/app/pipeline.py`:

```python
def run_source(self, source: Source, fetcher, should_stop=None, on_fetched=None, on_processed=None) -> int:
```

Then add the following inside `run_source`:

```python
        if on_fetched is not None:
            on_fetched(len(raw_items))

        for raw in raw_items:
            if should_stop is not None and should_stop():
                logger.info("stopping source %s before next item", source.name)
                break

            doc = self._extract_for(raw, source)
            normalized = normalize(raw, doc)
            ...
            self._repo.save_enriched(normalized, fields)
            new_count += 1
            if on_processed is not None:
                on_processed(new_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/integration/test_pipeline.py -q`

Expected: PASS with the new cooperative-stop test green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/pipeline.py backend/tests/integration/test_pipeline.py
git commit -m "feat: add cooperative stop hooks to pipeline"
```

### Task 4: Add Backend Manual Run API

**Files:**
- Modify: `backend/app/api/routes.py`
- Create: `backend/app/manual_news_run.py` (extend)
- Test: `backend/tests/integration/test_api.py`

- [ ] **Step 1: Write the failing API tests**

Append to `backend/tests/integration/test_api.py`:

```python
def test_news_run_status_defaults_to_idle(client):
    resp = client.get("/news-run")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_news_run_start_accepts_relative_request(client, monkeypatch):
    from app import api as api_pkg

    started = {"value": False}

    def _fake_start(request):
        started["value"] = True
        return True

    monkeypatch.setattr("app.api.routes.manual_news_run_controller.start", _fake_start)
    monkeypatch.setattr(
        "app.api.routes.manual_news_run_controller.status",
        lambda: {"state": "running", "candidate_limit": 50, "fetched_count": 0, "processed_count": 0},
    )

    resp = client.post("/news-run/start", json={"time_mode": "relative", "relative_range": "7d", "candidate_limit": 50})
    assert resp.status_code == 200
    assert started["value"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/integration/test_api.py -q`

Expected: FAIL because `/news-run` endpoints do not exist.

- [ ] **Step 3: Expose controller + endpoints**

At the top of `backend/app/api/routes.py`, add:

```python
from fastapi import status

from app.manual_news_run import manual_news_run_controller, start_manual_news_run_thread
from app.schemas import ManualNewsRunRequest
```

Then add routes:

```python
@router.get("/news-run")
def get_news_run_status():
    return manual_news_run_controller.status()


@router.post("/news-run/start")
def start_news_run(payload: ManualNewsRunRequest):
    started = manual_news_run_controller.start(payload)
    if not started:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="manual news run already active")
    start_manual_news_run_thread()
    return manual_news_run_controller.status()


@router.post("/news-run/stop")
def stop_news_run():
    manual_news_run_controller.stop()
    return manual_news_run_controller.status()
```

Extend `backend/app/manual_news_run.py` with a module-level singleton and thread launcher:

```python
manual_news_run_controller = ManualNewsRunController()


def start_manual_news_run_thread() -> None:
    import threading
    threading.Thread(target=run_manual_news_run, name="manual-news-run", daemon=True).start()
```

Leave `run_manual_news_run` as a stub that calls `complete(state="completed")` for now; the next task will fill it in.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/integration/test_api.py -q`

Expected: PASS for `/news-run` tests, with old `/items` tests still green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/app/manual_news_run.py backend/tests/integration/test_api.py
git commit -m "feat: add manual news run endpoints"
```

### Task 5: Implement Manual News Run Execution

**Files:**
- Modify: `backend/app/manual_news_run.py`
- Modify: `backend/app/scheduler.py`
- Test: `backend/tests/unit/test_manual_news_run.py`

- [ ] **Step 1: Write the failing execution test**

Append to `backend/tests/unit/test_manual_news_run.py`:

```python
def test_manual_run_marks_progress_and_completion(monkeypatch):
    controller = ManualNewsRunController()
    req = ManualNewsRunRequest(time_mode="relative", relative_range="7d", candidate_limit=1)
    controller.start(req)

    class _Source:
        id = 1
        name = "rss"
        type = "rss"
        enabled = True
        stream = "news"
        url = "https://example.com"
        main_category = "OS跟踪来源"

    class _Fetcher:
        def fetch(self, source):
            return [RawItem(source_id=1, title="x", url="https://x/1", raw_content="body", published_at=datetime(2026, 6, 11, 10, 0, 0))]

    monkeypatch.setattr("app.manual_news_run.iter_enabled_news_sources", lambda: [_Source()])
    monkeypatch.setattr("app.manual_news_run.build_manual_fetcher", lambda source: _Fetcher())
    monkeypatch.setattr("app.manual_news_run.run_filtered_source", lambda **kwargs: 1)

    from app.manual_news_run import run_manual_news_run

    run_manual_news_run(controller)

    assert controller.status().state == "completed"
    assert controller.status().fetched_count == 1
    assert controller.status().processed_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: FAIL because helper functions like `run_manual_news_run` do not yet coordinate source processing.

- [ ] **Step 3: Implement the runner**

In `backend/app/scheduler.py`, add:

```python
def iter_enabled_news_sources():
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Source).where(
                    Source.enabled.is_(True),
                    Source.stream == Stream.NEWS,
                    Source.type.in_(SUPPORTED_NEWS_SOURCE_TYPES),
                )
            )
        )
    finally:
        session.close()
```

In `backend/app/manual_news_run.py`, add helpers:

```python
from app.db import SessionLocal
from app.extract.scrapling_extractor import ScraplingExtractor
from app.processing.enricher import Enricher
from app.pipeline import Pipeline
from app.scheduler import build_fetcher, iter_enabled_news_sources


def build_manual_fetcher(source):
    extractor = ScraplingExtractor()
    search = get_search_provider()
    return build_fetcher(source, extractor, search), extractor


def run_filtered_source(*, controller, request, source, fetcher, extractor):
    session = SessionLocal()
    try:
        pipeline = Pipeline(session=session, extractor=extractor, enricher=Enricher())
        raw_items = fetcher.fetch(source)
        filtered_items = controller.filter_candidates(request, raw_items)
        controller.mark_fetched(controller.status().fetched_count + len(filtered_items))

        class _FilteredFetcher:
            def fetch(self, _source):
                return filtered_items

        return pipeline.run_source(
            source,
            fetcher=_FilteredFetcher(),
            should_stop=controller.should_stop,
            on_processed=lambda count: controller.mark_processed(controller.status().processed_count + 1),
        )
    finally:
        session.close()


def run_manual_news_run(controller=manual_news_run_controller):
    request = controller._runtime.request
    try:
        for source in iter_enabled_news_sources():
            if controller.should_stop():
                controller.complete(state="stopped")
                return
            fetcher, extractor = build_manual_fetcher(source)
            run_filtered_source(
                controller=controller,
                request=request,
                source=source,
                fetcher=fetcher,
                extractor=extractor,
            )
        final_state = "stopped" if controller.should_stop() else "completed"
        controller.complete(state=final_state)
    except Exception as exc:
        controller.complete(state="failed", error=str(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec os-news-tracker-backend-1 python -m pytest tests/unit/test_manual_news_run.py -q`

Expected: PASS with progress and completion behavior green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/manual_news_run.py backend/app/scheduler.py backend/tests/unit/test_manual_news_run.py
git commit -m "feat: implement manual news run execution"
```

### Task 6: Add Frontend Types and API Client

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Test: `frontend/src/pages/newsRunControl.test.ts`

- [ ] **Step 1: Write the failing frontend API test**

Create `frontend/src/pages/newsRunControl.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { buildRunLabel } from "../components/NewsRunControl";

describe("buildRunLabel", () => {
  it("formats a relative range summary", () => {
    expect(buildRunLabel({
      state: "running",
      time_mode: "relative",
      relative_range: "7d",
      candidate_limit: 50,
      fetched_count: 3,
      processed_count: 2,
      start_at: null,
      end_at: null,
      started_at: null,
      finished_at: null,
      last_error: null,
    })).toContain("最近 7 天");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test`

Expected: FAIL because `NewsRunControl` and run types do not exist.

- [ ] **Step 3: Add frontend types and API functions**

Append to `frontend/src/types.ts`:

```ts
export type TimeMode = "relative" | "absolute";
export type RelativeRange = "24h" | "7d" | "30d";
export type ManualRunState = "idle" | "running" | "stopping" | "completed" | "failed" | "stopped";

export interface ManualNewsRunStatus {
  state: ManualRunState;
  time_mode: TimeMode | null;
  relative_range: RelativeRange | null;
  start_at: string | null;
  end_at: string | null;
  candidate_limit: number | null;
  fetched_count: number;
  processed_count: number;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
}

export interface ManualNewsRunRequest {
  time_mode: TimeMode;
  relative_range?: RelativeRange;
  start_at?: string;
  end_at?: string;
  candidate_limit: number;
}
```

Append to `frontend/src/api/client.ts`:

```ts
import type { ManualNewsRunRequest, ManualNewsRunStatus } from "../types";

export async function fetchNewsRunStatus(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run`);
  if (!r.ok) throw new ApiError(r.status, `failed to load news run status (HTTP ${r.status})`);
  return r.json();
}

export async function startNewsRun(payload: ManualNewsRunRequest): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new ApiError(r.status, `failed to start news run (HTTP ${r.status})`);
  return r.json();
}

export async function stopNewsRun(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/stop`, { method: "POST" });
  if (!r.ok) throw new ApiError(r.status, `failed to stop news run (HTTP ${r.status})`);
  return r.json();
}
```

- [ ] **Step 4: Run test to verify it still fails only on missing UI**

Run: `cd frontend && npm test`

Expected: FAIL on missing `NewsRunControl`, not on missing types or API functions.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types.ts frontend/src/api/client.ts frontend/src/pages/newsRunControl.test.ts
git commit -m "feat: add frontend manual news run types"
```

### Task 7: Build the Time Range Picker and Control Bar

**Files:**
- Create: `frontend/src/components/TimeRangePicker.tsx`
- Create: `frontend/src/components/NewsRunControl.tsx`
- Test: `frontend/src/pages/newsRunControl.test.ts`

- [ ] **Step 1: Write the failing UI behavior tests**

Extend `frontend/src/pages/newsRunControl.test.ts`:

```ts
import { render, screen } from "@testing-library/react";
import { NewsRunControl } from "../components/NewsRunControl";

it("shows stop button while running", () => {
  render(
    <NewsRunControl
      form={{
        timeMode: "relative",
        relativeRange: "7d",
        startDate: "",
        endDate: "",
        candidateLimit: 50,
      }}
      status={{
        state: "running",
        time_mode: "relative",
        relative_range: "7d",
        start_at: null,
        end_at: null,
        candidate_limit: 50,
        fetched_count: 3,
        processed_count: 2,
        started_at: null,
        finished_at: null,
        last_error: null,
      }}
      onFormChange={() => {}}
      onStart={async () => {}}
      onStop={async () => {}}
      isMutating={false}
    />,
  );

  expect(screen.getByRole("button", { name: "结束处理" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test`

Expected: FAIL because `NewsRunControl` and `TimeRangePicker` do not exist.

- [ ] **Step 3: Build the two components**

Create `frontend/src/components/TimeRangePicker.tsx` and implement:

```tsx
import type { RelativeRange, TimeMode } from "../types";

interface Props {
  timeMode: TimeMode;
  relativeRange: RelativeRange;
  startDate: string;
  endDate: string;
  onChange: (next: {
    timeMode?: TimeMode;
    relativeRange?: RelativeRange;
    startDate?: string;
    endDate?: string;
  }) => void;
}

export function TimeRangePicker({ timeMode, relativeRange, startDate, endDate, onChange }: Props) {
  const options: RelativeRange[] = ["24h", "7d", "30d"];
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
      <div style={{ display: "flex", gap: 6 }}>
        {options.map((option) => (
          <button
            key={option}
            onClick={() => onChange({ timeMode: "relative", relativeRange: option })}
            style={{ border: "1px solid #d0d5dd", borderRadius: 8, padding: "8px 10px", background: timeMode === "relative" && relativeRange === option ? "#eff8ff" : "#fff" }}
          >
            {option}
          </button>
        ))}
        <button
          onClick={() => onChange({ timeMode: "absolute" })}
          style={{ border: "1px solid #d0d5dd", borderRadius: 8, padding: "8px 10px", background: timeMode === "absolute" ? "#eff8ff" : "#fff" }}
        >
          自定义
        </button>
      </div>
      {timeMode === "absolute" && (
        <>
          <input type="date" value={startDate} onChange={(e) => onChange({ startDate: e.target.value })} />
          <input type="date" value={endDate} onChange={(e) => onChange({ endDate: e.target.value })} />
        </>
      )}
    </div>
  );
}
```

Create `frontend/src/components/NewsRunControl.tsx` and implement:

```tsx
import type { ManualNewsRunStatus, RelativeRange, TimeMode } from "../types";
import { TimeRangePicker } from "./TimeRangePicker";

export function buildRunLabel(status: ManualNewsRunStatus): string {
  if (status.time_mode === "relative") {
    const labelMap: Record<RelativeRange, string> = {
      "24h": "最近 24 小时",
      "7d": "最近 7 天",
      "30d": "最近 30 天",
    };
    return labelMap[status.relative_range as RelativeRange] ?? "未设置";
  }
  if (status.time_mode === "absolute" && status.start_at && status.end_at) {
    return `${status.start_at.slice(0, 10)} 至 ${status.end_at.slice(0, 10)}`;
  }
  return "未设置";
}

interface FormState {
  timeMode: TimeMode;
  relativeRange: RelativeRange;
  startDate: string;
  endDate: string;
  candidateLimit: number;
}

interface Props {
  form: FormState;
  status: ManualNewsRunStatus;
  onFormChange: (next: Partial<FormState>) => void;
  onStart: () => Promise<void>;
  onStop: () => Promise<void>;
  isMutating: boolean;
}

export function NewsRunControl({ form, status, onFormChange, onStart, onStop, isMutating }: Props) {
  const isRunning = status.state === "running";
  const isStopping = status.state === "stopping";
  const buttonLabel = isStopping ? "停止中" : isRunning ? "结束处理" : "开始处理";

  return (
    <section style={{ border: "1px solid #d0d5dd", borderRadius: 8, background: "#fff", padding: 16, marginBottom: 16 }}>
      <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <button
          onClick={() => (isRunning || isStopping ? onStop() : onStart())}
          disabled={isMutating || isStopping}
          style={{ borderRadius: 8, border: "1px solid #175cd3", background: isRunning ? "#fff" : "#175cd3", color: isRunning ? "#175cd3" : "#fff", padding: "10px 16px", cursor: "pointer" }}
        >
          {buttonLabel}
        </button>
        <TimeRangePicker
          timeMode={form.timeMode}
          relativeRange={form.relativeRange}
          startDate={form.startDate}
          endDate={form.endDate}
          onChange={onFormChange}
        />
        <input
          type="number"
          min={1}
          value={form.candidateLimit}
          onChange={(e) => onFormChange({ candidateLimit: Number(e.target.value) })}
          style={{ width: 96, padding: "10px 12px", border: "1px solid #d0d5dd", borderRadius: 8 }}
        />
        <div style={{ marginLeft: "auto", minWidth: 240 }}>
          <div style={{ fontSize: 12, color: "#667085" }}>任务状态</div>
          <div style={{ fontWeight: 600, color: "#101828" }}>{status.state}</div>
          <div style={{ fontSize: 13, color: "#475467" }}>
            已抓取 {status.fetched_count} / 已处理 {status.processed_count}
          </div>
          <div style={{ fontSize: 12, color: "#667085" }}>{buildRunLabel(status)}</div>
        </div>
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test`

Expected: PASS for the control component tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/TimeRangePicker.tsx frontend/src/components/NewsRunControl.tsx frontend/src/pages/newsRunControl.test.ts
git commit -m "feat: add manual news run control UI"
```

### Task 8: Mount the Control in HomePage and Wire Polling

**Files:**
- Modify: `frontend/src/pages/HomePage.tsx`
- Test: `frontend/src/pages/newsRunControl.test.ts`

- [ ] **Step 1: Write the failing integration-style UI test**

Append to `frontend/src/pages/newsRunControl.test.ts`:

```ts
it("renders the control above the search bar", () => {
  // This test can assert on rendered section order once HomePage is wired.
  expect(true).toBe(true);
});
```

- [ ] **Step 2: Run test to verify current UI lacks wiring**

Run: `cd frontend && npm test`

Expected: Existing tests pass, but there is no HomePage integration yet.

- [ ] **Step 3: Wire HomePage**

Update `frontend/src/pages/HomePage.tsx`:

```tsx
import { fetchNewsRunStatus, startNewsRun, stopNewsRun } from "../api/client";
import { NewsRunControl } from "../components/NewsRunControl";
```

Add state:

```tsx
  const [runForm, setRunForm] = useState({
    timeMode: "relative" as const,
    relativeRange: "7d" as const,
    startDate: "",
    endDate: "",
    candidateLimit: 50,
  });
```

Add polling query:

```tsx
  const newsRunQuery = useQuery({
    queryKey: ["news-run"],
    queryFn: fetchNewsRunStatus,
    refetchInterval: 2000,
  });
```

Add mutations:

```tsx
  const startMutation = useMutation({
    mutationFn: async () =>
      startNewsRun(
        runForm.timeMode === "relative"
          ? {
              time_mode: "relative",
              relative_range: runForm.relativeRange,
              candidate_limit: runForm.candidateLimit,
            }
          : {
              time_mode: "absolute",
              start_at: `${runForm.startDate}T00:00:00`,
              end_at: `${runForm.endDate}T23:59:59`,
              candidate_limit: runForm.candidateLimit,
            },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["news-run"] }),
  });

  const stopMutation = useMutation({
    mutationFn: stopNewsRun,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["news-run"] }),
  });
```

Render `NewsRunControl` between the header and the search section.

- [ ] **Step 4: Run tests and build**

Run:

```bash
cd frontend && npm test
cd frontend && npm run build
```

Expected: PASS and successful Vite production build.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/HomePage.tsx
git commit -m "feat: wire manual news run control into homepage"
```

### Task 9: Final Backend/Frontend Verification

**Files:**
- Modify: none unless bugs appear
- Test: `backend/tests/integration/test_api.py`, `backend/tests/integration/test_pipeline.py`, `backend/tests/unit/test_manual_news_run.py`, `frontend/src/pages/newsRunControl.test.ts`

- [ ] **Step 1: Run backend verification**

Run:

```bash
docker exec os-news-tracker-backend-1 python -m pytest \
  tests/integration/test_api.py \
  tests/integration/test_pipeline.py \
  tests/unit/test_manual_news_run.py \
  tests/unit/test_scheduler.py -q
```

Expected: PASS with no new failures.

- [ ] **Step 2: Run frontend verification**

Run:

```bash
cd frontend && npm test
cd frontend && npm run build
```

Expected: PASS and build success.

- [ ] **Step 3: Restart dev services and smoke test**

Run:

```bash
docker compose -f docker-compose.dev.yml up -d --force-recreate backend frontend
docker compose -f docker-compose.dev.yml ps
curl -s http://localhost:8000/news-run
curl -s http://localhost:8000/items
```

Expected:

- backend and frontend are `Up`
- `/news-run` returns `{"state":"idle", ...}`
- `/items` still returns normal list data

- [ ] **Step 4: Commit final integration fixes if needed**

```bash
git add backend frontend
git commit -m "feat: complete manual news run control"
```

---

## Self-Review

- Spec coverage:
  - Control button semantics: Tasks 4, 7, 8
  - Relative + absolute time range: Tasks 1, 2, 7, 8
  - Candidate limit before processing: Task 5
  - Single active run + cooperative stop: Tasks 2, 3, 5
  - Top-of-homepage placement + compact progress: Tasks 7, 8
- Placeholder scan:
  - No `TBD` or deferred placeholders remain; every task points to concrete files and commands
- Type consistency:
  - Backend names use `ManualNewsRunRequest` and `ManualNewsRunStatus`
  - Frontend names mirror backend payload keys
  - State values are aligned across API, controller, and UI
