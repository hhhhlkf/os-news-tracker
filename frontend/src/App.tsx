import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import { useState, type CSSProperties } from "react";
import { HomePage } from "./pages/HomePage";
import { DiscoveryPage } from "./pages/DiscoveryPage";
import { StatisticsDiscoveryPage } from "./pages/StatisticsDiscoveryPage";
import { isSystemAuthenticated, logout, systemLogin } from "./auth";
import { clampInput, INPUT_LIMITS } from "./inputLimits";

function TopNav({ authenticated, onAuthenticatedChange }: { authenticated: boolean; onAuthenticatedChange: (value: boolean) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const linkStyle = ({ isActive }: { isActive: boolean }): CSSProperties => ({
    fontSize: 13,
    fontWeight: 700,
    color: isActive ? "#175cd3" : "#667085",
    textDecoration: "none",
    padding: "8px 12px",
    borderRadius: 8,
    background: isActive ? "#eff6ff" : "transparent",
  });
  async function handleLogin() {
    setError(null);
    try {
      await systemLogin(password);
      setPassword("");
      onAuthenticatedChange(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
      onAuthenticatedChange(false);
    }
  }
  function handleLogout() {
    logout();
    onAuthenticatedChange(false);
    setPassword("");
    setError(null);
  }
  return (
    <nav style={{ display: "flex", gap: 12, padding: "10px 24px", borderBottom: "1px solid #eaecf0", alignItems: "center", flexWrap: "wrap" }}>
      <div style={{ display: "flex", gap: 6 }}>
        <NavLink to="/" end style={linkStyle}>
          新闻流
        </NavLink>
        <NavLink to="/discover" style={linkStyle}>
          站点发现
        </NavLink>
        <NavLink to="/statistics" style={linkStyle}>
          统计与发现
        </NavLink>
      </div>
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {authenticated ? (
          <>
            <span style={{ fontSize: 12, color: "#027a48", fontWeight: 700 }}>管理已解锁</span>
            <button type="button" style={navButton} onClick={handleLogout}>退出登录</button>
          </>
        ) : (
          <>
            <input
              type="password"
              placeholder="管理密码"
              value={password}
              maxLength={INPUT_LIMITS.password}
              onChange={(event) => setPassword(clampInput(event.target.value, INPUT_LIMITS.password))}
              onKeyDown={(event) => {
                if (event.key === "Enter" && password.trim()) void handleLogin();
              }}
              style={passwordInput}
            />
            <button type="button" style={navButton} disabled={!password.trim()} onClick={() => void handleLogin()}>登录</button>
            {error && <span style={{ fontSize: 12, color: "#b42318" }}>{error}</span>}
          </>
        )}
      </div>
    </nav>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(() => isSystemAuthenticated());
  return (
    <BrowserRouter>
      <TopNav authenticated={authenticated} onAuthenticatedChange={setAuthenticated} />
      <Routes>
        <Route path="/" element={<HomePage hasSystemAccess={authenticated} />} />
        <Route path="/discover" element={<DiscoveryPage hasSystemAccess={authenticated} />} />
        <Route path="/statistics" element={<StatisticsDiscoveryPage hasSystemAccess={authenticated} />} />
      </Routes>
    </BrowserRouter>
  );
}

const navButton: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  background: "#fff",
  color: "#344054",
  padding: "7px 12px",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
};

const passwordInput: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "7px 10px",
  fontSize: 12,
  minWidth: 150,
};
