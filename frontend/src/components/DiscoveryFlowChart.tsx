// frontend/src/components/DiscoveryFlowChart.tsx
import { useMemo } from "react";
import { ReactFlow, Background, BackgroundVariant, MarkerType, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { FLOW_NODES, computeNodeStates, attemptCount, type FlowNodeId, type NodeState } from "../discovery/flowState";
import type { DiscoveryRun } from "../types";

// 固定坐标：预处理竖排居中入环，4 agent 环形，存库底部居中
const POS: Record<FlowNodeId, { x: number; y: number }> = {
  fetch_homepage: { x: 96, y: 0 },
  capture_network: { x: 96, y: 80 },
  explorer: { x: 176, y: 170 },
  validator: { x: 290, y: 250 },
  dsl_writer: { x: 176, y: 330 },
  auditor: { x: 62, y: 250 },
  save_method: { x: 176, y: 410 },
};

const STATE_STYLE: Record<NodeState, { fill: string; stroke: string; color: string; dash?: string }> = {
  pending: { fill: "#f8fafc", stroke: "#cbd5e1", color: "#94a3b8", dash: "4 3" },
  running: { fill: "#eff6ff", stroke: "#175cd3", color: "#175cd3" },
  done: { fill: "#ecfdf3", stroke: "#059669", color: "#059669" },
  failed: { fill: "#fef2f2", stroke: "#dc2626", color: "#dc2626" },
};

function NodeBox({ data }: { data: { label: string; id: string; kind: "det" | "agent"; state: NodeState } }) {
  const s = STATE_STYLE[data.state];
  const isAgent = data.kind === "agent";
  const radius = isAgent ? "50%" : "12px";
  return (
    <div style={{
      width: 132, padding: "8px 10px", textAlign: "center",
      background: s.fill, border: `2px solid ${s.stroke}`, borderRadius: radius,
      color: s.color, fontSize: 13, fontWeight: 700,
      boxShadow: data.state === "running" ? "0 0 0 4px rgba(23,92,211,0.15)" : undefined,
      borderStyle: s.dash ? "dashed" : "solid",
    }}>
      <div>{data.label}</div>
      <div style={{ fontSize: 9, fontFamily: "JetBrains Mono, monospace", opacity: 0.7 }}>{data.id}</div>
    </div>
  );
}

const nodeTypes = { flow: NodeBox };

export function DiscoveryFlowChart({ run, onSelectNode, selectedNode }: {
  run: DiscoveryRun;
  onSelectNode?: (id: FlowNodeId) => void;
  selectedNode?: FlowNodeId | null;
}) {
  const states = useMemo(() => computeNodeStates(run), [run]);
  const attempt = useMemo(() => attemptCount(run.node_trace), [run.node_trace]);

  const nodes: Node[] = useMemo(() => FLOW_NODES.map((n) => ({
    id: n.id, type: "flow", position: POS[n.id],
    data: { label: n.label, id: n.id, kind: n.kind, state: states[n.id] },
    selectable: true, selected: selectedNode === n.id,
  })), [states, selectedNode]);

  const edges: Edge[] = useMemo(() => {
    const e = (id: string, s: FlowNodeId, t: FlowNodeId, opts?: Partial<Edge>): Edge => ({
      id, source: s, target: t,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      ...opts,
    });
    const lastEntry = run.node_trace[run.node_trace.length - 1];
    const isRetryTarget = run.node_trace.length > 0 &&
      (lastEntry.step === "dsl_writer" || lastEntry.step === "validator") &&
      run.node_trace.filter((e) => e.step === lastEntry.step).length >= 2;
    return [
      e("e1", "fetch_homepage", "capture_network"),
      e("e2", "capture_network", "explorer"),
      e("e3", "explorer", "validator"),
      e("e4", "validator", "dsl_writer"),
      e("e5", "dsl_writer", "auditor"),
      e("e6", "auditor", "save_method", { label: "通过", style: { stroke: "#059669" } }),
      e("e7", "auditor", "dsl_writer", {
        label: `不通过·重试(${attempt}/3)`, animated: isRetryTarget && run.status === "running",
        style: { stroke: "#d97706", strokeDasharray: "6 4" },
      }),
    ];
  }, [run, attempt]);

  return (
    <div style={{ position: "relative", height: 520 }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodeClick={(_, n) => onSelectNode?.(n.id as FlowNodeId)}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable
        panOnDrag={false} zoomOnScroll={false} zoomOnPinch={false} panOnScroll={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#e4e8ee" />
      </ReactFlow>
      <div style={{ position: "absolute", bottom: 4, left: 0, right: 0, textAlign: "center",
        fontSize: 11, color: "#667085" }}>
        圆 = AI agent　胶囊 = 确定性步骤　绿=完成 · 蓝=进行中 · 灰=待执行 · 红=失败
      </div>
    </div>
  );
}
