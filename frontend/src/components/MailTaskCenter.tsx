import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ItemQueryParams } from "../api/client";
import { fetchMailTemplates } from "../mail/api";
import { MailImmediateSendPanel } from "./MailImmediateSendPanel";

type MailTab = "immediate" | "templates" | "schedules";

const tabMeta: Array<{ key: MailTab; label: string; description: string; icon: string }> = [
  { key: "immediate", label: "立即发送", description: "基于当前筛选发一封", icon: "✉" },
  { key: "templates", label: "模板列表", description: "保存并复用筛选和收件人", icon: "▤" },
  { key: "schedules", label: "已预定发送", description: "查看定时发送任务", icon: "◷" },
];

export function MailTaskCenter(props: {
  open: boolean;
  onClose: () => void;
  homeFilters: ItemQueryParams;
}) {
  const { open, onClose, homeFilters } = props;
  const [activeTab, setActiveTab] = useState<MailTab>("immediate");
  const [navCollapsed, setNavCollapsed] = useState(false);
  const queryClient = useQueryClient();
  const templatesQuery = useQuery({
    queryKey: ["mail-templates"],
    queryFn: fetchMailTemplates,
    enabled: open,
    retry: false,
  });

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
            <div style={{ fontSize: 24, fontWeight: 800, color: "#101828" }}>邮件任务中心</div>
            <div style={{ fontSize: 13, color: "#667085", marginTop: 6 }}>当前筛选发送、模板复用、已预定发送，全都从这里进入。</div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              onClick={() => setNavCollapsed((v) => !v)}
              style={{
                border: "1px solid #d7dee7",
                borderRadius: 999,
                padding: "6px 10px",
                fontSize: 12,
                color: "#475467",
                background: "#fff",
                fontWeight: 700,
                cursor: "pointer",
              }}
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

        <div style={{ display: "grid", gridTemplateColumns: navCollapsed ? "56px minmax(0,1fr)" : "230px minmax(0,1fr)", minHeight: 0 }}>
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

          <div style={{ padding: 14, overflowY: "auto", background: "#fff" }}>
            {activeTab === "immediate" && (
              <MailImmediateSendPanel
                homeFilters={homeFilters}
                onTemplateSaved={() => {
                  void queryClient.invalidateQueries({ queryKey: ["mail-templates"] });
                }}
              />
            )}

            {activeTab === "templates" && (
              <div style={{ display: "grid", gap: 12 }}>
                <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14 }}>
                  <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 10 }}>
                    模板列表骨架
                  </div>
                  {templatesQuery.isLoading && <div style={{ color: "#667085", fontSize: 13 }}>正在加载模板…</div>}
                  {templatesQuery.isError && <div style={{ color: "#b42318", fontSize: 13 }}>模板列表加载失败</div>}
                  {!templatesQuery.isLoading && !templatesQuery.isError && (
                    <div style={{ display: "grid", gap: 10 }}>
                      {(templatesQuery.data ?? []).map((template) => (
                        <div key={template.id} style={{ border: "1px solid #dde4ec", borderRadius: 12, padding: "12px 14px", background: "#fff" }}>
                          <div style={{ fontWeight: 800, color: "#101828" }}>{template.name}</div>
                          <div style={{ fontSize: 12, color: "#667085", marginTop: 6 }}>{template.subject}</div>
                        </div>
                      ))}
                      {(templatesQuery.data ?? []).length === 0 && (
                        <div style={{ color: "#667085", fontSize: 13 }}>还没有邮件模板，Task 2/3 会接入从当前筛选保存模板的完整流程。</div>
                      )}
                    </div>
                  )}
                </section>
              </div>
            )}

            {activeTab === "schedules" && (
              <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14, background: "#f8fafc", color: "#667085", fontSize: 13 }}>
                Task 1 仅接入已预定发送的容器页。任务列表、状态摘要和执行日志会在 Task 4 落地。
              </section>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
