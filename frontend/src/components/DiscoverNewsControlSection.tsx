import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  cancelAgentRun,
  deleteAgentSource,
  fetchAgentRuns,
  fetchAgentSourceCandidates,
  fetchAgentSources,
  fetchNewsRunStatus,
  startNewsRun,
  stopNewsRun,
  triggerAgentRun,
  triggerAgentRunFromCandidate,
} from "../api/client";
import { AgentRunControl } from "./AgentRunControl";
import { buildManualNewsRunRequest, NewsRunControl } from "./NewsRunControl";
import { SourceManager } from "./SourceManager";
import {
  agentCandidateRunRefreshKeys,
  buildAgentWarmupRun,
  isAgentSourceRunning,
  isAgentWarmupResolved,
  isManualNewsRunActive,
} from "../pages/homeData";
import type { ManualNewsRunRequest, ManualNewsRunState } from "../types";
import type { NewsRunFormState } from "./NewsRunControl";

const AGENT_CANDIDATE_PAGE_SIZE = 5;

function formatUnknownAgentError(prefix: string, error: unknown) {
  if (error instanceof Error && error.message) {
    return `${prefix}: ${error.message}`;
  }
  return prefix;
}

interface DiscoverNewsControlSectionProps {
  runLimitState: NewsRunFormState;
  onRunLimitStateChange: Dispatch<SetStateAction<NewsRunFormState>>;
}

export function DiscoverNewsControlSection(props: DiscoverNewsControlSectionProps) {
  const { runLimitState, onRunLimitStateChange } = props;
  const [controlMode, setControlMode] = useState<"standard" | "agent">("standard");
  const [runActionError, setRunActionError] = useState<string | null>(null);
  const [runActionPending, setRunActionPending] = useState(false);
  const [agentCandidatePage, setAgentCandidatePage] = useState(1);
  const [agentTriggerPendingSourceId, setAgentTriggerPendingSourceId] = useState<number | null>(null);
  const [agentCancelPendingSourceId, setAgentCancelPendingSourceId] = useState<number | null>(null);
  const [agentDeletePendingSourceId, setAgentDeletePendingSourceId] = useState<number | null>(null);
  const [agentTriggerErrors, setAgentTriggerErrors] = useState<Record<number, string | null>>({});
  const [candidateTriggerPendingSourceId, setCandidateTriggerPendingSourceId] = useState<number | null>(null);
  const [candidateTriggerErrors, setCandidateTriggerErrors] = useState<Record<number, string | null>>({});
  const [agentPollingEnabled, setAgentPollingEnabled] = useState(false);
  const [agentWarmupSourceId, setAgentWarmupSourceId] = useState<number | null>(null);
  const queryClient = useQueryClient();
  const previousRunState = useRef<ManualNewsRunState | null>(null);
  const previousActiveAgentSourceIds = useRef<number[]>([]);

  const newsRunQuery = useQuery({
    queryKey: ["news-run"],
    queryFn: fetchNewsRunStatus,
    retry: false,
    refetchInterval: (query) => {
      return isManualNewsRunActive(query.state.data?.state) ? 2000 : false;
    },
  });
  const agentFeatureEnabled = true;

  const agentSourcesQuery = useQuery({
    queryKey: ["agent-sources"],
    queryFn: fetchAgentSources,
    enabled: agentFeatureEnabled && controlMode === "agent",
    retry: false,
    refetchInterval: () => (agentPollingEnabled || agentWarmupSourceId !== null) ? 2000 : false,
  });

  const agentCandidatesQuery = useQuery({
    queryKey: ["agent-source-candidates", agentCandidatePage, AGENT_CANDIDATE_PAGE_SIZE],
    queryFn: () => fetchAgentSourceCandidates(agentCandidatePage, AGENT_CANDIDATE_PAGE_SIZE),
    enabled: agentFeatureEnabled && controlMode === "agent",
    retry: false,
  });

  const agentRunsQueries = useQueries({
    queries: (agentSourcesQuery.data ?? []).map((source) => ({
      queryKey: ["agent-runs", source.id],
      queryFn: () => fetchAgentRuns(source.id),
      enabled: agentFeatureEnabled && controlMode === "agent",
      retry: false,
      refetchInterval: () => (agentPollingEnabled || agentWarmupSourceId !== null) ? 2000 : false,
    })),
  });

  const agentRunsBySourceId = useMemo(
    () =>
      Object.fromEntries(
        (agentSourcesQuery.data ?? []).map((source, index) => {
          const runs = agentRunsQueries[index]?.data;
          if ((!runs || runs.length === 0) && source.id === agentWarmupSourceId) {
            return [source.id, [buildAgentWarmupRun()]];
          }
          return [source.id, runs];
        }),
      ) as Record<number, typeof agentRunsQueries[number]["data"]>,
    [agentRunsQueries, agentSourcesQuery.data, agentWarmupSourceId],
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
    const totalPages = agentCandidatesQuery.data?.total_pages;
    if (totalPages && agentCandidatePage > totalPages) {
      setAgentCandidatePage(totalPages);
    }
  }, [agentCandidatePage, agentCandidatesQuery.data?.total_pages]);

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

  useEffect(() => {
    if (agentWarmupSourceId == null) return;
    if (isAgentWarmupResolved(agentRunsBySourceId[agentWarmupSourceId]?.[0])) {
      setAgentWarmupSourceId(null);
    }
  }, [agentRunsBySourceId, agentWarmupSourceId]);

  useEffect(() => {
    if (agentWarmupSourceId == null) return;
    const timer = window.setTimeout(() => setAgentWarmupSourceId(null), 10000);
    return () => window.clearTimeout(timer);
  }, [agentWarmupSourceId]);

  async function handleStartNewsRun(request: ManualNewsRunRequest) {
    setRunActionPending(true);
    setRunActionError(null);
    try {
      await startNewsRun(request);
      await newsRunQuery.refetch();
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
      const request = buildAgentCrawlRunRequest();
      if (!request) {
        setAgentTriggerErrors((state) => ({ ...state, [sourceId]: "抓取限制需要有效的时间范围和数量上限" }));
        return;
      }
      setAgentPollingEnabled(true);
      setAgentWarmupSourceId(sourceId);
      await triggerAgentRun(sourceId, request);
      await queryClient.invalidateQueries({ queryKey: ["agent-runs", sourceId] });
      await queryClient.invalidateQueries({ queryKey: ["agent-sources"] });
    } catch (error) {
      setAgentTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "触发 Agent Crawl 失败",
      }));
      setAgentWarmupSourceId(null);
    } finally {
      setAgentTriggerPendingSourceId(null);
    }
  }

  async function handleDeleteAgentSource(sourceId: number) {
    setAgentDeletePendingSourceId(sourceId);
    try {
      await deleteAgentSource(sourceId);
      await queryClient.invalidateQueries({ queryKey: ["agent-sources"] });
      await queryClient.invalidateQueries({ queryKey: ["agent-source-candidates"] });
    } catch (error) {
      setAgentTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "删除失败",
      }));
    } finally {
      setAgentDeletePendingSourceId(null);
    }
  }

  async function handleCancelAgentRun(sourceId: number, runId: number) {
    setAgentCancelPendingSourceId(sourceId);
    try {
      await cancelAgentRun(sourceId, runId);
      await queryClient.invalidateQueries({ queryKey: ["agent-runs", sourceId] });
    } catch (error) {
      setAgentTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "取消失败",
      }));
    } finally {
      setAgentCancelPendingSourceId(null);
    }
  }

  async function handleTriggerAgentRunFromCandidate(sourceId: number) {
    setCandidateTriggerPendingSourceId(sourceId);
    setCandidateTriggerErrors((state) => ({ ...state, [sourceId]: null }));
    try {
      const request = buildAgentCrawlRunRequest();
      if (!request) {
        setCandidateTriggerErrors((state) => ({ ...state, [sourceId]: "抓取限制需要有效的时间范围和数量上限" }));
        return;
      }
      const response = await triggerAgentRunFromCandidate(sourceId, request);
      setAgentPollingEnabled(true);
      setAgentWarmupSourceId(response.agent_source_id);
      if ((agentCandidatesQuery.data?.items.length ?? 0) === 1 && agentCandidatePage > 1) {
        setAgentCandidatePage((page) => Math.max(1, page - 1));
      }
      for (const queryKey of agentCandidateRunRefreshKeys(response.agent_source_id)) {
        await queryClient.invalidateQueries({ queryKey });
      }
      await queryClient.fetchQuery({
        queryKey: ["agent-runs", response.agent_source_id],
        queryFn: () => fetchAgentRuns(response.agent_source_id),
      });
    } catch (error) {
      setCandidateTriggerErrors((state) => ({
        ...state,
        [sourceId]: error instanceof ApiError ? error.message : "一键 Agent 运行失败",
      }));
      setAgentWarmupSourceId(null);
    } finally {
      setCandidateTriggerPendingSourceId(null);
    }
  }

  async function handleSourcesChanged() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["agent-sources"] }),
      queryClient.invalidateQueries({ queryKey: ["agent-source-candidates"] }),
      queryClient.invalidateQueries({ queryKey: ["items"] }),
      queryClient.invalidateQueries({ queryKey: ["facets"] }),
    ]);
  }

  function buildAgentCrawlRunRequest() {
    return buildManualNewsRunRequest(runLimitState);
  }

  const agentRunError = useMemo(() => {
    if (agentSourcesQuery.error instanceof ApiError) {
      return agentSourcesQuery.error.message;
    }
    if (agentSourcesQuery.error) {
      return formatUnknownAgentError("Agent source 列表加载失败", agentSourcesQuery.error);
    }
    if (agentCandidatesQuery.error instanceof ApiError) {
      return agentCandidatesQuery.error.message;
    }
    if (agentCandidatesQuery.error) {
      return formatUnknownAgentError("标准抓取来源加载失败", agentCandidatesQuery.error);
    }
    const firstError = agentRunsQueries.find((query) => query.error)?.error;
    if (firstError instanceof ApiError) {
      return firstError.message;
    }
    return firstError ? formatUnknownAgentError("Agent 运行记录加载失败", firstError) : null;
  }, [agentCandidatesQuery.error, agentRunsQueries, agentSourcesQuery.error]);

  return (
    <NewsRunControl
      mode={controlMode}
      onModeChange={setControlMode}
      sourceManagerContent={<SourceManager onSourcesChanged={handleSourcesChanged} />}
      agentContent={
        <AgentRunControl
          sources={agentSourcesQuery.data ?? []}
          candidatePage={agentCandidatesQuery.data ?? null}
          runsBySourceId={agentRunsBySourceId}
          isLoading={
            agentSourcesQuery.isLoading ||
            agentCandidatesQuery.isLoading ||
            agentRunsQueries.some((query) => query.isLoading)
          }
          errorMessage={agentRunError}
          triggerPendingSourceId={agentTriggerPendingSourceId}
          triggerErrors={agentTriggerErrors}
          cancelPendingSourceId={agentCancelPendingSourceId}
          deletePendingSourceId={agentDeletePendingSourceId}
          candidateTriggerPendingSourceId={candidateTriggerPendingSourceId}
          candidateTriggerErrors={candidateTriggerErrors}
          onTrigger={handleTriggerAgentRun}
          onCancel={handleCancelAgentRun}
          onDelete={handleDeleteAgentSource}
          onTriggerCandidate={handleTriggerAgentRunFromCandidate}
          onCandidatePageChange={setAgentCandidatePage}
        />
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
      formState={runLimitState}
      onFormStateChange={onRunLimitStateChange}
      showRunLimitPanel={false}
      onStart={handleStartNewsRun}
      onStop={handleStopNewsRun}
    />
  );
}
