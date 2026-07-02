// frontend/src/components/DiscoveryPanel.tsx
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, cancelDiscoveryRun, getDiscoveryRun, startDiscoveryRun, suggestDiscoveryName } from "../api/client";
import { DiscoveryFlowChart } from "./DiscoveryFlowChart";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { useDiscoveryLogs } from "../hooks/useDiscoveryLogs";
import { attemptCount, type FlowNodeId } from "../discovery/flowState";

export function DiscoveryPanel({ onMethodAdded }: { onMethodAdded?: (methodId: number) => void }) {
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [runId, setRunId] = useState<number | null>(null);
  const [dup, setDup] = useState<{ method_id: number; domain: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<FlowNodeId | null>(null);
  const [startLocked, setStartLocked] = useState(false);
  const startLockRef = useRef(false);

  const runQuery = useQuery({
    queryKey: ["discovery-run", runId],
    queryFn: () => getDiscoveryRun(runId!),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const logs = useDiscoveryLogs(runId, runId != null && runQuery.data?.status === "running");
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
    mutationFn: (vars: { url: string; name?: string; force: boolean }) =>
      startDiscoveryRun(vars.url, vars.name, vars.force),
    onSuccess: (res) => {
      startLockRef.current = false;
      setStartLocked(false);
      setError(null);
      if (res.status === "started" && res.run_id != null) { setDup(null); setRunId(res.run_id); }
      else if (res.status === "duplicate" && res.existing_method) {
        setDup({ method_id: res.existing_method.method_id, domain: res.existing_method.domain });
      }
    },
    onError: (e) => {
      startLockRef.current = false;
      setStartLocked(false);
      setError(e instanceof ApiError ? e.message : "启动探查失败");
    },
  });

  const nameMut = useMutation({
    mutationFn: (u: string) => suggestDiscoveryName(u),
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
  const startBusy = running || startPending || startLocked || startLockRef.current;
  const startDisabled = !url || startBusy;
  const cancelBusy = cancelMut.isPending;

  function startRun(force: boolean) {
    if (!url || running || startMut.isPending || startLockRef.current) return;
    startLockRef.current = true;
    setStartLocked(true);
    if (force) setDup(null);
    startMut.mutate({ url, name: name || undefined, force });
  }

  // 完成后通知新方式（useEffect + ref 守卫，避免渲染期副作用）
  const notifiedRef = useRef<number | null>(null);
  useEffect(() => {
    const mid = runQuery.data?.resulting_method_id;
    if (completed && mid != null && notifiedRef.current !== mid) {
      notifiedRef.current = mid;
      onMethodAdded?.(mid);
    }
  }, [completed, runQuery.data?.resulting_method_id, onMethodAdded]);

  const selectedEntry = runQuery.data?.node_trace.findLast((e) => e.step === selectedNode);

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>智能探查</div>
        {runQuery.data && (
          <div style={{
            fontSize: 12, fontWeight: 700, borderRadius: 999, padding: "3px 11px",
            color: running ? "#175cd3" : completed ? "#059669" : cancelled ? "#b54708" : "#dc2626",
            background: running ? "#eff6ff" : completed ? "#ecfdf3" : cancelled ? "#fffaeb" : "#fef2f2",
            border: `1px solid ${running ? "#b9d4ff" : completed ? "#a3e0c4" : cancelled ? "#fedf89" : "#fca5a5"}`,
          }}>
            {running ? `探查中 · 第 ${attemptCount(runQuery.data.node_trace)} / 3 轮` : completed ? "探查完成" : cancelled ? "已取消" : "探查失败"}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 14 }}>
        <input placeholder="站点 URL，如 openanolis.cn/blog" value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setNameError(null);
          }} style={{ ...inputBase, flex: "1 1 260px", minWidth: 200 }} />
        <div style={{ display: "flex", gap: 6, flex: "1 1 210px", minWidth: 190 }}>
          <input placeholder="名称（选填）" value={name}
            onChange={(e) => {
              setName(e.target.value);
              setNameError(null);
            }} style={{ ...inputBase, flex: 1 }} />
          <button type="button" onClick={() => {
            if (!url) return;
            setNameError(null);
            nameMut.mutate(url);
          }}
            disabled={!url || nameMut.isPending}
            style={btnGhost}>{nameMut.isPending ? "命名中…" : "✨ 自动"}</button>
        </div>
        <button type="button" disabled={startDisabled} onClick={() => startRun(false)}
          style={startDisabled ? btnDisabled : btnPrimary}>{running ? "探查中…" : startBusy ? "启动中…" : "开始探查"}</button>
        {running && runId != null && (
          <button
            type="button"
            disabled={cancelBusy}
            onClick={() => cancelMut.mutate(runId)}
            style={cancelBusy ? btnDisabled : btnDanger}
          >
            {cancelBusy ? "取消中…" : "取消探查"}
          </button>
        )}
      </div>

      {dup && (
        <div style={{ border: "1px solid #fec84b", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13 }}>
          该域名已有爬取方式（{dup.domain}）。是否覆盖重新探查？
          <button type="button" disabled={startDisabled} style={{ ...(startDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }}
            onClick={() => startRun(true)}>{startBusy ? "启动中…" : "覆盖重探"}</button>
          <button type="button" style={{ ...btnGhost, marginLeft: 8 }} onClick={() => setDup(null)}>取消</button>
        </div>
      )}

      {runQuery.data && (
        <div
          data-testid="discovery-layout-grid"
          style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 14, alignItems: "stretch" }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: WORKSPACE_HEIGHT, height: WORKSPACE_HEIGHT }}>
            <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#f8fafc", padding: 10, flex: 1, minHeight: 0 }}>
              <DiscoveryFlowChart run={runQuery.data} onSelectNode={setSelectedNode} selectedNode={selectedNode} />
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
                  探查完成 · 已存入爬取方式库 <a style={{ color: "#175cd3", cursor: "pointer", marginLeft: 8 }} onClick={() => runQuery.data?.resulting_method_id && onMethodAdded?.(runQuery.data.resulting_method_id)}>查看新方式 →</a>
                </div>
              )}
              {failed && (
                <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", color: "#b42318", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                  探查失败：{runQuery.data.error_message ?? "未知错误"}
                  <button type="button" disabled={startDisabled} style={{ ...(startDisabled ? btnDisabled : btnPrimary), marginLeft: 12 }} onClick={() => startRun(false)}>
                    {startBusy ? "启动中…" : "重新探查"}
                  </button>
                </div>
              )}
              {cancelled && (
                <div style={{ border: "1px solid #fedf89", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                  探查已取消：{runQuery.data.error_message ?? "已停止后续调用"}
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
};
const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "9px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDisabled: CSSProperties = { ...btnPrimary, background: "#98a2b3", cursor: "not-allowed" };
const btnDanger: CSSProperties = { ...btnPrimary, background: "#dc2626" };
const btnGhost: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 8, padding: "9px 11px", fontSize: 12, color: "#475467", cursor: "pointer" };
const WORKSPACE_HEIGHT = 760;
