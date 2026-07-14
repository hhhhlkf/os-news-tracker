const TOKEN_KEY = "os_tracker_token";
const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";

export function getToken(): string | null {
  if (typeof window === "undefined" || !window.localStorage) {
    return null;
  }
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined" || !window.localStorage) return;
  localStorage.setItem(TOKEN_KEY, token);
}

export function logout(): void {
  if (typeof window === "undefined" || !window.localStorage) return;
  localStorage.removeItem(TOKEN_KEY);
}

export function isSystemAuthenticated(): boolean {
  const token = getToken();
  if (!token) return false;
  const payload = decodeJwtPayload(token);
  return payload?.role === "system_admin" || payload?.role === "admin";
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function systemLogin(password: string): Promise<void> {
  const response = await fetch(`${BASE}/auth/system-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    throw new Error(`登录失败（HTTP ${response.status}）`);
  }
  const data = await response.json() as { access_token?: string };
  if (!data.access_token) {
    throw new Error("登录失败：未返回 token");
  }
  setToken(data.access_token);
}

function decodeJwtPayload(token: string): { role?: string } | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    return JSON.parse(window.atob(padded)) as { role?: string };
  } catch {
    return null;
  }
}
