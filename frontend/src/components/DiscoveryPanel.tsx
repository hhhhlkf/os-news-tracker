// frontend/src/components/DiscoveryPanel.tsx
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, getDiscoveryRun, startDiscoveryRun, suggestDiscoveryName } from "../api/client";
import { DiscoveryFlowChart } from "./DiscoveryFlowChart";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { useDiscoveryLogs } from "../hooks/useDiscoveryLogs";
import { attemptCount, type FlowNodeId } from "../discovery/flowState";

export function DiscoveryPanel({ onMethodAdded }: { onMethodAdded?: (methodId: number) => void }) {
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [runId, setRunId] = useState<number | null>(null);
  const [dup, setDup] = useState<{ method_id: number; domain: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<FlowNodeId | null>(null);

  const runQuery = useQuery({
    queryKey: ["discovery-run", runId],
    queryFn: () => getDiscoveryRun(runId!),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
  const logs = useDiscoveryLogs(runId, runId != null && runQuery.data?.status === "running");

  const startMut = useMutation({
    mutationFn: (vars: { url: string; name?: string; force: boolean }) =>
      startDiscoveryRun(vars.url, vars.name, vars.force),
    onSuccess: (res) => {
      setError(null);
      if (res.status === "started" && res.run_id != null) { setDup(null); setRunId(res.run_id); }
      else if (res.status === "duplicate" && res.existing_method) {
        setDup({ method_id: res.existing_method.method_id, domain: res.existing_method.domain });
      }
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "启动探查失败"),
  });

  const nameMut = useMutation({
    mutationFn: (u: string) => suggestDiscoveryName(u),
    onSuccess: (r) => setName(r.name),
    onError: () => {},
  });

  const running = runQuery.data?.status === "running";
  const completed = runQuery.data?.status === "completed";
  const failed = runQuery.data?.status === "failed";

  // 完成后通知新方式（useEffect + ref 守卫，避免渲染期副作用）
  const notifiedRef = useRef<number | null>(null);
  useEffect(() => {
    const mid = runQuery.data?.resulting_method_id;
    if (completed && mid != null && notifiedRef.current !== mid) {
      notifiedRef.current = mid;
      onMethodAdded?.(mid);
    }
  }, [completed, runQuery.data?.resulting_method_id, onMethodAdded]);

  const selectedEntry = runQuery.data?.node_trace.find((e) => e.step === selectedNode);

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>智能探查</div>
        {runQuery.data && (
          <div style={{
            fontSize: 12, fontWeight: 700, borderRadius: 999, padding: "3px 11px",
            color: running ? "#175cd3" : completed ? "#059669" : "#dc2626",
            background: running ? "#eff6ff" : completed ? "#ecfdf3" : "#fef2f2",
            border: `1px solid ${running ? "#b9d4ff" : completed ? "#a3e0c4" : "#fca5a5"}`,
          }}>
            {running ? `探查中 · 第 ${attemptCount(runQuery.data.node_trace)} / 3 轮` : completed ? "探查完成" : "探查失败"}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 14 }}>
        <input placeholder="站点 URL，如 openanolis.cn/blog" value={url}
          onChange={(e) => setUrl(e.target.value)} style={{ ...inputBase, flex: "1 1 260px", minWidth: 200 }} />
        <div style={{ display: "flex", gap: 6, flex: "1 1 210px", minWidth: 190 }}>
          <input placeholder="名称（选填）" value={name}
            onChange={(e) => setName(e.target.value)} style={{ ...inputBase, flex: 1 }} />
          <button type="button" onClick={() => url && nameMut.mutate(url)}
            disabled={!url || nameMut.isPending}
            style={btnGhost}>✨ 自动</button>
        </div>
        <button type="button" disabled={!url || running} onClick={() => startMut.mutate({ url, name: name || undefined, force: false })}
          style={running ? btnDisabled : btnPrimary}>{running ? "探查中…" : "开始探查"}</button>
      </div>

      {dup && (
        <div style={{ border: "1px solid #fec84b", background: "#fffaeb", color: "#b54708", borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13 }}>
          该域名已有爬取方式（{dup.domain}）。是否覆盖重新探查？
          <button type="button" style={{ ...btnPrimary, marginLeft: 12 }}
            onClick={() => { startMut.mutate({ url, name: name || undefined, force: true }); setDup(null); }}>覆盖重探</button>
          <button type="button" style={{ ...btnGhost, marginLeft: 8 }} onClick={() => setDup(null)}>取消</button>
        </div>
      )}

      {runQuery.data && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#f8fafc", padding: 10 }}>
              <DiscoveryFlowChart run={runQuery.data} onSelectNode={setSelectedNode} selectedNode={selectedNode} />
            </div>
            {selectedNode && <DiscoveryNodeDetail nodeId={selectedNode} entry={selectedEntry} />}
            {completed && (
              <div style={{ border: "1px solid #a3e0c4", background: "#ecfdf3", color: "#059669", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                探查完成 · 已存入爬取方式库 <a style={{ color: "#175cd3", cursor: "pointer", marginLeft: 8 }} onClick={() => runQuery.data?.resulting_method_id && onMethodAdded?.(runQuery.data.resulting_method_id)}>查看新方式 →</a>
              </div>
            )}
            {failed && (
              <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", color: "#b42318", borderRadius: 8, padding: "10px 12px", fontSize: 13 }}>
                探查失败：{runQuery.data.error_message ?? "未知错误"}
                <button type="button" style={{ ...btnPrimary, marginLeft: 12 }} onClick={() => startMut.mutate({ url, name: name || undefined, force: false })}>重新探查</button>
              </div>
            )}
          </div>
          <DiscoveryLogPanel logs={logs} />
        </div>
      )}
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
const btnGhost: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 8, padding: "9px 11px", fontSize: 12, color: "#475467", cursor: "pointer" };
