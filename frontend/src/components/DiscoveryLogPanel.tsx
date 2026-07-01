// frontend/src/components/DiscoveryLogPanel.tsx
import type { NewsRunLogEntry } from "../types";

function ts(t: string) {
  const d = new Date(t); return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString("zh-CN", { hour12: false });
}

export function DiscoveryLogPanel({ logs }: { logs: NewsRunLogEntry[] }) {
  const recent = logs.slice(-80).reverse();
  return (
    <div style={{ background: "#0b1220", borderRadius: 10, padding: "12px 14px",
      fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12, lineHeight: 1.7, color: "#d0d5dd",
      display: "flex", flexDirection: "column", minHeight: 280 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ color: "#f8fafc", fontWeight: 700, fontSize: 13 }}>实时日志</span>
        <span style={{ color: "#98a2b3", fontSize: 11 }}>轮询 /news-run/logs</span>
      </div>
      <div style={{ overflowY: "auto", flex: 1 }}>
        {recent.length === 0 ? (
          <div style={{ color: "#98a2b3" }}>暂无日志。开始探查后这里实时显示各步进度。</div>
        ) : recent.map((l) => (
          <div key={l.id}>
            <span style={{ color: "#7cd4fd" }}>{ts(l.ts)}</span>{" "}
            <span style={{ color: "#a6f4c5" }}>[{l.stage}]</span>{" "}
            <span>{l.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
