// frontend/src/components/DiscoveryPanel.tsx
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, cancelDiscoveryRun, getDiscoveryRun, startDiscoveryRun, suggestDiscoveryName } from "../api/client";
import type {
  DiscoveryRouteType,
  DiscoveryRun,
  MultiDiscoveryStartRequest,
  MultiDiscoveryNameRequest,
} from "../types";
import { DiscoveryFlowChart } from "./DiscoveryFlowChart";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { useDiscoveryLogs } from "../hooks/useDiscoveryLogs";
import { currentAttemptRound, type FlowNodeId } from "../discovery/flowState";
import {
  resolveDiscoveryRouteState,
  ROUTE_TYPE_LABELS,
  type RouteInputState,
} from "../discovery/routeInput";

const DISCOVERY_PANEL_STORAGE_KEY = "os-news-tracker.discovery-panel";

interface DiscoveryPanelPersistedState {
  rawInput?: string;
  selectedRouteType?: DiscoveryRouteType | null;
  name?: string;
  runId?: number | null;
  selectedNode?: FlowNodeId | null;
  expanded?: boolean;
}

function readDiscoveryPanelState(): DiscoveryPanelPersistedState {
  if (typeof window === "undefined" || !window.localStorage) return {};
  try {
    const raw = window.localStorage.getItem(DISCOVERY_PANEL_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const validRouteTypes = new Set(["website", "wechat_search", "wechat_history", "internal_forum"]);
    const validNodeIds = new Set<FlowNodeId>([
      "fetch_homepage", "capture_network", "explorer",
      "validator", "dsl_writer", "auditor", "save_method",
    ]);
    const st = typeof parsed.selectedRouteType === "string" && validRouteTypes.has(parsed.selectedRouteType)
      ? (parsed.selectedRouteType as DiscoveryRouteType)
      : null;
    const sn = typeof parsed.selectedNode === "string" && validNodeIds.has(parsed.selectedNode as FlowNodeId)
      ? (parsed.selectedNode as FlowNodeId)
      : null;
    return {
      rawInput: typeof parsed.rawInput === "string" ? parsed.rawInput : undefined,
      selectedRouteType: st,
      name: typeof parsed.name === "string" ? parsed.name : undefined,
      runId: typeof parsed.runId === "number" ? parsed.runId : null,
      selectedNode: sn,
      expanded: typeof parsed.expanded === "boolean" ? parsed.expanded : undefined,
    };
  } catch {
    return {};
  }
}

function writeDiscoveryPanelState(state: DiscoveryPanelPersistedState): void {
  if (typeof window === "undefined" || !window.localStorage) return;
  window.localStorage.setItem(DISCOVERY_PANEL_STORAGE_KEY, JSON.stringify(state));
}

function clearDiscoveryPanelState(): void {
  if (typeof window === "undefined" || !window.localStorage) return;
  window.localStorage.removeItem(DISCOVERY_PANEL_STORAGE_KEY);
}

export function DiscoveryPanel({ onMethodAdded }: { onMethodAdded?: (methodId: number) => void }) {
  const queryClient = useQueryClient();
  const [persistedState] = useState<DiscoveryPanelPersistedState>(() => readDiscoveryPanelState());
  const [rawInput, setRawInput] = useState(persistedState.rawInput ?? "");
  const [selectedRouteType, setSelectedRouteType] = useState<DiscoveryRouteType | null>(
    persistedState.selectedRouteType ?? null,
  );
  const [name, setName] = useState(persistedState.name ?? "");
  const [nameError, setNameError] = useState<string | null>(null);
  const [runId, setRunId] = useState<number | null>(persistedState.runId ?? null);
  const [dup, setDup] = useState<{ method_id: number; domain: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<FlowNodeId | null>(persistedState.selectedNode ?? null);
  const [expanded, setExpanded] = useState(persistedState.expanded ?? true);
  const [startLocked, setStartLocked] = useState(false);
  const startLockRef = useRef(false);
  const skipNextPersistRef = useRef(false);
  const notifiedRef = useRef<number | null>(null);

  // Derive route state — single source of truth for disabled states
  const routeState: RouteInputState = resolveDiscoveryRouteState(rawInput, selectedRouteType);

  const runQuery = useQuery({
    queryKey: ["discovery-run", runId],
    queryFn: () => getDiscoveryRun(runId!),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const logs = useDiscoveryLogs(runId, true, true);
  const cancelMut = useMutation({
    mutationFn: (id: number) => cancelDiscoveryRun(id),
    onSuccess: () => {
      setError(null);
      runQuery.refetch();
    },
    onError: (e) => {
      setError(e instanceof ApiError ? e.message : "取消探查失败");
    },
  });

  const startMut = useMutation({
    mutationFn: (request: MultiDiscoveryStartRequest) => startDiscoveryRun(request),
    onSuccess: (res) => {
      startLockRef.current = false;
      setStartLocked(false);
      setError(null);
      if (res.status === "started" && res.run_id != null) {
        setDup(null);
        setRunId(res.run_id);
      } else if (res.status === "completed" && res.method_id != null) {
        setDup(null);
        if (onMethodAdded) onMethodAdded(res.method_id);
      } else if (res.status === "duplicate" && res.existing_method) {
        setDup({
          method_id: res.existing_method.method_id,
          domain: res.existing_method.domain,
        });
      }
    },
    onError: (e) => {
      startLockRef.current = false;
      setStartLocked(false);
      setError(e instanceof ApiError ? e.message : "启动探查失败");
    },
  });

  const nameMut = useMutation({
    mutationFn: (request: MultiDiscoveryNameRequest) => suggestDiscoveryName(request),
    onSuccess: (r) => {
      setName(r.name);
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

  const running = runQuery.data?.status === "running";
  const completed = runQuery.data?.status === "completed";
  const failed = runQuery.data?.status === "failed";
  const cancelled = runQuery.data?.status === "cancelled";
  const startPending = startMut.isPending;
  const cancelBusy = cancelMut.isPending;
  const startBusy = running || startPending || startLocked || startLockRef.current;

  // Disabled states from parser
  const startDisabled = Boolean(routeState.validationError) || startBusy;
  const autoNameDisabled = Boolean(routeState.validationError) || !routeState.value || nameMut.isPending;

  function startRun(force: boolean) {
    if (routeState.validationError || running || startMut.isPending || startLockRef.current) return;
    if (!routeState.resolvedRouteType) return;
    startLockRef.current = true;
    setStartLocked(true);
    if (force) setDup(null);
    const request: MultiDiscoveryStartRequest = {
      input: routeState.value,
      display_input: routeState.displayValue,
      selected_route_type: selectedRouteType,
      resolved_route_type: routeState.resolvedRouteType,
      route_source: routeState.routeSource,
      name: name || undefined,
      force,
    };
    startMut.mutate(request);
  }

  function resetPanelState() {
    startLockRef.current = false;
    skipNextPersistRef.current = true;
    notifiedRef.current = null;
    clearDiscoveryPanelState();
    setRawInput("");
    setSelectedRouteType(null);
    setName("");
    setNameError(null);
    setRunId(null);
    setDup(null);
    setError(null);
    setSelectedNode(null);
    setExpanded(true);
    setStartLocked(false);
    startMut.reset();
    cancelMut.reset();
    nameMut.reset();
    queryClient.removeQueries({ queryKey: ["discovery-run"] });
  }

  // 完成后通知新方式
  useEffect(() => {
    const mid = runQuery.data?.resulting_method_id;
    if (completed && mid != null && notifiedRef.current !== mid) {
      notifiedRef.current = mid;
      onMethodAdded?.(mid);
    }
  }, [completed, runQuery.data?.resulting_method_id, onMethodAdded]);

  // Persist state
  useEffect(() => {
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    writeDiscoveryPanelState({
      rawInput,
      selectedRouteType,
      name,
      runId,
      selectedNode,
      expanded,
    });
  }, [rawInput, selectedRouteType, name, runId, selectedNode, expanded]);

  useEffect(() => {
    if (!(runQuery.error instanceof ApiError) || runQuery.error.status !== 404 || runId == null) {
      return;
    }
    setRunId(null);
  }, [runId, runQuery.error]);

  const displayRun: DiscoveryRun = runQuery.data ?? {
    id: 0,
    site_url: rawInput || "",
    status: "cancelled",
    resulting_method_id: null,
    llm_token_usage: 0,
    node_trace: [],
    retry_count: 0,
    current_step: null,
    started_at: null,
    ended_at: null,
    error_message: null,
  };
  const selectedEntry = displayRun.node_trace.findLast((e) => e.step === selectedNode);

  // Resolved route display label for the inferred badge
  const resolvedLabel = routeState.resolvedRouteType
    ? ROUTE_TYPE_LABELS[routeState.resolvedRouteType]
    : null;

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>智能探查</div>
          {runQuery.data && (
            <div style={{
              fontSize: 12, fontWeight: 700, borderRadius: 999, padding: "3px 11px",
              color: running ? "#175cd3" : completed ? "#059669" : cancelled ? "#b54708" : "#dc2626",
              background: running ? "#eff6ff" : completed ? "#ecfdf3" : cancelled ? "#fffaeb" : "#fef2f2",
              border: `1px solid ${running ? "#b9d4ff" : completed ? "#a3e0c4" : cancelled ? "#fedf89" : "#fca5a5"}`,
            }}>
              {running ? `探查中 · 第 ${currentAttemptRound(runQuery.data)} / 3 轮` : completed ? "探查完成" : cancelled ? "已取消" : "探查失败"}
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button type="button" onClick={resetPanelState} style={resetBtn}>
            重置状态
          </button>
          <button type="button" onClick={() => setExpanded((value) => !value)} style={toggleBtn}>
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      <div style={controlGrid}>
        <div style={routeColumn}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
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
              <option value="wechat_history">微信公众号</option>
              <option value="internal_forum">司内论坛</option>
            </select>
            <input
              placeholder="输入探查内容，如 URL、公众号名、搜索关键词"
              value={rawInput}
              onChange={(e) => {
                setRawInput(e.target.value);
                setNameError(null);
              }}
              style={{ ...inputBase, flex: 1 }}
            />
          </div>
          {resolvedLabel && routeState.routeSource === "inferred" && (
            <div style={{ fontSize: 12, color: "#475467" }}>
              自动推断为 <span style={{ fontWeight: 600, color: "#175cd3" }}>{resolvedLabel}</span>
            </div>
          )}
          {resolvedLabel && routeState.routeSource === "explicit" && selectedRouteType && (
            <div style={{ fontSize: 12, color: "#475467" }}>
              已锁定 <span style={{ fontWeight: 600, color: "#059669" }}>{resolvedLabel}</span>
            </div>
          )}
          {routeState.validationError && (
            <div style={{ color: "#b42318", fontSize: 13 }}>{routeState.validationError}</div>
          )}
        </div>
        <div style={actionColumn}>
          <div style={{ display: "flex", gap: 6, minWidth: 0, alignItems: "center", flexWrap: "nowrap" }}>
            <input
              placeholder="名称（选填）"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setNameError(null);
              }}
              style={{ ...inputBase, flex: "1 1 auto", minWidth: 0, width: "100%" }}
            />
            <button
              type="button"
              onClick={() => {
                if (autoNameDisabled) return;
                setNameError(null);
                const request: MultiDiscoveryNameRequest = {
                  input: routeState.value,
                  display_input: routeState.displayValue,
                  selected_route_type: selectedRouteType,
                  resolved_route_type: routeState.resolvedRouteType,
                  route_source: routeState.routeSource,
                };
                nameMut.mutate(request);
              }}
              disabled={autoNameDisabled}
              style={autoNameDisabled ? { ...btnGhost, cursor: "not-allowed", opacity: 0.5 } : btnGhost}
            >
              {nameMut.isPending ? "命名中…" : "✨ 自动"}
            </button>
            <button
              type="button"
              disabled={startDisabled}
              onClick={() => startRun(false)}
              style={startDisabled ? btnDisabled : btnPrimary}
            >
              {running ? "探查中…" : startBusy ? "启动中…" : "开始探查"}
            </button>
          </div>
          {running && runId != null && (
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-start", justifyContent: "flex-start" }}>
              <button
                type="button"
                disabled={cancelBusy}
                onClick={() => cancelMut.mutate(runId)}
                style={cancelBusy ? btnDisabled : btnDanger}
              >
                {cancelBusy ? "取消中…" : "取消探查"}
              </button>
            </div>
          )}
        </div>
      </div>

      {dup && (
        <div style={{ border: "1px solid #fec84b", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13 }}>
          该域名已有爬取方式（{dup.domain}）。是否覆盖重新探查？
          <button type="button" disabled={startDisabled} style={{ ...(startDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }}
            onClick={() => startRun(true)}>{startBusy ? "启动中…" : "覆盖重探"}</button>
          <button type="button" style={{ ...btnGhost, marginLeft: 8 }} onClick={() => setDup(null)}>取消</button>
        </div>
      )}

      {expanded && (
        <div
          data-testid="discovery-layout-grid"
          style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 14, alignItems: "stretch" }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: WORKSPACE_HEIGHT, height: WORKSPACE_HEIGHT }}>
            <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#f8fafc", padding: 10, flex: 1, minHeight: 0 }}>
              <DiscoveryFlowChart run={displayRun} onSelectNode={setSelectedNode} selectedNode={selectedNode} />
            </div>
            <div style={{ minHeight: 0, height: 210, overflowY: "auto", display: "flex", flexDirection: "column", gap: 10 }}>
              {selectedNode ? (
                <DiscoveryNodeDetail nodeId={selectedNode} entry={selectedEntry} />
              ) : (
                <div style={{ border: "1px dashed #d0d5dd", borderRadius: 10, background: "#fcfcfd", color: "#667085", padding: "16px 18px", fontSize: 13 }}>
                  点击流程图节点查看该步骤的摘要、证据和当前状态。
                </div>
              )}
              {completed && (
                <div style={{ border: "1px solid #a3e0c4", background: "#ecfdf3", color: "#059669", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                  探查完成 · 已进入待审核方式 <a style={{ color: "#175cd3", cursor: "pointer", marginLeft: 8 }} onClick={() => displayRun.resulting_method_id && onMethodAdded?.(displayRun.resulting_method_id)}>查看待审核方式 →</a>
                </div>
              )}
              {failed && (
                <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", color: "#b42318", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                  探查失败：{displayRun.error_message ?? "未知错误"}
                  <button type="button" disabled={startDisabled} style={{ ...(startDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }} onClick={() => startRun(false)}>
                    {startBusy ? "启动中…" : "重新探查"}
                  </button>
                </div>
              )}
              {cancelled && (
                <div style={{ border: "1px solid #fedf89", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                  探查已取消：{displayRun.error_message ?? "已停止后续调用"}
                  <button type="button" disabled={startDisabled} style={{ ...(startDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }} onClick={() => startRun(false)}>
                    {startBusy ? "启动中…" : "重新探查"}
                  </button>
                </div>
              )}
            </div>
          </div>
          <div style={{ minHeight: WORKSPACE_HEIGHT, height: WORKSPACE_HEIGHT }}>
            <DiscoveryLogPanel logs={logs} />
          </div>
        </div>
      )}
      {nameError && <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>{nameError}</div>}
      {error && <div style={{ color: "#b42318", fontSize: 13, marginTop: 10 }}>{error}</div>}
    </section>
  );
}

const inputBase: CSSProperties = {
  border: "1px solid #d0d5dd", borderRadius: 8, padding: "10px 12px",
  fontSize: 14, color: "#101828", background: "#fff",
  minWidth: 0, boxSizing: "border-box",
};
const selectBase: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "10px 12px",
  fontSize: 14,
  color: "#101828",
  background: "#fff",
  flexShrink: 0,
  minWidth: 110,
  boxSizing: "border-box" as const,
};
const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "9px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", flexShrink: 0 };
const btnDisabled: CSSProperties = { ...btnPrimary, background: "#98a2b3", cursor: "not-allowed", flexShrink: 0 };
const btnDanger: CSSProperties = { ...btnPrimary, background: "#dc2626" };
const btnGhost: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 8, padding: "9px 11px", fontSize: 12, color: "#475467", cursor: "pointer", flexShrink: 0 };
const toggleBtn: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 999, padding: "8px 14px", fontSize: 12, color: "#344054", fontWeight: 700, cursor: "pointer" };
const resetBtn: CSSProperties = { ...toggleBtn, color: "#047857", borderColor: "#6ee7b7", background: "#ecfdf3" };
const WORKSPACE_HEIGHT = 760;
const controlGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
  gap: 14,
  alignItems: "start",
  marginBottom: 14,
};
const routeColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  minWidth: 0,
};
const actionColumn: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 10,
  minWidth: 0,
};
