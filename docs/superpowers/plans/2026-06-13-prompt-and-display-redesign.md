# LLM Prompt & Display Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite LLM enrichment and relevance prompts for maintainer-focused content, restructure output fields (tech highlights, Chinese title, flat keywords), and update frontend display including a new "技术热点" facet in the sidebar.

**Architecture:** Zero database migration — DB column names stay the same, only the semantic content changes. LLM prompt changes drive the new field values; the `Enricher.enrich()` method maps LLM output keys to existing DB columns. Frontend display updates read the same API fields but render them with new labels and layout.

**Tech Stack:** Python (FastAPI, SQLAlchemy, Pydantic), React 18 (TypeScript, TanStack Query), pytest

**Spec:** `docs/superpowers/specs/2026-06-13-prompt-and-display-redesign.md`

**Execution:** Subagent-Driven Development — dispatch a fresh subagent per task, review between tasks.

---

## File Change Map

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/app/schemas.py` | Modify | Update `EnrichedFields` — rename fields, remove `why_it_matters`/`entities` |
| `backend/app/processing/enricher.py` | Modify | Rewrite prompt template, add field mapping in `enrich()` |
| `backend/app/processing/relevance.py` | Modify | Rewrite relevance prompt |
| `backend/app/repository.py` | Modify | Update `save_enriched()` — merge keywords into sub_tags, stop writing entities |
| `backend/app/api/routes.py` | Modify | Add `sub_tags` facet to `/facets`; add `sub_tag` filter param to `/items` |
| `frontend/src/types.ts` | Modify | Add `sub_tags` to `Facets` interface |
| `frontend/src/components/ItemDetail.tsx` | Modify | Title zh+en, 技术要点 with keyword bold, remove 影响/意义, 技术热点 from sub_tags |
| `frontend/src/components/FacetSidebar.tsx` | Modify | Add "技术热点" facet group (top 30, with counts, clickable) |
| `frontend/src/pages/HomePage.tsx` | Modify | Pass `sub_tags` default to FacetSidebar |
| `frontend/src/pages/homeData.ts` | Modify | Add `sub_tags` to `buildDemoFacets`, support `sub_tag` filter |
| `backend/tests/unit/test_enricher.py` | Modify | Update mock LLM responses and assertions |
| `backend/tests/integration/test_pipeline.py` | Modify | Update `EnrichedFields` construction in stubs |

---

### Task 1: Update `EnrichedFields` Schema

**Files:**
- Modify: `backend/app/schemas.py:23-39`

- [ ] **Step 1: Update the `EnrichedFields` class**

Replace the current `EnrichedFields` in `backend/app/schemas.py` (lines 23–39):

```python
class EntityRef(BaseModel):
    type: EntityType
    name: str
    role: str | None = None


class EnrichedFields(BaseModel):
    title_tldr: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    info_type: InfoType
    importance: Importance
    why_it_matters: str
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    confidence: float = 0.0
```

With:

```python
class EnrichedFields(BaseModel):
    title_zh: str
    summary: str
    tech_highlights: list[str] = Field(default_factory=list)
    info_type: InfoType
    importance: Importance
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.0
```

Remove the `EntityRef` class (lines 23–26) and remove the `EntityType` import from the top of the file.

The import line at the top changes from:

```python
from app.enums import InfoType, Importance, EntityType
```

To:

```python
from app.enums import InfoType, Importance
```

- [ ] **Step 2: Verify no other imports of `EntityRef`**

Run: `cd backend && grep -r "EntityRef" app/ tests/`

Any files that import `EntityRef` will need updating in later tasks (expected: `test_pipeline.py`).

- [ ] **Step 3: Run existing tests to confirm expected failures**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_enricher.py tests/integration/test_pipeline.py -v 2>&1 | head -40`

Expected: failures because the old `EnrichedFields` field names no longer exist.

---

### Task 2: Rewrite Enricher Prompt & Field Mapping

**Files:**
- Modify: `backend/app/processing/enricher.py`
- Test: `backend/tests/unit/test_enricher.py`

- [ ] **Step 1: Write the failing test**

Replace the entire content of `backend/tests/unit/test_enricher.py` with:

```python
import json

from app.processing.enricher import Enricher
from app.schemas import NormalizedItem


class _StubLlm:
    def __init__(self, payload):
        self._payload = payload

    def complete(self, prompt, **kw):
        return self._payload


def _valid_payload():
    return json.dumps(
        {
            "title_zh": "Linux 6.9 正式发布",
            "summary": "内核 6.9 发布，带来调度器改进。",
            "tech_highlights": [
                "[调度器] sched_ext 框架正式合入主线",
                "[能效] 改善大小核调度策略",
            ],
            "info_type": "发布",
            "importance": "高",
            "main_category": "OS性能发展",
            "sub_tags": ["kernel", "scheduler"],
            "keywords": ["Linux 6.9", "sched_ext", "EAS"],
            "confidence": 0.92,
        },
        ensure_ascii=False,
    )


def test_enricher_parses_new_schema():
    n = NormalizedItem(
        source_id=1,
        title="Linux 6.9",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    enricher = Enricher(llm=_StubLlm(_valid_payload()))
    fields = enricher.enrich(n)
    assert fields.title_zh == "Linux 6.9 正式发布"
    assert fields.info_type == "发布"
    assert fields.main_category == "OS性能发展"
    assert len(fields.tech_highlights) == 2
    assert "[调度器]" in fields.tech_highlights[0]
    assert fields.keywords == ["Linux 6.9", "sched_ext", "EAS"]


def test_enricher_handles_markdown_fenced_json():
    n = NormalizedItem(
        source_id=1,
        title="t",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    fenced = "```json\n" + _valid_payload() + "\n```"
    fields = Enricher(llm=_StubLlm(fenced)).enrich(n)
    assert fields.confidence == 0.92


def test_enricher_prompt_contains_role_and_exclusions():
    """Verify the prompt includes the new role positioning and exclusion rules."""
    prompts_sent: list[str] = []

    class _CaptureLlm:
        def complete(self, prompt, **kw):
            prompts_sent.append(prompt)
            return _valid_payload()

    n = NormalizedItem(
        source_id=1,
        title="t",
        url="https://x/a",
        canonical_url="https://x/a",
        clean_content="body",
    )
    Enricher(llm=_CaptureLlm()).enrich(n)
    prompt = prompts_sent[0]
    assert "技术情报分析师" in prompt
    assert "社区活动通知" in prompt
    assert "title_zh" in prompt
    assert "tech_highlights" in prompt
    assert "keywords" in prompt
    # Old fields should NOT be in prompt
    assert "why_it_matters" not in prompt
    assert "entities" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_enricher.py -v`

Expected: FAIL (old prompt template still in place).

- [ ] **Step 3: Rewrite the enricher**

Replace the entire content of `backend/app/processing/enricher.py` with:

```python
import json
import re

from app.enums import MAIN_CATEGORIES
from app.llm.client import LlmClient
from app.schemas import EnrichedFields, NormalizedItem

_PROMPT_TEMPLATE = """你是操作系统维护工程师的技术情报分析师。阅读下面的技术文章，为 OS maintainer 提取结构化情报。

只收录有专业价值的技术内容：版本发布、安全公告、新技术/工具发布、AI agent/LLM 工具链进展、性能基准测试、技术架构分析。
不收录：社区活动通知、招聘信息、用户入门教程、市场营销材料、非技术性公告。

输出严格 JSON（不要多余文字）。

可选主分类（必须选一个最贴切的）：{categories}

字段要求：
- title_zh: 中文翻译标题（准确翻译原标题，不是概括），20字以内
- summary: 2-4句核心摘要
- tech_highlights: 3-5条技术要点（字符串数组），每条格式为「[关键词] 具体说明」，如「[内核版本] Linux 6.12 引入了 sched_ext 调度器框架」
- info_type: 从 [发布, 更新, 性能数据, 适配, 观点/分析, 其他] 选一个
- importance: 从 [高, 中, 低] 选一个。高=版本发布/安全公告/重大新工具；中=技术更新/AI工具链；低=性能测试/架构分析
- main_category: 从可选主分类里选一个
- sub_tags: 细粒度子标签（字符串数组，如厂商名/产品名/技术名）
- keywords: 扁平关键词数组，列出文中的关键技术术语（如 ["Linux 6.12", "RHEL 10", "systemd 256", "eBPF"]）
- confidence: 0~1 的浮点，表示你对归类与摘要的把握

标题：{title}
正文：
{content}

只输出 JSON：
"""


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"No JSON found in LLM output: {text[:200]}")


class Enricher:
    def __init__(self, llm: LlmClient | None = None):
        self._llm = llm or LlmClient()

    def enrich(self, item: NormalizedItem) -> EnrichedFields:
        prompt = _PROMPT_TEMPLATE.format(
            categories=", ".join(MAIN_CATEGORIES),
            title=item.title,
            content=item.clean_content[:6000],
        )
        raw = self._llm.complete(prompt)
        data = _extract_json(raw)
        if data.get("main_category") not in MAIN_CATEGORIES:
            data["main_category"] = MAIN_CATEGORIES[-1]
        return EnrichedFields(**data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/test_enricher.py -v`

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd backend
git add app/schemas.py app/processing/enricher.py tests/unit/test_enricher.py
git commit -m "feat: rewrite enricher prompt for maintainer-focused content

- New role: 技术情报分析师 with explicit inclusion/exclusion rules
- Fields: title_zh, tech_highlights (with [keyword] prefix), keywords
- Removed: why_it_matters, entities (EntityRef)
- Zero DB migration: field mapping in enrich() reuses existing columns"
```

---

### Task 3: Rewrite Relevance Prompt

**Files:**
- Modify: `backend/app/processing/relevance.py`

- [ ] **Step 1: Rewrite the relevance prompt**

Replace the entire content of `backend/app/processing/relevance.py` with:

```python
from typing import Protocol


class _Completer(Protocol):
    def complete(self, prompt: str) -> str: ...


_PROMPT = (
    "你是操作系统维护团队的情报筛选员。判断以下内容是否值得 OS maintainer 关注。\n"
    "收录标准：版本发布、安全公告、软件包更新、新技术/工具发布、性能数据、AI agent/LLM 工具链进展。\n"
    "排除标准：纯社区活动通知、招聘、用户入门教程、市场营销、非技术公告。\n"
    "关键词：{keywords}\n"
    "标题：{title}\n"
    "正文片段：{snippet}\n"
    "只回答 true 或 false。"
)


def llm_relevance(title: str, content: str, keywords: str, client: _Completer | None = None) -> bool:
    if client is None:
        from app.llm.client import LlmClient

        client = LlmClient()
    prompt = _PROMPT.format(keywords=keywords, title=title or "", snippet=content[:800])
    answer = client.complete(prompt).strip().lower()
    return answer.startswith("true")
```

- [ ] **Step 2: Run existing relevance tests**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/ -k relevance -v`

Expected: PASS (the function signature and return type are unchanged).

- [ ] **Step 3: Commit**

```bash
cd backend
git add app/processing/relevance.py
git commit -m "feat: rewrite relevance prompt with maintainer-focused filtering criteria"
```

---

### Task 4: Update Repository — Merge Keywords into Sub-tags, Stop Writing Entities

**Files:**
- Modify: `backend/app/repository.py:49-77`

- [ ] **Step 1: Update `save_enriched()` method**

In `backend/app/repository.py`, replace the `save_enriched` method (lines 49–77) with:

```python
    def save_enriched(self, item: NormalizedItem, fields: EnrichedFields) -> Item:
        all_tags = list(dict.fromkeys(fields.sub_tags + fields.keywords))
        db_item = Item(
            source_id=item.source_id,
            title=item.title,
            url=item.canonical_url,
            url_hash=url_hash(item.canonical_url),
            content_hash=content_hash(item.clean_content),
            clean_content=item.clean_content,
            published_at=item.published_at,
            main_category=fields.main_category,
            title_tldr=fields.title_zh,
            summary=fields.summary,
            key_points=fields.tech_highlights,
            info_type=fields.info_type,
            importance=fields.importance,
            why_it_matters=None,
            status=ItemStatus.ENRICHED,
            llm_confidence=fields.confidence,
        )
        for tag_name in all_tags:
            db_item.tags.append(self._get_or_create_tag(tag_name, TagKind.SUB_TAG))
        db_item.tags.append(self._get_or_create_tag(fields.main_category, TagKind.MAIN_CATEGORY))
        self._s.add(db_item)
        self._s.flush()
        self._add_source_link_if_new(db_item.id, item.source_id, item.canonical_url)
        self._s.commit()
        return db_item
```

Key changes:
- `fields.title_zh` → `title_tldr` column
- `fields.tech_highlights` → `key_points` column
- `why_it_matters=None` — no longer written
- `all_tags = list(dict.fromkeys(fields.sub_tags + fields.keywords))` — merge + dedup
- Entity writes removed entirely

- [ ] **Step 2: Remove unused Entity import if no longer needed**

Check if `Entity` is used elsewhere in `repository.py`. The `_get_or_create_entity` helper is no longer called by `save_enriched`, but keep it for potential future use. The `Entity` import stays.

- [ ] **Step 3: Run all unit tests**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/unit/ -v 2>&1 | tail -20`

Expected: Some tests may fail in `test_pipeline.py` (uses old `EnrichedFields`). That's fixed in the next task.

- [ ] **Step 4: Commit**

```bash
cd backend
git add app/repository.py
git commit -m "feat: update save_enriched to map new schema fields to DB columns

- title_zh → title_tldr column
- tech_highlights → key_points column
- keywords merged into sub_tags (deduplicated)
- Entity writes removed from enrichment path"
```

---

### Task 5: Update Integration Test Stubs

**Files:**
- Modify: `backend/tests/integration/test_pipeline.py`

- [ ] **Step 1: Update `_StubEnricher` and `_InternalOnlyCategoryEnricher`**

In `backend/tests/integration/test_pipeline.py`, replace the `_StubEnricher` class (lines 40–53) with:

```python
class _StubEnricher:
    def enrich(self, item):
        return EnrichedFields(
            title_zh="Linux 6.9 正式发布",
            summary="s",
            tech_highlights=["[内核] 调度器改进"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            main_category="OS性能发展",
            sub_tags=["kernel"],
            keywords=["Linux 6.9"],
            confidence=0.9,
        )
```

Replace `_InternalOnlyCategoryEnricher` class (lines 65–78) with:

```python
class _InternalOnlyCategoryEnricher:
    def enrich(self, item):
        return EnrichedFields(
            title_zh="Linux 6.9 正式发布",
            summary="s",
            tech_highlights=["[内核] 调度器改进"],
            info_type=InfoType.RELEASE,
            importance=Importance.HIGH,
            main_category="司内AI工具",
            sub_tags=["kernel"],
            keywords=["Linux 6.9"],
            confidence=0.9,
        )
```

Also update the import at line 8 — remove `EntityRef`:

```python
from app.schemas import EnrichedFields, ExtractedDoc, RawItem
```

- [ ] **Step 2: Run integration tests**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_pipeline.py -v`

Expected: all 3 tests PASS.

- [ ] **Step 3: Run full test suite**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -30`

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
cd backend
git add tests/integration/test_pipeline.py
git commit -m "test: update pipeline integration test stubs for new EnrichedFields schema"
```

---

### Task 6: Add `sub_tags` Facet to Backend API

**Files:**
- Modify: `backend/app/api/routes.py:91-101` (facets endpoint)
- Modify: `backend/app/api/routes.py:37-88` (list_items endpoint — add sub_tag filter)

- [ ] **Step 1: Update the `/facets` endpoint**

In `backend/app/api/routes.py`, add the `Tag` and `ItemTag` imports at line 14:

```python
from app.models import Item, ItemSource, ItemTag, Tag
```

Replace the `facets` function (lines 91–101) with:

```python
@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    def _counts(column):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return [{"value": value, "count": count} for value, count in rows if value is not None]

    sub_tag_rows = db.execute(
        select(Tag.name, func.count(func.distinct(ItemTag.item_id)))
        .join(ItemTag, Tag.id == ItemTag.tag_id)
        .where(Tag.kind == "sub_tag")
        .group_by(Tag.name)
        .order_by(func.count(func.distinct(ItemTag.item_id)).desc())
        .limit(30)
    ).all()

    return {
        "main_category": _counts(Item.main_category),
        "info_type": _counts(Item.info_type),
        "importance": _counts(Item.importance),
        "sub_tags": [{"value": name, "count": count} for name, count in sub_tag_rows],
    }
```

- [ ] **Step 2: Add `sub_tag` filter to `/items` endpoint**

In `backend/app/api/routes.py`, add a `sub_tag` parameter to the `list_items` function. After the `importance` filter (around line 57), add:

```python
    sub_tag: str | None = None,
```

And add the filter logic after the `importance` filter block (after line 57):

```python
    if sub_tag:
        stmt = stmt.where(
            Item.id.in_(
                select(ItemTag.item_id)
                .join(Tag, Tag.id == ItemTag.tag_id)
                .where(Tag.name == sub_tag, Tag.kind == "sub_tag")
            )
        )
```

- [ ] **Step 3: Run API tests**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/integration/test_api.py -v 2>&1 | tail -20`

Expected: all existing tests PASS.

- [ ] **Step 4: Commit**

```bash
cd backend
git add app/api/routes.py
git commit -m "feat: add sub_tags facet (top 30) to /facets and sub_tag filter to /items"
```

---

### Task 7: Update Frontend Types and FacetSidebar

**Files:**
- Modify: `frontend/src/types.ts:23-28`
- Modify: `frontend/src/components/FacetSidebar.tsx:66-69`
- Modify: `frontend/src/pages/HomePage.tsx:260`
- Modify: `frontend/src/pages/homeData.ts:93-98`

- [ ] **Step 1: Add `sub_tags` to `Facets` interface**

In `frontend/src/types.ts`, replace the `Facets` interface (lines 24–28) with:

```typescript
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
  sub_tags: FacetValue[];
}
```

- [ ] **Step 2: Add "技术热点" group to FacetSidebar**

In `frontend/src/components/FacetSidebar.tsx`, update the `groups` array (lines 66–70) to include sub_tags:

```typescript
  const groups: [keyof Facets, string][] = [
    ["main_category", "主分类"],
    ["info_type", "信息类型"],
    ["importance", "重要度"],
    ["sub_tags", "技术热点"],
  ];
```

- [ ] **Step 3: Update HomePage default facets**

In `frontend/src/pages/HomePage.tsx`, update line 260 where the default facets are passed:

```typescript
            facets={facets ?? { main_category: [], info_type: [], importance: [], sub_tags: [] }}
```

- [ ] **Step 4: Update `buildDemoFacets` in homeData.ts**

In `frontend/src/pages/homeData.ts`, replace the `buildDemoFacets` function (lines 93–99) with:

```typescript
export function buildDemoFacets(items: ItemDetail[]) {
  const tagCounts = new Map<string, number>();
  for (const item of items) {
    for (const tag of item.sub_tags) {
      tagCounts.set(tag, (tagCounts.get(tag) ?? 0) + 1);
    }
  }
  const subTagFacets = Array.from(tagCounts, ([value, count]) => ({ value, count }))
    .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value, "zh-Hans-CN"))
    .slice(0, 30);

  return {
    main_category: countBy(items, (item) => item.main_category),
    info_type: countBy(items, (item) => item.info_type),
    importance: countBy(items, (item) => item.importance),
    sub_tags: subTagFacets,
  };
}
```

- [ ] **Step 5: Add `sub_tag` filter support to `filterDemoItems`**

In `frontend/src/pages/homeData.ts`, inside the `filterDemoItems` function, add a tag filter check. After the `matchesImportance` line (around line 33), add:

```typescript
    const matchesSubTag =
      !filters.sub_tag || item.sub_tags.includes(filters.sub_tag);
```

And update the return statement to include it:

```typescript
    return matchesSearch && matchesCategory && matchesType && matchesImportance && matchesSubTag && matchesTimeRange;
```

- [ ] **Step 6: Run frontend tests**

Run: `cd frontend && npx vitest run`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types.ts frontend/src/components/FacetSidebar.tsx frontend/src/pages/HomePage.tsx frontend/src/pages/homeData.ts
git commit -m "feat: add 技术热点 facet group to sidebar (top 30 sub_tags with counts)"
```

---

### Task 8: Redesign ItemDetail Display

**Files:**
- Modify: `frontend/src/components/ItemDetail.tsx`

- [ ] **Step 1: Rewrite ItemDetailBody**

Replace the entire `ItemDetailBody` function in `frontend/src/components/ItemDetail.tsx` (lines 7–76) with:

```tsx
function parseTechHighlight(text: string): { keyword: string; detail: string } | null {
  const match = text.match(/^\[(.+?)\]\s*(.*)/);
  if (match) return { keyword: match[1], detail: match[2] };
  return null;
}

function ItemDetailBody({ data }: { data: ItemDetailRecord }) {
  return (
    <div style={{ padding: 20 }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <ImportanceBadge value={data.importance} />
        <InfoTypeBadge value={data.info_type} />
        <span style={{ fontSize: 12, color: "#667085" }}>{data.main_category}</span>
      </div>

      {data.title_tldr ? (
        <>
          <h2 style={{ margin: "4px 0 4px" }}>{data.title_tldr}</h2>
          <div style={{ fontSize: 13, color: "#667085", marginBottom: 12 }}>{data.title}</div>
        </>
      ) : (
        <h2 style={{ margin: "4px 0 12px" }}>{data.title}</h2>
      )}

      {data.summary && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>摘要</h4>
          <p>{data.summary}</p>
        </section>
      )}

      {data.key_points.length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>技术要点</h4>
          <div>
            {data.key_points.map((kp, i) => {
              const parsed = parseTechHighlight(kp);
              return (
                <div key={i} style={{
                  border: "1px solid #eaecf0", borderRadius: 8,
                  padding: "8px 12px", marginBottom: 6, background: "#fafafa",
                }}>
                  {parsed ? (
                    <>
                      <strong style={{ color: "#175cd3" }}>[{parsed.keyword}]</strong>{" "}
                      {parsed.detail}
                    </>
                  ) : (
                    kp
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {data.sub_tags.length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>技术热点</h4>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {data.sub_tags.map((tag, i) => (
              <span key={i} style={{
                background: "#f2f4f7", borderRadius: 6, padding: "2px 8px", fontSize: 12,
              }}>{tag}</span>
            ))}
          </div>
        </section>
      )}

      {data.source_links.length > 0 && (
        <section>
          <h4 style={{ color: "#475467" }}>来源链接</h4>
          <ul>
            {Array.from(
              new Map(data.source_links.map((s) => [`${s.source_id}:${s.url}`, s])).values(),
            ).map((s) => (
              <li key={`${s.source_id}:${s.url}`}>
                <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
```

Key changes:
- Title: shows `title_tldr` (Chinese) as primary, `title` (English) as secondary gray text
- "关键点" → "技术要点" with `[keyword]` parsed and rendered in bold blue
- "影响/意义" section removed entirely
- "实体" section replaced with "技术热点" using `sub_tags` (flat chips, no count)

- [ ] **Step 2: Run frontend build to type-check**

Run: `cd frontend && npm run build`

Expected: build succeeds with no type errors.

- [ ] **Step 3: Commit**

```bash
cd frontend
git add src/components/ItemDetail.tsx
git commit -m "feat: redesign ItemDetail — Chinese title, tech highlights, flat keyword chips"
```

---

### Task 9: Final Verification

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && ENABLE_SCHEDULER=0 python -m pytest tests/ -v 2>&1 | tail -30`

Expected: all tests PASS.

- [ ] **Step 2: Run full frontend test suite**

Run: `cd frontend && npx vitest run`

Expected: all tests PASS.

- [ ] **Step 3: Run frontend build**

Run: `cd frontend && npm run build`

Expected: build succeeds.

- [ ] **Step 4: Final commit (if any remaining changes)**

If any files were missed, add and commit them.
