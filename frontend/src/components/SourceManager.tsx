import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  ApiError,
  createSource,
  createSourceFromProbe,
  deleteSource,
  discoverSource,
  fetchSources,
} from "../api/client";
import type {
  CrawlSource,
  DiscoverResponse,
  SourceCreateRequest,
  SourceShape,
} from "../types";
import { MAIN_CATEGORIES } from "../types";
import { clampInput, INPUT_LIMITS } from "../inputLimits";

interface SourceManagerApi {
  fetchSources: typeof fetchSources;
  createSource: typeof createSource;
  deleteSource: typeof deleteSource;
  discoverSource: typeof discoverSource;
  createSourceFromProbe: typeof createSourceFromProbe;
}

interface SourceManagerProps {
  onSourcesChanged?: () => Promise<void> | void;
  api?: SourceManagerApi;
  collapseSignal?: number;
}

const defaultApi: SourceManagerApi = {
  fetchSources,
  createSource,
  deleteSource,
  discoverSource,
  createSourceFromProbe,
};

const typeLabels: Record<string, string> = {
  rss: "RSS",
  api: "API",
  page_monitor: "新闻列表页",
  search: "搜索",
  agent_crawl: "Agent",
};

const SOURCE_TYPES: SourceShape[] = ["rss", "api", "page_monitor", "search"];
const SOURCE_PAGE_SIZE = 5;

type AddMode = "advanced" | "discover";

export function SourceManager({ onSourcesChanged, api = defaultApi, collapseSignal = 0 }: SourceManagerProps) {
  const [sources, setSources] = useState<CrawlSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [addMode, setAddMode] = useState<AddMode>("discover");

  // Shared fields
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [mainCategory, setMainCategory] = useState<string>(MAIN_CATEGORIES[0]);

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

  // 智能探测（Agent 链接发现）
  const [discoverResult, setDiscoverResult] = useState<DiscoverResponse | null>(null);
  const [discoverAutoCreate, setDiscoverAutoCreate] = useState(false);

  const [busy, setBusy] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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

  async function loadSources(): Promise<CrawlSource[]> {
    setLoading(true);
    try {
      const list = await api.fetchSources();
      setSources(list);
      setError(null);
      return list;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "加载抓取来源失败");
      return [];
    } finally {
      setLoading(false);
    }
  }

  function focusSourceInList(list: CrawlSource[], sourceId: number) {
    const sorted = [...list].sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
    const index = sorted.findIndex((s) => s.id === sourceId);
    if (index >= 0) setPage(Math.floor(index / SOURCE_PAGE_SIZE) + 1);
  }

  function validateUrl(): boolean {
    if (!url.trim()) {
      setError("请先填写网址");
      return false;
    }
    return true;
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

  // ── 智能探测（API 发现）──

  async function handleDiscover() {
    if (!validateUrl()) return;
    setBusy(true);
    setNotice(null);
    setDiscoverResult(null);
    try {
      const result = await api.discoverSource({
        url: url.trim(),
        create_source: discoverAutoCreate,
        name: name.trim() || null,
        main_category: mainCategory,
      });
      setDiscoverResult(result);
      if (result.name_suggestion && !name.trim()) {
        setName(result.name_suggestion);
      }
      if (result.created_source) {
        const created = result.created_source;
        const list = await loadSources();
        focusSourceInList(list, created.id);
        await onSourcesChanged?.();
        setError(null);
        setNotice(`已添加到抓取来源列表：「${created.name}」（API，#${created.id}），到「Agent 运行」点「一键 Agent 运行」即可抓取`);
      } else if (!result.success) {
        setError("未发现可用 API，请查看候选请求排查");
      } else {
        setError(null);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "智能探测失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateFromProbe() {
    if (!discoverResult || !discoverResult.api_url) return;
    setBusy(true);
    try {
      const created = await api.createSourceFromProbe({
        api_url: discoverResult.api_url,
        method: discoverResult.method,
        items_path: discoverResult.items_path ?? "",
        fields: discoverResult.fields,
        name: name.trim() || discoverResult.name_suggestion || null,
        main_category: mainCategory,
      });
      resetForm();
      const list = await loadSources();
      focusSourceInList(list, created.id);
      await onSourcesChanged?.();
      setError(null);
      setNotice(`已添加到抓取来源列表：「${created.name}」（API，#${created.id}），到「Agent 运行」点「一键 Agent 运行」即可抓取`);
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
    setDiscoverResult(null);
    setDiscoverAutoCreate(false);
    setAddMode("discover");
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
          <div style={{ fontSize: 12, color: "#667085", marginTop: 4 }}>管理 RSS、API 和新闻列表页来源，Agent 候选会自动同步。</div>
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

          {notice && (
            <div style={{ border: "1px solid #a6f4c5", background: "#ecfdf3", color: "#027a48", borderRadius: 8, padding: 10, fontSize: 13, display: "flex", justifyContent: "space-between", gap: 10 }}>
              <span>{notice}</span>
              <button type="button" onClick={() => setNotice(null)} style={{ border: "none", background: "transparent", color: "#027a48", cursor: "pointer", fontWeight: 700 }}>×</button>
            </div>
          )}

          {open && (
            <div style={{ display: "grid", gap: 12, borderTop: "1px solid #eaecf0", paddingTop: 12 }}>
              {/* Mode selector */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {([
                  { key: "discover", label: "智能探测" },
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
                  <input value={url} maxLength={INPUT_LIMITS.url} onChange={(e) => setUrl(clampInput(e.target.value, INPUT_LIMITS.url))} placeholder="https://example.com/feed.xml" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>来源名称</span>
                  <input value={name} maxLength={INPUT_LIMITS.displayName} onChange={(e) => setName(clampInput(e.target.value, INPUT_LIMITS.displayName))} placeholder="可自动拟定" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>内容类型</span>
                  <select value={mainCategory} onChange={(e) => setMainCategory(e.target.value)} style={inputStyle}>
                    {MAIN_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </label>
              </div>

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
                          <input value={advItemsPath} maxLength={INPUT_LIMITS.pathExpr} onChange={(e) => setAdvItemsPath(clampInput(e.target.value, INPUT_LIMITS.pathExpr))} placeholder="如 obj.records，根数组留空" style={inputStyle} />
                        </label>
                      </div>

                      <div style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(120px, 1fr)", gap: 12 }}>
                        <label style={labelStyle}>
                          <span>标题字段名</span>
                          <input value={advFieldTitle} maxLength={INPUT_LIMITS.pathExpr} onChange={(e) => setAdvFieldTitle(clampInput(e.target.value, INPUT_LIMITS.pathExpr))} placeholder="title" style={inputStyle} />
                        </label>
                        <label style={labelStyle}>
                          <span>日期字段名</span>
                          <input value={advFieldDate} maxLength={INPUT_LIMITS.pathExpr} onChange={(e) => setAdvFieldDate(clampInput(e.target.value, INPUT_LIMITS.pathExpr))} placeholder="date" style={inputStyle} />
                        </label>
                      </div>

                      <div style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(200px, 2fr)", gap: 12 }}>
                        <label style={labelStyle}>
                          <span>链接字段名</span>
                          <input value={advFieldUrl} maxLength={INPUT_LIMITS.pathExpr} onChange={(e) => setAdvFieldUrl(clampInput(e.target.value, INPUT_LIMITS.pathExpr))} placeholder="url，无则留空" style={inputStyle} />
                        </label>
                        <label style={labelStyle}>
                          <span>链接模板 (url_template)</span>
                          <input value={advFieldUrlTemplate} maxLength={INPUT_LIMITS.url} onChange={(e) => setAdvFieldUrlTemplate(clampInput(e.target.value, INPUT_LIMITS.url))} placeholder="https://site.com/{item.path}" style={inputStyle} />
                        </label>
                      </div>

                      <label style={labelStyle}>
                        <span>内容字段名</span>
                        <input value={advFieldContent} maxLength={INPUT_LIMITS.pathExpr} onChange={(e) => setAdvFieldContent(clampInput(e.target.value, INPUT_LIMITS.pathExpr))} placeholder="summary 或 summary,content" style={inputStyle} />
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

                  {advType === "page_monitor" && (
                    <div style={{ border: "1px solid #d1e9ff", background: "#f0f9ff", color: "#175cd3", borderRadius: 8, padding: 10, fontSize: 13 }}>
                      系统会自动识别文章链接、标题和发布日期；适合新闻/博客列表页。若规则识别不完整，后端会调用 LLM 兜底判断。
                    </div>
                  )}

                  <div style={{ display: "flex", gap: 10 }}>
                    <button type="button" onClick={() => void handleAdvancedCreate()} disabled={busy} style={buttonStyle("#175cd3", "#fff")}>
                      {busy ? "创建中" : "确认添加"}
                    </button>
                  </div>
                </>
              )}

              {/* 智能探测（API 发现）mode */}
              {addMode === "discover" && (
                <>
                  <div style={{ fontSize: 12, color: "#667085" }}>
                    Agent 会用浏览器渲染该链接，监听页面发出的 JSON XHR/Fetch 请求，识别文章列表 API，
                    自动映射 title/url/date 字段并生成 probe 配置，再用 probe 自检确认能取到有效条目。
                    请填写「网址」为内容列表页（如 https://openanolis.cn/blog）。整个过程可能需要 10–30 秒。
                  </div>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
                    <button type="button" onClick={() => void handleDiscover()} disabled={busy} style={buttonStyle("#175cd3", "#fff")}>
                      {busy ? "探测中…" : "开始智能探测"}
                    </button>
                    <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, color: "#344054" }}>
                      <input type="checkbox" checked={discoverAutoCreate} onChange={(e) => setDiscoverAutoCreate(e.target.checked)} />
                      探测通过后自动创建为标准 API 来源
                    </label>
                  </div>

                  {discoverResult && (
                    <div style={{ display: "grid", gap: 10 }}>
                      {/* 探测摘要 */}
                      <div style={infoBoxStyle(
                        discoverResult.success ? "#d1fadf" : "#fef0c7",
                        discoverResult.success ? "#f0fdf4" : "#fffcf5",
                        discoverResult.success ? "#037947" : "#b54708",
                      )}>
                        <div style={{ fontWeight: 700, marginBottom: 6 }}>
                          {discoverResult.success
                            ? `发现 API 端点（自检通过，有效条目 ${discoverResult.real_content_count} 条）`
                            : "未发现可用 API"}
                        </div>
                        {discoverResult.success && discoverResult.api_url && (
                          <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                            <div>API 端点：<code style={{ overflowWrap: "anywhere" }}>{discoverResult.api_url}</code></div>
                            <div>请求方法：{discoverResult.method}</div>
                            <div>列表路径：<code>{discoverResult.items_path || "（根数组）"}</code></div>
                            <div>字段映射：</div>
                            <ul style={{ margin: "2px 0 0 16px", padding: 0 }}>
                              <li>标题：{String(discoverResult.fields.title ?? "—")}</li>
                              <li>链接：{String(discoverResult.fields.url ?? "—")}{discoverResult.fields.url_template ? `（模板：${String(discoverResult.fields.url_template)}）` : ""}</li>
                              <li>日期：{String(discoverResult.fields.published_at ?? "—")}</li>
                              <li>正文：{String(discoverResult.fields.content ?? "—")}</li>
                            </ul>
                          </div>
                        )}
                        {discoverResult.created_source ? (
                          <div style={{ marginTop: 8, fontWeight: 700 }}>
                            已创建标准 API 来源：{discoverResult.created_source.name}（#{discoverResult.created_source.id}）
                          </div>
                        ) : (
                          discoverResult.success && (
                            <div style={{ marginTop: 10 }}>
                              <button
                                type="button"
                                onClick={() => void handleCreateFromProbe()}
                                disabled={busy}
                                style={buttonStyle("#039855", "#fff")}
                              >
                                {busy ? "创建中…" : "创建为标准 API 来源"}
                              </button>
                            </div>
                          )
                        )}
                        {discoverResult.notes.length > 0 && (
                          <details style={{ marginTop: 8 }}>
                            <summary style={{ cursor: "pointer", fontSize: 12 }}>探测备注</summary>
                            <ul style={{ margin: "4px 0 0 16px", fontSize: 12 }}>
                              {discoverResult.notes.map((n, i) => <li key={i}>{n}</li>)}
                            </ul>
                          </details>
                        )}
                        {!discoverResult.success && discoverResult.candidates.length > 0 && (
                          <details style={{ marginTop: 8 }}>
                            <summary style={{ cursor: "pointer", fontSize: 12 }}>抓到的候选请求（{discoverResult.candidates.length}）</summary>
                            <ul style={{ margin: "4px 0 0 16px", fontSize: 12 }}>
                              {discoverResult.candidates.map((c, i) => (
                                <li key={i}>
                                  <code>{c.method}</code> {c.api_url} → {c.items_count} 条（path: {c.items_path || "根"}，score: {c.score}）
                                </li>
                              ))}
                            </ul>
                          </details>
                        )}
                      </div>

                      {/* 样例条目 */}
                      {discoverResult.sample_items.length > 0 && (
                        <div style={{ display: "grid", gap: 8 }}>
                          <div style={{ fontSize: 13, fontWeight: 700, color: "#344054" }}>样例条目</div>
                          {discoverResult.sample_items.map((s, i) => (
                            <div key={i} style={{ border: "1px solid #eaecf0", borderRadius: 8, padding: 10, background: "#fff" }}>
                              <div style={{ fontSize: 13, fontWeight: 700, color: "#101828" }}>{s.title}</div>
                              {s.published_at && <div style={{ fontSize: 12, color: "#667085" }}>发布时间：{s.published_at}</div>}
                              <div style={{ fontSize: 12, color: "#175cd3", marginTop: 4, overflowWrap: "anywhere" }}>{s.url}</div>
                              {s.content_preview && <div style={{ fontSize: 12, color: "#475467", marginTop: 4 }}>{s.content_preview}…</div>}
                            </div>
                          ))}
                        </div>
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
