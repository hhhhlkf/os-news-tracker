// frontend/src/components/DiscoveryFlowChart.tsx
import { useMemo, type CSSProperties } from "react";
import { ReactFlow, Background, BackgroundVariant, Handle, MarkerType, Position, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  FLOW_NODES,
  FLOW_EDGES,
  computeNodeStates,
  computeEdgeStates,
  attemptCount,
  currentAttemptRound,
  type FlowNodeId,
  type NodeState,
  type EdgeState,
} from "../discovery/flowState";
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
  warning: { fill: "#fffaeb", stroke: "#f59e0b", color: "#b54708" },
};

const EDGE_STYLE: Record<EdgeState, { stroke: string; width: number; dash?: string; animated?: boolean; opacity?: number }> = {
  pending: { stroke: "#98a2b3", width: 1.8, opacity: 0.9 },
  active: { stroke: "#175cd3", width: 2.4, dash: "10 6", animated: true, opacity: 1 },
  done: { stroke: "#059669", width: 2.2, opacity: 1 },
  retrying: { stroke: "#f59e0b", width: 2.4, dash: "10 6", animated: true, opacity: 1 },
};

const NODE_PULSE_KEYFRAMES = `
@keyframes discovery-node-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(23, 92, 211, 0.28);
    transform: scale(1);
  }
  55% {
    box-shadow: 0 0 0 8px rgba(23, 92, 211, 0.08);
    transform: scale(1.015);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(23, 92, 211, 0.18);
    transform: scale(1);
  }
}
`;

export function getNodeBoxVisualStyle({
  state,
  isAgent,
}: {
  state: NodeState;
  isAgent: boolean;
}): CSSProperties {
  const s = STATE_STYLE[state];
  return {
    width: 132,
    padding: "8px 10px",
    textAlign: "center",
    background: s.fill,
    borderWidth: 2,
    borderStyle: s.dash ? "dashed" : "solid",
    borderColor: s.stroke,
    borderRadius: isAgent ? "50%" : "12px",
    color: s.color,
    fontSize: 13,
    fontWeight: 700,
    boxShadow: state === "running" ? "0 0 0 4px rgba(23,92,211,0.15)" : undefined,
    animation: state === "running" ? "discovery-node-pulse 1.35s ease-in-out infinite" : undefined,
    transformOrigin: state === "running" ? "center" : undefined,
  };
}

function NodeBox({ data }: { data: { label: string; id: string; kind: "det" | "agent"; state: NodeState } }) {
  const isAgent = data.kind === "agent";
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
      <div style={getNodeBoxVisualStyle({ state: data.state, isAgent })}>
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
  const edgeStates = useMemo(() => computeEdgeStates(run), [run]);
  const attempt = useMemo(() => currentAttemptRound(run), [run]);

  const nodes: Node[] = useMemo(() => FLOW_NODES.map((n) => ({
    id: n.id, type: "flow", position: POS[n.id],
    data: { label: n.label, id: n.id, kind: n.kind, state: states[n.id] },
    selectable: true, selected: selectedNode === n.id,
  })), [states, selectedNode]);

  const edges: Edge[] = useMemo(() => {
    const handles: Record<string, { sourceHandle: string; targetHandle: string }> = {
      "fetch_homepage->capture_network": { sourceHandle: "bottom-out", targetHandle: "top-in" },
      "capture_network->explorer": { sourceHandle: "bottom-out", targetHandle: "top-in" },
      "explorer->validator": { sourceHandle: "right-out", targetHandle: "left-in" },
      "validator->dsl_writer": { sourceHandle: "bottom-out", targetHandle: "top-in" },
      "dsl_writer->auditor": { sourceHandle: "left-out", targetHandle: "right-in" },
      "auditor->save_method": { sourceHandle: "bottom-out", targetHandle: "left-in" },
      "auditor->explorer": { sourceHandle: "top-out", targetHandle: "bottom-in" },
    };

    return FLOW_EDGES.map((edge, index) => {
      const visual = EDGE_STYLE[edgeStates[edge.id]];
      const handle = handles[edge.id];
      const isRetry = edge.id === "auditor->explorer";
      const label = edge.id === "auditor->save_method"
        ? "通过"
        : isRetry ? `不通过·重试(${attempt}/3)` : undefined;
      return {
        id: `e${index + 1}`,
        source: edge.source,
        target: edge.target,
        sourceHandle: handle.sourceHandle,
        targetHandle: handle.targetHandle,
        type: "step",
        animated: Boolean(visual.animated),
        label,
        labelStyle: label ? {
          fill: visual.stroke,
          fontWeight: 700,
          fontSize: 11,
        } : undefined,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: visual.stroke,
        },
        style: {
          stroke: visual.stroke,
          strokeWidth: visual.width,
          strokeDasharray: isRetry ? (visual.dash ?? "6 4") : visual.dash,
          strokeLinecap: "round",
          opacity: visual.opacity,
        },
      } satisfies Edge;
    });
  }, [attempt, edgeStates]);

  return (
    <div style={{ position: "relative", height: 520 }}>
      <style>{NODE_PULSE_KEYFRAMES}</style>
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
