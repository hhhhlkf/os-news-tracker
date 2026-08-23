import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import * as echarts from "echarts";
import type { DiscussionDetail, DiscussionTopologyMessage } from "../discussions/api";

type GraphNode = {
  id: string;
  name: string;
  author: string;
  message: DiscussionTopologyMessage;
  symbolSize: number;
  itemStyle: { color: string; borderColor: string; borderWidth: number; opacity: number };
  label: { show: boolean; opacity: number };
};

type MessageLanguage = "zh" | "en";

export function DiscussionTopology({ discussion }: { discussion: DiscussionDetail }) {
  const [expanded, setExpanded] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [language, setLanguage] = useState<MessageLanguage>("zh");
  const evidenceIds = useMemo(() => collectEvidenceIds(discussion), [discussion]);
  const latestIds = useMemo(() => new Set(discussion.snapshots[0]?.new_message_ids ?? []), [discussion.snapshots]);
  const selected = discussion.messages.find((message) => message.id === selectedId) ?? null;
  if (!discussion.messages.length) return null;
  return <>
    <section style={section}>
      <div style={headingRow}><div><div style={heading}>回复拓扑图</div><div style={subheading}>圆点表示邮件或 GitHub 讨论消息，连线只表示平台真实回复关系；Issue 评论保持同级。</div></div><div style={headingControls}><LanguageToggle language={language} onChange={setLanguage} /><Legend /></div></div>
      <GraphCanvas messages={discussion.messages} evidenceIds={evidenceIds} latestIds={latestIds} search={search} setSearch={setSearch} language={language} selectedId={selectedId} onSelect={setSelectedId} maxNodes={50} compact onExpand={() => setExpanded(true)} />
      {selected && <SelectedMessage message={selected} language={language} />}
    </section>
    {expanded && <div style={overlay} role="dialog" aria-modal="true" aria-label="回复拓扑图全屏视图">
      <div style={modal}><div style={modalHeader}><div><b>回复拓扑图</b><span style={modalHint}>可拖动圆形节点 · 拖动画布 · 滚轮缩放 · 点击节点查看详细邮件信息</span></div><button type="button" onClick={() => setExpanded(false)} style={closeButton}>关闭</button></div>
        <GraphCanvas messages={discussion.messages} evidenceIds={evidenceIds} latestIds={latestIds} search={search} setSearch={setSearch} language={language} selectedId={selectedId} onSelect={setSelectedId} maxNodes={200} />
        {selected && <SelectedMessage message={selected} language={language} full />}
      </div>
    </div>}
  </>;
}

function GraphCanvas({ messages, evidenceIds, latestIds, search, setSearch, language, selectedId, onSelect, maxNodes, compact = false, onExpand }: {
  messages: DiscussionTopologyMessage[]; evidenceIds: Set<string>; latestIds: Set<string>; search: string; setSearch: (value: string) => void; language: MessageLanguage; selectedId: number | null; onSelect: (id: number) => void; maxNodes: number; compact?: boolean; onExpand?: () => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const normalizedSearch = search.trim().toLowerCase();
  const visibleMessages = useMemo(() => prioritizeMessages(messages, evidenceIds, latestIds, maxNodes), [messages, evidenceIds, latestIds, maxNodes]);
  const nodes = useMemo(() => graphNodes(visibleMessages, evidenceIds, normalizedSearch, language, selectedId), [visibleMessages, evidenceIds, normalizedSearch, language, selectedId]);
  const edges = useMemo(() => graphEdges(visibleMessages), [visibleMessages]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const chart = echarts.init(host);
    const clickHandler = (event: echarts.ECElementEvent): void => {
      if (event.dataType !== "node") return;
      const node = event.data as GraphNode;
      const messageId = node?.message?.id;
      if (typeof messageId === "number") onSelect(messageId);
    };
    chart.on("click", clickHandler);
    chart.setOption({
      animationDuration: 360,
      animationDurationUpdate: 220,
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (params: { dataType?: string; data?: GraphNode }) => params.dataType === "node" && params.data
          ? `<b>${escapeHtml(oneSentenceSummary(params.data.message, language))}</b><br/><span style="color:#667085">${escapeHtml(params.data.message.subject)} · ${escapeHtml(params.data.author)}</span>`
          : "回复关系",
      },
      series: [{
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        data: nodes,
        links: edges,
        edgeSymbol: ["none", "arrow"],
        edgeSymbolSize: [0, 7],
        label: {
          show: true,
          position: "right",
          distance: 7,
          width: compact ? 120 : 190,
          overflow: "truncate",
          color: "#344054",
          fontSize: compact ? 10 : 12,
          lineHeight: compact ? 14 : 17,
          formatter: (params: { data: GraphNode }) => `${params.data.name}\n{author|${params.data.author}}`,
          rich: { author: { color: "#98a2b3", fontSize: compact ? 9 : 10, lineHeight: compact ? 12 : 14 } },
        },
        lineStyle: { color: "#98a2b3", width: 1.35, curveness: 0.08, opacity: 0.76 },
        emphasis: { focus: "adjacency", lineStyle: { color: "#175cd3", width: 2.1 }, scale: true },
        force: { repulsion: compact ? 170 : 300, gravity: 0.08, edgeLength: compact ? [70, 110] : [100, 175], layoutAnimation: true },
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(host);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [nodes, edges, compact, language, onSelect]);

  return <div style={{ ...canvasFrame, height: compact ? 270 : "min(68vh, 680px)" }}>
    <div style={toolbar}><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索内容或参与者" style={searchInput} />{compact && <button type="button" onClick={onExpand} style={primaryToolButton}>全屏查看</button>}</div>
    <div ref={hostRef} style={graphHost} />
    {messages.length > visibleMessages.length && <div style={foldNotice}>缩略图已折叠 {messages.length - visibleMessages.length} 封邮件</div>}
  </div>;
}

function graphNodes(messages: DiscussionTopologyMessage[], evidenceIds: Set<string>, search: string, language: MessageLanguage, selectedId: number | null): GraphNode[] {
  return messages.map((message) => {
  const author = (message.author_name || message.author_email || message.platform_user_id || "未知参与者").replace(/\s+/g, " ").trim();
    const phrase = shortPhrase(message, language);
    const matching = !search || [phrase, oneSentenceSummary(message, language), message.subject, author, message.author_email].some((value) => value?.toLowerCase().includes(search));
    const root = message.parent_message_id == null || !messages.some((candidate) => candidate.id === message.parent_message_id);
    const evidence = evidenceIds.has(message.message_id) || evidenceIds.has(`mail:${message.message_id}`);
    const color = root ? "#dbeafe" : message.context_incomplete ? "#fef0c7" : evidence ? "#d1fadf" : "#f2f4f7";
    const borderColor = selectedId === message.id ? "#175cd3" : root ? "#528bff" : message.context_incomplete ? "#f79009" : evidence ? "#12b76a" : "#98a2b3";
    return {
      id: String(message.id),
      name: truncate(phrase, 26),
      author: truncate(author, 18),
      message,
      symbolSize: root ? 46 : selectedId === message.id ? 36 : 26,
      itemStyle: { color, borderColor, borderWidth: selectedId === message.id ? 3 : 1.7, opacity: matching ? 1 : 0.18 },
      label: { show: true, opacity: matching ? 1 : 0.18 },
    };
  });
}

function originalPreview(message: DiscussionTopologyMessage): string {
  const lines = (message.authored_text || message.body_text || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter((line) => line.length > 0)
    .filter((line) => !/^(?:>|On .+wrote:|From:|Sent:|To:|Subject:)/i.test(line));
  return lines[0] || (message.provider === "github" ? "未提取讨论正文" : "未提取邮件正文");
}

function shortPhrase(message: DiscussionTopologyMessage, language: MessageLanguage): string {
  if (language === "zh") return message.translation_phrase || "翻译处理中";
  return originalPreview(message);
}

function oneSentenceSummary(message: DiscussionTopologyMessage, language: MessageLanguage): string {
  if (language === "zh") return message.translation_summary || "讨论翻译处理中。";
  return originalPreview(message);
}

function graphEdges(messages: DiscussionTopologyMessage[]) {
  const ids = new Set(messages.map((message) => message.id));
  return messages.flatMap((message) => message.parent_message_id != null && ids.has(message.parent_message_id)
    ? [{ source: String(message.parent_message_id), target: String(message.id) }]
    : []);
}

function prioritizeMessages(messages: DiscussionTopologyMessage[], evidenceIds: Set<string>, latestIds: Set<string>, maxNodes: number): DiscussionTopologyMessage[] {
  const byId = new Map(messages.map((message) => [message.id, message]));
  const chosen: DiscussionTopologyMessage[] = [];
  const addWithAncestors = (message: DiscussionTopologyMessage): void => {
    const ancestor = message.parent_message_id == null ? undefined : byId.get(message.parent_message_id);
    if (ancestor) addWithAncestors(ancestor);
    if (!chosen.some((candidate) => candidate.id === message.id) && chosen.length < maxNodes) chosen.push(message);
  };
  const roots = messages.filter((message) => message.parent_message_id == null || !byId.has(message.parent_message_id));
  const priority = [...roots, ...messages.filter((message) => evidenceIds.has(message.message_id) || evidenceIds.has(`mail:${message.message_id}`) || latestIds.has(message.message_id)), ...messages];
  priority.forEach(addWithAncestors);
  return chosen;
}

function SelectedMessage({ message, language, full = false }: { message: DiscussionTopologyMessage; language: MessageLanguage; full?: boolean }) {
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { setCollapsed(false); }, [message.id]);
  const when = message.sent_at ? new Date(message.sent_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "时间未知";
  const body = (language === "zh" ? message.translated_body_text : (message.body_text || message.authored_text))?.trim() || (language === "zh" ? "讨论翻译处理中。" : "No message body is available.");
  const kindLabel = message.provider === "github" ? "GitHub 消息" : "邮件";
  return <div style={{ ...selectedCard, ...(full ? fullSelectedCard : {}) }}><div style={messageHeading}><div style={{ minWidth: 0 }}><div style={{ fontWeight: 700, color: "#101828" }}>{oneSentenceSummary(message, language)}</div><div style={{ color: "#667085", marginTop: 3 }}>{message.subject} · {kindLabel} · {message.author_name || message.author_email || message.platform_user_id || "未知参与者"} · {when}</div></div><button type="button" onClick={() => setCollapsed((value) => !value)} style={collapseButton}>{collapsed ? "展开内容" : "收起内容"}</button></div>{!collapsed && <div style={messageBody}>{body}</div>}{message.public_url && <a href={message.public_url} target="_blank" rel="noreferrer" style={githubLink}>前往 GitHub</a>}</div>;
}

function Legend() { return <div style={legend}><span><i style={{ ...legendDot, background: "#528bff" }} />根邮件</span><span><i style={{ ...legendDot, background: "#12b76a" }} />关键证据</span><span><i style={{ ...legendDot, background: "#f79009" }} />上下文缺失</span></div>; }
function LanguageToggle({ language, onChange }: { language: MessageLanguage; onChange: (language: MessageLanguage) => void }) { return <div style={languageToggle}><button type="button" onClick={() => onChange("zh")} style={{ ...languageButton, ...(language === "zh" ? languageButtonActive : {}) }}>中文</button><button type="button" onClick={() => onChange("en")} style={{ ...languageButton, ...(language === "en" ? languageButtonActive : {}) }}>English</button></div>; }
function collectEvidenceIds(discussion: DiscussionDetail): Set<string> { const result = discussion.structured_result ?? {}; const ids = Array.isArray(result.key_message_ids) ? result.key_message_ids.filter((value): value is string => typeof value === "string") : []; const viewpoints = Array.isArray(result.key_viewpoints) ? result.key_viewpoints : []; for (const viewpoint of viewpoints) if (viewpoint && typeof viewpoint === "object" && Array.isArray((viewpoint as Record<string, unknown>).evidence_message_ids)) for (const id of (viewpoint as Record<string, unknown>).evidence_message_ids as unknown[]) if (typeof id === "string") ids.push(id); return new Set(ids); }
function truncate(value: string, length: number): string { return value.length > length ? `${value.slice(0, length - 1)}…` : value; }
function escapeHtml(value: string): string { return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] ?? character); }

const section: CSSProperties = { marginBottom: 16, padding: 12, border: "1px solid #dfe6ee", borderRadius: 10, background: "#f8fafc" };
const headingRow: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", flexWrap: "wrap", marginBottom: 10 };
const headingControls: CSSProperties = { display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" };
const heading: CSSProperties = { fontSize: 14, fontWeight: 700, color: "#101828" };
const subheading: CSSProperties = { marginTop: 3, fontSize: 11, color: "#667085" };
const legend: CSSProperties = { display: "flex", gap: 8, flexWrap: "wrap", color: "#667085", fontSize: 10, alignItems: "center" };
const legendDot: CSSProperties = { display: "inline-block", width: 7, height: 7, borderRadius: 999, marginRight: 3 };
const languageToggle: CSSProperties = { display: "inline-flex", overflow: "hidden", border: "1px solid #b9d4ff", borderRadius: 6, background: "#fff" };
const languageButton: CSSProperties = { border: 0, padding: "4px 7px", background: "transparent", color: "#667085", fontSize: 10, cursor: "pointer" };
const languageButtonActive: CSSProperties = { background: "#eff6ff", color: "#175cd3", fontWeight: 700 };
const canvasFrame: CSSProperties = { position: "relative", border: "1px solid #d0d5dd", borderRadius: 9, background: "#fff", overflow: "hidden", minHeight: 180 };
const toolbar: CSSProperties = { position: "absolute", top: 8, left: 8, right: 8, zIndex: 1, display: "flex", justifyContent: "space-between", gap: 8, pointerEvents: "none" };
const searchInput: CSSProperties = { width: 176, padding: "6px 8px", border: "1px solid #d0d5dd", borderRadius: 6, color: "#344054", background: "rgba(255,255,255,.94)", fontSize: 11, pointerEvents: "auto" };
const primaryToolButton: CSSProperties = { border: "1px solid #b9d4ff", borderRadius: 6, padding: "6px 8px", background: "#eff6ff", color: "#175cd3", fontSize: 11, fontWeight: 700, cursor: "pointer", pointerEvents: "auto" };
const graphHost: CSSProperties = { width: "100%", height: "100%" };
const foldNotice: CSSProperties = { position: "absolute", right: 10, bottom: 10, padding: "4px 8px", borderRadius: 999, background: "#fffaeb", color: "#b54708", fontSize: 10 };
const selectedCard: CSSProperties = { marginTop: 10, padding: "10px 11px", border: "1px solid #b9d4ff", borderRadius: 8, background: "#f8fbff", fontSize: 12, lineHeight: 1.5 };
const fullSelectedCard: CSSProperties = { marginTop: 10 };
const messageHeading: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 };
const collapseButton: CSSProperties = { flexShrink: 0, border: "1px solid #b9d4ff", borderRadius: 6, padding: "5px 8px", background: "#fff", color: "#175cd3", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const messageBody: CSSProperties = { maxHeight: 300, overflow: "auto", color: "#475467", marginTop: 8, paddingTop: 8, borderTop: "1px solid #dbeafe", whiteSpace: "pre-wrap" };
const githubLink: CSSProperties = { display: "inline-block", marginTop: 8, color: "#175cd3", fontSize: 11, fontWeight: 700 };
const overlay: CSSProperties = { position: "fixed", inset: 0, zIndex: 90, padding: 22, background: "rgba(16,24,40,.56)", display: "flex", alignItems: "center", justifyContent: "center" };
const modal: CSSProperties = { width: "min(1280px, 96vw)", maxHeight: "94vh", overflow: "auto", padding: 14, borderRadius: 12, background: "#fff", boxShadow: "0 24px 56px rgba(16,24,40,.32)" };
const modalHeader: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 10 };
const modalHint: CSSProperties = { display: "block", marginTop: 3, fontSize: 11, color: "#667085" };
const closeButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 7, padding: "7px 11px", background: "#fff", color: "#344054", cursor: "pointer", fontSize: 12, fontWeight: 700 };
