// frontend/src/components/CrawlMethodList.tsx
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, deleteDiscoveryMethod, fetchDiscoveryMethod, listDiscoveryMethods } from "../api/client";
import type { CrawlMethod } from "../types";
import { buildManualNewsRunRequest } from "./NewsRunControl";
import type { NewsRunFormState } from "./NewsRunControl";

type RowState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; discovered: number; stored: number }
  | { kind: "cancelled" }
  | { kind: "error"; msg: string };

const PAGE_SIZE_OPTIONS = [10, 20, 50];

export function CrawlMethodList({ onOpenMethod, highlightId, runLimitState }: {
  onOpenMethod?: (id: number) => void;
  highlightId?: number | null;
  runLimitState: NewsRunFormState;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [rowStates, setRowStates] = useState<Record<number, RowState>>({});
  const [summary, setSummary] = useState<{ text: string; tone: "success" | "danger"; showItemsLink: boolean } | null>(null);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchCancelling, setBatchCancelling] = useState(false);
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE_OPTIONS[0]);
  const abortRef = useRef<AbortController | null>(null);
  const cancelledRef = useRef(false);

  const list = useQuery({ queryKey: ["discovery-methods"], queryFn: listDiscoveryMethods });
  const methods = useMemo(() => list.data ?? [], [list.data]);
  const totalPages = Math.max(1, Math.ceil(methods.length / pageSize));
  const pageStart = (page - 1) * pageSize;
  const pageMethods = methods.slice(pageStart, pageStart + pageSize);
  const selectablePageIds = pageMethods.filter((m) => m.status !== "disabled").map((m) => m.id);
  const pageSelectedCount = selectablePageIds.filter((id) => selected.has(id)).length;
  const allPageSelected = selectablePageIds.length > 0 && pageSelectedCount === selectablePageIds.length;

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  const fetchMut = useMutation({
    mutationFn: ({ id, request, signal }: { id: number; request: ReturnType<typeof buildManualNewsRunRequest>; signal?: AbortSignal }) =>
      fetchDiscoveryMethod(id, request, signal),
  });
  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteDiscoveryMethod(id),
  });

  async function batchFetch() {
    const ids = [...selected];
    const request = buildManualNewsRunRequest(runLimitState);
    if (!request) {
      setSummary({ text: "抓取限制无效，请先补全时间范围和目标条目数。", tone: "danger", showItemsLink: false });
      return;
    }
    if (ids.length === 0) {
      setSummary({ text: "请先选择至少一个爬取方式。", tone: "danger", showItemsLink: false });
      return;
    }
    setSummary(null);
    setBatchRunning(true);
    setBatchCancelling(false);
    cancelledRef.current = false;
    let totalDisc = 0;
    let totalStored = 0;
    try {
      for (const id of ids) {
        if (cancelledRef.current) break;
        const controller = new AbortController();
        abortRef.current = controller;
        setRowStates((s) => ({ ...s, [id]: { kind: "running" } }));
        try {
          const r = await fetchMut.mutateAsync({ id, request, signal: controller.signal });
          if (cancelledRef.current || controller.signal.aborted) {
            setRowStates((s) => ({ ...s, [id]: { kind: "cancelled" } }));
            break;
          }
          totalDisc += r.discovered_count;
          totalStored += r.stored_count;
          setRowStates((s) => ({ ...s, [id]: { kind: "done", discovered: r.discovered_count, stored: r.stored_count } }));
        } catch (e) {
          const aborted = controller.signal.aborted || e instanceof DOMException && e.name === "AbortError";
          if (aborted || cancelledRef.current) {
            setRowStates((s) => ({ ...s, [id]: { kind: "cancelled" } }));
            break;
          }
          setRowStates((s) => ({ ...s, [id]: { kind: "error", msg: e instanceof ApiError ? e.message : "抓取失败" } }));
        } finally {
          if (abortRef.current === controller) {
            abortRef.current = null;
          }
        }
      }
      setSummary({
        text: cancelledRef.current ? `已取消抓取 · 已处理 ${totalDisc} 条 · 入库 ${totalStored} 条` : `本次抓取 ${totalDisc} 条 · 入库 ${totalStored} 条`,
        tone: "success",
        showItemsLink: true,
      });
      await qc.invalidateQueries({ queryKey: ["discovery-methods"] });
    } finally {
      setBatchRunning(false);
      setBatchCancelling(false);
      abortRef.current = null;
    }
  }

  function cancelBatch() {
    if (!batchRunning) return;
    cancelledRef.current = true;
    setBatchCancelling(true);
    abortRef.current?.abort();
    setSummary({ text: "正在取消当前抓取批次…", tone: "danger", showItemsLink: false });
  }

  async function batchDelete() {
    const ids = [...selected];
    if (ids.length === 0) {
      setSummary({ text: "请先选择至少一个爬取方式。", tone: "danger", showItemsLink: false });
      return;
    }
    const confirmed = window.confirm(`删除选中的 ${ids.length} 个链接方式？此操作会从方式库移除它们。`);
    if (!confirmed) return;
    setSummary(null);
    setBatchDeleting(true);
    let deletedCount = 0;
    try {
      for (const id of ids) {
        await deleteMut.mutateAsync(id);
        deletedCount += 1;
      }
      setSelected(new Set());
      setRowStates((states) => {
        const next = { ...states };
        for (const id of ids) {
          delete next[id];
        }
        return next;
      });
      setSummary({ text: `已批量删除 ${deletedCount} 个链接方式`, tone: "success", showItemsLink: false });
      await qc.invalidateQueries({ queryKey: ["discovery-methods"] });
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "批量删除失败", tone: "danger", showItemsLink: false });
    } finally {
      setBatchDeleting(false);
    }
  }

  function toggle(id: number, enabled: boolean) {
    setSelected((s) => { const n = new Set(s); enabled ? n.add(id) : n.delete(id); return n; });
  }

  function toggleCurrentPage(enabled: boolean) {
    setSelected((s) => {
      const n = new Set(s);
      for (const id of selectablePageIds) {
        enabled ? n.add(id) : n.delete(id);
      }
      return n;
    });
  }

  return (
    <section style={{ background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · 爬取方式库</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2 }}>按顺序抓取已选方式，可随时取消当前批次。结果会进入新闻流和运行日志。</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#475467" }}>已选 <b style={{ color: "#101828" }}>{selected.size}</b> 个</span>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#475467", cursor: selectablePageIds.length === 0 ? "not-allowed" : "pointer" }}>
            <input
              type="checkbox"
              aria-label="全选当前页爬取方式"
              checked={allPageSelected}
              disabled={selectablePageIds.length === 0 || batchRunning || batchDeleting}
              onChange={(e) => toggleCurrentPage(e.target.checked)}
            />
            全选本页
          </label>
          <button
            type="button"
            style={batchRunning ? btnDanger : btnPrimary}
            disabled={batchRunning ? batchCancelling : selected.size === 0 || batchDeleting}
            onClick={batchRunning ? cancelBatch : batchFetch}
          >
            {batchRunning ? (batchCancelling ? "取消中…" : "取消抓取") : "抓取选中"}
          </button>
          <button
            type="button"
            style={selected.size === 0 || batchRunning || batchDeleting ? btnDangerDisabled : btnDangerGhost}
            disabled={selected.size === 0 || batchRunning || batchDeleting}
            onClick={batchDelete}
          >
            {batchDeleting ? "删除中…" : "批量删除链接"}
          </button>
        </div>
      </div>

      {summary && (
        <div style={{ fontSize: 13, color: summary.tone === "danger" ? "#b42318" : "#059669", marginBottom: 10 }}>
          {summary.text}
          {summary.showItemsLink ? <> · <a style={{ color: "#175cd3", cursor: "pointer" }} onClick={() => (window.location.href = "/")}>查看入库条目 →</a></> : null}
        </div>
      )}

      <div style={{ display: "grid", gap: 8 }}>
        {list.isLoading && (
          <div style={infoBox}>
            加载爬取方式中...
          </div>
        )}
        {list.isError && (
          <div style={{ ...infoBox, border: "1px solid #fecdca", background: "#fef3f2", color: "#b42318" }}>
            加载爬取方式失败，请稍后重试。
          </div>
        )}
        {pageMethods.map((m) => (
          <MethodRow key={m.id} m={m} selected={selected.has(m.id)} state={rowStates[m.id]}
            onToggle={(en) => toggle(m.id, en)} onOpen={() => onOpenMethod?.(m.id)} highlight={highlightId === m.id} busy={batchRunning || batchDeleting} />
        ))}
        {list.data && methods.length === 0 && (
          <div style={{ border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 }}>
            还没有爬取方式。用上方"智能探查"为一个网站生成爬取方式。
          </div>
        )}
      </div>
      {methods.length > 0 && (
        <div style={pagerBar}>
          <div style={{ color: "#667085" }}>
            第 {pageStart + 1}-{Math.min(pageStart + pageSize, methods.length)} 条 / 共 {methods.length} 条
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              每页
              <select
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value));
                  setPage(1);
                }}
                style={selectStyle}
              >
                {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
              </select>
            </label>
            <button type="button" style={page <= 1 ? pagerButtonDisabled : pagerButton} disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
              上一页
            </button>
            <span style={{ minWidth: 56, textAlign: "center", color: "#475467" }}>{page} / {totalPages}</span>
            <button type="button" style={page >= totalPages ? pagerButtonDisabled : pagerButton} disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>
              下一页
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function MethodRow({ m, selected, state, onToggle, onOpen, highlight, busy }: {
  m: CrawlMethod; selected: boolean; state?: RowState;
  onToggle: (enabled: boolean) => void; onOpen: () => void; highlight: boolean; busy: boolean;
}) {
  const disabled = m.status === "disabled";
  const primaryLabel = m.source_name?.trim() || m.domain;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, border: `1px solid ${selected ? "#b9d4ff" : highlight ? "#175cd3" : "#eaecf0"}`,
      borderRadius: 9, padding: "9px 11px", background: selected ? "#f8fbff" : highlight ? "#eff6ff" : "#fff" }}>
      <input type="checkbox" aria-label={`选择 ${primaryLabel}`} checked={selected} disabled={disabled || busy} onChange={(e) => onToggle(e.target.checked)} />
      <div style={{ flex: 1, minWidth: 0, cursor: "pointer" }} onClick={onOpen}>
        <div style={{ fontSize: 14, fontWeight: 700, color: disabled ? "#98a2b3" : "#101828" }}>
          {primaryLabel} <span style={badge(m.status)}>{m.status}</span>
        </div>
        <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all" }}>
          {m.domain !== primaryLabel ? `${m.domain} · ` : ""}{m.entry_url}
        </div>
      </div>
      <div style={{ fontSize: 12, color: "#475467", textAlign: "right", minWidth: 150 }}>
        {state?.kind === "running" && <span style={{ color: "#175cd3" }}>抓取中…</span>}
        {state?.kind === "done" && <>抓取 {state.discovered} · 入库 {state.stored}</>}
        {state?.kind === "cancelled" && <span style={{ color: "#b54708" }}>已取消</span>}
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
const btnDanger: CSSProperties = { ...btnPrimary, background: "#dc2626" };
const btnDangerGhost: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDangerDisabled: CSSProperties = { ...btnDangerGhost, color: "#98a2b3", border: "1px solid #eaecf0", cursor: "not-allowed" };
const infoBox: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
const pagerBar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 12,
  flexWrap: "wrap",
  marginTop: 12,
  paddingTop: 12,
  borderTop: "1px solid #eaecf0",
  fontSize: 12,
};
const pagerButton: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "6px 10px",
  background: "#fff",
  color: "#344054",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
};
const pagerButtonDisabled: CSSProperties = {
  ...pagerButton,
  color: "#98a2b3",
  background: "#f9fafb",
  cursor: "not-allowed",
};
const selectStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "5px 8px",
  background: "#fff",
  color: "#344054",
  fontSize: 12,
};
