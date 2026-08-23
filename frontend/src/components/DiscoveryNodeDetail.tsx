import type { CSSProperties } from "react";
import type { DiscoveryPhaseId } from "./DiscoveryFlowChart";
import type { DiscoveryEventLogEntry } from "../hooks/useDiscoveryLogs";
import type { DiscoveryNodeTraceEntry } from "../types";
import type { FlowNodeId } from "../discovery/flowState";

const LABELS: Record<DiscoveryPhaseId, string> = {
  context: "Context",
  explore: "Explore",
  build: "Build",
  execute: "Execute",
  evaluate_repair: "Evaluate / Repair",
  package: "Package",
  pending_review: "Pending review",
};

const CATEGORY_KEYS = {
  error: ["error_summary", "error_type", "error_code", "error_stage", "returncode", "stderr_summary"],
  evidence: ["sandbox_tool_evidence", "public_job_id", "event_count", "log_samples", "observation_summary"],
  diff: ["diff", "code_diff", "change_summary", "checksum"],
  output: ["output", "result", "stats", "stdout", "stderr"],
  evaluation: ["evaluation", "failures", "checks", "plugin_review", "passed"],
} as const;

function matchesPhase(eventPhase: string | null, selected: DiscoveryPhaseId): boolean {
  if (selected === "evaluate_repair") return eventPhase === "evaluate" || eventPhase === "repair";
  if (selected === "pending_review") return eventPhase === "package";
  return eventPhase === selected;
}

function safeDisplay(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[无法显示]";
  }
}

export function DiscoveryNodeDetail({ nodeId, events = [], entry }: {
  nodeId: DiscoveryPhaseId | FlowNodeId;
  events?: DiscoveryEventLogEntry[];
  entry?: DiscoveryNodeTraceEntry;
}) {
  if (entry) {
    const summary = entry.summary ?? {};
    if (nodeId === "auditor") {
      const issues = Array.isArray(summary.issues) ? summary.issues.map(String) : [];
      return <div style={{ border: "1px solid #b9d4ff", borderRadius: 10, background: "#f8fbff", padding: "12px 14px" }}>
        <div style={{ color: "#175cd3", fontSize: 12, fontWeight: 800, marginBottom: 8 }}>▸ 审计</div>
        <div style={detailScrollStyle}>
          <div style={{ color: summary.passed ? "#059669" : "#dc2626", fontWeight: 700 }}>{summary.passed ? "审计成功" : "审计失败"}</div>
          {issues.map((issue) => <div key={issue} style={{ fontSize: 12, marginTop: 4 }}>{issue}</div>)}
        </div>
      </div>;
    }
    return <div style={{ border: "1px solid #b9d4ff", borderRadius: 10, background: "#f8fbff", padding: "12px 14px" }}>
      <div style={{ color: "#175cd3", fontSize: 12, fontWeight: 800, marginBottom: 8 }}>▸ {nodeId}</div>
      <div data-testid="discovery-node-summary" style={detailScrollStyle}>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 11 }}>{safeDisplay(summary)}</pre>
      </div>
    </div>;
  }
  const phaseId = nodeId as DiscoveryPhaseId;
  const phaseEvents = events.filter((event) => matchesPhase(event.phase, phaseId)).slice(-30);
  const categorized = Object.entries(CATEGORY_KEYS).map(([category, keys]) => ({
    category,
    rows: phaseEvents.flatMap((event) => {
      const payload = event.payload ?? {};
      return keys.flatMap((key) => key in payload
        ? [{ key, value: payload[key], sequence: event.sequence }]
        : []);
    }),
  })).filter((section) => section.rows.length > 0);

  return (
    <div style={{ border: "1px solid #b9d4ff", borderRadius: 10, background: "#f8fbff", padding: "12px 14px", width: "100%", minWidth: 0, boxSizing: "border-box", overflow: "hidden" }}>
      <div style={{ fontSize: 12, fontWeight: 800, color: "#175cd3", marginBottom: 8 }}>
        ▸ {LABELS[phaseId]} <span style={{ color: "#98a2b3", fontFamily: "JetBrains Mono, monospace", fontWeight: 500 }}>{phaseId}</span>
      </div>
      {phaseEvents.length === 0 ? (
        <div style={{ fontSize: 12, color: "#98a2b3" }}>该阶段尚无安全审计事件。</div>
      ) : (
        <div style={{ ...detailScrollStyle, maxHeight: 160, display: "grid", gap: 9 }}>
          {categorized.length === 0 && phaseEvents.map((event) => (
            <div key={event.sequence} style={{ fontSize: 12, color: "#344054" }}>
              <span style={{ color: "#667085" }}>#{event.sequence}</span> {event.message}
            </div>
          ))}
          {categorized.map((section) => (
            <div key={section.category}>
              <div style={{ color: "#667085", fontSize: 11, fontWeight: 800, textTransform: "uppercase" }}>{section.category}</div>
              {section.rows.map((row) => (
                <pre key={`${row.sequence}-${row.key}`} style={{ margin: "4px 0", padding: 7, borderRadius: 6, background: "#fff", border: "1px solid #eaecf0", color: "#344054", fontSize: 10, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {row.key}: {safeDisplay(row.value)}
                </pre>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const detailScrollStyle: CSSProperties = {
  maxHeight: 176,
  overflowY: "auto",
  overflowX: "hidden",
  scrollbarGutter: "stable",
  paddingRight: 6,
};
