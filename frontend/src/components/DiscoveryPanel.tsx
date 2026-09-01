// frontend/src/components/DiscoveryPanel.tsx
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, enqueueDiscoveryQueue, getBatchDiscoveryRun, listDiscoveryQueue, suggestDiscoveryName } from "../api/client";
import { readStoredDiscoveryLoopBudget } from "./DiscoveryLoopBudgetCard";
import type {
  DiscoveryQueueItem,
  DiscoveryRouteType,
  DiscoveryRun,
  MultiDiscoveryStartRequest,
  MultiDiscoveryNameRequest,
} from "../types";
import { DiscoveryFlowChart, type DiscoveryPhaseId } from "./DiscoveryFlowChart";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { useDiscoveryLogs } from "../hooks/useDiscoveryLogs";
import {
  resolveDiscoveryRouteState,
  ROUTE_TYPE_LABELS,
  type RouteInputState,
} from "../discovery/routeInput";
import { clampInput, INPUT_LIMITS } from "../inputLimits";

const DISCOVERY_PANEL_STORAGE_KEY = "os-news-tracker.discovery-panel";
const QUEUE_QUERY_KEY = ["discovery-queue"] as const;
const WORKSPACE_HEIGHT = 760;

interface DiscoveryPanelPersistedState {
  rawInput?: string;
  selectedRouteType?: DiscoveryRouteType | null;
  name?: string;
  selectedNode?: DiscoveryPhaseId | null;
  expanded?: boolean;
  logsResetAt?: number | null;
}

function readDiscoveryPanelState(): DiscoveryPanelPersistedState {
  if (typeof window === "undefined" || !window.sessionStorage) return {};
  try {
    const raw = window.sessionStorage.getItem(DISCOVERY_PANEL_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const validRouteTypes = new Set(["website", "wechat_search", "internal_forum"]);
    const validNodeIds = new Set<DiscoveryPhaseId>([
      "context", "explore", "build", "execute", "evaluate_repair", "package", "pending_review",
    ]);
    const st = typeof parsed.selectedRouteType === "string" && validRouteTypes.has(parsed.selectedRouteType)
      ? (parsed.selectedRouteType as DiscoveryRouteType)
      : null;
    const sn = typeof parsed.selectedNode === "string" && validNodeIds.has(parsed.selectedNode as DiscoveryPhaseId)
      ? (parsed.selectedNode as DiscoveryPhaseId)
      : null;
    return {
      rawInput: typeof parsed.rawInput === "string" ? parsed.rawInput : undefined,
      selectedRouteType: st,
      name: typeof parsed.name === "string" ? parsed.name : undefined,
      selectedNode: sn,
      expanded: typeof parsed.expanded === "boolean" ? parsed.expanded : undefined,
      logsResetAt: Number.isFinite(Number(parsed.logsResetAt)) && Number(parsed.logsResetAt) > 0
        ? Number(parsed.logsResetAt)
        : null,
    };
  } catch {
    return {};
  }
}

function writeDiscoveryPanelState(state: DiscoveryPanelPersistedState): void {
  if (typeof window === "undefined" || !window.sessionStorage) return;
  window.sessionStorage.setItem(DISCOVERY_PANEL_STORAGE_KEY, JSON.stringify(state));
}

function idleRun(item: DiscoveryQueueItem | null, fallbackUrl = ""): DiscoveryRun {
  const route = item?.resolved_route_type ?? item?.selected_route_type ?? null;
  return {
    id: 0,
    site_url: item?.display_input || item?.input || fallbackUrl,
    status: "cancelled",
    resulting_method_id: null,
    llm_token_usage: 0,
    node_trace: [],
    retry_count: 0,
    current_step: null,
    started_at: null,
    ended_at: null,
    error_message: item?.error_message ?? null,
    trigger_type: "manual",
    phase: "context",
    round: 0,
    queue_position: null,
    runtime_version: null,
    repair_method_id: null,
    source_kind: route?.startsWith("wechat")
      ? "wechat"
      : route === "website"
        ? "website"
        : "unknown",
    review_status: null,
    elapsed_seconds: 0,
    remaining_seconds: 1200,
  };
}

export function DiscoveryPanel({
  selectedQueueItemId = null,
  onSelectQueueItem,
  onMethodAdded,
  hideLogs = false,
}: {
  selectedQueueItemId?: number | null;
  onSelectQueueItem?: (id: number | null) => void;
  onMethodAdded?: (methodId: number) => void;
  hideLogs?: boolean;
}) {
  const queryClient = useQueryClient();
  const [persistedState] = useState<DiscoveryPanelPersistedState>(() => readDiscoveryPanelState());
  const [rawInput, setRawInput] = useState(
    clampInput(persistedState.rawInput ?? "", INPUT_LIMITS.discoveryInput),
  );
  const [selectedRouteType, setSelectedRouteType] = useState<DiscoveryRouteType | null>(
    persistedState.selectedRouteType ?? null,
  );
  const [name, setName] = useState(
    clampInput(persistedState.name ?? "", INPUT_LIMITS.displayName),
  );
  const [nameError, setNameError] = useState<string | null>(null);
  const [hasEnteredInput, setHasEnteredInput] = useState(() => Boolean((persistedState.rawInput ?? "").trim()));
  const [dup, setDup] = useState<{ method_id: number; domain: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<DiscoveryPhaseId | null>(persistedState.selectedNode ?? null);
  const [logsResetAt, setLogsResetAt] = useState<number | null>(persistedState.logsResetAt ?? null);
  const [enqueueLocked, setEnqueueLocked] = useState(false);
  const enqueueLockRef = useRef(false);
  const skipNextPersistRef = useRef(false);
  const notifiedRef = useRef<number | null>(null);

  const routeState: RouteInputState = resolveDiscoveryRouteState(rawInput, selectedRouteType);

  const queueQuery = useQuery({
    queryKey: QUEUE_QUERY_KEY,
    queryFn: listDiscoveryQueue,
    refetchInterval: 1500,
  });

  const pending = queueQuery.data?.pending ?? [];
  const failedItems = queueQuery.data?.failed ?? [];
  const completedItems = queueQuery.data?.completed ?? [];
  const selected = pending.find((item) => item.id === selectedQueueItemId)
    ?? failedItems.find((item) => item.id === selectedQueueItemId)
    ?? completedItems.find((item) => item.id === selectedQueueItemId)
    ?? null;
  const watchRunId = selected?.run_id ?? null;

  const runQuery = useQuery({
    queryKey: ["discovery-run", watchRunId],
    queryFn: () => getBatchDiscoveryRun(watchRunId!),
    enabled: watchRunId != null,
    refetchInterval: (q) => {
      const run = q.state.data;
      if (["queued", "running", "repairing"].includes(run?.status ?? "")) return 1500;
      if (
        run?.status === "completed"
        && run.resulting_method_id
        && (run.review_status == null || run.review_status === "pending")
      ) return 5000;
      return false;
    },
  });

  const discoveryLogs = useDiscoveryLogs(watchRunId, watchRunId != null, "run");
  const manualMethodLogs = useDiscoveryLogs(0, true, true);
  const combinedLogs = useMemo(
    () => [
      ...discoveryLogs,
      ...manualMethodLogs
        .filter(
          (log) =>
            typeof log.method_id === "number" &&
            (log.trigger_type === "manual" || log.trigger_type === "manual_method"),
        )
        .map((log) => ({ ...log, id: -log.id, sequence: -log.sequence })),
    ].sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts)),
    [discoveryLogs, manualMethodLogs],
  );
  const logs = logsResetAt == null
    ? combinedLogs
    : combinedLogs.filter((log) => Date.parse(log.ts) > logsResetAt);

  const enqueueMut = useMutation({
    mutationFn: (request: MultiDiscoveryStartRequest) => enqueueDiscoveryQueue(request),
    onSuccess: (res) => {
      enqueueLockRef.current = false;
      setEnqueueLocked(false);
      setError(null);
      if (res.status === "duplicate" && res.existing_method) {
        setDup({
          method_id: res.existing_method.method_id,
          domain: res.existing_method.domain,
        });
        setNotice(null);
        return;
      }
      setDup(null);
      setNotice("已加入批量探查队列");
      if (res.item?.id != null) onSelectQueueItem?.(res.item.id);
      void queryClient.invalidateQueries({ queryKey: QUEUE_QUERY_KEY });
    },
    onError: (e) => {
      enqueueLockRef.current = false;
      setEnqueueLocked(false);
      setNotice(null);
      setError(e instanceof ApiError ? e.message : "加入队列失败");
    },
  });

  const nameMut = useMutation({
    mutationFn: (request: MultiDiscoveryNameRequest) => suggestDiscoveryName(request),
    onSuccess: (r) => {
      setName(clampInput(r.name, INPUT_LIMITS.displayName));
      setNameError(null);
    },
    onError: (e) => {
      const message =
        e instanceof Error && e.message
          ? e.message
          : "自动命名失败，请稍后重试";
      setNameError(message);
    },
  });

  const enqueuePending = enqueueMut.isPending || enqueueLocked || enqueueLockRef.current;
  const enqueueDisabled = Boolean(routeState.validationError) || enqueuePending;
  const namingPending = nameMut.isPending;
  const displayRun = runQuery.data ?? idleRun(selected, rawInput);
  const completed = runQuery.data?.status === "completed" || selected?.status === "completed";
  const failed = runQuery.data?.status === "failed" || selected?.status === "failed";
  const cancelled = runQuery.data?.status === "cancelled";
  const reviewStatus = runQuery.data?.review_status ?? null;
  const resetDisabled = ["starting", "running", "repairing", "cancelling"].includes(
    selected?.status ?? runQuery.data?.status ?? "",
  );

  function enqueue(force: boolean) {
    if (routeState.validationError || enqueueMut.isPending || enqueueLockRef.current) return;
    if (!routeState.resolvedRouteType) return;
    enqueueLockRef.current = true;
    setEnqueueLocked(true);
    if (force) setDup(null);
    const trimmedName = name.trim();
    if (!trimmedName && !nameMut.isPending) {
      setNameError(null);
      nameMut.mutate({
        input: routeState.value,
        display_input: routeState.displayValue,
        selected_route_type: selectedRouteType,
        resolved_route_type: routeState.resolvedRouteType,
        route_source: routeState.routeSource,
      });
    }
    const request: MultiDiscoveryStartRequest = {
      input: routeState.value,
      display_input: routeState.displayValue,
      selected_route_type: selectedRouteType,
      resolved_route_type: routeState.resolvedRouteType,
      route_source: routeState.routeSource,
      name: trimmedName || undefined,
      force,
      agent_budget: readStoredDiscoveryLoopBudget(),
    };
    enqueueMut.mutate(request);
  }

  function resetPanelState() {
    if (resetDisabled) return;
    const resetAt = Date.now();
    skipNextPersistRef.current = true;
    notifiedRef.current = null;
    writeDiscoveryPanelState({ logsResetAt: resetAt });
    setLogsResetAt(resetAt);
    setRawInput("");
    setHasEnteredInput(false);
    setSelectedRouteType(null);
    setName("");
    setNameError(null);
    setDup(null);
    setError(null);
    setNotice(null);
    setSelectedNode(null);
    setEnqueueLocked(false);
    enqueueLockRef.current = false;
    enqueueMut.reset();
    nameMut.reset();
    onSelectQueueItem?.(null);
    queryClient.removeQueries({ queryKey: ["discovery-run"] });
  }

  useEffect(() => {
    const mid = runQuery.data?.resulting_method_id;
    if (runQuery.data?.status === "completed" && mid != null && notifiedRef.current !== mid) {
      notifiedRef.current = mid;
      onMethodAdded?.(mid);
    }
  }, [onMethodAdded, runQuery.data?.resulting_method_id, runQuery.data?.status]);

  useEffect(() => {
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    writeDiscoveryPanelState({
      rawInput,
      selectedRouteType,
      name,
      selectedNode,
      expanded: true,
      logsResetAt,
    });
  }, [rawInput, selectedRouteType, name, selectedNode, logsResetAt]);

  const resolvedLabel = routeState.resolvedRouteType
    ? ROUTE_TYPE_LABELS[routeState.resolvedRouteType]
    : null;

  return (
    <section style={hideLogs ? embeddedSection : stackedSection}>
      <div style={headerRow}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>智能探查</div>
        <button type="button" disabled={resetDisabled} onClick={resetPanelState} style={resetDisabled ? disabledResetBtn : resetBtn}>
          重置状态
        </button>
      </div>

      <div style={hideLogs ? embeddedBody : undefined}>
          <div style={hideLogs ? embeddedControlGrid : controlGrid}>
            <div style={routeColumn}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <input
                  placeholder="输入网页 URL 或搜索关键词"
                  value={rawInput}
                  maxLength={INPUT_LIMITS.discoveryInput}
                  onChange={(e) => {
                    const nextInput = clampInput(e.target.value, INPUT_LIMITS.discoveryInput);
                    setRawInput(nextInput);
                    if (nextInput.trim()) setHasEnteredInput(true);
                    setNameError(null);
                    setNotice(null);
                  }}
                  style={{ ...inputBase, flex: "1 1 220px", minWidth: 160 }}
                />
                <select
                  value={selectedRouteType ?? ""}
                  onChange={(e) => {
                    const val = e.target.value;
                    setSelectedRouteType(val ? (val as DiscoveryRouteType) : null);
                  }}
                  style={selectBase}
                >
                  <option value="">自动推断</option>
                  <option value="website">网页</option>
                  <option value="wechat_search">微信搜索</option>
                </select>
                {routeState.validationError && hasEnteredInput ? (
                  <span style={routeHintError}>{routeState.validationError}</span>
                ) : resolvedLabel && routeState.routeSource === "inferred" ? (
                  <span style={routeHint}>自动推断为 <b style={{ color: "#175cd3", fontWeight: 600 }}>{resolvedLabel}</b></span>
                ) : resolvedLabel && routeState.routeSource === "explicit" && selectedRouteType ? (
                  <span style={routeHint}>已锁定 <b style={{ color: "#059669", fontWeight: 600 }}>{resolvedLabel}</b></span>
                ) : (
                  <span style={routeHint}>根据输入自动推断网页或微信搜索</span>
                )}
              </div>
            </div>
            <div style={actionColumn}>
              <div style={{ display: "flex", gap: 6, minWidth: 0, alignItems: "center", flexWrap: "nowrap" }}>
                <input
                  placeholder={namingPending ? "正在自动命名…" : "名称（选填）"}
                  value={name}
                  maxLength={INPUT_LIMITS.displayName}
                  disabled={namingPending}
                  aria-busy={namingPending}
                  onChange={(e) => {
                    setName(clampInput(e.target.value, INPUT_LIMITS.displayName));
                    setNameError(null);
                  }}
                  style={{
                    ...inputBase,
                    flex: "1 1 auto",
                    minWidth: 0,
                    width: "100%",
                    ...(namingPending ? { background: "#f2f4f7", color: "#667085" } : null),
                  }}
                />
                <button
                  type="button"
                  disabled={enqueueDisabled}
                  onClick={() => enqueue(false)}
                  style={enqueueDisabled ? btnDisabled : btnPrimary}
                >
                  {enqueuePending ? "加入中…" : "加入队列"}
                </button>
              </div>
            </div>
          </div>

          {dup && (
            <div style={{ border: "1px solid #fec84b", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13 }}>
              该域名已有爬取方式（{dup.domain}）。是否覆盖重新探查？
              <button type="button" disabled={enqueueDisabled} style={{ ...(enqueueDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }}
                onClick={() => enqueue(true)}>{enqueuePending ? "加入中…" : "覆盖重探"}</button>
              <button type="button" style={{ ...btnGhost, marginLeft: 8 }} onClick={() => setDup(null)}>取消</button>
            </div>
          )}
          {notice && <div style={{ color: "#027a48", fontSize: 13, marginTop: 2 }}>{notice}</div>}

          <div
            data-testid="discovery-layout-grid"
            style={hideLogs ? embeddedWorkspace : stackedWorkspace}
          >
            <div style={hideLogs ? embeddedChartColumn : stackedChartColumn}>
              <div style={{ border: "1px solid #eaecf0", borderRadius: 8, background: "#f8fafc", padding: hideLogs ? 6 : 10 }}>
                <DiscoveryFlowChart compact={hideLogs} run={displayRun} onSelectNode={setSelectedNode} selectedNode={selectedNode} />
              </div>
              <div style={{ minHeight: 0, ...(hideLogs ? {} : { height: 210, overflowY: "auto" }), display: "flex", flexDirection: "column", gap: 6 }}>
                {selectedNode ? (
                  <DiscoveryNodeDetail nodeId={selectedNode} events={logs} />
                ) : (
                  <div style={{ border: "1px dashed #d0d5dd", borderRadius: 8, background: "#fcfcfd", color: "#667085", padding: hideLogs ? "6px 10px" : "8px 12px", fontSize: 12 }}>
                    {selected
                      ? "点击流程图节点查看该步骤的摘要、证据和当前状态。"
                      : "加入队列或点选下方任务后，这里会显示探查流程图。"}
                  </div>
                )}
                {completed && (
                  <div style={{
                    border: `1px solid ${reviewStatus === "rejected" ? "#fca5a5" : "#a3e0c4"}`,
                    background: reviewStatus === "rejected" ? "#fef2f2" : "#ecfdf3",
                    color: reviewStatus === "rejected" ? "#b42318" : "#059669",
                    borderRadius: 8, padding: "10px 12px", fontSize: 13,
                  }}>
                    {reviewStatus === "approved"
                      ? "探查完成 · 方式已批准"
                      : reviewStatus === "rejected"
                        ? "探查完成 · 方式审核已拒绝"
                        : "探查完成 · 已进入待审核方式"}
                    <a style={{ color: "#175cd3", cursor: "pointer", marginLeft: 8 }} onClick={() => displayRun.resulting_method_id && onMethodAdded?.(displayRun.resulting_method_id)}>查看方式 →</a>
                  </div>
                )}
                {failed && (
                  <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", color: "#b42318", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                    探查失败：{selected?.error_message ?? displayRun.error_message ?? "未知错误"}
                  </div>
                )}
                {cancelled && watchRunId != null && (
                  <div style={{ border: "1px solid #fedf89", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                    探查已取消：{displayRun.error_message ?? "已停止后续调用"}
                  </div>
                )}
              </div>
            </div>
            {!hideLogs && (
              <div style={{ minHeight: WORKSPACE_HEIGHT, height: WORKSPACE_HEIGHT }}>
                <DiscoveryLogPanel logs={logs} />
              </div>
            )}
          </div>

          {nameError && <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>{nameError}</div>}
          {error && <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>{error}</div>}
        </div>
    </section>
  );
}

const inputBase: CSSProperties = {
  border: "1px solid #d0d5dd", borderRadius: 8, padding: "7px 10px",
  fontSize: 13, color: "#101828", background: "#fff",
  minWidth: 0, boxSizing: "border-box",
};
const selectBase: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "7px 10px",
  fontSize: 13,
  color: "#101828",
  background: "#fff",
  flexShrink: 0,
  minWidth: 110,
  boxSizing: "border-box" as const,
};
const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "7px 12px", background: "#175cd3", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer", flexShrink: 0 };
const btnDisabled: CSSProperties = { ...btnPrimary, background: "#98a2b3", cursor: "not-allowed", flexShrink: 0 };
const btnGhost: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 8, padding: "7px 10px", fontSize: 12, color: "#475467", cursor: "pointer", flexShrink: 0 };
const toggleBtn: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 999, padding: "8px 14px", fontSize: 13, color: "#344054", fontWeight: 700, cursor: "pointer" };
const resetBtn: CSSProperties = { ...toggleBtn, color: "#3d6b62", borderColor: "#c5d6d3", background: "#ecf2f1", position: "absolute", top: 10, right: 10 };
const disabledResetBtn: CSSProperties = { ...resetBtn, cursor: "not-allowed", opacity: 0.55 };
const stackedSection: CSSProperties = { position: "relative", background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 };
const embeddedSection: CSSProperties = {
  ...stackedSection,
  padding: 10,
  height: "100%",
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
};
const headerRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  marginBottom: 8,
  paddingRight: 96,
  minHeight: 32,
};
const stackedWorkspace: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
  gap: 14,
  alignItems: "stretch",
  marginTop: 14,
};
const embeddedWorkspace: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  minHeight: 0,
  marginTop: 8,
};
const stackedChartColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 10,
  minHeight: WORKSPACE_HEIGHT,
  height: WORKSPACE_HEIGHT,
};
const embeddedChartColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 6,
  minHeight: 0,
};
const embeddedBody: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  minHeight: 0,
};
const controlGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
  gap: 14,
  alignItems: "start",
  marginBottom: 8,
};
const embeddedControlGrid: CSSProperties = {
  ...controlGrid,
  gridTemplateColumns: "1fr",
  gap: 6,
  marginBottom: 4,
};
const routeColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  minWidth: 0,
};
const routeHint: CSSProperties = {
  fontSize: 12,
  color: "#667085",
  flexShrink: 0,
  lineHeight: 1.4,
};
const routeHintError: CSSProperties = {
  ...routeHint,
  color: "#b42318",
};
const actionColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 10,
  minWidth: 0,
};
