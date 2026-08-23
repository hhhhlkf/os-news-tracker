import type { CSSProperties } from "react";
import type { DiscoveryRun } from "../types";
import type { NodeState } from "../discovery/flowState";

export type DiscoveryPhaseId =
  | "context"
  | "explore"
  | "build"
  | "execute"
  | "evaluate_repair"
  | "package"
  | "pending_review";

const PHASES: Array<{ id: DiscoveryPhaseId; label: string; hint: string }> = [
  { id: "context", label: "Context", hint: "目标与经验" },
  { id: "explore", label: "Explore", hint: "受控探查" },
  { id: "build", label: "Build", hint: "编写连接器" },
  { id: "execute", label: "Execute", hint: "gVisor 实跑" },
  { id: "evaluate_repair", label: "Evaluate", hint: "确定性验收" },
  { id: "package", label: "Package", hint: "固化制品" },
  { id: "pending_review", label: "Pending review", hint: "等待人工审核" },
];

const ORDER = PHASES.map((phase) => phase.id);

const STATE_STYLE: Record<NodeState, { fill: string; stroke: string; color: string; dash?: string }> = {
  pending: { fill: "#f8fafc", stroke: "#cbd5e1", color: "#94a3b8", dash: "4 3" },
  running: { fill: "#eff6ff", stroke: "#175cd3", color: "#175cd3" },
  done: { fill: "#ecfdf3", stroke: "#059669", color: "#059669" },
  failed: { fill: "#fef2f2", stroke: "#dc2626", color: "#dc2626" },
  warning: { fill: "#fffaeb", stroke: "#f59e0b", color: "#b54708" },
};

const NODE_PULSE_KEYFRAMES = `
@keyframes discovery-node-pulse {
  0%, 100% { box-shadow: 0 0 0 1px rgba(23, 92, 211, 0.12); }
  50% { box-shadow: 0 0 0 7px rgba(23, 92, 211, 0.20); }
}
@keyframes discovery-arrow-flow {
  from { background-position: 0 0; }
  to { background-position: 0 12px; }
}
@keyframes discovery-arrow-pulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}
@keyframes discovery-loop-flow {
  from { stroke-dashoffset: 0; opacity: 0.5; }
  to { stroke-dashoffset: -22; opacity: 1; }
}`;

export function getNodeBoxVisualStyle({
  state,
  isAgent: _isAgent,
}: {
  state: NodeState;
  isAgent: boolean;
}): CSSProperties {
  const visual = STATE_STYLE[state];
  return {
    background: visual.fill,
    borderWidth: 2,
    borderStyle: visual.dash ? "dashed" : "solid",
    borderColor: visual.stroke,
    borderRadius: 12,
    color: visual.color,
    boxShadow: state === "running" ? "0 0 0 4px rgba(23,92,211,0.15)" : undefined,
    animation: state === "running" ? "discovery-node-pulse 1.35s ease-in-out infinite" : undefined,
  };
}

/** Retained for callers transitioning from the legacy graph labels. */
export function getEdgeLabel(edgeId: string, attempt: number): string | undefined {
  if (edgeId === "auditor->save_method") return "通过";
  if (edgeId === "auditor->dsl_writer") return `第 ${attempt} / 3 轮`;
  if (edgeId === "auditor->explorer") return `不通过·回探查(${attempt}/3)`;
  return undefined;
}

function normalizedPhase(phase: string | null | undefined): DiscoveryPhaseId | null {
  // Repair rewrites the connector, so the linear view intentionally moves back
  // to Build while the side rail communicates the Evaluate -> Repair -> Build loop.
  if (phase === "repair") return "build";
  if (phase === "evaluate") return "evaluate_repair";
  if (ORDER.includes(phase as DiscoveryPhaseId)) return phase as DiscoveryPhaseId;
  return null;
}

function phaseState(run: DiscoveryRun, id: DiscoveryPhaseId): NodeState {
  if (run.id <= 0) return "pending";
  if (run.status === "completed") {
    if (id !== "pending_review") return "done";
    if (!run.resulting_method_id) return "pending";
    if (run.review_status === "approved") return "done";
    if (run.review_status === "rejected") return "failed";
    return "warning";
  }
  const current = normalizedPhase(run.phase);
  if (current === null) return "pending";
  const currentIndex = ORDER.indexOf(current);
  const index = ORDER.indexOf(id);
  if (run.status === "interrupted" || run.status === "cancelled") {
    if (index < currentIndex) return "done";
    return id === current ? "warning" : "pending";
  }
  if (run.status === "failed") {
    if (index < currentIndex) return "done";
    return id === current ? "failed" : "pending";
  }
  if (index < currentIndex) return "done";
  if (id === current) return "running";
  return "pending";
}

type EdgeState = "pending" | "active" | "done";

function edgeState(run: DiscoveryRun, _from: DiscoveryPhaseId, to: DiscoveryPhaseId): EdgeState {
  if (run.id <= 0) return "pending";
  if (run.status === "completed") return "done";
  const current = normalizedPhase(run.phase);
  if (current === null) return "pending";
  const currentIndex = ORDER.indexOf(current);
  const toIndex = ORDER.indexOf(to);
  if (currentIndex > toIndex) return "done";
  // Animate only the edge entering the active node. The outgoing edge remains
  // grey until the backend advances phase, avoiding two active directions.
  if (["queued", "running", "repairing"].includes(run.status) && currentIndex === toIndex) {
    return "active";
  }
  return "pending";
}

function formatDuration(seconds: number | undefined): string {
  const safe = Math.max(0, Math.floor(seconds ?? 0));
  return `${Math.floor(safe / 60)}m ${String(safe % 60).padStart(2, "0")}s`;
}

export function DiscoveryFlowChart({ run, onSelectNode, selectedNode }: {
  run: DiscoveryRun;
  onSelectNode?: (id: DiscoveryPhaseId) => void;
  selectedNode?: DiscoveryPhaseId | null;
}) {
  const notStarted = run.id <= 0;
  const sourceLabel = run.source_kind === "wechat"
    ? "微信"
    : run.source_kind === "website"
      ? "网站"
      : "未知来源";
  const statusLabel = notStarted
    ? "未启动"
    : `${run.status}${run.queue_position != null ? ` · queue #${run.queue_position}` : ""}`;
  const phaseButton = (phase: (typeof PHASES)[number], width = 190) => {
    const state = phaseState(run, phase.id);
    const stateLabel = phase.id === "pending_review" && run.status === "completed" && run.resulting_method_id
      ? run.review_status === "approved"
        ? "approved"
        : run.review_status === "rejected"
          ? "rejected"
          : "waiting"
      : phase.id === "evaluate_repair" && run.phase === "repair" && state === "running"
        ? "repairing"
      : state;
    const selectedOutline = state === "running"
      ? "rgba(23,92,211,0.18)"
      : state === "done"
        ? "rgba(5,150,105,0.18)"
        : "rgba(148,163,184,0.24)";
    return <button
      key={phase.id}
      type="button"
      onClick={() => onSelectNode?.(phase.id)}
      aria-pressed={selectedNode === phase.id}
      style={{
        ...getNodeBoxVisualStyle({ state, isAgent: false }),
        minHeight: 50,
        padding: "5px 10px",
        cursor: "pointer",
        outline: selectedNode === phase.id ? `3px solid ${selectedOutline}` : "none",
        fontFamily: "inherit",
        width,
      }}
    >
      <div style={{ fontSize: 12, fontWeight: 800 }}>{phase.label}</div>
      <div style={{ marginTop: 4, fontSize: 10, opacity: 0.78 }}>{phase.hint}</div>
      <div style={{ marginTop: 4, fontSize: 9, fontFamily: "JetBrains Mono, monospace" }}>{stateLabel}</div>
    </button>;
  };
  const repairing = run.phase === "repair";
  const loopActive = ["explore", "build", "execute", "evaluate", "evaluate_repair", "repair"].includes(run.phase ?? "")
    && ["queued", "running", "repairing"].includes(run.status);
  return (
    <div style={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column", gap: 10 }}>
      <style>{NODE_PULSE_KEYFRAMES}</style>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ borderRadius: 999, background: notStarted ? "#f8fafc" : "#eef4ff", color: notStarted ? "#94a3b8" : "#175cd3", border: `1px solid ${notStarted ? "#d0d5dd" : "#b9d4ff"}`, padding: "5px 10px", fontSize: 12, fontWeight: 800 }}>
          一个 Agent · {sourceLabel} · Round {Math.max(0, run.round ?? 0)}
        </div>
        <div style={{ fontSize: 11, color: "#667085", fontFamily: "JetBrains Mono, monospace" }}>
          {statusLabel} · remaining {formatDuration(run.remaining_seconds ?? 1200)}
          {run.runtime_version ? ` · ${run.runtime_version}` : ""}
        </div>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "10px 2px 4px" }}>
        <div style={{ width: "100%", maxWidth: 560, margin: "0 auto", display: "flex", flexDirection: "column", alignItems: "center" }}>
          {phaseButton(PHASES[0])}
          <SmallArrow state={edgeState(run, "context", "explore")} />

          <div style={{
            width: "100%",
            position: "relative",
            border: "1px solid #d0d5dd",
            borderRadius: 14,
            background: "#fcfcfd",
            marginTop: 2,
            padding: "16px 12px 12px",
            boxShadow: loopActive ? "0 0 0 3px rgba(23,92,211,0.08)" : undefined,
          }}>
            <span style={{ ...loopBadge, position: "absolute", top: -8, left: 10 }}>CONTROLLED LOOP</span>
            <span style={{
              ...roundBadge,
              ...(notStarted ? { background: "#f8fafc", borderColor: "#d0d5dd", color: "#94a3b8" } : {}),
              position: "absolute",
              top: -8,
              right: 10,
            }}>
              ROUND {Math.max(0, run.round ?? 0)}
            </span>

            <div style={{
              width: "100%",
              display: "grid",
              gridTemplateColumns: "1fr 190px 1fr",
              gridTemplateRows: "auto 10px auto 10px auto 10px auto",
              justifyItems: "center",
              alignItems: "center",
            }}>
              <div style={{ gridColumn: 2, gridRow: 1 }}>{phaseButton(PHASES[1])}</div>
              <div style={{ gridColumn: 2, gridRow: 2 }}><SmallArrow state={edgeState(run, "explore", "build")} /></div>
              <div style={{ gridColumn: 2, gridRow: 3 }}>{phaseButton(PHASES[2])}</div>
              <div style={{ gridColumn: 3, gridRow: "3 / 8", alignSelf: "stretch", justifySelf: "stretch" }}>
                <RepairRail active={repairing} onSelect={() => onSelectNode?.("evaluate_repair")} />
              </div>
              <div style={{ gridColumn: 2, gridRow: 4 }}><SmallArrow state={edgeState(run, "build", "execute")} /></div>
              <div style={{ gridColumn: 2, gridRow: 5 }}>{phaseButton(PHASES[3])}</div>
              <div style={{ gridColumn: 2, gridRow: 6 }}><SmallArrow state={edgeState(run, "execute", "evaluate_repair")} /></div>
              <div style={{ gridColumn: 2, gridRow: 7 }}>{phaseButton(PHASES[4])}</div>
            </div>
          </div>
          <SmallArrow label="通过" state={repairing ? "pending" : edgeState(run, "evaluate_repair", "package")} />
          {phaseButton(PHASES[5])}
          <SmallArrow state={edgeState(run, "package", "pending_review")} />
          {phaseButton(PHASES[6])}
        </div>
      </div>
      <div style={{ textAlign: "center", fontSize: 11, color: "#667085" }}>
        阶段由后端 phase / round 驱动 · Agent 不自证执行与验收结果
      </div>
    </div>
  );
}

const loopBadge: CSSProperties = {
  borderRadius: 999,
  background: "#fcfcfd",
  border: "1px solid #d0d5dd",
  color: "#475467",
  padding: "3px 7px",
  fontSize: 9,
  fontWeight: 800,
  letterSpacing: "0.04em",
  textAlign: "center",
  lineHeight: 1.25,
  whiteSpace: "nowrap",
};

const roundBadge: CSSProperties = {
  borderRadius: 999,
  background: "#eef4ff",
  lineHeight: 1.25,
  border: "1px solid #b9d4ff",
  color: "#175cd3",
  padding: "3px 7px",
  fontSize: 9,
  fontWeight: 800,
  textAlign: "center",
  whiteSpace: "nowrap",
};

function RepairRail({ active, onSelect }: { active: boolean; onSelect?: () => void }) {
  const tone = active ? "#175cd3" : "#cbd5e1";
  return (
    <div style={{ position: "relative", height: "100%", minHeight: 0 }}>
      <svg aria-hidden="true" viewBox="0 0 160 170" preserveAspectRatio="none" style={{ position: "absolute", inset: 0, width: "calc(100% - 56px)", height: "100%", overflow: "visible" }}>
        <path
          d="M0 140 H158 V30 H0"
          fill="none"
          stroke={tone}
          strokeWidth="1.5"
          strokeDasharray="6 5"
          style={{ animation: active ? "discovery-loop-flow 0.75s linear infinite" : undefined }}
        />
      </svg>
      <span aria-hidden="true" style={{
        position: "absolute",
        top: 30,
        left: 0,
        transform: "translate(-100%, -50%)",
        width: 0,
        height: 0,
        borderTop: "3px solid transparent",
        borderBottom: "3px solid transparent",
        borderRight: `4px solid ${tone}`,
      }} />
      <button
        type="button"
        onClick={onSelect}
        style={{
          position: "absolute",
          top: "42%",
          right: 56,
          transform: "translate(50%, -50%)",
          width: 104,
          borderRadius: 999,
          padding: "4px 6px",
          border: `1px solid ${tone}`,
          background: active ? "#eff6ff" : "#f8fafc",
          color: active ? "#175cd3" : "#94a3b8",
          fontFamily: "inherit",
          fontSize: 10,
          fontWeight: 800,
          cursor: "pointer",
          zIndex: 1,
          animation: active ? "discovery-node-pulse 1.35s ease-in-out infinite" : undefined,
        }}
      >
        Repair ↺ Build
      </button>
      <div style={{
        position: "absolute",
        bottom: 30,
        left: 10,
        transform: "translateY(50%)",
        color: active ? "#175cd3" : "#94a3b8",
        fontSize: 9,
        fontWeight: 700,
        background: "#fcfcfd",
        padding: "0 4px",
        zIndex: 1,
        whiteSpace: "nowrap",
      }}>
        验收失败
      </div>
    </div>
  );
}

function SmallArrow({ label, state = "pending" }: { label?: string; state?: EdgeState }) {
  const tone = state === "active" ? "#175cd3" : state === "done" ? "#059669" : "#98a2b3";
  return (
    <div aria-hidden="true" style={{ height: 10, width: 56, position: "relative", display: "flex", justifyContent: "center", justifySelf: "center" }}>
      <span style={{
        width: state === "active" ? 2 : 1,
        height: 6,
        background: state === "active"
          ? "repeating-linear-gradient(to bottom, #175cd3 0 3px, rgba(23,92,211,0.12) 3px 6px)"
          : tone,
        backgroundSize: state === "active" ? "100% 12px" : undefined,
        animation: state === "active" ? "discovery-arrow-flow 0.55s linear infinite" : undefined,
      }} />
      <span style={{
        position: "absolute",
        left: "50%",
        bottom: 0,
        transform: "translateX(-50%)",
        width: 0,
        height: 0,
        borderLeft: "2.5px solid transparent",
        borderRight: "2.5px solid transparent",
        borderTop: `3.5px solid ${tone}`,
        animation: state === "active" ? "discovery-arrow-pulse 0.8s ease-in-out infinite" : undefined,
      }} />
      {label && (
        <span style={{ position: "absolute", left: "calc(50% + 6px)", top: -1, color: tone, fontSize: 9, fontWeight: 700 }}>
          {label}
        </span>
      )}
    </div>
  );
}
