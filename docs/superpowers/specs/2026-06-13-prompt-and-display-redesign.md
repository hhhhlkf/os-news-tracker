# LLM Prompt & Display Redesign

**Date:** 2026-06-13
**Status:** Draft

## Problem

Current LLM enrichment produces results that are too generic and not aligned with what OS maintainers actually need:

1. **Noisy content**: Community announcements, marketing posts, and non-technical notifications slip through
2. **Vague structure**: `key_points` and `why_it_matters` are generic summaries with low information density
3. **Over-structured entities**: `{type, name}` entity format is awkward to read; maintainers want flat keyword tags
4. **Missing Chinese titles**: Detail page only shows English titles; maintainers need Chinese translation as primary title

## Scope

Two subsystems changed:

1. **LLM prompts** — both `enricher.py` (enrichment quality) and `relevance.py` (filtering precision)
2. **Frontend display** — `ItemDetail.tsx` (detail page) and `FacetSidebar.tsx` (new "技术热点" facet)

One subsystem untouched: database schema (zero Alembic migration — field semantics change, column names stay).

## Content Priority

What to collect (in priority order):

| Priority | Content Type | Examples |
|----------|-------------|---------|
| Highest | Version releases (OS/kernel/packages) | RHEL 10 GA, Linux 6.12 release, glibc 2.40 |
| High | Security advisories | CVE-2026-xxxx, RHSA notices |
| High | New technology/tool releases | New filesystem, new container runtime |
| High | AI agent / LLM toolchain | Internal AI tools, LLM frameworks, agent SDKs |
| Low | Performance benchmarks | Phoronix comparisons, kernel benchmark results |
| Low | Technical architecture analysis | LWN deep-dives, architecture decision posts |

What to **exclude** — never collect:

- Community event announcements / meetup notices
- Job postings / hiring announcements
- User-facing beginner tutorials
- Marketing materials / press releases
- Non-technical organizational announcements

## Design

### 1. Enricher Prompt Redesign (`backend/app/processing/enricher.py`)

**Role change:** "技术新闻整理助手" → "操作系统维护工程师的技术情报分析师"

**New prompt structure:**

```
你是操作系统维护工程师的技术情报分析师。阅读下面的技术文章，为 OS maintainer 提取结构化情报。

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
```

**Field mapping (LLM output → DB column):**

| LLM JSON key | DB column | Notes |
|-------------|-----------|-------|
| `title_zh` | `title_tldr` | Reuse existing column, semantics change from "TLDR" to "Chinese title" |
| `summary` | `summary` | Unchanged |
| `tech_highlights` | `key_points` | Reuse JSON column, semantics change from "key points" to "tech highlights with keyword prefix" |
| `info_type` | `info_type` | Unchanged |
| `importance` | `importance` | Unchanged |
| `main_category` | `main_category` | Unchanged |
| `sub_tags` | Tags table (kind=`sub_tag`) | Unchanged |
| `keywords` | Tags table (kind=`sub_tag`) | Merge into sub_tags — keywords are appended to the sub_tags list |
| `confidence` | `llm_confidence` | Unchanged |

**Removed fields:**

| Old field | Disposition |
|----------|-------------|
| `why_it_matters` | Not requested from LLM; DB column retained but no longer written; frontend hides |
| `entities` (EntityRef list) | Not requested from LLM; Entity table still exists but no new writes from enricher |

**`EnrichedFields` schema change:**

```python
class EnrichedFields(BaseModel):
    title_zh: str                                    # was: title_tldr
    summary: str
    tech_highlights: list[str] = Field(default_factory=list)  # was: key_points
    info_type: InfoType
    importance: Importance
    main_category: str
    sub_tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # was: entities
    confidence: float = 0.0
    # Removed: why_it_matters, entities
```

**`Enricher.enrich()` mapping logic:**

After parsing LLM JSON into `EnrichedFields`, map to DB-compatible dict:
- `title_tldr` ← `title_zh`
- `key_points` ← `tech_highlights`
- `why_it_matters` ← `None` (not written)
- `sub_tags` ← `sub_tags` + `keywords` (merged, deduplicated)
- No Entity writes

### 2. Relevance Prompt Redesign (`backend/app/processing/relevance.py`)

**Current prompt:** Simple keyword relevance check — "判断是否与关键词相关的技术新闻"

**New prompt:**

```
你是操作系统维护团队的情报筛选员。判断以下内容是否值得 OS maintainer 关注。
收录标准：版本发布、安全公告、软件包更新、新技术/工具发布、性能数据、AI agent/LLM 工具链进展。
排除标准：纯社区活动通知、招聘、用户入门教程、市场营销、非技术公告。
关键词：{keywords}
标题：{title}
正文片段：{snippet}
只回答 true 或 false。
```

### 3. Frontend Detail Page (`frontend/src/components/ItemDetail.tsx`)

**Title area:**
- Primary: `data.title_tldr` (Chinese translated title) in large font
- Secondary: `data.title` (English original) in smaller gray text below
- Fallback: If `title_tldr` is null (legacy data), show only `data.title`

**"关键点" → "技术要点":**
- Section heading changes from "关键点" to "技术要点"
- Each item: parse `[关键词]` prefix via regex `^\[(.+?)\]\s*(.*)` → render keyword in bold + rest as normal text
- Fallback: If no `[keyword]` prefix found (legacy data), render as-is

**"影响/意义" → removed:**
- The `why_it_matters` section is no longer rendered
- Data remains in DB for legacy items

**"实体" → "技术热点" (detail page version):**
- Section heading changes from "实体" to "技术热点"
- Data source changes from `data.entities` to `data.sub_tags`
- Display as flat chips without `type:` prefix
- No count numbers on detail page

### 4. Facet Sidebar — New "技术热点" Section (`frontend/src/components/FacetSidebar.tsx`)

**New facet group** in the left sidebar, alongside existing `main_category`, `info_type`, `importance`:

- Title: "技术热点"
- Shows top 30 sub_tags by item count, descending
- Each entry displays: tag name + count badge (e.g., `Linux 6.12 (5)`)
- Clickable to filter items by that tag

**Backend `/facets` API change:**
- Add `sub_tags: FacetValue[]` to the `Facets` response
- Query: `SELECT t.name, COUNT(DISTINCT it.item_id) FROM tags t JOIN item_tags it ... WHERE t.kind = 'sub_tag' GROUP BY t.name ORDER BY count DESC LIMIT 30`

**Frontend `types.ts` change:**

```typescript
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
  sub_tags: FacetValue[];  // NEW
}
```

### 5. Test Changes

**Unit tests affected:**
- `test_enricher.py` — Update mock LLM responses to match new prompt/schema
- `test_search_provider.py` / `test_relevance` — Update expected relevance prompt

**Integration tests affected:**
- `test_pipeline.py` — Update enriched field expectations
- `test_api.py` — Add tests for `sub_tags` facet

**No migration tests needed** — zero schema change.

## File Change Summary

| File | Change |
|------|--------|
| `backend/app/processing/enricher.py` | Rewrite prompt template; update field mapping in `enrich()` |
| `backend/app/processing/relevance.py` | Rewrite relevance prompt |
| `backend/app/schemas.py` | Update `EnrichedFields` (rename fields, remove `why_it_matters`/`entities`) |
| `backend/app/repository.py` | Update `save_enriched()` to merge `keywords` into `sub_tags`; stop writing entities |
| `backend/app/api/routes.py` | Add `sub_tags` facet query to `/facets` endpoint |
| `frontend/src/types.ts` | Add `sub_tags` to `Facets` interface |
| `frontend/src/components/ItemDetail.tsx` | Title: zh+en; 技术要点 with keyword bold; remove 影响/意义; 技术热点 from sub_tags |
| `frontend/src/components/FacetSidebar.tsx` | Add "技术热点" facet group (top 30, with counts, clickable filter) |
| `backend/tests/unit/test_enricher.py` | Update mock responses and assertions |
| `backend/tests/integration/test_pipeline.py` | Update enriched field expectations |

## Non-Goals

- No Alembic migration (column names unchanged)
- No changes to RSS/page_monitor/search fetcher logic
- No changes to dedup logic
- No changes to scheduler
- No new API endpoints (only modify existing `/facets` response)
- No Entity table deletion (table stays, just no new writes from enricher)
