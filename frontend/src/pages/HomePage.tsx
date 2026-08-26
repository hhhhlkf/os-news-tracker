import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { fetchFacets, fetchItems, listDiscoveryMethods } from "../api/client";
import { FacetSidebar } from "../components/FacetSidebar";
import { EdgePageArrows } from "../components/EdgePageArrows";
import { HomeFilterDrawer, HomeFilterTrigger } from "../components/HomeFilterDrawer";
import { HomeModuleDeck, homeModuleIndex } from "../components/HomeModuleDeck";
import { ItemList } from "../components/ItemList";
import { ItemDetail } from "../components/ItemDetail";
import { MailTaskCenter } from "../components/MailTaskCenter";
import { MorningCrawlModal } from "../components/MorningCrawlModal";
import { TrendCarousel } from "../features/trends/TrendCarousel";
import { fetchMailSchedules, fetchMailTemplates } from "../mail/api";
import { fetchMorningCrawlDashboard } from "../morningCrawl/api";
import { demoItems } from "../demoData";
import { buildDemoFacets, filterDemoItems, makeListResponse, resolveHomeDataMode } from "./homeData";
import type { CrawlMethod } from "../types";
import { clampInput, INPUT_LIMITS } from "../inputLimits";

const PAGE_SIZE = 10;
const ENTRY_CARD_MIN_HEIGHT = 196;

const MORNING_STATUS_META: Record<string, { text: string; bg: string; color: string }> = {
  not_run: { text: "今日未执行", bg: "#f2f4f7", color: "#475467" },
  running: { text: "执行中", bg: "#eff6ff", color: "#175cd3" },
  stopping: { text: "停止中", bg: "#fffaeb", color: "#b54708" },
  cancelled: { text: "已停止", bg: "#f2f4f7", color: "#475467" },
  success: { text: "今日已完成", bg: "#ecfdf3", color: "#067647" },
  partial: { text: "部分成功", bg: "#fffaeb", color: "#b54708" },
  failed: { text: "执行失败", bg: "#fef3f2", color: "#b42318" },
};

const FILTER_LABELS: Array<{ key: string; label: string }> = [
  { key: "q", label: "关键词" },
  { key: "main_category", label: "分类" },
  { key: "info_type", label: "类型" },
  { key: "importance", label: "重要度" },
  { key: "item_kind", label: "条目类型" },
  { key: "sub_tag", label: "热点" },
];

function localDateDaysAgo(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function summarizeTimeFilter(filters: Record<string, string>, prefix: "published" | "fetched", label: string): string | null {
  const afterMode = filters[`${prefix}_after_mode`];
  const afterValue = filters[`${prefix}_after_value`];
  if (afterMode === "relative" && afterValue) {
    if (afterValue === "24h") return `${label}：最近 24h`;
    if (afterValue === "7d") return `${label}：最近 7d`;
    if (afterValue === "30d") return `${label}：最近 30d`;
  }
  const after = filters[`${prefix}_after`];
  const before = filters[`${prefix}_before`];
  if (!after && !before) return null;
  if (after && !before) {
    if (after === localDateDaysAgo(1)) return `${label}：最近 24h`;
    if (after === localDateDaysAgo(7)) return `${label}：最近 7d`;
    if (after === localDateDaysAgo(30)) return `${label}：最近 30d`;
  }
  const from = after || "不限";
  const to = before || "至今";
  return `${label}：${from} ~ ${to}`;
}

function splitItemIds(raw: string | undefined): string[] {
  return (raw ?? "").split(",").map((value) => value.trim()).filter(Boolean);
}

function parseItemId(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function parsePage(value: string | null): number {
  if (value === null || !/^\d+$/.test(value)) return 1;
  const page = Number(value);
  return Number.isSafeInteger(page) && page > 0 ? page : 1;
}

function methodDisplayName(method: CrawlMethod): string {
  return method.source_name?.trim() || method.domain || method.entry_url || `来源#${method.source_id ?? method.id}`;
}

function summarizeActiveFilters(filters: Record<string, string>, methods: CrawlMethod[] = []): string[] {
  const chips: string[] = [];
  for (const { key, label } of FILTER_LABELS) {
    const value = filters[key];
    if (!value) continue;
    const display = value.includes(",") ? value.split(",").map((part) => part.trim()).filter(Boolean).join(" / ") : value;
    chips.push(`${label}：${display}`);
  }
  const timeSummary = summarizeTimeFilter(filters, "published", "发布时间");
  if (timeSummary) {
    chips.push(timeSummary);
  }
  const fetchedSummary = summarizeTimeFilter(filters, "fetched", "查询时间");
  if (fetchedSummary) {
    chips.push(fetchedSummary);
  }
  const exactItemIds = splitItemIds(filters.item_ids);
  if (exactItemIds.length > 0) {
    chips.push(`趋势筛选：${exactItemIds.length} 条新闻`);
  }
  if (filters.source_id) {
    const nameBySourceId = new Map(
      methods
        .filter((method) => method.source_id != null)
        .map((method) => [String(method.source_id), methodDisplayName(method)]),
    );
    const names = filters.source_id
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean)
      .map((id) => nameBySourceId.get(id) ?? `来源#${id}`);
    if (names.length > 0) {
      chips.push(`查询链接：${names.join(" / ")}`);
    }
  }
  return chips;
}

function hasTimeFilter(filters: Record<string, string>, prefix: "published" | "fetched"): boolean {
  return !!(
    filters[`${prefix}_after_mode`]
    || filters[`${prefix}_after_value`]
    || filters[`${prefix}_after`]
    || filters[`${prefix}_before_mode`]
    || filters[`${prefix}_before_value`]
    || filters[`${prefix}_before`]
  );
}

function countActiveFilters(filters: Record<string, string>): number {
  let count = 0;
  for (const { key } of FILTER_LABELS) {
    if (filters[key]) {
      count += 1;
    }
  }
  if (hasTimeFilter(filters, "published")) {
    count += 1;
  }
  if (hasTimeFilter(filters, "fetched")) {
    count += 1;
  }
  if (filters.source_id) {
    count += 1;
  }
  if (splitItemIds(filters.item_ids).length > 0) {
    count += 1;
  }
  return count;
}

/** What the trend carousel injected into the list filters, so it can be undone. */
interface TrendSelection {
  resultId: string | null;
  itemId: number | null;
  label: string;
  /** The search word to restore, set only when a source pill overwrote it. */
  restoreQuery: string | null;
}

export function HomePage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const [locationSearch, setLocationSearch] = useState(() => window.location.search);
  const [filters, setFilters] = useState<Record<string, string>>({ q: "", sort_by: "last_activity_at", sort_dir: "desc" });
  const [mailOpen, setMailOpen] = useState(false);
  const [morningCrawlOpen, setMorningCrawlOpen] = useState(false);
  const [trendSelection, setTrendSelection] = useState<TrendSelection | null>(null);
  const [moduleIndex, setModuleIndex] = useState(() => homeModuleIndex("news"));
  const [filterOpen, setFilterOpen] = useState(false);
  const searchParams = useMemo(() => new URLSearchParams(locationSearch), [locationSearch]);
  const openId = parseItemId(searchParams.get("item"));
  const page = parsePage(searchParams.get("page"));

  useEffect(() => {
    const syncLocation = () => setLocationSearch(window.location.search);
    window.addEventListener("popstate", syncLocation);
    return () => window.removeEventListener("popstate", syncLocation);
  }, []);

  const updateLocationSearch = (update: (current: URLSearchParams) => URLSearchParams) => {
    const next = update(new URLSearchParams(window.location.search));
    const search = next.toString();
    window.history.pushState(null, "", `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`);
    setLocationSearch(window.location.search);
  };

  const setPage = (nextPage: number) => {
    updateLocationSearch((current) => {
      const next = new URLSearchParams(current);
      next.set("page", String(nextPage));
      return next;
    });
  };

  const setOpenId = (id: number | null) => {
    updateLocationSearch((current) => {
      const next = new URLSearchParams(current);
      if (id === null) {
        next.delete("item");
      } else {
        next.set("item", String(id));
      }
      next.set("page", String(page));
      return next;
    });
  };

  const setFilter = (key: string, value: string) => {
    setPage(1);
    setFilters((f) => ({ ...f, [key]: value }));
  };

  // A source pill fills the search box for display only. Typing turns that text
  // back into a real search word, so the exact trend filter is dropped first.
  const setSearchQuery = (value: string) => {
    setPage(1);
    const hadTrendFilter = trendSelection !== null;
    setTrendSelection(null);
    setFilters((f) => ({ ...f, q: value, ...(hadTrendFilter ? { item_ids: "" } : {}) }));
  };

  const applyTrendFilter = (trend: { resultId: string; topic: string; itemIds: number[] }) => {
    setPage(1);
    setOpenId(null);
    setTrendSelection({
      resultId: trend.resultId,
      itemId: null,
      label: trend.topic || "所选趋势",
      restoreQuery: trendSelection?.restoreQuery ?? null,
    });
    setFilters((f) => ({ ...f, item_ids: trend.itemIds.join(",") }));
    setModuleIndex(homeModuleIndex("news"));
  };

  const applyTrendSourceFilter = (source: { itemId: number; title: string }) => {
    setPage(1);
    setOpenId(null);
    // The title only fills the search box; filtering stays on the exact item_id,
    // so same-named news can never be matched by accident.
    setTrendSelection({
      resultId: null,
      itemId: source.itemId,
      label: source.title,
      restoreQuery: trendSelection?.restoreQuery ?? filters.q ?? "",
    });
    setFilters((f) => ({ ...f, item_ids: String(source.itemId), q: source.title }));
    setModuleIndex(homeModuleIndex("news"));
  };

  const clearTrendFilter = () => {
    setPage(1);
    const restoreQuery = trendSelection?.restoreQuery ?? null;
    setFilters((f) => ({ ...f, item_ids: "", ...(restoreQuery === null ? {} : { q: restoreQuery }) }));
    setTrendSelection(null);
  };

  // The title a source pill wrote into the search box is display text, never a
  // query term: the exact item_id alone decides what the list shows.
  const isSearchInjectedByTrend = trendSelection?.restoreQuery != null;
  const effectiveFilters = useMemo(() => {
    if (!isSearchInjectedByTrend) return filters;
    const { q: _displayOnlyTitle, ...rest } = filters;
    return rest;
  }, [filters, isSearchInjectedByTrend]);

  const params = useMemo(
    () => ({
      ...Object.fromEntries(Object.entries(effectiveFilters).filter(([, value]) => value)),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [effectiveFilters, page],
  );
  // The trend carousel's exact item filter is an ephemeral browse action, so it
  // stays out of the mail task center's filter snapshot and estimate.
  const mailFilters = useMemo(
    () => ({
      ...Object.fromEntries(
        Object.entries(effectiveFilters).filter(([key, value]) => value && key !== "item_ids"),
      ),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [effectiveFilters, page],
  );
  const itemsQuery = useQuery({
    queryKey: ["items", params],
    queryFn: () => fetchItems(params),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const facetsQuery = useQuery({
    queryKey: ["facets"],
    queryFn: fetchFacets,
    retry: false,
  });
  const crawlMethodsQuery = useQuery({
    queryKey: ["discovery-methods", "home-filter"],
    queryFn: listDiscoveryMethods,
    retry: false,
  });
  const mailTemplatesQuery = useQuery({
    queryKey: ["mail-templates"],
    queryFn: fetchMailTemplates,
    retry: false,
  });
  const mailSchedulesQuery = useQuery({
    queryKey: ["mail-schedules"],
    queryFn: fetchMailSchedules,
    retry: false,
  });
  const morningCrawlQuery = useQuery({
    queryKey: ["morning-crawl"],
    queryFn: fetchMorningCrawlDashboard,
    enabled: hasSystemAccess,
    retry: false,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });

  const mode = resolveHomeDataMode({
    itemsFailed: itemsQuery.isError,
    facetsFailed: facetsQuery.isError,
  });

  const demoFilteredItems = useMemo(() => filterDemoItems(demoItems, effectiveFilters), [effectiveFilters]);
  const demoList = useMemo(
    () => makeListResponse(demoFilteredItems, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    [demoFilteredItems, page],
  );
  const facets = mode === "demo" ? buildDemoFacets(demoItems) : facetsQuery.data;
  const listData = mode === "demo" ? demoList : itemsQuery.data;
  const selectedDemoItem = mode === "demo"
    ? demoItems.find((item) => item.id === openId) ?? undefined
    : undefined;
  const selectedLiveItemId = mode === "live" ? openId ?? undefined : undefined;

  const hasLiveEmptyState = mode === "live" && listData?.total === 0;
  const totalPages = Math.max(1, Math.ceil((listData?.total ?? 0) / PAGE_SIZE));
  const activeFilterCount = countActiveFilters(effectiveFilters);
  const crawlMethods = crawlMethodsQuery.data ?? [];
  const activeCrawlMethods = useMemo(
    () => crawlMethods.filter((method) => method.status === "active"),
    [crawlMethods],
  );
  const filterChips = summarizeActiveFilters(effectiveFilters, activeCrawlMethods);
  const templateCount = mailTemplatesQuery.data?.length ?? 0;
  const scheduleCount = mailSchedulesQuery.data?.length ?? 0;
  const enabledScheduleCount = (mailSchedulesQuery.data ?? []).filter((s) => s.enabled).length;
  const mailBadge = mailSchedulesQuery.isError
    ? { text: "未连接", bg: "#fef3f2", color: "#b42318" }
    : mailSchedulesQuery.isLoading
      ? { text: "加载中", bg: "#f2f4f7", color: "#475467" }
      : enabledScheduleCount > 0
        ? { text: `${enabledScheduleCount} 个预定运行中`, bg: "#ecfdf3", color: "#067647" }
        : scheduleCount > 0
          ? { text: "预定全部暂停", bg: "#fffaeb", color: "#b54708" }
          : { text: "暂无预定", bg: "#f2f4f7", color: "#475467" };
  const morningDashboard = morningCrawlQuery.data;
  const morningStatus = morningDashboard?.today_status ?? "not_run";
  const morningStatusMeta = MORNING_STATUS_META[morningStatus] ?? MORNING_STATUS_META.not_run;

  useEffect(() => {
    if (!crawlMethodsQuery.data || !filters.source_id) return;
    const activeSourceIds = new Set(
      activeCrawlMethods
        .map((method) => method.source_id)
        .filter((sourceId): sourceId is number => typeof sourceId === "number")
        .map(String),
    );
    const kept = filters.source_id
      .split(",")
      .map((id) => id.trim())
      .filter((id) => id && activeSourceIds.has(id));
    const nextValue = kept.join(",");
    if (nextValue !== filters.source_id) {
      setFilter("source_id", nextValue);
    }
  }, [activeCrawlMethods, crawlMethodsQuery.data, filters.source_id]);

  const searchToolbar = (
    <section
      style={{
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        background: "#fff",
        padding: 16,
        flexShrink: 0,
      }}
    >
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <input
          placeholder="搜索标题、摘要、分类…"
          value={filters.q ?? ""}
          maxLength={INPUT_LIMITS.searchQuery}
          onChange={(e) => setSearchQuery(clampInput(e.target.value, INPUT_LIMITS.searchQuery))}
          style={{
            flex: "1 1 420px",
            minWidth: 260,
            padding: "12px 14px",
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            fontSize: 14,
          }}
        />
        <select
          value={`${filters.sort_by ?? "published_at"}:${filters.sort_dir ?? "desc"}`}
          onChange={(e) => {
            const [sort_by, sort_dir] = e.target.value.split(":") as ["published_at" | "fetched_at" | "last_activity_at", "desc" | "asc"];
            setPage(1);
            setFilters((f) => ({ ...f, sort_by, sort_dir }));
          }}
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: "12px 14px",
            fontSize: 14,
            background: "#fff",
            color: "#344054",
          }}
        >
          <option value="last_activity_at:desc">最近活动 最新优先</option>
          <option value="last_activity_at:asc">最近活动 最早优先</option>
          <option value="published_at:desc">发布时间 最新优先</option>
          <option value="published_at:asc">发布时间 最早优先</option>
          <option value="fetched_at:desc">入库时间 最新优先</option>
          <option value="fetched_at:asc">入库时间 最早优先</option>
        </select>
        <select
          value={filters.item_kind ?? ""}
          onChange={(event) => setFilter("item_kind", event.target.value)}
          style={{ border: "1px solid #d0d5dd", borderRadius: 8, padding: "12px 14px", fontSize: 14, background: "#fff", color: "#344054" }}
        >
          <option value="">全部条目</option>
          <option value="news">新闻</option>
          <option value="discussion">技术讨论</option>
        </select>
        <HomeFilterTrigger activeCount={activeFilterCount} onClick={() => setFilterOpen(true)} />
        <button
          onClick={() => {
            setPage(1);
            setFilters({ q: "", sort_by: "last_activity_at", sort_dir: "desc" });
            setOpenId(null);
            setTrendSelection(null);
          }}
          style={{
            border: "1px solid #d0d5dd",
            background: "#fff",
            borderRadius: 8,
            padding: "12px 14px",
            cursor: "pointer",
            color: "#344054",
          }}
        >
          清空筛选
        </button>
      </div>
      {trendSelection && (
        <div
          style={{
            marginTop: 12,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 12,
            flexWrap: "wrap",
            border: "1px solid #d3e3fb",
            background: "#eff6ff",
            borderRadius: 8,
            padding: "10px 12px",
          }}
        >
          <div style={{ fontSize: 12, color: "#175cd3", minWidth: 0 }}>
            趋势筛选：{trendSelection.label}
            <span style={{ color: "#667085" }}>
              {" "}
              · 按 {splitItemIds(filters.item_ids).length} 条新闻 ID 精确匹配
            </span>
          </div>
          <button
            onClick={clearTrendFilter}
            style={{
              border: "1px solid #84adff",
              background: "#fff",
              color: "#175cd3",
              borderRadius: 8,
              padding: "6px 12px",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            清除趋势筛选
          </button>
        </div>
      )}
    </section>
  );

  const facetSidebar = (
    <FacetSidebar
      facets={facets ?? { main_category: [], info_type: [], importance: [], sub_tags: [] }}
      crawlMethods={activeCrawlMethods}
      isLoading={mode === "live" && facetsQuery.isLoading}
      isMethodsLoading={crawlMethodsQuery.isLoading}
      selected={filters}
      onSelect={setFilter}
    />
  );

  const mailCard = (
    <div
      style={{
        border: "1px solid #bfd7ff",
        background: "linear-gradient(180deg,#f8fbff 0%,#ffffff 100%)",
        borderRadius: 8,
        padding: 28,
        minHeight: ENTRY_CARD_MIN_HEIGHT + 160,
        width: 560,
        maxWidth: "100%",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ fontSize: 21, fontWeight: 800, color: "#101828", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, letterSpacing: "0.01em" }}>
        邮件任务中心
        <span style={{ fontSize: 13, fontWeight: 700, borderRadius: 999, padding: "5px 11px", background: mailBadge.bg, color: mailBadge.color }}>
          {mailBadge.text}
        </span>
      </div>

      <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
        <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "14px 16px" }}>
          <div style={{ fontSize: 36, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{templateCount}</div>
          <div style={{ fontSize: 15, color: "#667085", marginTop: 6 }}>模板</div>
        </div>
        <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "14px 16px" }}>
          <div style={{ fontSize: 36, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{scheduleCount}</div>
          <div style={{ fontSize: 15, color: "#667085", marginTop: 6 }}>已预定</div>
        </div>
      </div>

      <div style={{ fontSize: 14, fontWeight: 700, color: "#98a2b3", marginBottom: 8 }}>当前筛选</div>
      {filterChips.length > 0 ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {filterChips.map((chip) => (
            <span
              key={chip}
              style={{
                fontSize: 14,
                color: "#344054",
                background: "#eff6ff",
                border: "1px solid #d3e3fb",
                borderRadius: 6,
                padding: "4px 8px",
                wordBreak: "break-all",
              }}
            >
              {chip}
            </span>
          ))}
        </div>
      ) : (
        <div style={{ fontSize: 15, color: "#98a2b3" }}>全部条目（未设置筛选）</div>
      )}

      <button
        onClick={() => {
          setMailOpen(true);
          setFilterOpen(true);
        }}
        style={{
          marginTop: "auto",
          width: "100%",
          border: "none",
          borderRadius: 8,
          padding: "14px 16px",
          fontSize: 16,
          fontWeight: 700,
          color: "#fff",
          background: "#175cd3",
          cursor: "pointer",
        }}
      >
        打开邮件任务中心
      </button>
    </div>
  );

  const morningCard = hasSystemAccess ? (
    <div
      style={{
        border: "1px solid #cbd9ea",
        background: "linear-gradient(180deg,#f7faff 0%,#ffffff 100%)",
        borderRadius: 8,
        padding: 28,
        minHeight: ENTRY_CARD_MIN_HEIGHT + 160,
        width: 560,
        maxWidth: "100%",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ fontSize: 21, fontWeight: 800, color: "#101828", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, letterSpacing: "0.01em" }}>
        系统定时抓取
        <span style={{ fontSize: 13, fontWeight: 700, borderRadius: 999, padding: "5px 11px", background: morningStatusMeta.bg, color: morningStatusMeta.color }}>
          {morningStatusMeta.text}
        </span>
      </div>

      <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
        <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "14px 16px" }}>
          <div style={{ fontSize: 36, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{morningDashboard?.active_method_count ?? 0}</div>
          <div style={{ fontSize: 15, color: "#667085", marginTop: 6 }}>爬取方式</div>
        </div>
        <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "14px 16px" }}>
          <div style={{ fontSize: 36, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{morningDashboard?.today_run?.stored_count ?? 0}</div>
          <div style={{ fontSize: 15, color: "#667085", marginTop: 6 }}>今日入库</div>
        </div>
      </div>

      <div style={{ fontSize: 14, fontWeight: 700, color: "#98a2b3", marginBottom: 8 }}>下次执行</div>
      <div style={{ fontSize: 15, color: "#344054" }}>
        {morningDashboard?.config.next_run_at
          ? `${morningDashboard.config.next_run_at.split("T")[0]} ${morningDashboard.config.next_run_at.split("T")[1]?.slice(0, 5) ?? ""}（北京时间）`
          : "未排程"}
      </div>

      <button
        onClick={() => setMorningCrawlOpen(true)}
        style={{
          marginTop: "auto",
          width: "100%",
          border: "none",
          borderRadius: 8,
          padding: "14px 16px",
          fontSize: 16,
          fontWeight: 700,
          color: "#fff",
          background: "#0e7090",
          cursor: "pointer",
        }}
      >
        打开系统定时抓取
      </button>
    </div>
  ) : null;

  const renderTitleCard = (opts?: { compact?: boolean }) => {
    const compact = Boolean(opts?.compact);
    return (
    <header className={`home-title-card${compact ? " home-title-card--compact" : ""}`}>
      <div style={{ fontSize: compact ? 11 : 13, color: "#98a2b3", marginBottom: compact ? 6 : 10 }}>OS News Tracker</div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: compact ? 10 : 16,
          alignItems: compact ? "center" : "flex-end",
          flexWrap: "wrap",
        }}
      >
        <div style={{ maxWidth: compact ? "100%" : 720, minWidth: 0, flex: compact ? "1 1 180px" : undefined }}>
          <h1 style={{ margin: 0, fontSize: compact ? 22 : 32, lineHeight: 1.2 }}>技术新闻追踪</h1>
          <p style={{ marginTop: compact ? 4 : 10, marginBottom: 0, color: "#d0d5dd", fontSize: compact ? 12 : undefined, lineHeight: compact ? 1.45 : undefined }}>
            汇总 OS、兼容性、安全与内部 AI 相关动态，支持搜索、筛选与详情查看。
          </p>
        </div>
        <div style={{ display: "flex", gap: compact ? 6 : 12, flexWrap: compact ? "nowrap" : "wrap" }}>
          <div style={{ minWidth: compact ? 72 : 140, background: "#182230", borderRadius: 8, padding: compact ? "8px 10px" : 14 }}>
            <div style={{ fontSize: compact ? 10 : 12, color: "#98a2b3", marginBottom: compact ? 3 : 6 }}>当前数据源</div>
            <div style={{ fontSize: compact ? 16 : 24, fontWeight: 700 }}>{mode === "demo" ? "演示" : "实时"}</div>
          </div>
          <div style={{ minWidth: compact ? 72 : 140, background: "#182230", borderRadius: 8, padding: compact ? "8px 10px" : 14 }}>
            <div style={{ fontSize: compact ? 10 : 12, color: "#98a2b3", marginBottom: compact ? 3 : 6 }}>当前条目数</div>
            <div style={{ fontSize: compact ? 16 : 24, fontWeight: 700 }}>{listData?.total ?? 0}</div>
          </div>
          <div style={{ minWidth: compact ? 72 : 140, background: "#182230", borderRadius: 8, padding: compact ? "8px 10px" : 14 }}>
            <div style={{ fontSize: compact ? 10 : 12, color: "#98a2b3", marginBottom: compact ? 3 : 6 }}>激活筛选</div>
            <div style={{ fontSize: compact ? 16 : 24, fontWeight: 700 }}>{activeFilterCount}</div>
          </div>
        </div>
      </div>
    </header>
    );
  };

  return (
    <div className="home-shell">
      <div className="home-deck-host">
        <HomeModuleDeck
          index={moduleIndex}
          onIndexChange={(next) => {
            setModuleIndex(next);
            setFilterOpen(false);
          }}
          locked={openId !== null || mailOpen || morningCrawlOpen || filterOpen}
        >
          <div className="home-module__frame home-module__frame--news">
            <div className="home-module__stack">
              {renderTitleCard()}
              {searchToolbar}
              {hasLiveEmptyState && (
                <div
                  style={{
                    flexShrink: 0,
                    border: "1px dashed #d0d5dd",
                    background: "#fcfcfd",
                    color: "#667085",
                    borderRadius: 8,
                    padding: 16,
                  }}
                >
                  数据库里还没有已入库条目。后端目前只会 seed 数据源，不会自动生成新闻样例，所以前端会显得很空。
                </div>
              )}
              <div className="home-module__news" data-home-scroll="true">
                <ItemList
                  items={listData?.items ?? []}
                  total={listData?.total ?? 0}
                  page={page}
                  pageSize={PAGE_SIZE}
                  isLoading={mode === "live" && itemsQuery.isLoading}
                  isFetching={
                    mode === "live" &&
                    (itemsQuery.isFetching || itemsQuery.isPlaceholderData) &&
                    !itemsQuery.isLoading
                  }
                  emptyMessage="没有匹配的条目，试试放宽搜索词或取消筛选条件。"
                  sortBy={(filters.sort_by as "published_at" | "fetched_at") ?? "published_at"}
                  openId={openId}
                  onOpen={setOpenId}
                  onPageChange={setPage}
                />
              </div>
            </div>
          </div>

          <div className="home-module__frame">
            <div className="home-module__stack">
              {renderTitleCard()}
              {mode === "demo" && (
                <div
                  style={{
                    border: "1px solid #bfd7ff",
                    background: "#eff6ff",
                    color: "#175cd3",
                    borderRadius: 8,
                    padding: "12px 14px",
                  }}
                >
                  当前未连接到后端 API，页面自动切换为演示数据，方便先检查交互和布局。
                </div>
              )}
              <TrendCarousel
                onSelectTrend={applyTrendFilter}
                onSelectSource={applyTrendSourceFilter}
                activeResultId={trendSelection?.resultId ?? null}
                activeItemId={trendSelection?.itemId ?? null}
              />
            </div>
          </div>

          <div className="home-module__frame" style={{ position: "relative" }}>
            <div className={`home-module__stack home-module__stack--subscribe${hasSystemAccess ? "" : " is-single-card"}`}>
              {renderTitleCard({ compact: !hasSystemAccess })}
              <div
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "stretch",
                  gap: 20,
                  flexWrap: "wrap",
                  width: "100%",
                }}
              >
                {mailCard}
                {morningCard}
              </div>
            </div>
          </div>
        </HomeModuleDeck>

        <HomeFilterDrawer
          open={filterOpen}
          quiet={mailOpen}
          activeCount={activeFilterCount}
          onOpen={() => setFilterOpen(true)}
          onClose={() => setFilterOpen(false)}
        >
          {facetSidebar}
        </HomeFilterDrawer>
      </div>

      <EdgePageArrows page={page} totalPages={totalPages} onPageChange={setPage} hidden={openId !== null || moduleIndex !== homeModuleIndex("news")} />

      {openId !== null && (
        <div onClick={() => setOpenId(null)} style={{
          position: "fixed", inset: 0, background: "rgba(15, 23, 42, 0.42)",
          display: "flex", justifyContent: "flex-end", zIndex: 55,
        }}>
          <div onClick={(e) => e.stopPropagation()} style={{
            width: 620, maxWidth: "92vw", background: "#fff",
            height: "100%", overflowY: "auto", boxShadow: "-24px 0 48px rgba(16, 24, 40, 0.16)",
          }}>
            <ItemDetail id={selectedLiveItemId} item={selectedDemoItem} />
          </div>
        </div>
      )}

      <MailTaskCenter
        open={mailOpen}
        onClose={() => {
          setMailOpen(false);
          setFilterOpen(false);
        }}
        homeFilters={mailFilters}
      />
      {hasSystemAccess && <MorningCrawlModal open={morningCrawlOpen} onClose={() => setMorningCrawlOpen(false)} />}
    </div>
  );
}
