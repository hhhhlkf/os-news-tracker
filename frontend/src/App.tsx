import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import type { CSSProperties } from "react";
import { HomePage } from "./pages/HomePage";
import { DiscoveryPage } from "./pages/DiscoveryPage";

function TopNav() {
  const linkStyle = ({ isActive }: { isActive: boolean }): CSSProperties => ({
    fontSize: 13,
    fontWeight: 700,
    color: isActive ? "#175cd3" : "#667085",
    textDecoration: "none",
    padding: "8px 12px",
    borderRadius: 8,
    background: isActive ? "#eff6ff" : "transparent",
  });
  return (
    <nav style={{ display: "flex", gap: 6, padding: "10px 24px", borderBottom: "1px solid #eaecf0" }}>
      <NavLink to="/" end style={linkStyle}>
        新闻流
      </NavLink>
      <NavLink to="/discover" style={linkStyle}>
        站点发现
      </NavLink>
    </nav>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <TopNav />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/discover" element={<DiscoveryPage />} />
      </Routes>
    </BrowserRouter>
  );
}
