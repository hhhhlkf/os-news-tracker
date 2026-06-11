import type { FacetValue, ItemDetail, ItemListResponse } from "../types";

type ModeInputs = {
  itemsFailed: boolean;
  facetsFailed: boolean;
};

export function resolveHomeDataMode({ itemsFailed, facetsFailed }: ModeInputs): "live" | "demo" {
  return itemsFailed || facetsFailed ? "demo" : "live";
}

function normalize(value: string | null | undefined): string {
  return (value ?? "").trim().toLowerCase();
}

type SortBy = "published_at" | "fetched_at" | undefined;
type SortDir = "desc" | "asc" | undefined;

export function filterDemoItems(items: ItemDetail[], filters: Record<string, string>): ItemDetail[] {
  const q = normalize(filters.q);

  const filtered = items.filter((item) => {
    const matchesSearch =
      q.length === 0 ||
      [item.title, item.title_tldr, item.summary, item.main_category, item.info_type]
        .filter(Boolean)
        .some((value) => normalize(value).includes(q));

    const matchesCategory =
      !filters.main_category || item.main_category === filters.main_category;
    const matchesType = !filters.info_type || item.info_type === filters.info_type;
    const matchesImportance =
      !filters.importance || item.importance === filters.importance;

    // Time-range filtering on published_at
    const hasTimeFilter = !!(filters.published_after || filters.published_before);
    let matchesTimeRange = true;
    if (hasTimeFilter) {
      if (!item.published_at) {
        matchesTimeRange = false; // exclude items with null published_at when time filter is active
      } else {
        if (filters.published_after) {
          if (item.published_at < filters.published_after) {
            matchesTimeRange = false;
          }
        }
        if (filters.published_before) {
          // published_before is inclusive: compute next day as upper bound
          const beforeDate = new Date(filters.published_before + "T00:00:00Z");
          beforeDate.setDate(beforeDate.getDate() + 1);
          const upperBound = beforeDate.toISOString();
          if (item.published_at >= upperBound) {
            matchesTimeRange = false;
          }
        }
      }
    }

    return matchesSearch && matchesCategory && matchesType && matchesImportance && matchesTimeRange;
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
  return {
    main_category: countBy(items, (item) => item.main_category),
    info_type: countBy(items, (item) => item.info_type),
    importance: countBy(items, (item) => item.importance),
  };
}

export function makeListResponse(items: ItemDetail[], limit?: number, offset?: number): ItemListResponse {
  const sliced = limit != null ? items.slice(offset ?? 0, (offset ?? 0) + limit) : items;
  return {
    total: items.length,
    items: sliced.map(({ summary: _summary, key_points: _keyPoints, why_it_matters: _why, llm_confidence: _confidence, sub_tags: _tags, entities: _entities, source_links: _sources, ...summaryItem }) => summaryItem),
  };
}
