// frontend/src/components/DiscoveryNodeDetail.tsx
import { Fragment } from "react";
import { FLOW_NODES, type FlowNodeId } from "../discovery/flowState";
import type { DiscoveryNodeTraceEntry } from "../types";

const LABELS: Record<FlowNodeId, string> = Object.fromEntries(FLOW_NODES.map((n) => [n.id, n.label])) as Record<FlowNodeId, string>;

export function DiscoveryNodeDetail({ nodeId, entry }: {
  nodeId: FlowNodeId; entry?: DiscoveryNodeTraceEntry;
}) {
  const meta = FLOW_NODES.find((n) => n.id === nodeId);
  if (!meta) return null;
  const summary = (entry?.summary ?? {}) as Record<string, unknown>;
  const rows = Object.entries(summary).filter(([, v]) => v !== null && v !== undefined);
  return (
    <div style={{ border: "1px solid #b9d4ff", borderRadius: 10, background: "#f8fbff", padding: "12px 14px" }}>
      <div style={{ fontSize: 12, fontWeight: 800, color: "#175cd3", marginBottom: 8 }}>
        ▸ {LABELS[nodeId]} <span style={{ color: "#98a2b3", fontFamily: "JetBrains Mono, monospace", fontWeight: 500 }}>{nodeId}</span>
      </div>
      {rows.length === 0 ? (
        <div style={{ fontSize: 12, color: "#98a2b3" }}>该步尚无产出摘要。</div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: 12, color: "#344054" }}>
          {rows.map(([k, v]) => (
            <Fragment key={k}><span style={{ color: "#667085" }}>{k}</span><span style={{ fontFamily: "JetBrains Mono, monospace" }}>{String(v)}</span></Fragment>
          ))}
        </div>
      )}
      {entry?.status !== "done" && <div style={{ fontSize: 11, color: "#175cd3", fontStyle: "italic", marginTop: 8 }}>进行中…</div>}
    </div>
  );
}
