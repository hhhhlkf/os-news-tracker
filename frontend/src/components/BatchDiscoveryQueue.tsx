import { useEffect, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  deleteDiscoveryQueueItem,
  listDiscoveryQueue,
  requeueDiscoveryQueueItem,
  stopDiscoveryQueue,
} from "../api/client";
import type { DiscoveryQueueItem } from "../types";
import { ROUTE_TYPE_LABELS } from "../discovery/routeInput";

const QUEUE_QUERY_KEY = ["discovery-queue"] as const;

function itemLabel(item: DiscoveryQueueItem): string {
  return item.name?.trim() || item.display_input?.trim() || item.input;
}

function itemRoute(item: DiscoveryQueueItem): string | null {
  const route = item.resolved_route_type ?? item.selected_route_type;
  return route ? ROUTE_TYPE_LABELS[route] : null;
}

export function BatchDiscoveryQueue({
  canManage = false,
  selectedId = null,
  onSelect,
}: {
  canManage?: boolean;
  selectedId?: number | null;
  onSelect?: (id: number | null, item?: DiscoveryQueueItem) => void;
}) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const queueQuery = useQuery({
    queryKey: QUEUE_QUERY_KEY,
    queryFn: listDiscoveryQueue,
    refetchInterval: 1500,
  });

  const pending = queueQuery.data?.pending ?? [];
  const failed = queueQuery.data?.failed ?? [];
  const runningCount = queueQuery.data?.running_count ?? pending.filter((item) => ["starting", "running", "cancelling"].includes(item.status)).length;
  const maxParallel = queueQuery.data?.max_parallel ?? 2;

  useEffect(() => {
    if (selectedId != null) return;
    const firstRunning = pending.find((item) => item.status === "running");
    if (firstRunning) onSelect?.(firstRunning.id);
  }, [onSelect, pending, selectedId]);

  const requeueMut = useMutation({
    mutationFn: requeueDiscoveryQueueItem,
    onSuccess: (snapshot) => {
      setError(null);
      queryClient.setQueryData(QUEUE_QUERY_KEY, snapshot);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "重新放入失败"),
  });
  const deleteMut = useMutation({
    mutationFn: deleteDiscoveryQueueItem,
    onSuccess: (snapshot, itemId) => {
      setError(null);
      queryClient.setQueryData(QUEUE_QUERY_KEY, snapshot);
      if (selectedId === itemId) onSelect?.(null);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "删除失败"),
  });
  const stopMut = useMutation({
    mutationFn: stopDiscoveryQueue,
    onSuccess: (snapshot) => {
      setError(null);
      queryClient.setQueryData(QUEUE_QUERY_KEY, snapshot);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "停止探查失败"),
  });

  const busy = requeueMut.isPending || deleteMut.isPending || stopMut.isPending;

  return (
    <section style={shell}>
      <div style={headerRow}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, flexWrap: "wrap" }}>
          <SlotMeter active={runningCount} max={maxParallel} />
          <span style={countChip}>{pending.length} 未查取</span>
          <span style={{ ...countChip, color: "#b54708", background: "#fffaeb", borderColor: "#fedf89" }}>{failed.length} 失败</span>
        </div>
        <div style={batchActions}>
          {canManage && (
            <button
              type="button"
              disabled={busy || runningCount === 0}
              onClick={() => stopMut.mutate()}
              style={runningCount === 0 || busy ? btnDisabled : btnDanger}
            >
              {stopMut.isPending ? "停止中…" : "停止全部"}
            </button>
          )}
          <button type="button" onClick={() => setExpanded((value) => !value)} style={toggleBtn}>
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {expanded && (
        <>
          <div style={laneGrid}>
            <QueueLane
              title="未查取"
              empty="队列空闲。从上方加入目标后，最多同时探查两个。"
              items={pending}
              selectedId={selectedId}
              canManage={canManage}
              busy={busy}
              onSelect={(id, item) => onSelect?.(id, item)}
              onDelete={(id) => deleteMut.mutate(id)}
            />
            <QueueLane
              title="失败"
              empty="失败项会停在这里，可重新放入未查取队列。"
              items={failed}
              selectedId={selectedId}
              canManage={canManage}
              busy={busy}
              onSelect={(id, item) => onSelect?.(id, item)}
              onDelete={(id) => deleteMut.mutate(id)}
              onRequeue={(id) => requeueMut.mutate(id)}
              requeuePending={requeueMut.isPending}
            />
          </div>
          <div style={idleHint}>点选队列中的任务，流程图与运行日志会显示在上方智能探查区。</div>
        </>
      )}
      {error && <div style={{ color: "#b42318", fontSize: 13, marginTop: 8 }}>{error}</div>}
    </section>
  );
}

function SlotMeter({ active, max }: { active: number; max: number }) {
  const filled = Math.max(0, Math.min(max, active));
  return (
    <div style={slotMeter} title={`并行 ${filled}/${max}`}>
      {Array.from({ length: max }, (_, index) => (
        <span
          key={index}
          style={{
            width: 8,
            height: 8,
            borderRadius: 2,
            background: index < filled ? "#175cd3" : "#d0d5dd",
            boxShadow: index < filled ? "0 0 0 3px rgba(23,92,211,0.16)" : undefined,
          }}
        />
      ))}
      <span style={{ fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 11, color: "#175cd3", fontWeight: 700 }}>
        {filled}/{max}
      </span>
    </div>
  );
}

function QueueLane({
  title,
  empty,
  items,
  selectedId,
  canManage,
  busy,
  onSelect,
  onDelete,
  onRequeue,
  requeuePending = false,
}: {
  title: string;
  empty: string;
  items: DiscoveryQueueItem[];
  selectedId: number | null;
  canManage: boolean;
  busy: boolean;
  onSelect: (id: number, item: DiscoveryQueueItem) => void;
  onDelete: (id: number) => void;
  onRequeue?: (id: number) => void;
  requeuePending?: boolean;
}) {
  return (
    <div style={lane}>
      <div style={laneTitle}>{title} · {items.length}</div>
      <div style={laneList}>
        {items.length === 0 ? (
          <div style={laneEmpty}>{empty}</div>
        ) : items.map((item) => {
          const selected = item.id === selectedId;
          const running = ["starting", "running", "cancelling"].includes(item.status);
          return (
            <div
              key={item.id}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(item.id, item)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(item.id, item);
                }
              }}
              style={{
                ...laneRow,
                borderColor: selected ? "#b9d4ff" : "#eaecf0",
                background: selected ? "#eff6ff" : "#fff",
                boxShadow: running ? "inset 3px 0 0 #175cd3" : item.status === "failed" ? "inset 3px 0 0 #dc2626" : "inset 3px 0 0 #d0d5dd",
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={rowName}>{itemLabel(item)}</div>
                <div style={rowMeta}>
                  {item.status === "cancelling" ? "停止中" : item.status === "starting" ? "启动中" : running ? "探查中" : item.status === "failed" ? "失败" : "排队"}
                  {itemRoute(item) ? ` · ${itemRoute(item)}` : ""}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }} onClick={(event) => event.stopPropagation()}>
                {onRequeue && (
                  <button type="button" disabled={busy} onClick={() => onRequeue(item.id)} style={rowBtn}>
                    {requeuePending ? "放入中…" : "重新放入"}
                  </button>
                )}
                {canManage && (
                  <button type="button" disabled={busy} onClick={() => onDelete(item.id)} style={rowBtnDanger}>
                    删除
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const shell: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 12 };
const headerRow: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "nowrap" };
const batchActions: CSSProperties = { display: "flex", alignItems: "center", gap: 6, flexWrap: "nowrap", flexShrink: 0 };
const countChip: CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: "#344054",
  background: "#f8fafc",
  border: "1px solid #eaecf0",
  borderRadius: 999,
  padding: "3px 8px",
};
const slotMeter: CSSProperties = { display: "flex", alignItems: "center", gap: 6, padding: "3px 8px", borderRadius: 999, background: "#eff6ff", border: "1px solid #b9d4ff" };
const toggleBtn: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 999, padding: "4px 8px", fontSize: 11, color: "#344054", fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" };
const btnDanger: CSSProperties = { border: "none", borderRadius: 999, padding: "4px 8px", background: "#dc2626", color: "#fff", fontSize: 11, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" };
const btnDisabled: CSSProperties = { ...btnDanger, background: "#98a2b3", cursor: "not-allowed" };
const laneGrid: CSSProperties = { display: "flex", flexDirection: "column", gap: 10, marginTop: 10 };
const lane: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 8, background: "#fcfcfd", minWidth: 0, overflow: "hidden" };
const laneTitle: CSSProperties = { fontSize: 12, fontWeight: 800, color: "#344054", padding: "8px 10px 6px", borderBottom: "1px solid #eaecf0" };
const laneList: CSSProperties = { maxHeight: 220, overflowY: "auto", padding: 6, display: "grid", gap: 6 };
const laneEmpty: CSSProperties = { color: "#98a2b3", fontSize: 12, padding: "14px 8px", textAlign: "center" };
const laneRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  border: "1px solid #eaecf0",
  borderRadius: 8,
  padding: "7px 8px",
  cursor: "pointer",
  minWidth: 0,
};
const rowName: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const rowMeta: CSSProperties = { fontSize: 11, color: "#667085", marginTop: 2 };
const rowBtn: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", borderRadius: 6, padding: "4px 8px", fontSize: 11, color: "#344054", cursor: "pointer" };
const rowBtnDanger: CSSProperties = { ...rowBtn, color: "#b42318", borderColor: "#fca5a5" };
const idleHint: CSSProperties = { marginTop: 10, color: "#667085", fontSize: 12, padding: "8px 2px" };
