import type { AgentRunRecord, AgentRunStage, FacetValue, ItemDetail, ItemListResponse } from "../types";
import type { ItemQueryParams } from "../api/client";

type ModeInputs = {
  itemsFailed: boolean;
  facetsFailed: boolean;
};

export function resolveHomeDataMode({ itemsFailed, facetsFailed }: ModeInputs): "live" | "demo" {
  return itemsFailed || facetsFailed ? "demo" : "live";
}

const activeAgentStages: AgentRunStage[] = ["planning", "crawling", "quality", "summarizing"];

export function isAgentSourceRunning(run: AgentRunRecord | null | undefined): boolean {
  if (!run) return false;
  return run.status === "running" || activeAgentStages.includes(run.current_stage);
}

export function buildAgentWarmupRun(): AgentRunRecord {
  return {
    id: 0,
    status: "running",
    current_stage: "planning",
    stage_message: "正在创建运行记录",
    plan_urls_count: 0,
    fetched_count: 0,
    quality_passed: 0,
    items_created: 0,
    target_count: null,
    started_at: null,
    completed_at: null,
    error_message: null,
  };
}

export function isAgentWarmupResolved(run: AgentRunRecord | null | undefined): boolean {
  return !!run && run.id > 0;
}

export function agentCandidateRunRefreshKeys(agentSourceId: number): (string | number)[][] {
  return [
    ["agent-sources"],
    ["agent-source-candidates"],
    ["agent-runs", agentSourceId],
    ["items"],
  ];
}

function normalize(value: string | null | undefined): string {
  return (value ?? "").trim().toLowerCase();
}

type SortBy = "published_at" | "fetched_at" | undefined;
type SortDir = "desc" | "asc" | undefined;
type DemoItemFilters = ItemQueryParams & { item_ids?: string };

function relativeBoundaryIso(value: string | undefined): string | null {
  const now = new Date();
  if (value === "24h") {
    now.setHours(now.getHours() - 24);
    return now.toISOString();
  }
  if (value === "7d") {
    now.setDate(now.getDate() - 7);
    return now.toISOString();
  }
  if (value === "30d") {
    now.setDate(now.getDate() - 30);
    return now.toISOString();
  }
  return null;
}

function matchesTimeRange(
  value: string | null | undefined,
  filters: DemoItemFilters,
  prefix: "published" | "fetched",
): boolean {
  const afterMode = filters[`${prefix}_after_mode` as keyof DemoItemFilters];
  const afterValue = filters[`${prefix}_after_value` as keyof DemoItemFilters];
  const after = filters[`${prefix}_after` as keyof DemoItemFilters];
  const before = filters[`${prefix}_before` as keyof DemoItemFilters];
  const relativeAfter = afterMode === "relative"
    ? relativeBoundaryIso(typeof afterValue === "string" ? afterValue : undefined)
    : null;
  const absoluteAfter = typeof after === "string" ? after : undefined;
  const absoluteBefore = typeof before === "string" ? before : undefined;
  const hasTimeFilter = !!(relativeAfter || absoluteAfter || absoluteBefore);
  if (!hasTimeFilter) {
    return true;
  }
  if (!value) {
    return false;
  }
  if (relativeAfter && value < relativeAfter) {
    return false;
  }
  if (!relativeAfter && absoluteAfter && value < absoluteAfter) {
    return false;
  }
  if (absoluteBefore) {
    const beforeDate = new Date(absoluteBefore + "T00:00:00Z");
    beforeDate.setDate(beforeDate.getDate() + 1);
    const upperBound = beforeDate.toISOString();
    if (value >= upperBound) {
      return false;
    }
  }
  return true;
}

export function filterDemoItems(
  items: ItemDetail[],
  filters: DemoItemFilters,
): ItemDetail[] {
  const keywords = [filters.q, ...(filters.keywords ?? [])]
    .map(normalize)
    .filter(Boolean);

  const filtered = items.filter((item) => {
    const searchableValues = filters.strict_title ? [
      item.title,
      item.title_tldr,
      item.original_title,
    ].filter(Boolean) : [
      item.title,
      item.title_tldr,
      item.summary,
      ...item.key_points,
      item.url,
      item.main_category,
      item.info_type,
      item.importance,
      item.why_it_matters,
      item.os_insight,
      item.original_title,
      ...item.source_links.map((source) => source.url),
      ...item.sub_tags,
    ].filter(Boolean);
    const matchesSearch = keywords.length === 0 || keywords.some((keyword) =>
      searchableValues.some((value) => normalize(value).includes(keyword)),
    );

    const selectedCategories = (filters.main_category ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const matchesCategory =
      selectedCategories.length === 0
      || (!!item.main_category && selectedCategories.includes(item.main_category));
    const matchesType = !filters.info_type || item.info_type === filters.info_type;
    const selectedImportances = (filters.importance ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const matchesImportance =
      selectedImportances.length === 0
      || (!!item.importance && selectedImportances.includes(item.importance));
    const selectedSubTags = (filters.sub_tag ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const matchesSubTag =
      selectedSubTags.length === 0
      || item.sub_tags.some((tag) => selectedSubTags.includes(tag));
    const selectedSourceIds = (filters.source_id ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean)
      .map(Number)
      .filter((value) => Number.isFinite(value));
    const matchesSource =
      selectedSourceIds.length === 0
      || item.source_links.some((source) => selectedSourceIds.includes(source.source_id));
    const selectedItemIds = (filters.item_ids ?? "")
      .split(",")
      .map((value) => Number(value.trim()))
      .filter((value) => Number.isInteger(value) && value > 0);
    const matchesItemId = selectedItemIds.length === 0 || selectedItemIds.includes(item.id);

    const matchesPublishedTime = matchesTimeRange(item.published_at, filters, "published");
    const matchesFetchedTime = matchesTimeRange(item.fetched_at, filters, "fetched");

    return matchesSearch && matchesCategory && matchesType && matchesImportance && matchesSubTag && matchesSource && matchesItemId && matchesPublishedTime && matchesFetchedTime;
  });

  const sortBy: SortBy = (filters.sort_by as SortBy) ?? "published_at";
  const sortDir: SortDir = (filters.sort_dir as SortDir) ?? "desc";

  return [...filtered].sort((a, b) => {
    const fieldA = sortBy === "fetched_at" ? (a.fetched_at ?? "") : (a.published_at ?? "");
    const fieldB = sortBy === "fetched_at" ? (b.fetched_at ?? "") : (b.published_at ?? "");
    const cmp = fieldA < fieldB ? -1 : fieldA > fieldB ? 1 : 0;
    return sortDir === "asc" ? cmp : -cmp;
  });
}

function sortFacetValues(values: FacetValue[]): FacetValue[] {
  return values.sort((left, right) => {
    if (right.count !== left.count) return right.count - left.count;
    return left.value.localeCompare(right.value, "zh-Hans-CN");
  });
}

function countBy(items: ItemDetail[], pick: (item: ItemDetail) => string | null): FacetValue[] {
  const counts = new Map<string, number>();
  for (const item of items) {
    const value = pick(item);
    if (!value) continue;
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }

  return sortFacetValues(
    Array.from(counts, ([value, count]) => ({ value, count })),
  );
}

export function buildDemoFacets(items: ItemDetail[]) {
  const tagCounts = new Map<string, number>();
  for (const item of items) {
    for (const tag of item.sub_tags) {
      tagCounts.set(tag, (tagCounts.get(tag) ?? 0) + 1);
    }
  }

  return {
    main_category: countBy(items, (item) => item.main_category),
    info_type: countBy(items, (item) => item.info_type),
    importance: sortImportanceFacets(countBy(items, (item) => item.importance)),
    sub_tags: sortFacetValues(
      Array.from(tagCounts, ([value, count]) => ({ value, count })),
    ).slice(0, 30),
  };
}

// 重要度筛选项固定顺序：高 → 中 → 低
const IMPORTANCE_ORDER: Record<string, number> = { "高": 0, "中": 1, "低": 2 };

function sortImportanceFacets(values: FacetValue[]): FacetValue[] {
  return [...values].sort(
    (left, right) =>
      (IMPORTANCE_ORDER[left.value] ?? Object.keys(IMPORTANCE_ORDER).length) -
      (IMPORTANCE_ORDER[right.value] ?? Object.keys(IMPORTANCE_ORDER).length),
  );
}

export function makeListResponse(items: ItemDetail[], limit?: number, offset?: number): ItemListResponse {
  const sliced = limit != null ? items.slice(offset ?? 0, (offset ?? 0) + limit) : items;
  return {
    total: items.length,
    items: sliced.map(({ summary: _summary, key_points: _keyPoints, llm_confidence: _confidence, sub_tags: _tags, source_links: _sources, ...summaryItem }) => summaryItem),
  };
}
