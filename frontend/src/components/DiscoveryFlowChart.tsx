// frontend/src/components/DiscoveryFlowChart.tsx
import { useMemo } from "react";
import { ReactFlow, Background, BackgroundVariant, Handle, MarkerType, Position, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { FLOW_NODES, computeNodeStates, attemptCount, type FlowNodeId, type NodeState } from "../discovery/flowState";
import type { DiscoveryRun } from "../types";

// 固定坐标：预处理竖排居中入环，4 agent 环形，存库底部居中
const POS: Record<FlowNodeId, { x: number; y: number }> = {
  fetch_homepage: { x: 96, y: 0 },
  capture_network: { x: 96, y: 92 },
  explorer: { x: 96, y: 196 },
  validator: { x: 292, y: 196 },
  dsl_writer: { x: 292, y: 314 },
  auditor: { x: 96, y: 314 },
  save_method: { x: 292, y: 438 },
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
    <div style={{ position: "relative" }}>
      <Handle id="top-in" type="target" position={Position.Top} style={handleStyle} />
      <Handle id="top-out" type="source" position={Position.Top} style={handleStyle} />
      <Handle id="right-in" type="target" position={Position.Right} style={handleStyle} />
      <Handle id="right-out" type="source" position={Position.Right} style={handleStyle} />
      <Handle id="bottom-in" type="target" position={Position.Bottom} style={handleStyle} />
      <Handle id="bottom-out" type="source" position={Position.Bottom} style={handleStyle} />
      <Handle id="left-in" type="target" position={Position.Left} style={handleStyle} />
      <Handle id="left-out" type="source" position={Position.Left} style={handleStyle} />
      <div style={{
        width: 132, padding: "8px 10px", textAlign: "center",
        background: s.fill, borderWidth: 2, borderStyle: s.dash ? "dashed" : "solid", borderColor: s.stroke, borderRadius: radius,
        color: s.color, fontSize: 13, fontWeight: 700,
        boxShadow: data.state === "running" ? "0 0 0 4px rgba(23,92,211,0.15)" : undefined,
      }}>
        <div>{data.label}</div>
        <div style={{ fontSize: 9, fontFamily: "JetBrains Mono, monospace", opacity: 0.7 }}>{data.id}</div>
      </div>
    </div>
  );
}

const nodeTypes = { flow: NodeBox };
const handleStyle = { width: 8, height: 8, background: "transparent", border: "none", opacity: 0 } as const;

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
    const e = (
      id: string,
      s: FlowNodeId,
      t: FlowNodeId,
      sourceHandle: string,
      targetHandle: string,
      opts?: Partial<Edge>,
    ): Edge => ({
      id, source: s, target: t,
      sourceHandle, targetHandle,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      style: { stroke: "#98a2b3", strokeWidth: 1.8 },
      ...opts,
    });
    const lastEntry = run.node_trace[run.node_trace.length - 1];
    const isRetryTarget = run.node_trace.length > 0 &&
      (lastEntry.step === "dsl_writer" || lastEntry.step === "validator") &&
      run.node_trace.filter((e) => e.step === lastEntry.step).length >= 2;
    return [
      e("e1", "fetch_homepage", "capture_network", "bottom-out", "top-in", { type: "step" }),
      e("e2", "capture_network", "explorer", "bottom-out", "top-in", { type: "step" }),
      e("e3", "explorer", "validator", "right-out", "left-in", { type: "step" }),
      e("e4", "validator", "dsl_writer", "bottom-out", "top-in", { type: "step" }),
      e("e5", "dsl_writer", "auditor", "left-out", "right-in", { type: "step" }),
      e("e6", "auditor", "save_method", "bottom-out", "left-in", {
        type: "step",
        label: "通过",
        style: { stroke: "#059669", strokeWidth: 1.8 },
      }),
      e("e7", "auditor", "explorer", "top-out", "bottom-in", {
        type: "step",
        label: `不通过·重试(${attempt}/3)`, animated: isRetryTarget && run.status === "running",
        style: { stroke: "#d97706", strokeWidth: 1.8, strokeDasharray: "6 4" },
      }),
    ];
  }, [run, attempt]);

  return (
    <div style={{ position: "relative", height: 520 }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodeClick={(_, n) => onSelectNode?.(n.id as FlowNodeId)}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable
        panOnDrag zoomOnScroll={false} zoomOnPinch={false} panOnScroll={false}
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
