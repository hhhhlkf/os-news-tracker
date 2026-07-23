import { useEffect, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelWechatQrSession,
  fetchWechatAuthProfile,
  fetchWechatQrSession,
  startWechatQrSession,
} from "../api/client";
import type { WechatQrSessionStatus } from "../types";

const SECTION: CSSProperties = {
  background: "#fff",
  border: "1px solid #d0d5dd",
  borderRadius: 10,
  padding: 16,
  marginTop: 14,
};

const STORAGE_KEY = "discovery.wechat-auth-panel.expanded";

const terminalStatuses = new Set<WechatQrSessionStatus>([
  "success",
  "expired",
  "failed",
  "cancelled",
]);

const statusLabels: Record<string, string> = {
  unconfigured: "未配置",
  valid: "有效",
  expired: "已过期",
  failed: "失败",
  pending: "正在启动浏览器",
  qr_ready: "等待扫码",
  scanned: "已扫码，等待手机确认",
  success: "续期成功",
  cancelled: "已取消",
};

function readExpandedState(): boolean {
  if (typeof window === "undefined") return true;
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw == null ? true : raw === "true";
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}

export function WechatAuthPanel() {
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(readExpandedState);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY, String(expanded));
  }, [expanded]);

  const profileQuery = useQuery({
    queryKey: ["wechat-auth-profile"],
    queryFn: fetchWechatAuthProfile,
    retry: false,
  });
  const sessionQuery = useQuery({
    queryKey: ["wechat-qr-session", sessionId],
    queryFn: () => fetchWechatQrSession(sessionId!),
    enabled: Boolean(sessionId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && terminalStatuses.has(status) ? false : 1500;
    },
    retry: false,
  });
  const startMutation = useMutation({
    mutationFn: startWechatQrSession,
    onSuccess: (session) => setSessionId(session.session_id),
  });
  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelWechatQrSession(id),
  });

  useEffect(() => {
    if (sessionQuery.data?.status === "success") {
      void queryClient.invalidateQueries({ queryKey: ["wechat-auth-profile"] });
    }
  }, [queryClient, sessionQuery.data?.status]);

  const profile = profileQuery.data;
  const session = cancelMutation.data ?? sessionQuery.data;
  const active = Boolean(session && !terminalStatuses.has(session.status));
  const error = profileQuery.error ?? startMutation.error ?? sessionQuery.error ?? cancelMutation.error;

  return (
    <section style={SECTION}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · 微信认证</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2, lineHeight: 1.5 }}>
            扫码续期公众号登录态；成功后立即写入数据库，后续微信抓取无需改环境变量或重建容器。
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={statusBadge(profile?.status)}>
            {profileQuery.isPending ? "读取中" : statusLabels[profile?.status ?? ""] ?? profile?.status ?? "未知"}
          </span>
          <button type="button" onClick={() => setExpanded((value) => !value)} style={btnGhost}>
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {!expanded ? null : (
        <>
          <div style={detailGrid}>
            <Detail label="认证名称" value={profile?.profile_name ?? "—"} />
            <Detail
              label="认证来源"
              value={
                profile?.source === "database"
                  ? "数据库"
                  : profile?.source === "environment"
                    ? "环境变量（兼容）"
                    : "未配置"
              }
            />
            <Detail label="最近续期" value={formatTime(profile?.updated_at ?? null)} />
            <Detail label="最近验证" value={formatTime(profile?.last_verified_at ?? null)} />
          </div>
          {profile?.last_error && <div style={errorBox}>最近错误：{profile.last_error}</div>}

          <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
            <button
              type="button"
              style={btnPrimary}
              disabled={active || startMutation.isPending}
              onClick={() => {
                cancelMutation.reset();
                startMutation.reset();
                startMutation.mutate();
              }}
            >
              {active ? "扫码会话进行中" : "启动扫码续期"}
            </button>
            {active && sessionId && (
              <button
                type="button"
                style={btnGhost}
                disabled={cancelMutation.isPending}
                onClick={() => cancelMutation.mutate(sessionId)}
              >
                取消扫码
              </button>
            )}
          </div>
          {error instanceof Error && <div style={errorBox}>{error.message}</div>}

          {session && (
            <div style={sessionBox}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#101828" }}>扫码会话</div>
                <span style={statusBadge(session.status)}>
                  {statusLabels[session.status] ?? session.status}
                </span>
              </div>
              <p style={sessionCopy}>{session.message ?? "正在准备登录页面…"}</p>
              {session.expires_at && active && (
                <p style={{ ...sessionCopy, marginTop: 4, fontSize: 11 }}>有效期至 {formatTime(session.expires_at)}</p>
              )}
              {session.qr_image_data_url && (
                <div style={qrFrame}>
                  <img
                    src={session.qr_image_data_url}
                    alt="微信公众号平台扫码登录页面"
                    style={{ display: "block", width: "100%", height: "auto" }}
                  />
                </div>
              )}
              {session.status === "success" && (
                <div style={successBox}>续期完成。新登录态已写入数据库，下一次公众号查询会立即使用。</div>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: "#667085", fontSize: 11, marginBottom: 3 }}>{label}</div>
      <div style={{ color: "#101828", fontSize: 13, fontWeight: 650, overflowWrap: "anywhere" }}>{value}</div>
    </div>
  );
}

function statusBadge(status: string | undefined): CSSProperties {
  const good = status === "valid" || status === "success";
  const warning = status === "expired" || status === "failed";
  return {
    display: "inline-flex",
    borderRadius: 999,
    padding: "3px 8px",
    fontSize: 11,
    fontWeight: 700,
    color: good ? "#027a48" : warning ? "#b42318" : "#344054",
    background: good ? "#ecfdf3" : warning ? "#fef3f2" : "#f2f4f7",
    whiteSpace: "nowrap",
  };
}

const detailGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
  gap: 12,
  padding: 12,
  borderRadius: 8,
  background: "#f9fafb",
  border: "1px solid #f2f4f7",
};

const btnPrimary: CSSProperties = {
  border: "none",
  borderRadius: 999,
  background: "#175cd3",
  color: "#fff",
  padding: "8px 16px",
  fontSize: 13,
  fontWeight: 700,
  cursor: "pointer",
};

const btnGhost: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  background: "#fff",
  color: "#344054",
  padding: "8px 14px",
  fontSize: 13,
  fontWeight: 700,
  cursor: "pointer",
};

const sessionBox: CSSProperties = {
  marginTop: 14,
  padding: 12,
  borderRadius: 8,
  border: "1px solid #eaecf0",
  background: "#fcfcfd",
};

const sessionCopy: CSSProperties = {
  margin: "8px 0 0",
  color: "#667085",
  fontSize: 12,
  lineHeight: 1.5,
};

const qrFrame: CSSProperties = {
  maxWidth: 280,
  margin: "12px auto 0",
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  overflow: "hidden",
  background: "#fff",
};

const errorBox: CSSProperties = {
  marginTop: 12,
  padding: "8px 10px",
  borderRadius: 8,
  color: "#b42318",
  background: "#fef3f2",
  fontSize: 12,
  border: "1px solid #fecdca",
};

const successBox: CSSProperties = {
  marginTop: 12,
  padding: "8px 10px",
  borderRadius: 8,
  color: "#027a48",
  background: "#ecfdf3",
  fontSize: 12,
  fontWeight: 650,
  border: "1px solid #a6f4c5",
};
