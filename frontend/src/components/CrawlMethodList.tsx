// frontend/src/components/CrawlMethodList.tsx
import { useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchDiscoveryMethod, listDiscoveryMethods } from "../api/client";
import type { CrawlMethod } from "../types";

type RowState = { kind: "idle" } | { kind: "running" } | { kind: "done"; discovered: number; stored: number } | { kind: "error"; msg: string };

export function CrawlMethodList({ onOpenMethod, highlightId }: {
  onOpenMethod?: (id: number) => void; highlightId?: number | null;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [rowStates, setRowStates] = useState<Record<number, RowState>>({});
  const [summary, setSummary] = useState<string | null>(null);

  const list = useQuery({ queryKey: ["discovery-methods"], queryFn: listDiscoveryMethods });
  const fetchMut = useMutation({
    mutationFn: (id: number) => fetchDiscoveryMethod(id),
  });

  async function batchFetch() {
    const ids = [...selected];
    setSummary(null);
    let totalDisc = 0, totalStored = 0;
    await Promise.all(ids.map(async (id) => {
      setRowStates((s) => ({ ...s, [id]: { kind: "running" } }));
      try {
        const r = await fetchMut.mutateAsync(id);
        totalDisc += r.discovered_count; totalStored += r.stored_count;
        setRowStates((s) => ({ ...s, [id]: { kind: "done", discovered: r.discovered_count, stored: r.stored_count } }));
      } catch (e) {
        setRowStates((s) => ({ ...s, [id]: { kind: "error", msg: e instanceof ApiError ? e.message : "抓取失败" } }));
      }
    }));
    setSummary(`本次抓取 ${totalDisc} 条 · 入库 ${totalStored} 条`);
    await qc.invalidateQueries({ queryKey: ["discovery-methods"] });
  }

  function toggle(id: number, enabled: boolean) {
    setSelected((s) => { const n = new Set(s); enabled ? n.add(id) : n.delete(id); return n; });
  }

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · 爬取方式库</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2 }}>勾选若干个一键抓取，结果进入新闻流。</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#475467" }}>已选 <b style={{ color: "#101828" }}>{selected.size}</b> 个</span>
          <button type="button" style={btnPrimary} disabled={selected.size === 0} onClick={batchFetch}>抓取选中</button>
          <a style={{ fontSize: 12, color: "#667085", cursor: "pointer" }} onClick={() => setSelected(new Set())}>清空</a>
        </div>
      </div>

      {summary && <div style={{ fontSize: 13, color: "#059669", marginBottom: 10 }}>{summary} · <a style={{ color: "#175cd3", cursor: "pointer" }} onClick={() => (window.location.href = "/")}>查看入库条目 →</a></div>}

      <div style={{ display: "grid", gap: 8 }}>
        {(list.data ?? []).map((m) => (
          <MethodRow key={m.id} m={m} selected={selected.has(m.id)} state={rowStates[m.id]}
            onToggle={(en) => toggle(m.id, en)} onOpen={() => onOpenMethod?.(m.id)} highlight={highlightId === m.id} />
        ))}
        {list.data && list.data.length === 0 && (
          <div style={{ border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 }}>
            还没有爬取方式。用上方"智能探查"为一个网站生成爬取方式。
          </div>
        )}
      </div>
    </section>
  );
}

function MethodRow({ m, selected, state, onToggle, onOpen, highlight }: {
  m: CrawlMethod; selected: boolean; state?: RowState;
  onToggle: (enabled: boolean) => void; onOpen: () => void; highlight: boolean;
}) {
  const disabled = m.status === "disabled";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, border: `1px solid ${selected ? "#b9d4ff" : highlight ? "#175cd3" : "#eaecf0"}`,
      borderRadius: 9, padding: "9px 11px", background: selected ? "#f8fbff" : highlight ? "#eff6ff" : "#fff" }}>
      <input type="checkbox" checked={selected} disabled={disabled} onChange={(e) => onToggle(e.target.checked)} />
      <div style={{ flex: 1, minWidth: 0, cursor: "pointer" }} onClick={onOpen}>
        <div style={{ fontSize: 14, fontWeight: 700, color: disabled ? "#98a2b3" : "#101828" }}>
          {m.domain} <span style={badge(m.status)}>{m.status}</span>
        </div>
        <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all" }}>{m.entry_url}</div>
      </div>
      <div style={{ fontSize: 12, color: "#475467", textAlign: "right", minWidth: 150 }}>
        {state?.kind === "running" && <span style={{ color: "#175cd3" }}>抓取中…</span>}
        {state?.kind === "done" && <>抓取 {state.discovered} · 入库 {state.stored}</>}
        {state?.kind === "error" && <span style={{ color: "#b42318" }}>{state.msg}</span>}
        {(!state || state.kind === "idle") && (m.last_run_status ? `${m.last_run_status}` : "未运行")}
      </div>
    </div>
  );
}

function badge(status: string): CSSProperties {
  const base: CSSProperties = { fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999, marginLeft: 6 };
  if (status === "active") return { ...base, background: "#ecfdf3", color: "#027a48" };
  if (status === "failed") return { ...base, background: "#fef2f2", color: "#b42318" };
  return { ...base, background: "#f2f4f7", color: "#667085" };
}

const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
