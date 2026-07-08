import { useState } from "react";
import { MorningCrawlStatusPanel } from "./MorningCrawlStatusPanel";
import { MorningCrawlTimelinePanel } from "./MorningCrawlTimelinePanel";

type MorningCrawlTab = "status" | "timeline";

const tabMeta: Array<{ key: MorningCrawlTab; label: string; description: string; icon: string }> = [
  { key: "status", label: "状态与配置", description: "今日状态、定时抓取配置、立即执行", icon: "◧" },
  { key: "timeline", label: "时间线与失败", description: "今日时间线与失败方式", icon: "◷" },
];

export function MorningCrawlModal(props: { open: boolean; onClose: () => void }) {
  const { open, onClose } = props;
  const [activeTab, setActiveTab] = useState<MorningCrawlTab>("status");
  const [navCollapsed, setNavCollapsed] = useState(false);

  if (!open) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(15, 23, 42, 0.42)",
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        padding: 16,
        zIndex: 60,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 1120,
          maxWidth: "96vw",
          maxHeight: "92vh",
          overflow: "hidden",
          borderRadius: 18,
          background: "#fff",
          boxShadow: "0 24px 72px rgba(16,24,40,0.22)",
          display: "grid",
          gridTemplateRows: "auto 1fr",
        }}
      >
        <div
          style={{
            padding: "20px 22px 14px",
            borderBottom: "1px solid #eaecf0",
            background: "#fcfcfd",
            display: "flex",
            justifyContent: "space-between",
            gap: 16,
            alignItems: "flex-start",
          }}
        >
          <div>
            <div style={{ fontSize: 24, fontWeight: 800, color: "#101828" }}>系统定时抓取</div>
            <div style={{ fontSize: 13, color: "#667085", marginTop: 6 }}>按北京时间定时运行全部启用中的爬取方式，查看今日状态与执行明细。</div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              onClick={() => setNavCollapsed((v) => !v)}
              style={{ border: "1px solid #d7dee7", borderRadius: 999, padding: "6px 10px", fontSize: 12, color: "#475467", background: "#fff", fontWeight: 700, cursor: "pointer" }}
            >
              {navCollapsed ? "显示左侧列表" : "隐藏左侧列表"}
            </button>
            <button
              onClick={onClose}
              style={{ border: "none", background: "transparent", color: "#667085", cursor: "pointer", fontSize: 12 }}
            >
              关闭
            </button>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: navCollapsed ? "56px minmax(0,1fr)" : "230px minmax(0,1fr)", height: "clamp(480px, 64vh, 720px)", minHeight: 0 }}>
          <div
            style={{
              borderRight: "1px solid #eaecf0",
              background: "#f8fafc",
              padding: navCollapsed ? "10px 8px" : 14,
              display: "grid",
              gap: 8,
              alignContent: "start",
              justifyItems: navCollapsed ? "center" : undefined,
            }}
          >
            {tabMeta.map((tab) => {
              const active = tab.key === activeTab;
              return (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  title={navCollapsed ? tab.label : undefined}
                  style={{
                    border: active ? "1px solid #dbe4ee" : "1px solid transparent",
                    borderRadius: 12,
                    padding: navCollapsed ? 0 : 12,
                    width: navCollapsed ? 40 : "100%",
                    height: navCollapsed ? 40 : "auto",
                    background: active ? "#fff" : "transparent",
                    boxShadow: active ? "0 8px 18px rgba(16,24,40,0.04)" : "none",
                    cursor: "pointer",
                    display: navCollapsed ? "grid" : "block",
                    placeItems: navCollapsed ? "center" : undefined,
                    textAlign: "left",
                  }}
                >
                  {navCollapsed ? (
                    <span style={{ fontSize: 16, color: active ? "#175cd3" : "#64748b" }}>{tab.icon}</span>
                  ) : (
                    <>
                      <strong style={{ display: "block", fontSize: 14, color: "#101828", marginBottom: 4 }}>{tab.label}</strong>
                      <span style={{ display: "block", fontSize: 11, color: "#667085", lineHeight: 1.45 }}>{tab.description}</span>
                    </>
                  )}
                </button>
              );
            })}
          </div>

          <div style={{ padding: 14, overflowY: "auto", background: "#fff", minHeight: 0 }}>
            {activeTab === "status" && <MorningCrawlStatusPanel />}
            {activeTab === "timeline" && <MorningCrawlTimelinePanel />}
          </div>
        </div>
      </div>
    </div>
  );
}
