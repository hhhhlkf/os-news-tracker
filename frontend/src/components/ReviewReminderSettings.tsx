import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getCrawlMethodReviewReminderConfig,
  sendCrawlMethodReviewReminderNow,
  updateCrawlMethodReviewReminderConfig,
} from "../api/client";
import { clampInput, INPUT_LIMITS } from "../inputLimits";
import { emailListError, parseEmailList } from "../mail/emailValidation";

export function ReviewReminderSettings() {
  const queryClient = useQueryClient();
  const [recipientsText, setRecipientsText] = useState("");
  const [notice, setNotice] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const reminder = useQuery({ queryKey: ["discovery-review-reminder"], queryFn: getCrawlMethodReviewReminderConfig });
  const updateMutation = useMutation({ mutationFn: updateCrawlMethodReviewReminderConfig });
  const sendMutation = useMutation({ mutationFn: sendCrawlMethodReviewReminderNow });
  const recipientsError = useMemo(() => emailListError(parseEmailList(recipientsText)), [recipientsText]);

  useEffect(() => {
    if (reminder.data) setRecipientsText(reminder.data.recipients.join(", "));
  }, [reminder.data]);

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  async function save(enabled?: boolean): Promise<void> {
    const current = reminder.data;
    const parsed = parseEmailList(recipientsText);
    if (recipientsError) { setNotice({ text: recipientsError, tone: "danger" }); return; }
    try {
      const next = await updateMutation.mutateAsync({
        enabled: enabled ?? current?.enabled ?? false,
        interval_minutes: current?.interval_minutes ?? 1440,
        recipients: parsed.valid,
      });
      setRecipientsText(next.recipients.join(", "));
      setNotice({ text: "审核提醒设置已保存", tone: "success" });
      await queryClient.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "保存提醒设置失败", tone: "danger" }); }
  }

  async function setInterval(intervalMinutes: number): Promise<void> {
    try {
      await updateMutation.mutateAsync({ enabled: reminder.data?.enabled ?? false, interval_minutes: intervalMinutes, recipients: reminder.data?.recipients ?? [] });
      await queryClient.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
      setNotice({ text: "提醒间隔已更新", tone: "success" });
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "更新提醒间隔失败", tone: "danger" }); }
  }

  async function sendNow(): Promise<void> {
    try {
      const result = await sendMutation.mutateAsync();
      setNotice({ text: result.sent ? `已发送 ${result.count} 个待审核项提醒（含邮件列表）` : `未发送：${result.reason}${result.error ? ` · ${result.error}` : ""}`, tone: result.sent ? "success" : "danger" });
      await queryClient.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "发送提醒失败", tone: "danger" }); }
  }

  const busy = reminder.isLoading || updateMutation.isPending;
  return <section style={section}>
    <div style={title}>审核邮件提醒</div>
    <div style={subtitle}>提醒同时包含待审核的爬取方式和邮件列表，切换 Tab 后设置保持不变。</div>
    {notice && <div style={{ ...noticeStyle, display: "flex", gap: 8, alignItems: "flex-start", color: notice.tone === "danger" ? "#b42318" : "#067647", borderColor: notice.tone === "danger" ? "#fecdca" : "#abefc6", background: notice.tone === "danger" ? "#fef3f2" : "#ecfdf3" }}><span style={{ flex: 1 }}>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示" style={closeButton}>×</button></div>}
    <div style={controls}>
      <label style={inlineControl}><span>邮件提醒</span><input type="checkbox" checked={Boolean(reminder.data?.enabled)} disabled={busy} onChange={(event) => void save(event.target.checked)} /></label>
      <label style={inlineControl}><span>间隔</span><select value={reminder.data?.interval_minutes ?? 1440} disabled={busy} onChange={(event) => void setInterval(Number(event.target.value))} style={selectStyle}><option value={30}>30 分钟</option><option value={60}>1 小时</option><option value={360}>6 小时</option><option value={720}>12 小时</option><option value={1440}>24 小时</option></select></label>
      <div style={{ display: "grid", gap: 4, flex: 1, minWidth: 220 }}><input style={recipientInput} placeholder="管理员邮箱，多个用逗号分隔" value={recipientsText} maxLength={INPUT_LIMITS.emailList} onChange={(event) => setRecipientsText(clampInput(event.target.value, INPUT_LIMITS.emailList))} />{recipientsError && <span style={{ fontSize: 11, color: "#b42318" }}>{recipientsError}</span>}</div>
      <button type="button" style={button} disabled={updateMutation.isPending || Boolean(recipientsError)} onClick={() => void save()}>保存提醒</button>
      <button type="button" style={button} disabled={sendMutation.isPending} onClick={() => void sendNow()}>{sendMutation.isPending ? "发送中…" : "立即提醒"}</button>
      {reminder.data?.last_result_status && <span style={lastStatus}>上次：{reminder.data.last_result_status}</span>}
    </div>
  </section>;
}

const section: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 10, background: "#f8fafc", padding: "14px 12px 12px" };
const title: CSSProperties = { fontSize: 12, fontWeight: 800, color: "#101828", lineHeight: 1.4 };
const subtitle: CSSProperties = { marginTop: 3, marginBottom: 10, color: "#667085", fontSize: 11 };
const noticeStyle: CSSProperties = { border: "1px solid", borderRadius: 8, padding: "8px 10px", fontSize: 11, marginBottom: 10 };
const closeButton: CSSProperties = { border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 };
const controls: CSSProperties = { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" };
const inlineControl: CSSProperties = { display: "inline-flex", alignItems: "center", gap: 7, color: "#475467", fontSize: 11 };
const selectStyle: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 7, padding: "6px 8px", background: "#fff", color: "#344054", fontSize: 11 };
const recipientInput: CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid #d0d5dd", borderRadius: 7, padding: "7px 9px", background: "#fff", color: "#101828", fontSize: 11 };
const button: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 7, padding: "7px 10px", background: "#fff", color: "#344054", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const lastStatus: CSSProperties = { color: "#667085", fontSize: 11 };
