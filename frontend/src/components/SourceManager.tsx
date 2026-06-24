import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import {
  ApiError,
  createSource,
  deleteSource,
  detectSource,
  detectXhrSources,
  fetchSources,
  selectXhrCandidate,
} from "../api/client";
import type {
  CrawlSource,
  ProbeConfig,
  SourceCreateRequest,
  SourceDetectResponse,
  SourceShape,
  XhrCandidate,
  XhrDetectResponse,
  XhrSelectResponse,
} from "../types";
import { MAIN_CATEGORIES } from "../types";

interface SourceManagerApi {
  fetchSources: typeof fetchSources;
  detectSource: typeof detectSource;
  createSource: typeof createSource;
  deleteSource: typeof deleteSource;
  detectXhrSources: typeof detectXhrSources;
  selectXhrCandidate: typeof selectXhrCandidate;
}

interface SourceManagerProps {
  onSourcesChanged?: () => Promise<void> | void;
  api?: SourceManagerApi;
  collapseSignal?: number;
}

const defaultApi: SourceManagerApi = {
  fetchSources,
  detectSource,
  createSource,
  deleteSource,
  detectXhrSources,
  selectXhrCandidate,
};

const typeLabels: Record<string, string> = {
  rss: "RSS",
  api: "API",
  page_monitor: "网页",
  search: "搜索",
  agent_crawl: "Agent",
};

const SOURCE_TYPES: SourceShape[] = ["rss", "api", "page_monitor", "search"];
const SOURCE_PAGE_SIZE = 5;

type AddMode = "auto" | "advanced" | "xhr";

export function SourceManager({ onSourcesChanged, api = defaultApi, collapseSignal = 0 }: SourceManagerProps) {
  const [sources, setSources] = useState<CrawlSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [addMode, setAddMode] = useState<AddMode>("auto");

  // Shared fields
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [mainCategory, setMainCategory] = useState<string>(MAIN_CATEGORIES[0]);

  // Auto-detect
  const [detectResult, setDetectResult] = useState<SourceDetectResponse | null>(null);

  // Advanced
  const [advType, setAdvType] = useState<string>("rss");
  const [advMethod, setAdvMethod] = useState<string>("GET");
  const [advItemsPath, setAdvItemsPath] = useState("");
  const [advFieldTitle, setAdvFieldTitle] = useState("");
  const [advFieldUrl, setAdvFieldUrl] = useState("");
  const [advFieldUrlTemplate, setAdvFieldUrlTemplate] = useState("");
  const [advFieldDate, setAdvFieldDate] = useState("");
  const [advFieldContent, setAdvFieldContent] = useState("");
  const [advHeaders, setAdvHeaders] = useState("");
  const [advJsonBody, setAdvJsonBody] = useState("");

  // XHR
  const [xhrResult, setXhrResult] = useState<XhrDetectResponse | null>(null);
  const [xhrSelectResult, setXhrSelectResult] = useState<XhrSelectResponse | null>(null);
  const [xhrSelectedCandidate, setXhrSelectedCandidate] = useState<number | null>(null);

  const [busy, setBusy] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sortedSources = useMemo(
    () => [...sources].sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN")),
    [sources],
  );
  const totalPages = Math.max(1, Math.ceil(sortedSources.length / SOURCE_PAGE_SIZE));
  const pageSources = useMemo(
    () => sortedSources.slice((page - 1) * SOURCE_PAGE_SIZE, page * SOURCE_PAGE_SIZE),
    [page, sortedSources],
  );

  useEffect(() => {
    void loadSources();
  }, []);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  useEffect(() => {
    if (collapseSignal > 0) {
      setExpanded(false);
      setOpen(false);
    }
  }, [collapseSignal]);

  async function loadSources() {
    setLoading(true);
    try {
      setSources(await api.fetchSources());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载抓取来源失败");
    } finally {
      setLoading(false);
    }
  }

  function validateUrl(): boolean {
    if (!url.trim()) {
      setError("请先填写网址");
      return false;
    }
    return true;
  }

  // ── Auto detect ──

  async function handleDetect() {
    if (!validateUrl()) return;
    setBusy(true);
    try {
      const result = await api.detectSource(url.trim());
      setDetectResult(result);
      if (!name.trim()) setName(result.name_suggestion);
      setError(null);
    } catch (err) {
      setDetectResult(null);
      setError(err instanceof ApiError ? err.message : "无法识别链接形态");
    } finally {
      setBusy(false);
    }
  }

  async function handleAutoCreate() {
    if (!validateUrl()) return;
    setBusy(true);
    try {
      const request: SourceCreateRequest = {
        url: url.trim(),
        name: name.trim() || null,
        main_category: mainCategory,
      };
      await api.createSource(request);
      resetForm();
      await loadSources();
      await onSourcesChanged?.();
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "创建抓取来源失败");
    } finally {
      setBusy(false);
    }
  }

  // ── Advanced create ──

  async function handleAdvancedCreate() {
    if (!validateUrl()) return;
    if (!mainCategory) {
      setError("请选择内容类型");
      return;
    }
    setBusy(true);
    try {
      let apiConfig: Record<string, unknown> | null = null;
      if (advType === "api") {
        const probe: Record<string, unknown> = {
          mode: "json_list",
          method: advMethod,
          items_path: advItemsPath.trim() || null,
          fields: {
            title: advFieldTitle.trim() || null,
            url: advFieldUrl.trim() || null,
            url_template: advFieldUrlTemplate.trim() || null,
            published_at: advFieldDate.trim() || null,
            content: advFieldContent.trim() || null,
          },
        };
        if (advHeaders.trim()) {
          try { probe.headers = JSON.parse(advHeaders); } catch { /* ignore */ }
        }
        if (advJsonBody.trim() && advMethod === "POST") {
          try { probe.json_body = JSON.parse(advJsonBody); } catch { /* ignore */ }
        }
        apiConfig = { probe };
      }
      const request: SourceCreateRequest = {
        url: url.trim(),
        name: name.trim() || null,
        main_category: mainCategory,
        type: advType,
        api_config: apiConfig,
      };
      await api.createSource(request);
      resetForm();
      await loadSources();
      await onSourcesChanged?.();
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "创建抓取来源失败");
    } finally {
      setBusy(false);
    }
  }

  // ── XHR detect ──

  async function handleXhrDetect() {
    if (!validateUrl()) return;
    setBusy(true);
    setXhrResult(null);
    setXhrSelectResult(null);
    setXhrSelectedCandidate(null);
    try {
      const result = await api.detectXhrSources(url.trim());
      setXhrResult(result);
      if (result.candidates.length === 0) {
        setError("未探测到 JSON API 候选");
      } else {
        setError(null);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "XHR 探测失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleXhrSelect() {
    if (!xhrResult || xhrResult.candidates.length === 0) return;
    setBusy(true);
    setXhrSelectResult(null);
    try {
      const result = await api.selectXhrCandidate(xhrResult.page_url, xhrResult.candidates);
      setXhrSelectResult(result);
      if (result.selected_index !== null) {
        setXhrSelectedCandidate(result.selected_index);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Agent 选择失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleXhrCreate() {
    if (!xhrSelectResult?.api_config || !validateUrl()) return;
    setBusy(true);
    try {
      const probe = xhrSelectResult.api_config;
      const apiConfig = { probe };
      const request: SourceCreateRequest = {
        url: (probe as ProbeConfig).url || url.trim(),
        name: name.trim() || null,
        main_category: mainCategory,
        type: "api",
        api_config: apiConfig,
      };
      await api.createSource(request);
      resetForm();
      await loadSources();
      await onSourcesChanged?.();
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "创建抓取来源失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(source: CrawlSource) {
    const confirmed = window.confirm(
      `确认删除「${source.name}」？如果这是 seed 来源，数据库非空时不会自动恢复，需要重新添加或重新初始化。`,
    );
    if (!confirmed) return;
    setDeletingId(source.id);
    try {
      await api.deleteSource(source.id);
      await loadSources();
      await onSourcesChanged?.();
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "删除抓取来源失败");
    } finally {
      setDeletingId(null);
    }
  }

  function resetForm() {
    setOpen(false);
    setUrl("");
    setName("");
    setMainCategory(MAIN_CATEGORIES[0]);
    setDetectResult(null);
    setAdvType("rss");
    setAdvMethod("GET");
    setAdvItemsPath("");
    setAdvFieldTitle("");
    setAdvFieldUrl("");
    setAdvFieldUrlTemplate("");
    setAdvFieldDate("");
    setAdvFieldContent("");
    setAdvHeaders("");
    setAdvJsonBody("");
    setXhrResult(null);
    setXhrSelectResult(null);
    setXhrSelectedCandidate(null);
    setAddMode("auto");
  }

  return (
    <section
      style={{
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        padding: 14,
        background: "linear-gradient(135deg, #f8fafc 0%, #eef2f6 100%)",
        display: "grid",
        gap: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取来源</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 4 }}>管理 RSS、API 和网页监控来源，Agent 候选会自动同步。</div>
        </div>
        <button type="button" onClick={() => setExpanded((value) => !value)} style={sectionToggleStyle}>
          {expanded ? "收起" : "展开"}
        </button>
      </div>

      {expanded && (
        <>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div style={{ fontSize: 12, color: "#667085" }}>
              {loading ? "正在加载来源…" : `共 ${sortedSources.length} 条来源`}
            </div>
            <button type="button" onClick={() => setOpen((value) => !value)} style={buttonStyle("#175cd3", "#fff")}>
              {open ? "收起添加" : "添加来源"}
            </button>
          </div>

          {error && (
            <div style={{ border: "1px solid #fecdca", background: "#fef3f2", color: "#b42318", borderRadius: 8, padding: 10, fontSize: 13 }}>
              {error}
            </div>
          )}

          {open && (
            <div style={{ display: "grid", gap: 12, borderTop: "1px solid #eaecf0", paddingTop: 12 }}>
              {/* Mode selector */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {([
                  { key: "auto", label: "自动识别" },
                  { key: "xhr", label: "智能探测" },
                  { key: "advanced", label: "高级添加" },
                ] as { key: AddMode; label: string }[]).map((m) => (
                  <button
                    key={m.key}
                    type="button"
                    onClick={() => { setAddMode(m.key); setError(null); }}
                    style={addMode === m.key ? activeTabStyle : tabStyle}
                  >
                    {m.label}
                  </button>
                ))}
              </div>

              {/* Shared fields */}
              <div style={{ display: "grid", gridTemplateColumns: "minmax(220px, 2fr) minmax(160px, 1fr) minmax(160px, 1fr)", gap: 12 }}>
                <label style={labelStyle}>
                  <span>网址</span>
                  <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/feed.xml" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>来源名称</span>
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="可自动拟定" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>内容类型</span>
                  <select value={mainCategory} onChange={(e) => setMainCategory(e.target.value)} style={inputStyle}>
                    {MAIN_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </label>
              </div>

              {/* Auto mode */}
              {addMode === "auto" && (
                <>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                    <button type="button" onClick={() => void handleDetect()} disabled={busy} style={buttonStyle("#fff", "#344054")}>
                      {busy ? "识别中" : "识别链接形态"}
                    </button>
                    <button type="button" onClick={() => void handleAutoCreate()} disabled={busy || !detectResult} style={buttonStyle("#175cd3", "#fff")}>
                      确认添加
                    </button>
                  </div>
                  {detectResult && (
                    <div style={infoBoxStyle("#bfd7ff", "#eff6ff", "#175cd3")}>
                      <div style={{ fontWeight: 700, marginBottom: 6 }}>识别结果：{typeLabels[detectResult.detected_type] ?? detectResult.detected_type}</div>
                      {detectResult.notes.map((note) => <div key={note}>{note}</div>)}
                      {detectResult.api_config && (
                        <details style={{ marginTop: 8 }}>
                          <summary style={{ cursor: "pointer", fontWeight: 700 }}>API 配置预览</summary>
                          <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0 0", fontSize: 12 }}>{JSON.stringify(detectResult.api_config, null, 2)}</pre>
                        </details>
                      )}
                    </div>
                  )}
                </>
              )}

              {/* Advanced mode */}
              {addMode === "advanced" && (
                <>
                  <div style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr)", gap: 12 }}>
                    <label style={labelStyle}>
                      <span>来源类型</span>
                      <select value={advType} onChange={(e) => setAdvType(e.target.value)} style={inputStyle}>
                        {SOURCE_TYPES.map((t) => <option key={t} value={t}>{typeLabels[t]}</option>)}
                      </select>
                    </label>
                  </div>

                  {advType === "api" && (
                    <div style={{ display: "grid", gap: 10, border: "1px solid #eaecf0", borderRadius: 8, padding: 12, background: "#fff" }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: "#344054" }}>API 配置</div>

                      <div style={{ display: "grid", gridTemplateColumns: "minmax(100px, 1fr) minmax(200px, 2fr)", gap: 12 }}>
                        <label style={labelStyle}>
                          <span>请求方法</span>
                          <select value={advMethod} onChange={(e) => setAdvMethod(e.target.value)} style={inputStyle}>
                            <option value="GET">GET</option>
                            <option value="POST">POST</option>
                          </select>
                        </label>
                        <label style={labelStyle}>
                          <span>列表路径 (items_path)</span>
                          <input value={advItemsPath} onChange={(e) => setAdvItemsPath(e.target.value)} placeholder="如 obj.records，根数组留空" style={inputStyle} />
                        </label>
                      </div>

                      <div style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(120px, 1fr)", gap: 12 }}>
                        <label style={labelStyle}>
                          <span>标题字段名</span>
                          <input value={advFieldTitle} onChange={(e) => setAdvFieldTitle(e.target.value)} placeholder="title" style={inputStyle} />
                        </label>
                        <label style={labelStyle}>
                          <span>日期字段名</span>
                          <input value={advFieldDate} onChange={(e) => setAdvFieldDate(e.target.value)} placeholder="date" style={inputStyle} />
                        </label>
                      </div>

                      <div style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(200px, 2fr)", gap: 12 }}>
                        <label style={labelStyle}>
                          <span>链接字段名</span>
                          <input value={advFieldUrl} onChange={(e) => setAdvFieldUrl(e.target.value)} placeholder="url，无则留空" style={inputStyle} />
                        </label>
                        <label style={labelStyle}>
                          <span>链接模板 (url_template)</span>
                          <input value={advFieldUrlTemplate} onChange={(e) => setAdvFieldUrlTemplate(e.target.value)} placeholder="https://site.com/{item.path}" style={inputStyle} />
                        </label>
                      </div>

                      <label style={labelStyle}>
                        <span>内容字段名</span>
                        <input value={advFieldContent} onChange={(e) => setAdvFieldContent(e.target.value)} placeholder="summary 或 summary,content" style={inputStyle} />
                      </label>

                      {advMethod === "POST" && (
                        <label style={labelStyle}>
                          <span>请求体 JSON (可选)</span>
                          <textarea value={advJsonBody} onChange={(e) => setAdvJsonBody(e.target.value)} placeholder='{"page": 1, "pageSize": 20}' style={{ ...inputStyle, minHeight: 60, fontFamily: "monospace", fontSize: 12 }} />
                        </label>
                      )}

                      <details>
                        <summary style={{ cursor: "pointer", fontSize: 12, color: "#667085" }}>自定义请求头 (可选)</summary>
                        <textarea value={advHeaders} onChange={(e) => setAdvHeaders(e.target.value)} placeholder='{"Authorization": "Bearer xxx"}' style={{ ...inputStyle, minHeight: 50, fontFamily: "monospace", fontSize: 12, marginTop: 6 }} />
                      </details>

                      {/* 配置预览 */}
                      {advFieldTitle.trim() && (
                        <details>
                          <summary style={{ cursor: "pointer", fontSize: 12, color: "#667085" }}>生成配置预览</summary>
                          <pre style={{ whiteSpace: "pre-wrap", margin: "6px 0 0", fontSize: 11, color: "#475467", background: "#f9fafb", padding: 8, borderRadius: 6 }}>
{JSON.stringify({
  probe: {
    mode: "json_list",
    method: advMethod,
    items_path: advItemsPath.trim() || null,
    fields: {
      title: advFieldTitle.trim() || null,
      url: advFieldUrl.trim() || null,
      url_template: advFieldUrlTemplate.trim() || null,
      published_at: advFieldDate.trim() || null,
      content: advFieldContent.trim() || null,
    }
  }
}, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  )}

                  <div style={{ display: "flex", gap: 10 }}>
                    <button type="button" onClick={() => void handleAdvancedCreate()} disabled={busy} style={buttonStyle("#175cd3", "#fff")}>
                      {busy ? "创建中" : "确认添加"}
                    </button>
                  </div>
                </>
              )}

              {/* XHR mode */}
              {addMode === "xhr" && (
                <>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                    <button type="button" onClick={() => void handleXhrDetect()} disabled={busy} style={buttonStyle("#fff", "#344054")}>
                      {busy ? "探测中…" : "探测 XHR/Fetch"}
                    </button>
                    {xhrResult && xhrResult.candidates.length > 0 && (
                      <button type="button" onClick={() => void handleXhrSelect()} disabled={busy} style={buttonStyle("#175cd3", "#fff")}>
                        {busy ? "Agent 分析中…" : "Agent 选择最佳 API"}
                      </button>
                    )}
                  </div>

                  {/* Candidates list */}
                  {xhrResult && xhrResult.candidates.length > 0 && (
                    <div style={{ display: "grid", gap: 8 }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: "#344054" }}>探测到 {xhrResult.candidates.length} 个 JSON API 候选：</div>
                      {xhrResult.candidates.map((c, i) => (
                        <div
                          key={i}
                          style={{
                            border: xhrSelectedCandidate === i ? "2px solid #175cd3" : "1px solid #eaecf0",
                            borderRadius: 8,
                            padding: 10,
                            background: "#fff",
                            cursor: "pointer",
                          }}
                          onClick={() => setXhrSelectedCandidate(i)}
                        >
                          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                            <span style={{ ...badgeStyle, background: c.score >= 60 ? "#d1fadf" : c.score >= 30 ? "#fef0c7" : "#f2f4f7", color: c.score >= 60 ? "#037947" : c.score >= 30 ? "#b54708" : "#667085" }}>
                              评分 {c.score}
                            </span>
                            <span style={{ ...badgeStyle, background: "#eff6ff", color: "#175cd3" }}>{c.method}</span>
                            <span style={{ fontSize: 12, color: "#475467", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 400 }}>
                              {c.url}
                            </span>
                          </div>
                          <div style={{ fontSize: 12, color: "#667085", marginTop: 6 }}>
                            items_path: {c.inferred_items_path || "(根)"} | 字段: {[
                              c.inferred_fields.title && "title",
                              c.inferred_fields.url && "url",
                              c.inferred_fields.published_at && "date",
                              c.inferred_fields.content && "content",
                            ].filter(Boolean).join(", ") || "未识别"}
                          </div>
                          {c.notes.length > 0 && (
                            <details style={{ marginTop: 4 }}>
                              <summary style={{ cursor: "pointer", fontSize: 12, color: "#667085" }}>打分详情</summary>
                              <ul style={{ margin: "4px 0 0 16px", fontSize: 12, color: "#667085" }}>
                                {c.notes.map((note, ni) => <li key={ni}>{note}</li>)}
                              </ul>
                            </details>
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Agent result */}
                  {xhrSelectResult && (
                    <div style={infoBoxStyle(
                      xhrSelectResult.selected_index !== null ? "#d1fadf" : "#fef0c7",
                      xhrSelectResult.selected_index !== null ? "#f0fdf4" : "#fffcf5",
                      xhrSelectResult.selected_index !== null ? "#037947" : "#b54708",
                    )}>
                      <div style={{ fontWeight: 700, marginBottom: 6 }}>
                        Agent 推荐结果（置信度：{xhrSelectResult.confidence}）
                      </div>
                      <div style={{ marginBottom: 8 }}>{xhrSelectResult.reason}</div>
                      {xhrSelectResult.api_config && (
                        <>
                          <div style={{ fontSize: 12, color: "#667085", marginBottom: 4 }}>推荐 API 配置预览：</div>
                          <pre style={{ whiteSpace: "pre-wrap", margin: "0 0 8px", fontSize: 12, background: "#fff", padding: 8, borderRadius: 6, border: "1px solid #eaecf0" }}>
                            {JSON.stringify(xhrSelectResult.api_config, null, 2)}
                          </pre>
                          <button type="button" onClick={() => void handleXhrCreate()} disabled={busy} style={buttonStyle("#175cd3", "#fff")}>
                            {busy ? "创建中" : "确认创建来源"}
                          </button>
                        </>
                      )}
                      {xhrSelectResult.rejected_candidates.length > 0 && (
                        <details style={{ marginTop: 8 }}>
                          <summary style={{ cursor: "pointer", fontWeight: 700, fontSize: 12 }}>已排除候选 ({xhrSelectResult.rejected_candidates.length})</summary>
                          <ul style={{ margin: "4px 0 0 16px", fontSize: 12 }}>
                            {xhrSelectResult.rejected_candidates.map((r, i) => (
                              <li key={i}>#{r.index}: {r.reason}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Source list */}
          <div style={{ display: "grid", gap: 8 }}>
            {loading ? (
              <div style={{ color: "#667085", fontSize: 13 }}>正在加载来源…</div>
            ) : sortedSources.length === 0 ? (
              <div style={{ color: "#667085", fontSize: 13 }}>暂无抓取来源</div>
            ) : (
              <>
                {pageSources.map((source) => (
                  <div
                    key={source.id}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "minmax(140px, 1fr) minmax(220px, 2fr) 90px 120px 76px",
                      gap: 10,
                      alignItems: "center",
                      border: "1px solid #eaecf0",
                      borderRadius: 8,
                      padding: "10px 12px",
                      background: "#fff",
                    }}
                  >
                    <div style={{ fontWeight: 700, color: "#101828", minWidth: 0, overflowWrap: "anywhere" }}>{source.name}</div>
                    <div style={{ color: "#475467", fontSize: 12, minWidth: 0, overflowWrap: "anywhere" }}>{source.url}</div>
                    <div style={{ color: "#344054", fontSize: 12 }}>{typeLabels[source.type] ?? source.type}</div>
                    <div style={{ color: "#344054", fontSize: 12 }}>{source.main_category ?? "未分类"}</div>
                    <button type="button" onClick={() => void handleDelete(source)} disabled={deletingId === source.id} style={buttonStyle("#fff", "#b42318")}>
                      {deletingId === source.id ? "删除中" : "删除"}
                    </button>
                  </div>
                ))}
                {totalPages > 1 && (
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                    <div style={{ fontSize: 12, color: "#667085" }}>
                      第 {page} / {totalPages} 页，每页 {SOURCE_PAGE_SIZE} 条，共 {sortedSources.length} 条
                    </div>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <button type="button" onClick={() => setPage((v) => Math.max(1, v - 1))} disabled={page === 1} style={pageButtonStyle(page === 1)}>上一页</button>
                      {Array.from({ length: totalPages }, (_, i) => i + 1).map((pn) => (
                        <button key={pn} type="button" onClick={() => setPage(pn)} style={pn === page ? activePageButtonStyle : pageButtonStyle(false)}>{pn}</button>
                      ))}
                      <button type="button" onClick={() => setPage((v) => Math.min(totalPages, v + 1))} disabled={page === totalPages} style={pageButtonStyle(page === totalPages)}>下一页</button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function infoBoxStyle(border: string, bg: string, color: string): CSSProperties {
  return { border: `1px solid ${border}`, background: bg, color, borderRadius: 8, padding: 12, fontSize: 13 };
}

const labelStyle = {
  display: "grid",
  gap: 6,
  color: "#475467",
  fontSize: 13,
} satisfies CSSProperties;

const inputStyle = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "10px 12px",
  fontSize: 14,
  color: "#101828",
  background: "#fff",
  minWidth: 0,
} satisfies CSSProperties;

const badgeStyle: CSSProperties = {
  padding: "2px 8px",
  borderRadius: 6,
  fontSize: 12,
  fontWeight: 700,
  whiteSpace: "nowrap",
};

const tabStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "6px 12px",
  background: "#fff",
  color: "#344054",
  fontSize: 13,
  fontWeight: 600,
  cursor: "pointer",
};

const activeTabStyle: CSSProperties = {
  border: "1px solid #175cd3",
  borderRadius: 8,
  padding: "6px 12px",
  background: "#175cd3",
  color: "#fff",
  fontSize: 13,
  fontWeight: 600,
  cursor: "pointer",
};

function buttonStyle(background: string, color: string): CSSProperties {
  return {
    border: background === "#fff" ? "1px solid #d0d5dd" : "none",
    borderRadius: 999,
    padding: "9px 13px",
    background,
    color,
    fontSize: 13,
    fontWeight: 700,
    cursor: "pointer",
  };
}

function pageButtonStyle(disabled: boolean): CSSProperties {
  return {
    border: "1px solid #d0d5dd",
    borderRadius: 8,
    padding: "6px 10px",
    background: "#fff",
    color: disabled ? "#98a2b3" : "#344054",
    fontSize: 12,
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
  };
}

const activePageButtonStyle = {
  border: "1px solid #175cd3",
  borderRadius: 8,
  padding: "6px 10px",
  background: "#175cd3",
  color: "#fff",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
} satisfies CSSProperties;

const sectionToggleStyle = {
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  padding: "8px 12px",
  minWidth: 72,
  background: "linear-gradient(135deg, #ffffff 0%, #f2f4f7 100%)",
  color: "#344054",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
} satisfies CSSProperties;
