import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  approveDiscoveryMethods,
  deletePendingDiscoveryMethods,
  getCrawlMethodReviewReminderConfig,
  listPendingDiscoveryMethods,
  sendCrawlMethodReviewReminderNow,
  updateCrawlMethodReviewReminderConfig,
} from "../api/client";
import type { CrawlMethod } from "../types";

export function CrawlMethodReviewList({ onOpenMethod }: { onOpenMethod?: (id: number) => void }) {
  const qc = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [summary, setSummary] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [recipientsText, setRecipientsText] = useState("");
  const pending = useQuery({ queryKey: ["discovery-methods", "pending-review"], queryFn: listPendingDiscoveryMethods });
  const reminder = useQuery({
    queryKey: ["discovery-review-reminder"],
    queryFn: getCrawlMethodReviewReminderConfig,
  });
  const methods = useMemo(() => pending.data ?? [], [pending.data]);
  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const allSelected = methods.length > 0 && methods.every((method) => selected.has(method.id));

  const approveMut = useMutation({ mutationFn: approveDiscoveryMethods });
  const deleteMut = useMutation({ mutationFn: deletePendingDiscoveryMethods });
  const reminderMut = useMutation({ mutationFn: updateCrawlMethodReviewReminderConfig });
  const sendReminderMut = useMutation({ mutationFn: sendCrawlMethodReviewReminderNow });

  useEffect(() => {
    if (!reminder.data) return;
    setRecipientsText(reminder.data.recipients.join(", "));
  }, [reminder.data]);

  function toggle(id: number, checked: boolean) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return [...next];
    });
  }

  function toggleAll(checked: boolean) {
    setSelectedIds(checked ? methods.map((method) => method.id) : []);
  }

  async function refreshLists() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["discovery-methods"] }),
      qc.invalidateQueries({ queryKey: ["discovery-methods", "pending-review"] }),
    ]);
  }

  async function approveSelected() {
    if (selectedIds.length === 0) {
      setSummary({ text: "请先选择至少一个待审核方式。", tone: "danger" });
      return;
    }
    try {
      const result = await approveMut.mutateAsync(selectedIds);
      setSelectedIds([]);
      setSummary({ text: `已通过 ${result.approved_count} 个爬取方式`, tone: "success" });
      await refreshLists();
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "审核通过失败", tone: "danger" });
    }
  }

  async function deleteSelected() {
    if (selectedIds.length === 0) {
      setSummary({ text: "请先选择至少一个待审核方式。", tone: "danger" });
      return;
    }
    if (!window.confirm(`删除选中的 ${selectedIds.length} 个待审核爬取方式？`)) return;
    try {
      const result = await deleteMut.mutateAsync(selectedIds);
      setSelectedIds([]);
      setSummary({ text: `已删除 ${result.deleted_count} 个待审核方式`, tone: "success" });
      await refreshLists();
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "删除失败", tone: "danger" });
    }
  }

  async function saveReminderConfig(enabled?: boolean) {
    const current = reminder.data;
    const recipients = recipientsText
      .split(/[,;\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    try {
      const next = await reminderMut.mutateAsync({
        enabled: enabled ?? current?.enabled ?? false,
        interval_minutes: current?.interval_minutes ?? 1440,
        recipients,
      });
      setRecipientsText(next.recipients.join(", "));
      setSummary({ text: "审核提醒设置已保存", tone: "success" });
      await qc.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "保存提醒设置失败", tone: "danger" });
    }
  }

  async function changeInterval(intervalMinutes: number) {
    const current = reminder.data;
    try {
      await reminderMut.mutateAsync({
        enabled: current?.enabled ?? false,
        interval_minutes: intervalMinutes,
        recipients: current?.recipients ?? [],
      });
      await qc.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
      setSummary({ text: "提醒间隔已更新", tone: "success" });
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "更新提醒间隔失败", tone: "danger" });
    }
  }

  async function sendNow() {
    try {
      const result = await sendReminderMut.mutateAsync();
      setSummary({
        text: result.sent ? `已发送 ${result.count} 个待审核方式提醒` : `未发送：${result.reason}${result.error ? ` · ${result.error}` : ""}`,
        tone: result.sent ? "success" : "danger",
      });
      await qc.invalidateQueries({ queryKey: ["discovery-review-reminder"] });
    } catch (error) {
      setSummary({ text: error instanceof ApiError ? error.message : "发送提醒失败", tone: "danger" });
    }
  }

  return (
    <section style={section}>
      <div style={headerRow}>
        <div>
          <div style={title}>抓取模块 · 待审核方式</div>
          <div style={subtitle}>智能探查成功后先进入这里。通过审核后才会进入正式爬取方式库。</div>
        </div>
        <div style={actions}>
          {expanded && (
            <>
              <span style={counter}>待审核 <b>{methods.length}</b> 个 · 已选 <b>{selected.size}</b> 个</span>
              <label style={checkLabel}>
                <input type="checkbox" checked={allSelected} disabled={methods.length === 0} onChange={(event) => toggleAll(event.target.checked)} />
                全选
              </label>
              <button type="button" style={btnPrimary} disabled={selected.size === 0 || approveMut.isPending} onClick={approveSelected}>
                {approveMut.isPending ? "通过中..." : "批量通过"}
              </button>
              <button type="button" style={btnDangerGhost} disabled={selected.size === 0 || deleteMut.isPending} onClick={deleteSelected}>
                {deleteMut.isPending ? "删除中..." : "批量删除"}
              </button>
            </>
          )}
          <button type="button" style={btnGhost} onClick={() => setExpanded((value) => !value)}>
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {!expanded ? null : (
        <>
          {summary && <div style={{ ...notice, color: summary.tone === "danger" ? "#b42318" : "#059669" }}>{summary.text}</div>}
          <div style={reminderBar}>
            <label style={inlineControl}>
              <span>邮件提醒</span>
              <input
                type="checkbox"
                checked={Boolean(reminder.data?.enabled)}
                disabled={reminder.isLoading || reminderMut.isPending}
                onChange={(event) => saveReminderConfig(event.target.checked)}
              />
            </label>
            <label style={inlineControl}>
              <span>间隔</span>
              <select
                value={reminder.data?.interval_minutes ?? 1440}
                disabled={reminder.isLoading || reminderMut.isPending}
                onChange={(event) => changeInterval(Number(event.target.value))}
                style={selectStyle}
              >
                <option value={30}>30 分钟</option>
                <option value={60}>1 小时</option>
                <option value={360}>6 小时</option>
                <option value={720}>12 小时</option>
                <option value={1440}>24 小时</option>
              </select>
            </label>
            <input
              style={recipientInput}
              placeholder="管理员邮箱，多个用逗号分隔"
              value={recipientsText}
              onChange={(event) => setRecipientsText(event.target.value)}
            />
            <button type="button" style={btnGhost} disabled={reminderMut.isPending} onClick={() => saveReminderConfig()}>
              保存提醒
            </button>
            <button type="button" style={btnGhost} disabled={sendReminderMut.isPending || methods.length === 0} onClick={sendNow}>
              立即提醒
            </button>
            {reminder.data?.last_result_status && (
              <span style={lastStatus}>上次：{reminder.data.last_result_status}</span>
            )}
          </div>

          <div style={listGrid}>
            {pending.isLoading && <div style={infoBox}>加载待审核方式中...</div>}
            {pending.isError && <div style={{ ...infoBox, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>加载待审核方式失败。</div>}
            {methods.map((method) => (
              <ReviewRow
                key={method.id}
                method={method}
                selected={selected.has(method.id)}
                onToggle={(checked) => toggle(method.id, checked)}
                onOpen={() => onOpenMethod?.(method.id)}
              />
            ))}
            {pending.data && methods.length === 0 && (
              <div style={emptyBox}>暂无待审核爬取方式。新的智能探查结果会先出现在这里。</div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function ReviewRow({ method, selected, onToggle, onOpen }: {
  method: CrawlMethod;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onOpen: () => void;
}) {
  const primaryLabel = method.source_name?.trim() || method.domain;
  return (
    <div style={{ ...row, borderColor: selected ? "#b9d4ff" : "#eaecf0", background: selected ? "#f8fbff" : "#fff" }}>
      <input type="checkbox" checked={selected} aria-label={`选择 ${primaryLabel}`} onChange={(event) => onToggle(event.target.checked)} />
      <QualityBadge method={method} />
      <div style={{ flex: 1, minWidth: 0, cursor: "pointer" }} onClick={onOpen}>
        <div style={rowTitle}>
          {primaryLabel} <span style={pendingBadge}>pending</span>
        </div>
        <div style={rowUrl}>{method.domain !== primaryLabel ? `${method.domain} · ` : ""}{method.entry_url}</div>
      </div>
      <div style={rowMeta}>{method.created_at ? new Date(method.created_at).toLocaleString() : "刚创建"}</div>
    </div>
  );
}

function QualityBadge({ method }: { method: CrawlMethod }) {
  const score = methodOverallScore(method);
  const label = typeof score === "number" ? `${gradeForScore(score)} ${score}` : "未审计";
  return <span title={method.quality_reason || ""} style={qualityBadgeStyle(score, method.quality_audit_status)}>{label}</span>;
}

function methodOverallScore(method: CrawlMethod) {
  if (typeof method.overall_score === "number") return method.overall_score;
  if (typeof method.quality_score !== "number") return null;
  const densityScore = typeof method.density_score === "number" ? method.density_score : method.quality_score;
  return Math.round(method.quality_score * 0.5 + densityScore * 0.5);
}

function gradeForScore(score: number) {
  if (score >= 85) return "A";
  if (score >= 70) return "B";
  if (score >= 50) return "C";
  return "D";
}

function qualityBadgeStyle(score: number | null | undefined, status: string | null | undefined): CSSProperties {
  const base: CSSProperties = {
    minWidth: 58,
    textAlign: "center",
    borderRadius: 8,
    padding: "4px 7px",
    fontSize: 11,
    fontWeight: 800,
    lineHeight: 1.1,
    whiteSpace: "nowrap",
    border: "1px solid #d0d5dd",
    color: "#475467",
    background: "#f9fafb",
  };
  if (typeof score !== "number") return base;
  if (status === "failed" || score < 50) return { ...base, border: "1px solid #fecdca", color: "#b42318", background: "#fef3f2" };
  if (status === "weak" || score < 70) return { ...base, border: "1px solid #fedf89", color: "#b54708", background: "#fffaeb" };
  return { ...base, border: "1px solid #abefc6", color: "#027a48", background: "#ecfdf3" };
}

const section: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 };
const headerRow: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 12 };
const title: CSSProperties = { fontSize: 15, fontWeight: 700, color: "#101828" };
const subtitle: CSSProperties = { fontSize: 12, color: "#667085", marginTop: 2 };
const actions: CSSProperties = { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" };
const counter: CSSProperties = { fontSize: 12, color: "#475467" };
const checkLabel: CSSProperties = { display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#475467" };
const notice: CSSProperties = { fontSize: 13, marginBottom: 10 };
const reminderBar: CSSProperties = { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "10px 0", borderTop: "1px solid #eaecf0", borderBottom: "1px solid #eaecf0", marginBottom: 10 };
const inlineControl: CSSProperties = { display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#475467" };
const listGrid: CSSProperties = { display: "grid", gap: 8 };
const row: CSSProperties = { display: "flex", alignItems: "center", gap: 12, border: "1px solid #eaecf0", borderRadius: 9, padding: "9px 11px" };
const rowTitle: CSSProperties = { fontSize: 14, fontWeight: 700, color: "#101828" };
const rowUrl: CSSProperties = { fontSize: 12, color: "#667085", wordBreak: "break-all" };
const rowMeta: CSSProperties = { fontSize: 12, color: "#667085", textAlign: "right", minWidth: 150 };
const pendingBadge: CSSProperties = { fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999, marginLeft: 6, background: "#fffaeb", color: "#b54708" };
const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnGhost: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#344054", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDangerGhost: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const infoBox: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
const emptyBox: CSSProperties = { ...infoBox };
const selectStyle: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, padding: "6px 8px", background: "#fff", color: "#344054", fontSize: 12 };
const recipientInput: CSSProperties = { minWidth: 260, flex: 1, border: "1px solid #d0d5dd", borderRadius: 8, padding: "7px 10px", fontSize: 12 };
const lastStatus: CSSProperties = { fontSize: 12, color: "#667085" };
