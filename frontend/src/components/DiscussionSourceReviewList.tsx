import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  approveDiscussionSources,
  deletePendingDiscussionSources,
  fetchDiscussionRules,
  fetchDiscussionSources,
} from "../discussions/api";

export function DiscussionSourceReviewList() {
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const sources = useQuery({ queryKey: ["discussion-sources"], queryFn: fetchDiscussionSources });
  const rules = useQuery({ queryKey: ["discussion-rules"], queryFn: fetchDiscussionRules });
  const pending = useMemo(() => (sources.data ?? []).filter((source) => source.review_status === "pending"), [sources.data]);
  const selected = new Set(selectedIds);
  const allSelected = pending.length > 0 && pending.every((source) => selected.has(source.id));

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  async function refresh(): Promise<void> {
    await Promise.all([sources.refetch(), rules.refetch()]);
  }

  function toggle(id: number, checked: boolean): void {
    setSelectedIds((current) => checked ? [...new Set([...current, id])] : current.filter((value) => value !== id));
  }

  async function approve(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个待审核邮件列表。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      const result = await approveDiscussionSources(selectedIds);
      setSelectedIds([]); setNotice({ text: `已通过 ${result.approved_count} 个邮件列表。`, tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "审核通过失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function remove(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个待审核邮件列表。", tone: "danger" }); return; }
    if (!window.confirm(`删除选中的 ${selectedIds.length} 个待审核邮件列表？`)) return;
    setNotice(null); setBusy(true);
    try {
      const result = await deletePendingDiscussionSources(selectedIds);
      setSelectedIds([]); setNotice({ text: `已删除 ${result.deleted_count} 个待审核邮件列表。`, tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "删除失败", tone: "danger" }); } finally { setBusy(false); }
  }

  return <section style={section}>
    <div style={headerRow}>
      <div>
        <div style={title}>邮件探查 · 待审核列表</div>
        <div style={subtitle}>新增入口位于“邮件探查 · 技术讨论”。这里仅处理审核；待审核项会纳入现有邮件提醒。</div>
      </div>
      <div style={actions}>
        <span style={counter}>待审核 <b>{pending.length}</b> 个 · 已选 <b>{selected.size}</b> 个</span>
        <label style={checkLabel}><input type="checkbox" checked={allSelected} disabled={!pending.length || busy} onChange={(event) => setSelectedIds(event.target.checked ? pending.map((source) => source.id) : [])} />全选</label>
        <button type="button" disabled={!selectedIds.length || busy} onClick={() => void approve()} style={!selectedIds.length || busy ? disabledPrimary : primary}>批量通过</button>
        <button type="button" disabled={!selectedIds.length || busy} onClick={() => void remove()} style={!selectedIds.length || busy ? disabledDanger : danger}>批量删除</button>
      </div>
    </div>
    {notice && <div style={{ ...noticeStyle, display: "flex", gap: 8, alignItems: "flex-start", color: notice.tone === "danger" ? "#b42318" : "#067647", borderColor: notice.tone === "danger" ? "#fecdca" : "#abefc6", background: notice.tone === "danger" ? "#fef3f2" : "#ecfdf3" }}><span style={{ flex: 1 }}>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示" style={closeButton}>×</button></div>}
    <div style={list}>
      {sources.isLoading && <div style={empty}>加载待审核邮件列表中...</div>}
      {sources.isError && <div style={{ ...empty, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>加载待审核邮件列表失败。</div>}
      {pending.map((source) => <SourceRow key={source.id} source={source} rules={rules.data ?? []} selected={selected.has(source.id)} busy={busy} onToggle={(checked) => toggle(source.id, checked)} />)}
      {sources.data && !pending.length && <div style={empty}>暂无待审核邮件列表。</div>}
    </div>
  </section>;
}

function SourceRow({ source, rules, selected, busy, onToggle }: { source: { id: number; name: string }; rules: Array<{ source_id: number; rule_type: string; match_value: string }>; selected: boolean; busy: boolean; onToggle: (checked: boolean) => void }) {
  const sourceRules = rules.filter((rule) => rule.source_id === source.id);
  return <div style={{ ...row, borderColor: selected ? "#b9d4ff" : "#eaecf0", background: selected ? "#f8fbff" : "#fff" }}><input type="checkbox" aria-label={`选择 ${source.name}`} checked={selected} disabled={busy} onChange={(event) => onToggle(event.target.checked)} /><div style={{ flex: 1, minWidth: 0 }}><div style={rowTitle}>{source.name}<span style={pendingBadge}>pending</span></div><div style={rulesStyle}>{sourceRules.map((rule) => `${rule.rule_type}: ${rule.match_value}`).join(" · ")}</div></div></div>;
}

const section: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 };
const headerRow: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 12 };
const title: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const subtitle: CSSProperties = { fontSize: 11, color: "#667085", marginTop: 3 };
const actions: CSSProperties = { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" };
const counter: CSSProperties = { fontSize: 11, color: "#475467" };
const checkLabel: CSSProperties = { display: "inline-flex", gap: 5, alignItems: "center", color: "#475467", fontSize: 11 };
const primary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const danger: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const disabledPrimary: CSSProperties = { ...primary, background: "#98a2b3", cursor: "not-allowed" };
const disabledDanger: CSSProperties = { ...danger, color: "#98a2b3", borderColor: "#eaecf0", cursor: "not-allowed" };
const noticeStyle: CSSProperties = { border: "1px solid", borderRadius: 8, padding: "8px 10px", fontSize: 11, marginBottom: 10 };
const closeButton: CSSProperties = { border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 };
const list: CSSProperties = { display: "grid", gap: 8 };
const row: CSSProperties = { display: "flex", alignItems: "flex-start", gap: 10, border: "1px solid", borderRadius: 9, padding: "9px 11px" };
const rowTitle: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const pendingBadge: CSSProperties = { marginLeft: 6, borderRadius: 999, padding: "2px 7px", background: "#fffaeb", color: "#b54708", fontSize: 11 };
const rulesStyle: CSSProperties = { marginTop: 5, color: "#475467", fontSize: 11, wordBreak: "break-word" };
const empty: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 14, color: "#667085", fontSize: 12 };
