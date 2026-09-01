import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchFacets, fetchItems, listDiscoveryMethods } from "../api/client";
import { FacetSidebar } from "../components/FacetSidebar";
import { EdgePageArrows } from "../components/EdgePageArrows";
import { HomeFilterTrigger, HomeSideDrawers } from "../components/HomeFilterDrawer";
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
const DRAWER_HANDOFF_MS = 300;

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
  const queryClient = useQueryClient();
  const [locationSearch, setLocationSearch] = useState(() => window.location.search);
  const [filters, setFilters] = useState<Record<string, string>>({ sort_by: "last_activity_at", sort_dir: "desc" });
  const [keywords, setKeywords] = useState<string[]>([]);
  const [keywordInput, setKeywordInput] = useState("");
  const [strictTitle, setStrictTitle] = useState(false);
  const [mailOpen, setMailOpen] = useState(false);
  const [morningCrawlOpen, setMorningCrawlOpen] = useState(false);
  const [trendSelection, setTrendSelection] = useState<TrendSelection | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [subscribeOpen, setSubscribeOpen] = useState(false);
  const [trendsOpen, setTrendsOpen] = useState(false);
  const drawerTimerRef = useRef(0);
  const keywordInputRef = useRef<HTMLInputElement>(null);
  const searchParams = useMemo(() => new URLSearchParams(locationSearch), [locationSearch]);
  const openId = parseItemId(searchParams.get("item"));
  const page = parsePage(searchParams.get("page"));

  useEffect(() => {
    const syncLocation = () => setLocationSearch(window.location.search);
    window.addEventListener("popstate", syncLocation);
    return () => window.removeEventListener("popstate", syncLocation);
  }, []);

  useEffect(() => () => window.clearTimeout(drawerTimerRef.current), []);

  const openFilterDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setSubscribeOpen(false);
    setTrendsOpen(false);
    setFilterOpen(true);
  };

  const openSubscribeDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setFilterOpen(false);
    setTrendsOpen(false);
    setSubscribeOpen(true);
  };

  const openTrendsDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setFilterOpen(false);
    setSubscribeOpen(false);
    setTrendsOpen(true);
  };

  const openFilterAfterSubscribeRetract = () => {
    window.clearTimeout(drawerTimerRef.current);
    setTrendsOpen(false);
    if (subscribeOpen) {
      setSubscribeOpen(false);
      drawerTimerRef.current = window.setTimeout(() => setFilterOpen(true), DRAWER_HANDOFF_MS);
      return;
    }
    setFilterOpen(true);
  };

  const closeFilterDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setFilterOpen(false);
  };

  const closeSubscribeDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setSubscribeOpen(false);
  };

  const closeTrendsDrawer = () => {
    window.clearTimeout(drawerTimerRef.current);
    setTrendsOpen(false);
  };

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

  useEffect(() => {
    if (openId === null) return;
    const previousOverflow = document.body.style.overflow;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenId(null);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [openId]);

  const setFilter = (key: string, value: string) => {
    setPage(1);
    setFilters((f) => ({ ...f, [key]: value }));
  };

  const addKeyword = () => {
    const value = keywordInput.trim();
    if (!value) return;
    setPage(1);
    const hadTrendFilter = trendSelection !== null;
    setTrendSelection(null);
    setKeywords((current) => current.some((keyword) => keyword.toLocaleLowerCase() === value.toLocaleLowerCase())
      ? current
      : [...current, value]);
    setKeywordInput("");
    if (hadTrendFilter) {
      setFilters((current) => ({ ...current, item_ids: "" }));
    }
  };

  const removeKeyword = (keyword: string) => {
    setPage(1);
    setKeywords((current) => current.filter((value) => value !== keyword));
  };

  const applyTrendFilter = (trend: { resultId: string; topic: string; itemIds: number[] }) => {
    setPage(1);
    setOpenId(null);
    setTrendSelection({
      resultId: trend.resultId,
      itemId: null,
      label: trend.topic || "所选趋势",
      restoreQuery: null,
    });
    setFilters((f) => ({ ...f, item_ids: trend.itemIds.join(",") }));
  };

  const applyTrendSourceFilter = (source: { itemId: number; title: string }) => {
    setPage(1);
    setOpenId(null);
    setTrendSelection({
      resultId: null,
      itemId: source.itemId,
      label: source.title,
      restoreQuery: null,
    });
    setFilters((f) => ({ ...f, item_ids: String(source.itemId) }));
  };

  const clearTrendFilter = () => {
    setPage(1);
    setFilters((f) => ({ ...f, item_ids: "" }));
    setTrendSelection(null);
  };

  const effectiveFilters = filters;

  const params = useMemo(
    () => ({
      ...Object.fromEntries(Object.entries(effectiveFilters).filter(([, value]) => value)),
      ...(keywords.length > 0 ? { keywords } : {}),
      ...(strictTitle ? { strict_title: true } : {}),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [effectiveFilters, keywords, page, strictTitle],
  );
  // The trend carousel's exact item filter is an ephemeral browse action, so it
  // stays out of the mail task center's filter snapshot and estimate.
  const mailFilters = useMemo(
    () => ({
      ...Object.fromEntries(
        Object.entries(effectiveFilters).filter(([key, value]) => value && key !== "item_ids"),
      ),
      ...(keywords.length > 0 ? { keywords } : {}),
      ...(strictTitle ? { strict_title: true } : {}),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [effectiveFilters, keywords, page, strictTitle],
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

  const demoFilteredItems = useMemo(
    () => filterDemoItems(demoItems, { ...effectiveFilters, keywords, strict_title: strictTitle }),
    [effectiveFilters, keywords, strictTitle],
  );
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
  const openListItem = (listData?.items ?? []).find((item) => item.id === openId);
  const isDiscussionOpen = (selectedDemoItem ?? openListItem)?.item_kind === "discussion";

  const hasLiveEmptyState = mode === "live" && listData?.total === 0;
  const totalPages = Math.max(1, Math.ceil((listData?.total ?? 0) / PAGE_SIZE));
  const activeFilterCount = countActiveFilters(effectiveFilters) + (keywords.length > 0 ? 1 : 0);
  const crawlMethods = crawlMethodsQuery.data ?? [];
  const activeCrawlMethods = useMemo(
    () => crawlMethods.filter((method) => method.status === "active"),
    [crawlMethods],
  );
  const filterChips = [
    ...summarizeActiveFilters(effectiveFilters, activeCrawlMethods),
    ...(keywords.length > 0 ? [`关键词筛选${strictTitle ? "（仅标题）" : ""}：${keywords.join(" / ")}`] : []),
  ];
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
        <div
          role="search"
          aria-label="关键词筛选条件"
          onClick={() => keywordInputRef.current?.focus()}
          style={{
            flex: "1 1 420px",
            minWidth: 260,
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: 6,
            padding: "6px 8px",
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            background: "#fff",
            cursor: "text",
          }}
        >
          {keywords.map((keyword) => (
            <button
              key={keyword}
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                removeKeyword(keyword);
              }}
              title={`移除关键词：${keyword}`}
              style={{
                border: "1px solid #b2ddff",
                background: "#eff8ff",
                color: "#175cd3",
                borderRadius: 999,
                padding: "4px 8px",
                cursor: "pointer",
                fontSize: 12,
                lineHeight: 1.2,
              }}
            >
              {keyword} ×
            </button>
          ))}
          <input
            ref={keywordInputRef}
            placeholder={keywords.length === 0 ? "输入关键词后按回车：搜索标题、正文和技术热点" : "继续输入后按回车"}
            value={keywordInput}
            maxLength={INPUT_LIMITS.searchQuery}
            onChange={(e) => setKeywordInput(clampInput(e.target.value, INPUT_LIMITS.searchQuery))}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addKeyword();
                return;
              }
              if (event.key === "Backspace" && !keywordInput && keywords.length > 0) {
                event.preventDefault();
                removeKeyword(keywords[keywords.length - 1]);
              }
            }}
            style={{
              flex: "1 1 160px",
              minWidth: 120,
              border: "none",
              outline: "none",
              padding: "6px 6px",
              fontSize: 14,
              background: "transparent",
            }}
          />
        </div>
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
        <HomeFilterTrigger activeCount={activeFilterCount} onClick={openFilterDrawer} />
        <button
          type="button"
          aria-pressed={strictTitle}
          onClick={() => {
            setPage(1);
            setStrictTitle((current) => !current);
            void queryClient.invalidateQueries({ queryKey: ["items"] });
          }}
          className="home-filter-trigger"
          style={strictTitle ? { background: "#175cd3", borderColor: "#175cd3", color: "#fff" } : undefined}
          title="开启后，关键词只匹配新闻标题"
        >
          严格筛选
        </button>
        <button
          onClick={() => {
            setPage(1);
            setFilters({ sort_by: "last_activity_at", sort_dir: "desc" });
            setKeywords([]);
            setKeywordInput("");
            setStrictTitle(false);
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
    <div className="home-ops-card home-ops-card--mail">
      <div className="home-ops-card__head">
        <div className="home-ops-card__title">邮件任务中心</div>
        <span className="home-ops-card__badge" style={{ background: mailBadge.bg, color: mailBadge.color }}>
          {mailBadge.text}
        </span>
      </div>
      <div className="home-ops-card__stats">
        <div className="home-ops-card__stat">
          <div className="home-ops-card__stat-value">{templateCount}</div>
          <div className="home-ops-card__stat-label">模板</div>
        </div>
        <div className="home-ops-card__stat">
          <div className="home-ops-card__stat-value">{scheduleCount}</div>
          <div className="home-ops-card__stat-label">已预定</div>
        </div>
      </div>
      <div className="home-ops-card__kicker">当前筛选</div>
      {filterChips.length > 0 ? (
        <div className="home-ops-card__chips">
          {filterChips.map((chip) => (
            <span key={chip} className="home-ops-card__chip">{chip}</span>
          ))}
        </div>
      ) : (
        <div className="home-ops-card__empty">全部条目（未设置筛选）</div>
      )}
      <button
        className="home-ops-card__action home-ops-card__action--mail"
        onClick={() => {
          setMailOpen(true);
          openFilterAfterSubscribeRetract();
        }}
      >
        打开邮件任务中心
      </button>
    </div>
  );

  const morningCard = hasSystemAccess ? (
    <div className="home-ops-card home-ops-card--morning">
      <div className="home-ops-card__head">
        <div className="home-ops-card__title">系统定时抓取</div>
        <span className="home-ops-card__badge" style={{ background: morningStatusMeta.bg, color: morningStatusMeta.color }}>
          {morningStatusMeta.text}
        </span>
      </div>
      <div className="home-ops-card__stats">
        <div className="home-ops-card__stat">
          <div className="home-ops-card__stat-value">{morningDashboard?.active_method_count ?? 0}</div>
          <div className="home-ops-card__stat-label">爬取方式</div>
        </div>
        <div className="home-ops-card__stat">
          <div className="home-ops-card__stat-value">{morningDashboard?.today_run?.stored_count ?? 0}</div>
          <div className="home-ops-card__stat-label">今日入库</div>
        </div>
      </div>
      <div className="home-ops-card__kicker">下次执行</div>
      <div className="home-ops-card__empty" style={{ color: "#344054" }}>
        {morningDashboard?.config.next_run_at
          ? `${morningDashboard.config.next_run_at.split("T")[0]} ${morningDashboard.config.next_run_at.split("T")[1]?.slice(0, 5) ?? ""}（北京时间）`
          : "未排程"}
      </div>
      <button
        className="home-ops-card__action home-ops-card__action--morning"
        onClick={() => {
          closeSubscribeDrawer();
          closeTrendsDrawer();
          setMorningCrawlOpen(true);
        }}
      >
        打开系统定时抓取
      </button>
    </div>
  ) : null;

  const titleCard = (
    <header className="home-title-card">
      <div style={{ fontSize: 13, color: "#98a2b3", marginBottom: 10 }}>OS News Tracker</div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 16,
          alignItems: "flex-end",
          flexWrap: "wrap",
        }}
      >
        <div style={{ maxWidth: 720, minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 32, lineHeight: 1.2 }}>技术新闻追踪</h1>
          <p style={{ marginTop: 10, marginBottom: 0, color: "#d0d5dd" }}>
            汇总 OS、兼容性、安全与内部 AI 相关动态，支持搜索、筛选与详情查看。
          </p>
        </div>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ minWidth: 140, background: "#182230", borderRadius: 8, padding: 14 }}>
            <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>当前数据源</div>
            <div style={{ fontSize: 24, fontWeight: 700 }}>{mode === "demo" ? "演示" : "实时"}</div>
          </div>
          <div style={{ minWidth: 140, background: "#182230", borderRadius: 8, padding: 14 }}>
            <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>当前条目数</div>
            <div style={{ fontSize: 24, fontWeight: 700 }}>{listData?.total ?? 0}</div>
          </div>
          <div style={{ minWidth: 140, background: "#182230", borderRadius: 8, padding: 14 }}>
            <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>激活筛选</div>
            <div style={{ fontSize: 24, fontWeight: 700 }}>{activeFilterCount}</div>
          </div>
        </div>
      </div>
    </header>
  );

  return (
    <div className="home-shell">
      <div className="home-deck-host">
        <div className="home-news-stage">
          <div className="home-module__frame home-module__frame--news">
            <div className="home-module__stack">
              {titleCard}
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
        </div>

        <HomeSideDrawers
          filterOpen={filterOpen}
          subscribeOpen={subscribeOpen}
          trendsOpen={trendsOpen}
          filterActiveCount={activeFilterCount}
          trendsActiveCount={trendSelection ? 1 : 0}
          suppressOutsideClose={mailOpen || morningCrawlOpen}
          onOpenFilter={openFilterDrawer}
          onCloseFilter={closeFilterDrawer}
          onOpenSubscribe={openSubscribeDrawer}
          onCloseSubscribe={closeSubscribeDrawer}
          onOpenTrends={openTrendsDrawer}
          onCloseTrends={closeTrendsDrawer}
          filter={facetSidebar}
          subscribe={(
            <>
              {mailCard}
              {morningCard}
            </>
          )}
          trends={(
            <TrendCarousel
              onSelectTrend={applyTrendFilter}
              onSelectSource={applyTrendSourceFilter}
              activeResultId={trendSelection?.resultId ?? null}
              activeItemId={trendSelection?.itemId ?? null}
            />
          )}
        />
      </div>

      <EdgePageArrows page={page} totalPages={totalPages} onPageChange={setPage} hidden={openId !== null} />

      {openId !== null && (
        <div className="item-reader-overlay" onClick={() => setOpenId(null)}>
          <ItemDetail
            id={selectedLiveItemId}
            item={selectedDemoItem}
            wide={isDiscussionOpen}
            onClose={() => setOpenId(null)}
          />
        </div>
      )}

      <MailTaskCenter
        open={mailOpen}
        onClose={() => {
          setMailOpen(false);
          closeFilterDrawer();
        }}
        homeFilters={mailFilters}
      />
      {hasSystemAccess && <MorningCrawlModal open={morningCrawlOpen} onClose={() => setMorningCrawlOpen(false)} />}
    </div>
  );
}
