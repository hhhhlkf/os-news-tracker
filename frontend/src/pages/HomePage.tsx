import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchFacets, fetchItems } from "../api/client";
import { FacetSidebar } from "../components/FacetSidebar";
import { ItemList } from "../components/ItemList";
import { ItemDetail } from "../components/ItemDetail";
import { MailTaskCenter } from "../components/MailTaskCenter";
import { MorningCrawlModal } from "../components/MorningCrawlModal";
import { fetchMailSchedules, fetchMailTemplates } from "../mail/api";
import { fetchMorningCrawlDashboard } from "../morningCrawl/api";
import { demoItems } from "../demoData";
import { buildDemoFacets, filterDemoItems, makeListResponse, resolveHomeDataMode } from "./homeData";

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

function summarizeActiveFilters(filters: Record<string, string>): string[] {
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
  return count;
}

export function HomePage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const [filters, setFilters] = useState<Record<string, string>>({ q: "", sort_by: "published_at", sort_dir: "desc" });
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const [mailOpen, setMailOpen] = useState(false);
  const [morningCrawlOpen, setMorningCrawlOpen] = useState(false);

  const setFilter = (key: string, value: string) => {
    setPage(1);
    setFilters((f) => ({ ...f, [key]: value }));
  };

  const params = useMemo(
    () => ({
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    [filters, page],
  );
  const itemsQuery = useQuery({
    queryKey: ["items", params],
    queryFn: () => fetchItems(params),
    retry: false,
  });
  const facetsQuery = useQuery({
    queryKey: ["facets"],
    queryFn: fetchFacets,
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

  const demoFilteredItems = useMemo(() => filterDemoItems(demoItems, filters), [filters]);
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
  const activeFilterCount = countActiveFilters(filters);
  const filterChips = summarizeActiveFilters(filters);
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

  return (
    <div style={{ minHeight: "100vh", background: "#f5f7fb" }}>
      <div style={{ maxWidth: 1280, margin: "0 auto", padding: 24 }}>
        <header
          style={{
            background: "#101828",
            color: "#f8fafc",
            borderRadius: 8,
            padding: 24,
            marginBottom: 20,
          }}
        >
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
            <div style={{ maxWidth: 720 }}>
              <h1 style={{ margin: 0, fontSize: 32, lineHeight: 1.2 }}>技术新闻追踪</h1>
              <p style={{ marginTop: 10, color: "#d0d5dd" }}>
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

        {mode === "demo" && (
          <div
            style={{
              marginBottom: 16,
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

        <section
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            background: "#fff",
            padding: 16,
            marginBottom: 16,
          }}
        >
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <input
              placeholder="搜索标题、摘要、分类…"
              value={filters.q ?? ""}
              onChange={(e) => setFilter("q", e.target.value)}
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
                const [sort_by, sort_dir] = e.target.value.split(":") as ["published_at" | "fetched_at", "desc" | "asc"];
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
              <option value="published_at:desc">发布时间 最新优先</option>
              <option value="published_at:asc">发布时间 最早优先</option>
              <option value="fetched_at:desc">入库时间 最新优先</option>
              <option value="fetched_at:asc">入库时间 最早优先</option>
            </select>
            <button
              onClick={() => {
                setPage(1);
                setFilters({ q: "", sort_by: "published_at", sort_dir: "desc" });
                setOpenId(null);
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
        </section>

        <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
          <div
            style={{
              width: 260,
              flexShrink: 0,
              display: "grid",
              gap: 16,
              alignContent: "start",
            }}
          >
            <div
              style={{
                border: "1px solid #bfd7ff",
                background: "linear-gradient(180deg,#f8fbff 0%,#ffffff 100%)",
                borderRadius: 8,
                padding: 14,
                minHeight: ENTRY_CARD_MIN_HEIGHT,
                display: "flex",
                flexDirection: "column",
              }}
            >
              <div style={{ fontSize: 14, fontWeight: 800, color: "#101828", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                邮件任务中心
                <span style={{ fontSize: 11, fontWeight: 700, borderRadius: 999, padding: "3px 8px", background: mailBadge.bg, color: mailBadge.color }}>
                  {mailBadge.text}
                </span>
              </div>

              <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "8px 10px" }}>
                  <div style={{ fontSize: 20, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{templateCount}</div>
                  <div style={{ fontSize: 11, color: "#667085", marginTop: 2 }}>模板</div>
                </div>
                <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "8px 10px" }}>
                  <div style={{ fontSize: 20, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{scheduleCount}</div>
                  <div style={{ fontSize: 11, color: "#667085", marginTop: 2 }}>已预定</div>
                </div>
              </div>

              <div style={{ fontSize: 11, fontWeight: 700, color: "#98a2b3", marginBottom: 6 }}>当前筛选</div>
              {filterChips.length > 0 ? (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {filterChips.map((chip) => (
                    <span
                      key={chip}
                      style={{
                        fontSize: 11,
                        color: "#344054",
                        background: "#eff6ff",
                        border: "1px solid #d3e3fb",
                        borderRadius: 6,
                        padding: "3px 7px",
                        wordBreak: "break-all",
                      }}
                    >
                      {chip}
                    </span>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: 12, color: "#98a2b3" }}>全部条目（未设置筛选）</div>
              )}

              <button
                onClick={() => setMailOpen(true)}
                style={{
                  marginTop: "auto",
                  paddingTop: 12,
                  width: "100%",
                  border: "none",
                  borderRadius: 8,
                  padding: "8px 14px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#fff",
                  background: "#175cd3",
                  cursor: "pointer",
                }}
              >
                打开邮件任务中心
              </button>
            </div>

            {hasSystemAccess && (
              <div
                style={{
                  border: "1px solid #cbd9ea",
                  background: "linear-gradient(180deg,#f7faff 0%,#ffffff 100%)",
                  borderRadius: 8,
                  padding: 14,
                  minHeight: ENTRY_CARD_MIN_HEIGHT,
                  display: "flex",
                  flexDirection: "column",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 800, color: "#101828", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                  系统定时抓取
                  <span style={{ fontSize: 11, fontWeight: 700, borderRadius: 999, padding: "3px 8px", background: morningStatusMeta.bg, color: morningStatusMeta.color }}>
                    {morningStatusMeta.text}
                  </span>
                </div>

                <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                  <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "8px 10px" }}>
                    <div style={{ fontSize: 20, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{morningDashboard?.active_method_count ?? 0}</div>
                    <div style={{ fontSize: 11, color: "#667085", marginTop: 2 }}>爬取方式</div>
                  </div>
                  <div style={{ flex: 1, background: "#fff", border: "1px solid #e4ebf5", borderRadius: 8, padding: "8px 10px" }}>
                    <div style={{ fontSize: 20, fontWeight: 800, color: "#101828", lineHeight: 1.1 }}>{morningDashboard?.today_run?.stored_count ?? 0}</div>
                    <div style={{ fontSize: 11, color: "#667085", marginTop: 2 }}>今日入库</div>
                  </div>
                </div>

                <div style={{ fontSize: 11, fontWeight: 700, color: "#98a2b3", marginBottom: 6 }}>下次执行</div>
                <div style={{ fontSize: 12, color: "#344054" }}>
                  {morningDashboard?.config.next_run_at
                    ? `${morningDashboard.config.next_run_at.split("T")[0]} ${morningDashboard.config.next_run_at.split("T")[1]?.slice(0, 5) ?? ""}（北京时间）`
                    : "未排程"}
                </div>

                <button
                  onClick={() => setMorningCrawlOpen(true)}
                  style={{
                    marginTop: "auto",
                    paddingTop: 12,
                    width: "100%",
                    border: "none",
                    borderRadius: 8,
                    padding: "8px 14px",
                    fontSize: 12,
                    fontWeight: 700,
                    color: "#fff",
                    background: "#0e7090",
                    cursor: "pointer",
                  }}
                >
                  打开系统定时抓取
                </button>
              </div>
            )}

            <FacetSidebar
              facets={facets ?? { main_category: [], info_type: [], importance: [], sub_tags: [] }}
              isLoading={mode === "live" && facetsQuery.isLoading}
              selected={filters}
              onSelect={setFilter}
            />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            {hasLiveEmptyState && (
              <div
                style={{
                  marginBottom: 16,
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
            <ItemList
              items={listData?.items ?? []}
              total={listData?.total ?? 0}
              page={page}
              pageSize={PAGE_SIZE}
              isLoading={mode === "live" && itemsQuery.isLoading}
              emptyMessage="没有匹配的条目，试试放宽搜索词或取消筛选条件。"
              sortBy={(filters.sort_by as "published_at" | "fetched_at") ?? "published_at"}
              onOpen={setOpenId}
              onPageChange={setPage}
            />
          </div>
        </div>

        {openId !== null && (
          <div onClick={() => setOpenId(null)} style={{
            position: "fixed", inset: 0, background: "rgba(15, 23, 42, 0.42)",
            display: "flex", justifyContent: "flex-end",
          }}>
            <div onClick={(e) => e.stopPropagation()} style={{
              width: 620, maxWidth: "92vw", background: "#fff",
              height: "100%", overflowY: "auto", boxShadow: "-24px 0 48px rgba(16, 24, 40, 0.16)",
            }}>
              <ItemDetail id={selectedLiveItemId} item={selectedDemoItem} />
            </div>
          </div>
        )}

        <MailTaskCenter open={mailOpen} onClose={() => setMailOpen(false)} homeFilters={params} />
        {hasSystemAccess && <MorningCrawlModal open={morningCrawlOpen} onClose={() => setMorningCrawlOpen(false)} />}
      </div>
    </div>
  );
}
