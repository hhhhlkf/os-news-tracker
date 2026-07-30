import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  backfillTrendNewsCards,
  backfillTrendVectors,
  fetchLatestTrendResults,
  fetchTrendCardList,
  fetchTrendCardStageStatus,
  fetchTrendCandidateClusterList,
  fetchTrendClusterStageStatus,
  fetchTrendRunStatus,
  fetchTrendStorylineReviews,
  fetchTrendStorylines,
  fetchTrendStorylineStageStatus,
  fetchTrendVectorStageStatus,
  runTrendCandidateClustering,
  reviewTrendCandidateClusters,
  startTrendRun,
} from "./api";
import { TrendProcessFlowChart, type TrendFlowNodeState } from "./TrendProcessFlowChart";
import type {
  TrendCandidateClusterListItem,
  TrendCardListItem,
  TrendCardListStatus,
  TrendCardStageStatus,
  TrendCategory,
  TrendClusterStageStatus,
  TrendLatestResults,
  TrendResult,
  TrendRunStageStatus,
  TrendStoryline,
  TrendStorylineReview,
  TrendStorylineReviewFilter,
  TrendStorylineStageStatus,
} from "./types";

const cardStatusQueryKey = ["trends", "cards", "status"] as const;
const cardListQueryKey = ["trends", "cards", "list"] as const;
const vectorStatusQueryKey = ["trends", "vectors", "status"] as const;
const clusterStatusQueryKey = ["trends", "clusters", "status"] as const;
const clusterListQueryKey = ["trends", "clusters", "list"] as const;
const storylineStatusQueryKey = ["trends", "storylines", "status"] as const;
const storylineReviewsQueryKey = ["trends", "storylines", "reviews"] as const;
const storylinesQueryKey = ["trends", "storylines", "list"] as const;
const trendRunStatusQueryKey = ["trends", "runs", "status"] as const;
const trendResultsQueryKey = ["trends", "results", "latest"] as const;
const cardListPageSize = 3;
const collapseStorageKey = "os-news-tracker:trends:flow-panel-collapsed";

type TrendStageId = 1 | 2 | 3 | 4;

const stages: Array<{ id: TrendStageId; name: string; description: string }> = [
  { id: 1, name: "新闻卡片生成", description: "补齐当前时间范围内缺失的新闻解释卡。" },
  { id: 2, name: "聚类", description: "按语义相似度组织候选事件簇。" },
  { id: 3, name: "故事线生成", description: "审查候选簇并沉淀可持续跟踪的故事线。" },
  { id: 4, name: "趋势总结", description: "结合身份模板生成并展示趋势结果。" },
];

function cardStageLabel(status: TrendCardStageStatus | undefined): string {
  if (!status) return "未开始";
  if (status.is_running) return "生成中";
  if (status.failed_count > 0 || status.error_message) return "部分失败";
  if (status.pending_count > 0) return "待生成";
  if (status.generated_count > 0 || status.skipped_count > 0) return "已完成";
  return "未开始";
}

function clusterStageLabel(status: TrendClusterStageStatus | undefined): string {
  if (!status) return "未开始";
  if (status.is_running) return "聚类中";
  if (status.error_message || status.vector_failed_count > 0) return "部分失败";
  if (status.pending_vector_count > 0) return "待向量";
  if (status.candidate_cluster_count > 0 || status.generated_vector_count > 0) return "已完成";
  return "未开始";
}

function storylineStageLabel(status: TrendStorylineStageStatus | undefined): string {
  if (!status) return "未开始";
  if (status.is_running) return "审查中";
  if (status.failed_count > 0 || status.error_message) return "部分失败";
  if (status.pending_count > 0) return "待审查";
  if (status.accepted_count + status.split_count + status.rejected_count > 0) return "已完成";
  return "未开始";
}

function trendStageLabel(status: TrendRunStageStatus | undefined): string {
  if (!status) return "未开始";
  if (status.is_running) return "总结中";
  if (status.status === "failed" || status.error_message) return "失败";
  if (status.status === "succeeded") return "已完成";
  if (status.reusable) return "已复用";
  if (status.status === "pending" || status.status === "running") return "总结中";
  return "未开始";
}

function trendFlowNodeState(label: string): TrendFlowNodeState {
  if (label === "生成中" || label === "聚类中" || label === "审查中" || label === "总结中") return "running";
  if (label === "已完成" || label === "已复用") return "done";
  if (label === "部分失败" || label === "失败") return "failed";
  return "pending";
}

function readCollapsedState(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(collapseStorageKey) === "true";
}

export function TrendFlow({ selectedTemplateId }: { selectedTemplateId: string | null }) {
  const [isCollapsed, setIsCollapsed] = useState(readCollapsedState);
  const toggleCollapsed = () => {
    setIsCollapsed((collapsed) => {
      const next = !collapsed;
      window.localStorage.setItem(collapseStorageKey, String(next));
      return next;
    });
  };
  const [selectedStageId, setSelectedStageId] = useState<TrendStageId>(1);
  const [cardListStatus, setCardListStatus] = useState<TrendCardListStatus | undefined>();
  const [cardListPage, setCardListPage] = useState(1);
  const [clusterListPage, setClusterListPage] = useState(1);
  const [storylineReviewPage, setStorylineReviewPage] = useState(1);
  const [storylineReviewFilter, setStorylineReviewFilter] = useState<TrendStorylineReviewFilter | undefined>();
  const [storylinePage, setStorylinePage] = useState(1);
  const [dismissedTrendMessageKey, setDismissedTrendMessageKey] = useState<string | null>(null);
  const [dismissedTrendFailureRunId, setDismissedTrendFailureRunId] = useState<string | null>(null);
  const wasCardTaskRunning = useRef(false);
  const wasStorylineTaskRunning = useRef(false);
  const wasTrendTaskRunning = useRef(false);
  const queryClient = useQueryClient();
  const statusQuery = useQuery({
    queryKey: cardStatusQueryKey,
    queryFn: fetchTrendCardStageStatus,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });
  const backfillMutation = useMutation({
    mutationFn: () => backfillTrendNewsCards(),
    onSuccess: async (status) => {
      queryClient.setQueryData(cardStatusQueryKey, status);
      await queryClient.invalidateQueries({ queryKey: cardListQueryKey });
    },
  });
  const vectorStatusQuery = useQuery({
    queryKey: vectorStatusQueryKey,
    queryFn: fetchTrendVectorStageStatus,
    enabled: selectedStageId === 2,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });
  const clusterStatusQuery = useQuery({
    queryKey: clusterStatusQueryKey,
    queryFn: fetchTrendClusterStageStatus,
    enabled: selectedStageId === 2,
    refetchInterval: () => (vectorStatusQuery.data?.is_running ? 2000 : false),
  });
  const vectorMutation = useMutation({
    mutationFn: backfillTrendVectors,
    onSuccess: async (status) => {
      queryClient.setQueryData(vectorStatusQueryKey, status);
      await queryClient.invalidateQueries({ queryKey: clusterStatusQueryKey });
    },
  });
  const clusterMutation = useMutation({
    mutationFn: runTrendCandidateClustering,
    onSuccess: async (status) => {
      queryClient.setQueryData(clusterStatusQueryKey, status);
      await queryClient.invalidateQueries({ queryKey: vectorStatusQueryKey });
      await queryClient.invalidateQueries({ queryKey: clusterListQueryKey });
    },
  });
  const storylineStatusQuery = useQuery({
    queryKey: storylineStatusQueryKey,
    queryFn: fetchTrendStorylineStageStatus,
    enabled: selectedStageId === 3,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });
  const storylineReviewMutation = useMutation({
    mutationFn: reviewTrendCandidateClusters,
    onSuccess: async (status) => {
      queryClient.setQueryData(storylineStatusQueryKey, status);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: storylineReviewsQueryKey }),
        queryClient.invalidateQueries({ queryKey: storylinesQueryKey }),
      ]);
    },
  });

  const cardStatus = statusQuery.data;
  const isBusy = Boolean(cardStatus?.is_running || backfillMutation.isPending);
  const isCardTaskRunning = cardStatus?.is_running ?? false;
  useEffect(() => {
    if (wasCardTaskRunning.current && !isCardTaskRunning) {
      void queryClient.invalidateQueries({ queryKey: cardListQueryKey });
    }
    wasCardTaskRunning.current = isCardTaskRunning;
  }, [isCardTaskRunning, queryClient]);
  useEffect(() => {
    setCardListPage(1);
  }, [cardListStatus]);
  const cardListQuery = useQuery({
    queryKey: [...cardListQueryKey, cardListStatus, cardListPage] as const,
    queryFn: () => fetchTrendCardList({
      status: cardListStatus,
      offset: (cardListPage - 1) * cardListPageSize,
      limit: cardListPageSize,
    }),
    enabled: selectedStageId === 1,
    refetchInterval: cardStatus?.is_running ? 2000 : false,
    placeholderData: keepPreviousData,
  });
  const cardListItems = cardListQuery.data?.items ?? [];
  const cardListTotal = cardListQuery.data?.total ?? 0;
  const cardListPageCount = Math.max(1, Math.ceil(cardListTotal / cardListPageSize));
  useEffect(() => {
    if (cardListQuery.data && cardListPage > cardListPageCount) {
      setCardListPage(cardListPageCount);
    }
  }, [cardListPage, cardListPageCount, cardListQuery.data]);
  const error = (statusQuery.error ?? backfillMutation.error) as Error | null;
  const hasNoSuccessfulCards = Boolean(
    cardStatus &&
      cardStatus.generated_count === 0,
  );
  const stageLabel = cardStageLabel(cardStatus);
  const clusterStatus = clusterStatusQuery.data;
  const clusterLabel = clusterStageLabel(clusterStatus);
  const storylineStatus = storylineStatusQuery.data;
  const storylineLabel = storylineStageLabel(storylineStatus);
  const isStorylineTaskRunning = storylineStatus?.is_running ?? false;
  useEffect(() => {
    if (wasStorylineTaskRunning.current && !isStorylineTaskRunning) {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: storylineStatusQueryKey }),
        queryClient.invalidateQueries({ queryKey: storylineReviewsQueryKey }),
        queryClient.invalidateQueries({ queryKey: storylinesQueryKey }),
      ]);
    }
    wasStorylineTaskRunning.current = isStorylineTaskRunning;
  }, [isStorylineTaskRunning, queryClient]);
  useEffect(() => {
    setClusterListPage(1);
  }, [clusterStatus?.candidate_cluster_count]);
  const clusterListQuery = useQuery({
    queryKey: [...clusterListQueryKey, clusterListPage] as const,
    queryFn: () => fetchTrendCandidateClusterList({
      offset: (clusterListPage - 1) * cardListPageSize,
      limit: cardListPageSize,
    }),
    enabled: selectedStageId === 2,
    placeholderData: keepPreviousData,
  });
  const clusterListItems = clusterListQuery.data?.items ?? [];
  const clusterListTotal = clusterListQuery.data?.total ?? 0;
  const clusterListPageCount = Math.max(1, Math.ceil(clusterListTotal / cardListPageSize));
  useEffect(() => {
    if (clusterListQuery.data && clusterListPage > clusterListPageCount) {
      setClusterListPage(clusterListPageCount);
    }
  }, [clusterListPage, clusterListPageCount, clusterListQuery.data]);
  useEffect(() => {
    setStorylineReviewPage(1);
  }, [storylineReviewFilter]);
  const storylineReviewsQuery = useQuery({
    queryKey: [...storylineReviewsQueryKey, storylineReviewFilter, storylineReviewPage] as const,
    queryFn: () => fetchTrendStorylineReviews({
      offset: (storylineReviewPage - 1) * cardListPageSize,
      limit: cardListPageSize,
      status: storylineReviewFilter,
    }),
    enabled: selectedStageId === 3,
    refetchInterval: storylineStatus?.is_running ? 2000 : false,
    placeholderData: keepPreviousData,
  });
  const storylinesQuery = useQuery({
    queryKey: [...storylinesQueryKey, storylinePage] as const,
    queryFn: () => fetchTrendStorylines({ offset: (storylinePage - 1) * cardListPageSize, limit: cardListPageSize }),
    enabled: selectedStageId === 3,
    refetchInterval: storylineStatus?.is_running ? 2000 : false,
    placeholderData: keepPreviousData,
  });
  const trendRunStatusQuery = useQuery({
    queryKey: [...trendRunStatusQueryKey, selectedTemplateId] as const,
    queryFn: () => fetchTrendRunStatus(selectedTemplateId!),
    enabled: selectedStageId === 4 && Boolean(selectedTemplateId),
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });
  const trendRunMutation = useMutation({
    mutationFn: () => startTrendRun(selectedTemplateId!),
    onSuccess: async (status) => {
      queryClient.setQueryData([...trendRunStatusQueryKey, selectedTemplateId], status);
      await queryClient.invalidateQueries({ queryKey: [...trendResultsQueryKey, selectedTemplateId] });
    },
  });
  const trendResultsQuery = useQuery({
    queryKey: [...trendResultsQueryKey, selectedTemplateId] as const,
    queryFn: () => fetchLatestTrendResults(selectedTemplateId!),
    enabled: selectedStageId === 4 && Boolean(selectedTemplateId),
    refetchInterval: trendRunStatusQuery.data?.is_running ? 2000 : false,
  });
  const selectedStage = stages.find((stage) => stage.id === selectedStageId) ?? stages[0];
  const trendStatus = trendRunStatusQuery.data;
  const trendLabel = trendStageLabel(trendStatus);
  const flowStages = stages.map((stage) => {
    const statusLabel = stage.id === 1 ? stageLabel
      : stage.id === 2 ? clusterLabel
        : stage.id === 3 ? storylineLabel : trendLabel;
    return { ...stage, statusLabel, state: trendFlowNodeState(statusLabel) };
  });
  const isTrendTaskRunning = trendStatus?.is_running ?? false;
  const trendMessageKey = trendStatus?.message ? `${trendStatus.run_id ?? "none"}:${trendStatus.status}:${trendStatus.message}` : null;
  const trendFailureStorageKey = selectedTemplateId
    ? `os-news-tracker:trends:dismissed-failure:${selectedTemplateId}`
    : null;
  const trendFailureDismissed = Boolean(
    trendStatus?.run_id && (
      dismissedTrendFailureRunId === trendStatus.run_id
      || (typeof window !== "undefined" && trendFailureStorageKey && window.localStorage.getItem(trendFailureStorageKey) === trendStatus.run_id)
    ),
  );
  const dismissTrendFailure = () => {
    if (!trendStatus?.run_id) return;
    if (trendFailureStorageKey) window.localStorage.setItem(trendFailureStorageKey, trendStatus.run_id);
    setDismissedTrendFailureRunId(trendStatus.run_id);
  };
  useEffect(() => {
    if (wasTrendTaskRunning.current && !isTrendTaskRunning) {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: [...trendRunStatusQueryKey, selectedTemplateId] }),
        queryClient.invalidateQueries({ queryKey: [...trendResultsQueryKey, selectedTemplateId] }),
      ]);
    }
    wasTrendTaskRunning.current = isTrendTaskRunning;
  }, [isTrendTaskRunning, queryClient, selectedTemplateId]);

  return (
    <section style={panel}>
      <div style={header}>
        <div>
          <div style={sectionTitle}>趋势生成流程</div>
          <div style={sectionCopy}>
            选择流程步骤，在下方查看该阶段的状态、处理明细和结果列表。
          </div>
        </div>
        <button
          type="button"
          style={collapseButton}
          onClick={toggleCollapsed}
          aria-expanded={!isCollapsed}
          aria-label={isCollapsed ? "展开趋势生成流程" : "收起趋势生成流程"}
        >
          {isCollapsed ? "展开" : "收起"}
        </button>
      </div>

      {!isCollapsed && <>
      <div style={flowViewport}>
        <TrendProcessFlowChart stages={flowStages} selectedStageId={selectedStageId} onSelectStage={setSelectedStageId} />
      </div>

      <div style={stageDetail}>
        <div style={summaryHeader}>
          <div>
            <div style={summaryTitle}>{selectedStage.name}</div>
            <div style={summaryCopy}>{selectedStage.description}</div>
          </div>
          {selectedStageId === 1 && (
            <button
              type="button"
              style={isBusy ? disabledButton : startButton}
              disabled={isBusy || statusQuery.isLoading}
              onClick={() => backfillMutation.mutate()}
            >
              {isBusy ? "卡片生成中…" : "补齐缺失卡片"}
            </button>
          )}
          {selectedStageId === 3 && (
            <button
              type="button"
              style={storylineStatus?.is_running || storylineReviewMutation.isPending ? disabledButton : startButton}
              disabled={Boolean(storylineStatus?.is_running || storylineReviewMutation.isPending)}
              onClick={() => storylineReviewMutation.mutate()}
            >
              {storylineStatus?.is_running ? "故事线审查中…" : "审查候选簇"}
            </button>
          )}
          {selectedStageId === 4 && (
            <button
              type="button"
              style={
                !selectedTemplateId || trendStatus?.is_running || trendRunMutation.isPending
                  ? disabledButton
                  : startButton
              }
              disabled={Boolean(!selectedTemplateId || trendStatus?.is_running || trendRunMutation.isPending)}
              onClick={() => trendRunMutation.mutate()}
            >
              {trendStatus?.is_running ? "趋势总结中…" : "运行趋势总结"}
            </button>
          )}
        </div>

        {selectedStageId === 1 && (
          <>
            {statusQuery.isLoading && <div style={loading}>正在读取新闻卡片状态…</div>}
            {error && (
              <div style={errorBox}>
                <div style={errorTitle}>新闻卡片阶段请求失败</div>
                <div>{error.message}</div>
                <div style={errorAction}>请确认后端与 LLM 服务可用，然后再次点击“补齐缺失卡片”。</div>
              </div>
            )}

            {cardStatus && (
              <>
                <div style={rangeMeta}>
                  {cardStatus.start_date} 至 {cardStatus.end_date}
                  <span style={rangeDivider}>·</span>
                  Prompt {cardStatus.card_prompt_version}
                </div>
                <div style={countGrid}>
                  <Count label="待生成" value={cardStatus.pending_count} />
                  <Count label="已生成" value={cardStatus.generated_count} />
                  <Count label="已跳过" value={cardStatus.skipped_count} />
                  <Count label="失败" value={cardStatus.failed_count} danger={cardStatus.failed_count > 0} />
                </div>
              </>
            )}

            {hasNoSuccessfulCards && (
              <div style={emptyState}>
                <div style={emptyTitle}>当前范围内尚未生成任何新闻解释卡</div>
                <div>
                  {cardStatus && cardStatus.pending_count > 0
                    ? `还有 ${cardStatus.pending_count} 条新闻待生成，点击“补齐缺失卡片”开始处理。`
                    : cardStatus && (cardStatus.skipped_count > 0 || cardStatus.failed_count > 0)
                      ? `已有 ${cardStatus.skipped_count} 条因正文不足被跳过、${cardStatus.failed_count} 条生成失败，当前没有成功卡片。`
                      : "当前时间范围内没有可补齐的新闻，或尚未触发卡片任务。"}
                </div>
              </div>
            )}

            {cardStatus && (cardStatus.failed_count > 0 || cardStatus.error_message) && (
              <div style={failureBox}>
                <div style={failureTitle}>有 {cardStatus.failed_count} 张卡片生成失败，失败项保持可重试。</div>
                {cardStatus.error_message && <div style={failureDetail}>{cardStatus.error_message}</div>}
                <div style={failureAction}>
                  {cardStatus.retry_guidance ?? "检查 LLM 服务和输出格式后，再次点击“补齐缺失卡片”。"}
                </div>
              </div>
            )}

            <CardList
              status={cardListStatus}
              total={cardListTotal}
              items={cardListItems}
              isLoading={cardListQuery.isLoading}
              isFetching={cardListQuery.isFetching}
              error={cardListQuery.error as Error | null}
              onStatusChange={setCardListStatus}
              page={cardListPage}
              pageCount={cardListPageCount}
              onPageChange={setCardListPage}
            />
          </>
        )}

        {selectedStageId === 2 && (
          <>
            <div style={buttonGroup}>
              <button
                type="button"
                style={vectorStatusQuery.data?.is_running || vectorMutation.isPending ? disabledButton : startButton}
                disabled={Boolean(vectorStatusQuery.data?.is_running || vectorMutation.isPending)}
                onClick={() => vectorMutation.mutate()}
              >
                {vectorStatusQuery.data?.is_running ? "向量生成中…" : "补齐事件核心向量"}
              </button>
              <button
                type="button"
                style={clusterMutation.isPending || clusterStatus?.pending_vector_count ? disabledButton : startButton}
                disabled={Boolean(clusterMutation.isPending || clusterStatus?.pending_vector_count)}
                onClick={() => clusterMutation.mutate()}
              >
                {clusterMutation.isPending ? "候选簇生成中…" : "生成候选簇"}
              </button>
            </div>
            {(vectorStatusQuery.isLoading || clusterStatusQuery.isLoading) && <div style={loading}>正在读取向量与聚类状态…</div>}
            {(vectorStatusQuery.error || clusterStatusQuery.error || vectorMutation.error || clusterMutation.error) && (
              <div style={errorBox}>
                <div style={errorTitle}>聚类阶段请求失败</div>
                <div>{((vectorStatusQuery.error ?? clusterStatusQuery.error ?? vectorMutation.error ?? clusterMutation.error) as Error).message}</div>
                <div style={errorAction}>先确认 Embedding Worker 已就绪、向量版本一致，再重新执行。</div>
              </div>
            )}
            {clusterStatus && (
              <>
                <div style={rangeMeta}>
                  {clusterStatus.start_date} 至 {clusterStatus.end_date}
                  <span style={rangeDivider}>·</span>
                  G={clusterStatus.candidate_goal}
                  <span style={rangeDivider}>·</span>
                  {clusterStatus.used_threshold === null ? "尚未选择阈值" : `阈值 ${clusterStatus.used_threshold.toFixed(2)}`}
                </div>
                <div style={countGrid}>
                  <Count label="待生成向量" value={clusterStatus.pending_vector_count} />
                  <Count label="已生成向量" value={clusterStatus.generated_vector_count} />
                  <Count label="候选簇" value={clusterStatus.candidate_cluster_count} />
                  <Count label="待匹配卡片" value={clusterStatus.pending_match_count} />
                </div>
                {(clusterStatus.error_message || clusterStatus.vector_failed_count > 0) && (
                  <div style={failureBox}>
                    <div style={failureTitle}>向量任务有 {clusterStatus.vector_failed_count} 条失败，候选簇不会使用不完整或混合版本的向量。</div>
                    {clusterStatus.error_message && <div style={failureDetail}>{clusterStatus.error_message}</div>}
                    <div style={failureAction}>{clusterStatus.retry_guidance ?? "修复 Embedding Worker 后重新补齐向量。"}</div>
                  </div>
                )}
                {clusterStatus.candidate_cluster_count === 0 && !clusterStatus.pending_vector_count && (
                  <div style={emptyState}>
                    <div style={emptyTitle}>当前范围内没有候选簇</div>
                    <div>未成簇卡片会保留为待匹配卡片，未来 90 天内的新卡片仍会与它们继续聚类。</div>
                  </div>
                )}
                {clusterStatus.candidate_cluster_count > 0 && (
                  <CandidateClusterList
                    total={clusterListTotal}
                    offset={clusterListQuery.data?.offset ?? 0}
                    items={clusterListItems}
                    isLoading={clusterListQuery.isLoading}
                    isFetching={clusterListQuery.isFetching}
                    error={clusterListQuery.error as Error | null}
                    page={clusterListPage}
                    pageCount={clusterListPageCount}
                    onPageChange={setClusterListPage}
                  />
                )}
              </>
            )}
          </>
        )}

        {selectedStageId === 3 && (
          <>
            {storylineStatusQuery.isLoading && <div style={loading}>正在读取故事线审查状态…</div>}
            {(storylineStatusQuery.error || storylineReviewMutation.error) && (
              <div style={errorBox}>
                <div style={errorTitle}>故事线审查请求失败</div>
                <div>{((storylineStatusQuery.error ?? storylineReviewMutation.error) as Error).message}</div>
                <div style={errorAction}>确认候选簇、LLM 服务与 JSON 输出契约后再次触发。</div>
              </div>
            )}
            {storylineStatus && (
              <>
                <div style={rangeMeta}>{storylineStatus.start_date} 至 {storylineStatus.end_date}<span style={rangeDivider}>·</span>版本 {storylineStatus.embedding_version}</div>
                <div style={countGrid}>
                  <Count label="待审查簇" value={storylineStatus.pending_count} />
                  <Count label="已接受" value={storylineStatus.accepted_count} />
                  <Count label="已拆分" value={storylineStatus.split_count} />
                  <Count label="已拒绝" value={storylineStatus.rejected_count} />
                  <Count label="失败" value={storylineStatus.failed_count} danger={storylineStatus.failed_count > 0} />
                </div>
                {storylineStatus.failed_count > 0 && (
                  <div style={failureBox}>
                    <div style={failureTitle}>失败候选簇未写入故事线，也不会影响其他已通过的候选簇。</div>
                    <div style={failureAction}>{storylineStatus.retry_guidance ?? "修复后可再次触发审查。"}</div>
                  </div>
                )}
                {storylineStatus.accepted_count + storylineStatus.split_count + storylineStatus.rejected_count === 0 && !storylineStatus.is_running && (
                  <div style={emptyState}><div style={emptyTitle}>尚无故事线审查结论</div><div>候选簇不会自动成为故事线；点击“审查候选簇”后才会沉淀经过 Agent 审查的结果。</div></div>
                )}
              </>
            )}
            <StorylineReviewList
              total={storylineReviewsQuery.data?.total ?? 0}
              items={storylineReviewsQuery.data?.items ?? []}
              statusFilter={storylineReviewFilter}
              isLoading={storylineReviewsQuery.isLoading}
              isFetching={storylineReviewsQuery.isFetching}
              error={storylineReviewsQuery.error as Error | null}
              page={storylineReviewPage}
              pageCount={Math.max(1, Math.ceil((storylineReviewsQuery.data?.total ?? 0) / cardListPageSize))}
              onPageChange={setStorylineReviewPage}
              onStatusFilterChange={setStorylineReviewFilter}
            />
            <StorylineList
              total={storylinesQuery.data?.total ?? 0}
              items={storylinesQuery.data?.items ?? []}
              isLoading={storylinesQuery.isLoading}
              isFetching={storylinesQuery.isFetching}
              error={storylinesQuery.error as Error | null}
              page={storylinePage}
              pageCount={Math.max(1, Math.ceil((storylinesQuery.data?.total ?? 0) / cardListPageSize))}
              onPageChange={setStorylinePage}
            />
          </>
        )}

        {selectedStageId === 4 && (
          <>
            {!selectedTemplateId && (
              <div style={emptyState}>
                <div style={emptyTitle}>请先选择身份模板</div>
                <div>趋势总结按身份模板隔离；选择左侧模板后再手动触发本次运行。</div>
              </div>
            )}
            {selectedTemplateId && trendRunStatusQuery.isLoading && (
              <div style={loading}>正在读取趋势总结状态…</div>
            )}
            {selectedTemplateId && (trendRunStatusQuery.error || trendRunMutation.error) && (
              <div style={errorBox}>
                <div style={errorTitle}>趋势总结请求失败</div>
                <div>
                  {((trendRunStatusQuery.error ?? trendRunMutation.error) as Error).message}
                </div>
                <div style={errorAction}>确认模板、故事线与 LLM 服务可用后再次触发。</div>
              </div>
            )}
            {selectedTemplateId && trendStatus && (
              <>
                <div style={rangeMeta}>
                  {trendStatus.window_start_date && trendStatus.window_end_date
                    ? `${trendStatus.window_start_date} 至 ${trendStatus.window_end_date}`
                    : "窗口待解析"}
                  <span style={rangeDivider}>·</span>
                  X={trendStatus.trend_count ?? "—"}
                  <span style={rangeDivider}>·</span>
                  状态 {trendStatus.status ?? "未开始"}
                  {trendStatus.reusable ? <><span style={rangeDivider}>·</span>复用运行中任务</> : null}
                </div>
                <div style={countGrid}>
                  <Count label="候选故事线" value={trendStatus.candidate_count} />
                  <Count label="已评估" value={trendStatus.completed_candidate_count} />
                  <Count label="已发布结果" value={trendStatus.result_count} />
                  <Count label="无法验证" value={trendStatus.unverified_count} />
                </div>
                {trendStatus.status !== "failed" && trendStatus.message && trendMessageKey !== dismissedTrendMessageKey && (
                  <div style={dismissibleNotice}>
                    <div style={cardReason}>{trendStatus.message}</div>
                    <button
                      type="button"
                      style={dismissButton}
                      onClick={() => setDismissedTrendMessageKey(trendMessageKey)}
                      aria-label="关闭趋势总结提示"
                      title="关闭"
                    >
                      ×
                    </button>
                  </div>
                )}
                {(trendStatus.status === "failed" || trendStatus.error_message) && !trendFailureDismissed && (
                  <div style={failureBox}>
                    <div style={dismissibleNotice}>
                      <div style={failureTitle}>本次趋势运行失败，未覆盖该模板最近成功发布。</div>
                      <button
                        type="button"
                        style={dismissButton}
                        onClick={dismissTrendFailure}
                        aria-label="关闭趋势总结失败提示"
                        title="关闭"
                      >
                        ×
                      </button>
                    </div>
                    {trendStatus.error_message && (
                      <div style={failureDetail}>{trendStatus.error_message}</div>
                    )}
                    <div style={failureAction}>
                      {trendStatus.retry_guidance ?? "修复后可再次触发趋势总结。"}
                      {trendStatus.completed_candidate_count > 0 && trendStatus.candidate_count > trendStatus.completed_candidate_count
                        ? ` 本次在第 ${trendStatus.completed_candidate_count + 1}/${trendStatus.candidate_count} 条候选故事线完成前中断。`
                        : null}
                    </div>
                  </div>
                )}
              </>
            )}
            {selectedTemplateId && (
              <TrendResultList
                results={trendResultsQuery.data}
                isLoading={trendResultsQuery.isLoading}
                isFetching={trendResultsQuery.isFetching}
                error={trendResultsQuery.error as Error | null}
              />
            )}
          </>
        )}
      </div>
      </>}
    </section>
  );
}

function TrendResultList({
  results,
  isLoading,
  isFetching,
  error,
}: {
  results: TrendLatestResults | undefined;
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
}) {
  const items = results?.items ?? [];
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(items.length / cardListPageSize));
  const visibleItems = items.slice(
    (page - 1) * cardListPageSize,
    page * cardListPageSize,
  );

  useEffect(() => {
    setPage(1);
  }, [results?.run_id]);

  useEffect(() => {
    if (page > pageCount) {
      setPage(pageCount);
    }
  }, [page, pageCount]);

  return (
    <section style={cardListSection} aria-label="趋势总结结果">
      <div style={cardListHeader}>
        <div>
          <div style={cardListTitle}>最近成功发布</div>
          <div style={cardListMeta}>
            {results?.run_id
              ? `运行 ${results.run_id.slice(0, 8)} · X=${results.trend_count ?? "—"} · unverified_change 不占 X`
              : "尚无成功发布的趋势结果"}
          </div>
        </div>
      </div>
      {isLoading ? (
        <div style={listLoading}>正在读取趋势结果…</div>
      ) : error ? (
        <div style={listError}>趋势结果读取失败：{error.message}</div>
      ) : items.length === 0 ? (
        <div style={listEmpty}>
          {results?.message ?? "完成趋势总结后，当前身份模板的最新成功结果会显示在这里。"}
        </div>
      ) : (
        <>
          <PagedListContent page={page} contentKey={items[0]?.result_id ?? "empty"} isFetching={isFetching}>
          <div style={cardListRows}>
            {visibleItems.map((item) => (
              <TrendResultRow key={item.result_id} item={item} />
            ))}
          </div>
          </PagedListContent>
          <Pagination
            total={items.length}
            page={page}
            pageCount={pageCount}
            onPageChange={setPage}
            label="趋势结果"
          />
        </>
      )}
    </section>
  );
}

function TrendResultRow({ item }: { item: TrendResult }) {
  return (
    <article style={cardListRow}>
      <div style={cardRowHeader}>
        <div style={cardTitle}>{item.topic ?? "无法形成可验证的共同变化"}</div>
        <span style={cardStatusBadge(item.category === "unverified_change" ? "skipped" : "ready")}>
          {trendCategoryLabel(item.category)}
        </span>
      </div>
      <div style={cardReason}>
        窗口 {item.window_start_date} 至 {item.window_end_date} · 排序分 {item.trend_rank_score.toFixed(1)} ·
        相关性 {item.template_relevance_score.toFixed(1)} · 窗口影响力 {item.window_score.toFixed(1)}
      </div>
      {item.trend_summary && <div style={cardReason}>{item.trend_summary}</div>}
      <div style={cardReason}>判定说明：{item.agent_review}</div>
      <div style={cardRowFooter}>
        <span>引用新闻 {item.item_ids.length} 条</span>
        {item.category === "unverified_change" && <span>不占 X / 不进轮播</span>}
      </div>
    </article>
  );
}

function trendCategoryLabel(category: TrendCategory): string {
  return {
    emerging_trend: "新兴趋势",
    hot_event: "热点事件",
    periodic_activity: "周期性活动",
    attention_declining: "注意力下降",
    unverified_change: "无法验证",
  }[category];
}

function StorylineReviewList({
  total,
  items,
  statusFilter,
  isLoading,
  isFetching,
  error,
  page,
  pageCount,
  onPageChange,
  onStatusFilterChange,
}: {
  total: number;
  items: TrendStorylineReview[];
  statusFilter: TrendStorylineReviewFilter | undefined;
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  onStatusFilterChange: (status: TrendStorylineReviewFilter | undefined) => void;
}) {
  return (
    <section style={cardListSection} aria-label="故事线审查记录">
      <div style={cardListHeader}>
        <div><div style={cardListTitle}>候选簇审查记录</div><div style={cardListMeta}>共 {total} 条记录，可用于校准候选簇质量</div></div>
        <select
          aria-label="筛选候选簇审查状态"
          value={statusFilter ?? "all"}
          onChange={(event) => onStatusFilterChange(
            event.target.value === "all" ? undefined : event.target.value as TrendStorylineReviewFilter,
          )}
          style={reviewFilterSelect}
        >
          <option value="all">全部</option>
          <option value="accepted">已接收</option>
          <option value="split">已拆分</option>
          <option value="rejected">已拒绝</option>
          <option value="unreviewed">未审核</option>
        </select>
      </div>
      {isLoading ? <div style={listLoading}>正在读取审查记录…</div> : error ? <div style={listError}>审查记录读取失败：{error.message}</div> : items.length === 0 ? <div style={listEmpty}>尚无候选簇审查记录。</div> : (
        <PagedListContent page={page} contentKey={items[0]?.review_id ?? "empty"} isFetching={isFetching}>
        <div style={cardListRows}>
          {items.map((review) => (
            <article key={review.review_id} style={cardListRow}>
              <div style={cardRowHeader}>
                <div style={cardTitle}>候选簇 {review.candidate_cluster_id ? `#${review.candidate_cluster_id.slice(0, 8)}` : "已保留审计记录"}</div>
                <span style={cardStatusBadge(review.status === "accepted" ? "ready" : review.status === "split" ? "pending" : review.status === "failed" ? "failed" : "skipped")}>{reviewStatusLabel(review.status)}</span>
              </div>
              <div style={cardReason}>动作：{review.decision ?? "未完成"} · 卡片：{review.member_card_ids.join(", ") || "无"}</div>
              {review.removed_card_ids.length > 0 && <div style={cardReason}>移除卡片：{review.removed_card_ids.join(", ")}</div>}
              {review.agent_review && <div style={cardReason}>审查说明：{review.agent_review}</div>}
              {review.error_message && <div style={cardFailure}>失败原因：{review.error_message}</div>}
            </article>
          ))}
        </div>
        </PagedListContent>
      )}
      <Pagination total={total} page={page} pageCount={pageCount} onPageChange={onPageChange} label="审查记录" />
    </section>
  );
}

function StorylineList({
  total,
  items,
  isLoading,
  isFetching,
  error,
  page,
  pageCount,
  onPageChange,
}: {
  total: number;
  items: TrendStoryline[];
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  return (
    <section style={cardListSection} aria-label="已形成故事线">
      <div style={cardListHeader}><div><div style={cardListTitle}>已形成故事线</div><div style={cardListMeta}>共 {total} 条；第 4 步趋势总结尚未开始</div></div></div>
      {isLoading ? <div style={listLoading}>正在读取故事线…</div> : error ? <div style={listError}>故事线读取失败：{error.message}</div> : items.length === 0 ? <div style={listEmpty}>尚未形成故事线。被拒绝或失败的候选簇不会显示在这里。</div> : (
        <PagedListContent page={page} contentKey={items[0]?.storyline_id ?? "empty"} isFetching={isFetching}>
        <div style={cardListRows}>
          {items.map((storyline) => (
            <article key={storyline.storyline_id} style={cardListRow}>
              <div style={cardRowHeader}><div style={cardTitle}>{storyline.title}</div><span style={cardStatusBadge(storyline.status === "active" ? "ready" : "skipped")}>{storyline.status === "active" ? "活跃" : "归档"}</span></div>
              <div style={cardReason}>{storyline.overall_start_date} 至 {storyline.overall_end_date} · 影响力 {storyline.overall_influence_score.toFixed(1)} · 凝聚度 {storyline.cohesion_score.toFixed(3)}</div>
              <div style={cardReason}>成员：{storyline.members.map((member) => `${member.card_id}（${membershipLabel(member.membership)}，${member.at}）`).join("；")}</div>
              <div style={cardReason}>审查说明：{storyline.agent_review}</div>
            </article>
          ))}
        </div>
        </PagedListContent>
      )}
      <Pagination total={total} page={page} pageCount={pageCount} onPageChange={onPageChange} label="故事线" />
    </section>
  );
}

function PagedListContent({
  page,
  contentKey,
  isFetching,
  children,
}: {
  page: number;
  contentKey: string | number;
  isFetching: boolean;
  children: ReactNode;
}) {
  const lastSettledPage = useRef(page);
  const [enterDirection, setEnterDirection] = useState<1 | -1>(1);
  const [enterKey, setEnterKey] = useState(`${page}:${contentKey}`);

  useLayoutEffect(() => {
    if (isFetching) return;
    const previousPage = lastSettledPage.current;
    if (previousPage === page && enterKey === `${page}:${contentKey}`) return;
    setEnterDirection(page >= previousPage ? 1 : -1);
    setEnterKey(`${page}:${contentKey}`);
    lastSettledPage.current = page;
  }, [contentKey, enterKey, isFetching, page]);

  const enterClass = enterDirection > 0 ? "news-page-enter-next" : "news-page-enter-prev";
  return (
    <div
      className="news-page-list"
      style={{
        opacity: isFetching ? 0.42 : 1,
        transform: isFetching ? `translate3d(${enterDirection * -10}px, 0, 0)` : "translate3d(0, 0, 0)",
        filter: isFetching ? "saturate(0.9)" : "none",
        transition: "opacity 200ms ease, transform 240ms cubic-bezier(0.22, 1, 0.36, 1), filter 200ms ease",
        pointerEvents: isFetching ? "none" : "auto",
        willChange: "opacity, transform",
      }}
    >
      <div
        key={enterKey}
        className={enterClass}
        style={{ animation: isFetching ? "none" : `${enterClass} 320ms cubic-bezier(0.22, 1, 0.36, 1)` }}
      >
        {children}
      </div>
    </div>
  );
}

function Pagination({ total, page, pageCount, onPageChange, label }: { total: number; page: number; pageCount: number; onPageChange: (page: number) => void; label: string }) {
  if (total === 0) return null;
  return <div style={pagination} aria-label={`${label}分页`}>
    <button type="button" style={page === 1 ? disabledPageButton : pageButton} disabled={page === 1} onClick={() => onPageChange(page - 1)}>上一页</button>
    {visiblePageNumbers(page, pageCount).map((entry) => typeof entry === "number" ? <button key={entry} type="button" style={entry === page ? activePageButton : pageButton} onClick={() => onPageChange(entry)}>{entry}</button> : <span key={entry} style={ellipsis}>…</span>)}
    <button type="button" style={page === pageCount ? disabledPageButton : pageButton} disabled={page === pageCount} onClick={() => onPageChange(page + 1)}>下一页</button>
  </div>;
}

function reviewStatusLabel(status: TrendStorylineReview["status"]): string {
  return { pending: "待审查", running: "审查中", accepted: "已接受", split: "已拆分", rejected: "已拒绝", failed: "失败" }[status];
}

function membershipLabel(membership: TrendStoryline["members"][number]["membership"]): string {
  return { core: "核心", supporting: "支持", duplicate: "近重复" }[membership];
}

function CardList({
  status,
  total,
  items,
  isLoading,
  isFetching,
  error,
  onStatusChange,
  page,
  pageCount,
  onPageChange,
}: {
  status: TrendCardListStatus | undefined;
  total: number;
  items: TrendCardListItem[];
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  onStatusChange: (status: TrendCardListStatus | undefined) => void;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  const filters: Array<{ label: string; value: TrendCardListStatus | undefined }> = [
    { label: "全部", value: undefined },
    { label: "待生成", value: "pending" },
    { label: "已生成", value: "ready" },
    { label: "已跳过", value: "skipped" },
    { label: "失败", value: "failed" },
  ];

  return (
    <section style={cardListSection} aria-label="新闻卡片列表">
      <div style={cardListHeader}>
        <div>
          <div style={cardListTitle}>新闻卡片列表</div>
          <div style={cardListMeta}>{status ? "当前筛选共" : "当前范围共"} {total} 条新闻</div>
        </div>
        <div style={filterGroup} aria-label="按卡片状态筛选">
          {filters.map((filter) => (
            <button
              key={filter.label}
              type="button"
              style={filter.value === status ? activeFilterButton : filterButton}
              onClick={() => onStatusChange(filter.value)}
              aria-pressed={filter.value === status}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div style={listLoading}>正在读取新闻卡片列表…</div>
      ) : error ? (
        <div style={listError}>新闻卡片列表读取失败：{error.message}</div>
      ) : items.length === 0 ? (
        <div style={listEmpty}>当前筛选条件下没有新闻卡片。</div>
      ) : (
        <PagedListContent page={page} contentKey={items[0]?.item_id ?? "empty"} isFetching={isFetching}>
        <div style={cardListRows}>
          {items.map((item) => <CardListRow key={item.item_id} item={item} />)}
        </div>
        </PagedListContent>
      )}

      {total > 0 && (
        <div style={pagination} aria-label="新闻卡片分页">
          <button
            type="button"
            style={page === 1 ? disabledPageButton : pageButton}
            disabled={page === 1}
            onClick={() => onPageChange(page - 1)}
          >
            上一页
          </button>
          {visiblePageNumbers(page, pageCount).map((entry) => (
            typeof entry === "number" ? (
              <button
                key={entry}
                type="button"
                style={entry === page ? activePageButton : pageButton}
                onClick={() => onPageChange(entry)}
                aria-current={entry === page ? "page" : undefined}
              >
                {entry}
              </button>
            ) : <span key={entry} style={ellipsis}>…</span>
          ))}
          <button
            type="button"
            style={page === pageCount ? disabledPageButton : pageButton}
            disabled={page === pageCount}
            onClick={() => onPageChange(page + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </section>
  );
}

function CandidateClusterList({
  total,
  offset,
  items,
  isLoading,
  isFetching,
  error,
  page,
  pageCount,
  onPageChange,
}: {
  total: number;
  offset: number;
  items: TrendCandidateClusterListItem[];
  isLoading: boolean;
  isFetching: boolean;
  error: Error | null;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}) {
  return (
    <section style={candidateClusterListSection} aria-label="候选簇列表">
      <div style={cardListHeader}>
        <div>
          <div style={cardListTitle}>候选簇列表</div>
          <div style={cardListMeta}>当前范围共 {total} 个候选簇</div>
        </div>
      </div>

      {isLoading ? (
        <div style={listLoading}>正在读取候选簇列表…</div>
      ) : error ? (
        <div style={listError}>候选簇列表读取失败：{error.message}</div>
      ) : (
        <PagedListContent page={page} contentKey={items[0]?.candidate_cluster_id ?? "empty"} isFetching={isFetching}>
        <div style={candidateClusterTableWrap}>
          <table style={candidateClusterTable}>
            <thead>
              <tr>
                <th style={candidateClusterHead}>候选簇</th>
                <th style={candidateClusterHead}>卡片数</th>
                <th style={candidateClusterHead}>凝聚度</th>
                <th style={candidateClusterHead}>实际阈值</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={item.candidate_cluster_id}>
                  <td style={candidateClusterCell}>#{String(offset + index + 1).padStart(2, "0")}</td>
                  <td style={candidateClusterCell}>{item.member_count}</td>
                  <td style={candidateClusterCell}>{item.cohesion_score.toFixed(3)}</td>
                  <td style={candidateClusterCell}>{item.threshold.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        </PagedListContent>
      )}

      {total > 0 && (
        <div style={pagination} aria-label="候选簇分页">
          <button type="button" style={page === 1 ? disabledPageButton : pageButton} disabled={page === 1} onClick={() => onPageChange(page - 1)}>上一页</button>
          {visiblePageNumbers(page, pageCount).map((entry) => (
            typeof entry === "number" ? (
              <button key={entry} type="button" style={entry === page ? activePageButton : pageButton} onClick={() => onPageChange(entry)} aria-current={entry === page ? "page" : undefined}>{entry}</button>
            ) : <span key={entry} style={ellipsis}>…</span>
          ))}
          <button type="button" style={page === pageCount ? disabledPageButton : pageButton} disabled={page === pageCount} onClick={() => onPageChange(page + 1)}>下一页</button>
        </div>
      )}
    </section>
  );
}

function CardListRow({ item }: { item: TrendCardListItem }) {
  const contentFields: Array<{ label: string; value: string | null }> = [
    { label: "主体", value: item.news_actor },
    { label: "动作", value: item.action },
    { label: "结果", value: item.result },
    { label: "潜在影响", value: item.potential_impact },
    { label: "原因", value: item.cause },
  ];
  const businessTime = item.published_at ?? item.fetched_at;

  return (
    <article style={cardListRow}>
      <div style={cardRowHeader}>
        <div style={cardTitle}>{item.title}</div>
        <time style={cardRowDate}>{formatCardDate(businessTime)}</time>
        <span style={cardStatusBadge(item.status)}>{cardListStatusLabel(item.status)}</span>
      </div>

      {item.status === "ready" && (
        <div style={cardContentGrid}>
          {contentFields.map((field) => (
            <div key={field.label} style={cardContentField}>
              <span style={cardContentLabel}>{field.label}</span>
              <span style={cardContentValue}>{field.value ?? "无"}</span>
            </div>
          ))}
        </div>
      )}
      {item.status === "skipped" && (
        <div style={cardReason}>跳过原因：{item.skip_reason ?? "没有可用于生成卡片的正文或摘要。"}</div>
      )}
      {item.status === "failed" && (
        <div style={cardFailure}>失败原因：{item.error_message ?? "生成卡片时未能得到有效结果，可再次补齐重试。"}</div>
      )}
      <div style={cardRowFooter}>
        {item.attempt_count > 0 && <span>已尝试 {item.attempt_count} 次</span>}
      </div>
    </article>
  );
}

function formatCardDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function visiblePageNumbers(page: number, pageCount: number): Array<number | "left-ellipsis" | "right-ellipsis"> {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);
  if (page <= 4) return [1, 2, 3, 4, 5, "right-ellipsis", pageCount];
  if (page >= pageCount - 3) return [1, "left-ellipsis", pageCount - 4, pageCount - 3, pageCount - 2, pageCount - 1, pageCount];
  return [1, "left-ellipsis", page - 1, page, page + 1, "right-ellipsis", pageCount];
}

function cardListStatusLabel(status: TrendCardListStatus): string {
  return { pending: "待生成", ready: "已生成", skipped: "已跳过", failed: "失败" }[status];
}

function Count({ label, value, danger = false }: { label: string; value: number; danger?: boolean }) {
  return (
    <div style={countCell}>
      <div style={countLabel}>{label}</div>
      <div style={danger ? dangerCount : countValue}>{value}</div>
    </div>
  );
}

function cardStatusBadge(status: TrendCardListStatus): CSSProperties {
  if (status === "ready") return { ...rowStatusBadge, background: "#ecfdf3", color: "#067647" };
  if (status === "skipped") return { ...rowStatusBadge, background: "#f2f4f7", color: "#667085" };
  if (status === "failed") return { ...rowStatusBadge, background: "#fef3f2", color: "#b42318" };
  return { ...rowStatusBadge, background: "#fffaeb", color: "#b54708" };
}

const panel: CSSProperties = {
  border: "1px solid #eaecf0",
  borderRadius: 12,
  padding: 20,
  background: "#fff",
  boxShadow: "0 1px 2px rgba(16,24,40,.04)",
};
const header: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 };
const sectionTitle: CSSProperties = { color: "#101828", fontSize: 16, fontWeight: 800 };
const sectionCopy: CSSProperties = { color: "#667085", fontSize: 12, marginTop: 5, lineHeight: 1.6, maxWidth: 760 };
const startButton: CSSProperties = { border: "1px solid #175cd3", borderRadius: 8, background: "#175cd3", color: "#fff", padding: "8px 12px", fontSize: 12, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" };
const disabledButton: CSSProperties = { ...startButton, borderColor: "#a4bcfd", background: "#eff4ff", color: "#84adff", cursor: "not-allowed" };
const collapseButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, background: "#fff", color: "#344054", padding: "8px 10px", fontSize: 12, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" };
const buttonGroup: CSSProperties = { display: "flex", gap: 10, flexWrap: "wrap", marginTop: 14 };
const flowViewport: CSSProperties = {
  width: "100%",
  overflowX: "auto",
  padding: "12px 0 0",
};
const stageDetail: CSSProperties = {
  marginTop: 16,
  border: "1px solid #eaecf0",
  borderRadius: 12,
  padding: "14px 16px 16px",
  background: "#f9fafb",
};
const summaryHeader: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 16,
  flexWrap: "wrap",
};
const summaryTitle: CSSProperties = { color: "#344054", fontSize: 13, fontWeight: 800 };
const summaryCopy: CSSProperties = { color: "#98a2b3", fontSize: 11, marginTop: 3 };
const countGrid: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 10, marginTop: 13 };
const countCell: CSSProperties = {
  minWidth: 0,
  border: "1px solid #eaecf0",
  borderRadius: 9,
  background: "#fff",
  padding: "10px 12px",
};
const countLabel: CSSProperties = { color: "#667085", fontSize: 11, whiteSpace: "nowrap" };
const countValue: CSSProperties = { color: "#344054", fontFamily: "var(--font-mono)", fontSize: 19, fontWeight: 800, marginTop: 4 };
const dangerCount: CSSProperties = { ...countValue, color: "#b42318" };
const rangeMeta: CSSProperties = { marginTop: 14, color: "#98a2b3", fontSize: 10, fontFamily: "var(--font-mono)", whiteSpace: "nowrap" };
const rangeDivider: CSSProperties = { padding: "0 6px", color: "#d0d5dd" };
const loading: CSSProperties = { color: "#98a2b3", textAlign: "center", padding: "18px 0 0", fontSize: 13 };
const emptyState: CSSProperties = { marginTop: 14, border: "1px dashed #d0d5dd", borderRadius: 10, color: "#667085", textAlign: "center", padding: "20px", fontSize: 13, lineHeight: 1.65 };
const emptyTitle: CSSProperties = { color: "#344054", fontWeight: 800, marginBottom: 5 };
const errorBox: CSSProperties = { marginTop: 14, border: "1px solid #fecdca", borderRadius: 8, background: "#fef3f2", color: "#b42318", padding: "10px 12px", fontSize: 12, lineHeight: 1.6 };
const errorTitle: CSSProperties = { fontWeight: 800, marginBottom: 3 };
const errorAction: CSSProperties = { marginTop: 5, color: "#912018" };
const failureBox: CSSProperties = { marginTop: 14, border: "1px solid #fecdca", borderRadius: 10, background: "#fef3f2", padding: "12px 14px" };
const failureTitle: CSSProperties = { color: "#b42318", fontSize: 13, fontWeight: 800 };
const failureDetail: CSSProperties = { color: "#912018", fontSize: 12, marginTop: 6, lineHeight: 1.6, overflowWrap: "anywhere" };
const failureAction: CSSProperties = { color: "#912018", fontSize: 12, marginTop: 6, lineHeight: 1.6 };
const dismissibleNotice: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 };
const dismissButton: CSSProperties = { flex: "0 0 auto", border: 0, background: "transparent", color: "#667085", fontSize: 20, lineHeight: 1, padding: 0, cursor: "pointer" };
const cardListSection: CSSProperties = { marginTop: 18, borderTop: "1px solid #eaecf0", paddingTop: 16 };
const candidateClusterListSection: CSSProperties = { marginTop: 18, borderTop: "1px solid #eaecf0", paddingTop: 16 };
const candidateClusterTableWrap: CSSProperties = { marginTop: 10, overflowX: "auto", border: "1px solid #eaecf0", borderRadius: 8, background: "#fff" };
const candidateClusterTable: CSSProperties = { width: "100%", borderCollapse: "collapse", minWidth: 460, fontSize: 12 };
const candidateClusterHead: CSSProperties = { padding: "9px 12px", color: "#667085", background: "#f9fafb", borderBottom: "1px solid #eaecf0", textAlign: "left", fontSize: 11, fontWeight: 800, whiteSpace: "nowrap" };
const candidateClusterCell: CSSProperties = { padding: "10px 12px", color: "#475467", borderBottom: "1px solid #f2f4f7", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" };
const cardListHeader: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap" };
const cardListTitle: CSSProperties = { color: "#344054", fontSize: 13, fontWeight: 800 };
const cardListMeta: CSSProperties = { marginTop: 3, color: "#98a2b3", fontSize: 11 };
const reviewFilterSelect: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 7, background: "#fff", color: "#475467", padding: "6px 26px 6px 9px", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const filterGroup: CSSProperties = { display: "flex", gap: 6, flexWrap: "wrap" };
const filterButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 999, background: "#fff", color: "#667085", padding: "5px 9px", fontSize: 11, cursor: "pointer" };
const activeFilterButton: CSSProperties = { ...filterButton, borderColor: "#84adff", background: "#eff4ff", color: "#175cd3", fontWeight: 800 };
const listLoading: CSSProperties = { color: "#98a2b3", padding: "20px 0", textAlign: "center", fontSize: 12 };
const listError: CSSProperties = { marginTop: 12, border: "1px solid #fecdca", borderRadius: 8, background: "#fef3f2", color: "#b42318", padding: "10px 12px", fontSize: 12 };
const listEmpty: CSSProperties = { marginTop: 12, border: "1px dashed #d0d5dd", borderRadius: 8, color: "#98a2b3", padding: 16, textAlign: "center", fontSize: 12 };
const cardListRows: CSSProperties = { display: "grid", gap: 7, marginTop: 10 };
const cardListRow: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 8, background: "#fff", padding: "8px 10px" };
const cardRowHeader: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 };
const cardTitle: CSSProperties = { minWidth: 0, flex: 1, color: "#344054", fontSize: 12, fontWeight: 800, lineHeight: 1.35, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const rowStatusBadge: CSSProperties = { flex: "0 0 auto", borderRadius: 999, padding: "3px 8px", fontSize: 10, fontWeight: 800, whiteSpace: "nowrap" };
const cardRowDate: CSSProperties = { flex: "0 0 auto", color: "#98a2b3", fontFamily: "var(--font-mono)", fontSize: 10, whiteSpace: "nowrap" };
const cardContentGrid: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "4px 10px", marginTop: 7, borderTop: "1px solid #f2f4f7", paddingTop: 7 };
const cardContentField: CSSProperties = { display: "flex", gap: 5, minWidth: 0, fontSize: 11, lineHeight: 1.4 };
const cardContentLabel: CSSProperties = { flex: "0 0 auto", color: "#98a2b3" };
const cardContentValue: CSSProperties = { color: "#475467", overflowWrap: "anywhere" };
const cardReason: CSSProperties = { marginTop: 7, color: "#667085", fontSize: 11, lineHeight: 1.4 };
const cardFailure: CSSProperties = { marginTop: 7, color: "#b42318", fontSize: 11, lineHeight: 1.4, overflowWrap: "anywhere" };
const cardRowFooter: CSSProperties = { display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6, color: "#98a2b3", fontSize: 10 };
const pagination: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "center", gap: 5, marginTop: 12 };
const pageButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 6, background: "#fff", color: "#475467", padding: "5px 8px", fontSize: 11, cursor: "pointer" };
const activePageButton: CSSProperties = { ...pageButton, borderColor: "#84adff", background: "#eff4ff", color: "#175cd3", fontWeight: 800 };
const disabledPageButton: CSSProperties = { ...pageButton, background: "#f2f4f7", color: "#98a2b3", cursor: "not-allowed" };
const ellipsis: CSSProperties = { color: "#98a2b3", fontSize: 13, lineHeight: "28px", padding: "0 2px" };
