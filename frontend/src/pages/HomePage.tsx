import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchAgentRuns, fetchAgentSourceCandidates, fetchAgentSources, fetchFacets, fetchItems, fetchNewsRunLogs, fetchNewsRunStatus, startNewsRun, stopNewsRun, triggerAgentRun, triggerAgentRunFromCandidate } from "../api/client";
import { AgentRunControl } from "../components/AgentRunControl";
import { FacetSidebar } from "../components/FacetSidebar";
import { ItemList } from "../components/ItemList";
import { ItemDetail } from "../components/ItemDetail";
import { NewsRunControl } from "../components/NewsRunControl";
import { NewsRunLogPanel } from "../components/NewsRunLogPanel";
import { demoItems } from "../demoData";
import { buildDemoFacets, filterDemoItems, isAgentSourceRunning, isManualNewsRunActive, makeListResponse, resolveHomeDataMode } from "./homeData";
import type { ManualNewsRunRequest, ManualNewsRunState } from "../types";

const PAGE_SIZE = 10;

export function HomePage() {
  const [filters, setFilters] = useState<Record<string, string>>({ q: "", sort_by: "published_at", sort_dir: "desc" });
  const [page, setPage] = useState(1);
  const [openId, setOpenId] = useState<number | null>(null);
  const [controlMode, setControlMode] = useState<"standard" | "agent">("standard");
  const [runActionError, setRunActionError] = useState<string | null>(null);
  const [runActionPending, setRunActionPending] = useState(false);
  const [agentTriggerPendingSourceId, setAgentTriggerPendingSourceId] = useState<number | null>(null);
  const [agentTriggerErrors, setAgentTriggerErrors] = useState<Record<number, string | null>>({});
  const [candidateTriggerPendingSourceId, setCandidateTriggerPendingSourceId] = useState<number | null>(null);
  const [candidateTriggerErrors, setCandidateTriggerErrors] = useState<Record<number, string | null>>({});
  const [agentPollingEnabled, setAgentPollingEnabled] = useState(false);
  const queryClient = useQueryClient();
  const previousRunState = useRef<ManualNewsRunState | null>(null);
  const previousActiveAgentSourceIds = useRef<number[]>([]);

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

  const newsRunQuery = useQuery({
    queryKey: ["news-run"],
    queryFn: fetchNewsRunStatus,
    retry: false,
    refetchInterval: (query) => {
      return isManualNewsRunActive(query.state.data?.state) ? 2000 : false;
    },
  });
  const runIsActive = isManualNewsRunActive(newsRunQuery.data?.state);
  const itemsQuery = useQuery({
    queryKey: ["items", params],
    queryFn: () => fetchItems(params),
    retry: false,
    refetchInterval: () => runIsActive ? 2000 : false,
  });
  const facetsQuery = useQuery({
    queryKey: ["facets"],
    queryFn: fetchFacets,
    retry: false,
    refetchInterval: () => runIsActive ? 2000 : false,
  });
  const newsRunLogsQuery = useQuery({
    queryKey: ["news-run-logs"],
    queryFn: fetchNewsRunLogs,
    retry: false,
    refetchInterval: () => {
      return runIsActive ? 2000 : false;
    },
  });

  const mode = resolveHomeDataMode({
    itemsFailed: itemsQuery.isError,
    facetsFailed: facetsQuery.isError,
  });

  const agentSourcesQuery = useQuery({
    queryKey: ["agent-sources"],
    queryFn: fetchAgentSources,
    enabled: mode === "live" && controlMode === "agent",
    retry: false,
    refetchInterval: () => agentPollingEnabled ? 2000 : false,
  });

  const agentCandidatesQuery = useQuery({
    queryKey: ["agent-source-candidates"],
    queryFn: fetchAgentSourceCandidates,
    enabled: mode === "live" && controlMode === "agent",
    retry: false,
  });

  const agentRunsQueries = useQueries({
    queries: (agentSourcesQuery.data ?? []).map((source) => ({
      queryKey: ["agent-runs", source.id],
      queryFn: () => fetchAgentRuns(source.id),
      enabled: mode === "live" && controlMode === "agent",
      retry: false,
      refetchInterval: () => agentPollingEnabled ? 2000 : false,
    })),
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
  const activeFilterCount = Object.entries(filters).filter(([key, value]) => value && key !== "sort_by" && key !== "sort_dir").length;
  const agentRunsBySourceId = useMemo(
    () =>
      Object.fromEntries(
        (agentSourcesQuery.data ?? []).map((source, index) => [source.id, agentRunsQueries[index]?.data]),
      ) as Record<number, typeof agentRunsQueries[number]["data"]>,
    [agentRunsQueries, agentSourcesQuery.data],
  );
  const activeAgentSourceIds = useMemo(
    () =>
      (agentSourcesQuery.data ?? [])
        .filter((source) => isAgentSourceRunning(agentRunsBySourceId[source.id]?.[0]))
        .map((source) => source.id),
    [agentRunsBySourceId, agentSourcesQuery.data],
  );

  useEffect(() => {
    const nextState = newsRunQuery.data?.state ?? null;
    const previousState = previousRunState.current;
    if (
      previousState &&
      isManualNewsRunActive(previousState) &&
      nextState &&
      !isManualNewsRunActive(nextState)
    ) {
      void queryClient.invalidateQueries({ queryKey: ["items"] });
      void queryClient.invalidateQueries({ queryKey: ["facets"] });
    }
    previousRunState.current = nextState;
  }, [newsRunQuery.data?.state, queryClient]);

  useEffect(() => {
    const previous = previousActiveAgentSourceIds.current;
    const finishedSources = previous.filter((sourceId) => !activeAgentSourceIds.includes(sourceId));
    if (finishedSources.length > 0) {
      void queryClient.invalidateQueries({ queryKey: ["items"] });
      void queryClient.invalidateQueries({ queryKey: ["agent-sources"] });
    }
    previousActiveAgentSourceIds.current = activeAgentSourceIds;
    setAgentPollingEnabled(activeAgentSourceIds.length > 0);
  }, [activeAgentSourceIds, queryClient]);

  async function handleStartNewsRun(request: ManualNewsRunRequest) {
    setRunActionPending(true);
    setRunActionError(null);
    try {
      await startNewsRun(request);
      await newsRunQuery.refetch();
      await newsRunLogsQuery.refetch();
    } catch (error) {
      setRunActionError(
        error instanceof ApiError ? error.message : "启动新闻处理任务失败",
      );
    } finally {
      setRunActionPending(false);
    }
  }

  async function handleStopNewsRun() {
    setRunActionPending(true);
    setRunActionError(null);
    try {
      await stopNewsRun();
      await newsRunQuery.refetch();
    } catch (error) {
      setRunActionError(
        error instanceof ApiError ? error.message : "停止新闻处理任务失败",
      );
    } finally {
      setRunActionPending(false);
    }
  }

  async function handleTriggerAgentRun(sourceId: number) {
    setAgentTriggerPendingSourceId(sourceId);
    setAgentTriggerErrors((state) => ({ ...state, [sourceId]: null }));
    try {
      await triggerAgentRun(sourceId);
      setAgentPollingEnabled(true);
      await queryClient.invalidateQueries({ queryKey: ["agent-runs", sourceId] });
      await queryClient.invalidateQueries({ queryKey: ["agent-sources"] });
    } catch (error) {
      setAgentTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "触发 Agent Crawl 失败",
      }));
    } finally {
      setAgentTriggerPendingSourceId(null);
    }
  }

  async function handleTriggerAgentRunFromCandidate(sourceId: number) {
    setCandidateTriggerPendingSourceId(sourceId);
    setCandidateTriggerErrors((state) => ({ ...state, [sourceId]: null }));
    try {
      await triggerAgentRunFromCandidate(sourceId);
      setAgentPollingEnabled(true);
      await queryClient.invalidateQueries({ queryKey: ["agent-sources"] });
      await queryClient.invalidateQueries({ queryKey: ["agent-source-candidates"] });
      await queryClient.invalidateQueries({ queryKey: ["items"] });
    } catch (error) {
      setCandidateTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "一键 Agent 运行失败",
      }));
    } finally {
      setCandidateTriggerPendingSourceId(null);
    }
  }

  const agentRunError = useMemo(() => {
    if (agentSourcesQuery.error instanceof ApiError) {
      return agentSourcesQuery.error.message;
    }
    if (agentCandidatesQuery.error instanceof ApiError) {
      return agentCandidatesQuery.error.message;
    }
    const firstError = agentRunsQueries.find((query) => query.error instanceof ApiError)?.error;
    return firstError instanceof ApiError ? firstError.message : null;
  }, [agentCandidatesQuery.error, agentRunsQueries, agentSourcesQuery.error]);

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

        <NewsRunControl
          mode={controlMode}
          onModeChange={setControlMode}
          agentContent={
            mode === "demo" ? (
              <div style={{ fontSize: 13, color: "#667085" }}>演示模式下不连接 Agent Crawl 接口。</div>
            ) : (
              <AgentRunControl
                sources={agentSourcesQuery.data ?? []}
                candidateSources={agentCandidatesQuery.data ?? []}
                runsBySourceId={agentRunsBySourceId}
                isLoading={
                  agentSourcesQuery.isLoading ||
                  agentCandidatesQuery.isLoading ||
                  agentRunsQueries.some((query) => query.isLoading)
                }
                errorMessage={agentRunError}
                triggerPendingSourceId={agentTriggerPendingSourceId}
                triggerErrors={agentTriggerErrors}
                candidateTriggerPendingSourceId={candidateTriggerPendingSourceId}
                candidateTriggerErrors={candidateTriggerErrors}
                onTrigger={handleTriggerAgentRun}
                onTriggerCandidate={handleTriggerAgentRunFromCandidate}
              />
            )
          }
          status={newsRunQuery.data}
          isLoading={newsRunQuery.isLoading}
          errorMessage={
            controlMode === "agent"
              ? agentRunError
              : (
                runActionError ??
                (newsRunQuery.error instanceof ApiError ? newsRunQuery.error.message : null)
              )
          }
          isSubmitting={runActionPending}
          onStart={handleStartNewsRun}
          onStop={handleStopNewsRun}
        />

        {controlMode === "standard" && (
          <NewsRunLogPanel
            logs={newsRunLogsQuery.data?.logs ?? []}
            isLoading={newsRunLogsQuery.isLoading}
          />
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
          <FacetSidebar
            facets={facets ?? { main_category: [], info_type: [], importance: [], sub_tags: [] }}
            isLoading={mode === "live" && facetsQuery.isLoading}
            selected={filters}
            onSelect={setFilter}
          />
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
      </div>
    </div>
  );
}
