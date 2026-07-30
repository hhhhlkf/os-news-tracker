import type { CSSProperties } from "react";
import { TrendWorkspace } from "./TrendWorkspace";

export function TrendWorkspacePage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }): React.JSX.Element {
  if (!hasSystemAccess) {
    return (
      <main style={page}>
        <header style={heroCard}>
          <div style={eyebrow}>News Trend Summary Workspace</div>
          <h1 style={pageTitle}>新闻趋势总结</h1>
          <p style={heroCopy}>管理身份模板、趋势窗口与后续运行规则。</p>
        </header>
        <div style={accessNotice}>该页面包含系统级趋势设置，请先在顶部使用管理密码登录。</div>
      </main>
    );
  }

  return (
    <main style={page}>
      <TrendWorkspace />
    </main>
  );
}

const page: CSSProperties = { maxWidth: 1280, margin: "0 auto", padding: "28px 24px 56px" };
const heroCard: CSSProperties = {
  background: "linear-gradient(135deg, #0f172a 0%, #101828 55%, #1d2939 100%)",
  color: "#f8fafc",
  borderRadius: 8,
  padding: 24,
  minHeight: 160,
  marginBottom: 22,
  boxShadow: "0 18px 40px rgba(15, 23, 42, 0.14)",
};
const eyebrow: CSSProperties = { fontSize: 13, color: "#98a2b3", marginBottom: 10 };
const pageTitle: CSSProperties = { margin: 0, fontSize: 32, lineHeight: 1.2, color: "#f8fafc" };
const heroCopy: CSSProperties = { marginTop: 10, marginBottom: 0, color: "#d0d5dd", fontSize: 14 };
const accessNotice: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 12, padding: 18, color: "#475467", background: "#fff" };
