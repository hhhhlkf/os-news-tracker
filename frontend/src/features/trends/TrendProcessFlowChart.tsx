import { useMemo, type CSSProperties } from "react";
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

export type TrendFlowNodeState = "pending" | "running" | "done" | "failed" | "warning";

interface TrendProcessNode {
  id: 1 | 2 | 3 | 4;
  name: string;
  statusLabel: string;
  state: TrendFlowNodeState;
}

const stateStyle: Record<TrendFlowNodeState, { stroke: string; dash?: string }> = {
  pending: { stroke: "#cbd5e1", dash: "4 3" },
  running: { stroke: "#175cd3" },
  done: { stroke: "#059669" },
  failed: { stroke: "#dc2626" },
  warning: { stroke: "#f59e0b" },
};

const edgeStyle: Record<TrendFlowNodeState, { stroke: string; dash?: string; animated?: boolean }> = {
  pending: { stroke: "#98a2b3" },
  running: { stroke: "#175cd3", dash: "10 6", animated: true },
  done: { stroke: "#059669" },
  failed: { stroke: "#dc2626" },
  warning: { stroke: "#f59e0b", dash: "10 6", animated: true },
};

const PULSE_KEYFRAMES = `
@keyframes trend-flow-node-pulse {
  0% { box-shadow: 0 0 0 0 rgba(71, 84, 103, .24); transform: scale(1); }
  55% { box-shadow: 0 0 0 8px rgba(71, 84, 103, .08); transform: scale(1.012); }
  100% { box-shadow: 0 0 0 0 rgba(71, 84, 103, .14); transform: scale(1); }
}`;

function ProcessNode({ data }: { data: TrendProcessNode & { selected: boolean } }) {
  const visual = stateStyle[data.state];
  return (
    <div style={{ position: "relative" }}>
      <Handle type="target" position={Position.Left} style={handleStyle} />
      <div style={{
        ...nodeBox,
        background: "#fff",
        borderColor: visual.stroke,
        borderStyle: visual.dash ? "dashed" : "solid",
        animation: data.selected ? "trend-flow-node-pulse 1.35s ease-in-out infinite" : undefined,
      }}>
        {data.state === "done" && <span style={{ ...completionMark, borderColor: visual.stroke, color: visual.stroke }}>✓</span>}
        <div style={nodeStep}>步骤 0{data.id}</div>
        <div style={nodeName}>{data.name}</div>
        <div style={nodeStatus}>{data.statusLabel}</div>
      </div>
      <Handle type="source" position={Position.Right} style={handleStyle} />
    </div>
  );
}

const nodeTypes = { trendProcess: ProcessNode };
const handleStyle = { width: 8, height: 8, opacity: 0, border: "none", background: "transparent" } as const;

export function TrendProcessFlowChart({
  stages,
  selectedStageId,
  onSelectStage,
}: {
  stages: TrendProcessNode[];
  selectedStageId: 1 | 2 | 3 | 4;
  onSelectStage: (stageId: 1 | 2 | 3 | 4) => void;
}) {
  const nodes = useMemo<Node[]>(() => stages.map((stage, index) => ({
    id: String(stage.id),
    type: "trendProcess",
    position: { x: 54 + index * 300, y: 22 },
    data: { ...stage, selected: selectedStageId === stage.id },
    selectable: false,
  })), [selectedStageId, stages]);
  const edges = useMemo<Edge[]>(() => stages.slice(0, -1).map((stage, index) => {
    const target = stages[index + 1];
    const state = target.state === "running" ? "running"
      : target.state === "failed" ? "failed"
        : target.state === "warning" ? "warning"
          : stage.state === "done" ? "done" : "pending";
    const visual = edgeStyle[state];
    return {
      id: `${stage.id}->${target.id}`,
      source: String(stage.id),
      target: String(target.id),
      animated: Boolean(visual.animated),
      markerEnd: { type: MarkerType.ArrowClosed, color: visual.stroke, width: 16, height: 16 },
      style: { stroke: visual.stroke, strokeWidth: 2.2, strokeDasharray: visual.dash, strokeLinecap: "round" },
    };
  }), [stages]);

  return (
    <div style={chartWrap}>
      <style>{PULSE_KEYFRAMES}</style>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelectStage(Number(node.id) as 1 | 2 | 3 | 4)}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
        fitViewOptions={{ padding: 0.16, maxZoom: 1 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#e4e8ee" />
      </ReactFlow>
    </div>
  );
}

const chartWrap: CSSProperties = { height: 142, minWidth: 1120, position: "relative" };
const nodeBox: CSSProperties = { width: 194, minHeight: 82, boxSizing: "border-box", borderWidth: 2, borderRadius: 10, padding: "11px 14px", textAlign: "center", color: "#344054", cursor: "pointer", transformOrigin: "center" };
const completionMark: CSSProperties = { position: "absolute", top: 8, right: 9, width: 17, height: 17, display: "inline-flex", alignItems: "center", justifyContent: "center", border: "1px solid", borderRadius: "50%", fontSize: 11, fontWeight: 900 };
const nodeStep: CSSProperties = { fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 800, color: "#98a2b3" };
const nodeName: CSSProperties = { marginTop: 4, fontSize: 13, fontWeight: 800, color: "#344054", whiteSpace: "nowrap" };
const nodeStatus: CSSProperties = { marginTop: 5, fontSize: 11, fontWeight: 600, color: "#667085" };
