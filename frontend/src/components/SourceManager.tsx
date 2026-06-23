import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { ApiError, createSource, deleteSource, detectSource, fetchSources } from "../api/client";
import type { CrawlSource, SourceCreateRequest, SourceDetectResponse } from "../types";
import { MAIN_CATEGORIES } from "../types";

interface SourceManagerApi {
  fetchSources: typeof fetchSources;
  detectSource: typeof detectSource;
  createSource: typeof createSource;
  deleteSource: typeof deleteSource;
}

interface SourceManagerProps {
  onSourcesChanged?: () => Promise<void> | void;
  api?: SourceManagerApi;
}

const defaultApi: SourceManagerApi = {
  fetchSources,
  detectSource,
  createSource,
  deleteSource,
};

const typeLabels: Record<string, string> = {
  rss: "RSS",
  api: "API",
  page_monitor: "网页",
  search: "搜索",
  agent_crawl: "Agent",
};

const SOURCE_PAGE_SIZE = 5;

export function SourceManager({ onSourcesChanged, api = defaultApi }: SourceManagerProps) {
  const [sources, setSources] = useState<CrawlSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [mainCategory, setMainCategory] = useState<string>(MAIN_CATEGORIES[0]);
  const [detectResult, setDetectResult] = useState<SourceDetectResponse | null>(null);
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
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

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

  function validateForm() {
    if (!url.trim()) {
      setError("请先填写网址");
      return false;
    }
    if (!mainCategory) {
      setError("请选择内容类型");
      return false;
    }
    return true;
  }

  async function handleDetect() {
    if (!validateForm()) return;
    setBusy(true);
    try {
      const result = await api.detectSource(url.trim());
      setDetectResult(result);
      if (!name.trim()) {
        setName(result.name_suggestion);
      }
      setError(null);
    } catch (err) {
      setDetectResult(null);
      setError(err instanceof ApiError ? err.message : "无法识别链接形态");
    } finally {
      setBusy(false);
    }
  }

  async function handleCreate() {
    if (!validateForm()) return;
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
              <div style={{ display: "grid", gridTemplateColumns: "minmax(220px, 2fr) minmax(160px, 1fr) minmax(160px, 1fr)", gap: 12 }}>
                <label style={labelStyle}>
                  <span>网址信息</span>
                  <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/feed.xml" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>来源名称</span>
                  <input value={name} onChange={(event) => setName(event.target.value)} placeholder="可自动拟定" style={inputStyle} />
                </label>
                <label style={labelStyle}>
                  <span>内容类型</span>
                  <select value={mainCategory} onChange={(event) => setMainCategory(event.target.value)} style={inputStyle}>
                    {MAIN_CATEGORIES.map((category) => (
                      <option key={category} value={category}>{category}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                <button type="button" onClick={() => void handleDetect()} disabled={busy} style={buttonStyle("#fff", "#344054")}>
                  {busy ? "识别中" : "识别链接形态"}
                </button>
                <button type="button" onClick={() => void handleCreate()} disabled={busy || !detectResult} style={buttonStyle("#175cd3", "#fff")}>
                  确认添加
                </button>
              </div>
              {detectResult && (
                <div style={{ border: "1px solid #bfd7ff", background: "#eff6ff", color: "#175cd3", borderRadius: 8, padding: 12, fontSize: 13 }}>
                  <div style={{ fontWeight: 700, marginBottom: 6 }}>识别结果：{typeLabels[detectResult.detected_type] ?? detectResult.detected_type}</div>
                  {detectResult.notes.map((note) => <div key={note}>{note}</div>)}
                  {detectResult.api_config && (
                    <details style={{ marginTop: 8 }}>
                      <summary style={{ cursor: "pointer", fontWeight: 700 }}>API 配置预览</summary>
                      <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0 0", fontSize: 12 }}>
                        {JSON.stringify(detectResult.api_config, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          )}

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
                    <button
                      type="button"
                      onClick={() => void handleDelete(source)}
                      disabled={deletingId === source.id}
                      style={buttonStyle("#fff", "#b42318")}
                    >
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
                      <button type="button" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={page === 1} style={pageButtonStyle(page === 1)}>
                        上一页
                      </button>
                      {Array.from({ length: totalPages }, (_, index) => index + 1).map((pageNumber) => (
                        <button
                          key={pageNumber}
                          type="button"
                          onClick={() => setPage(pageNumber)}
                          style={pageNumber === page ? activePageButtonStyle : pageButtonStyle(false)}
                        >
                          {pageNumber}
                        </button>
                      ))}
                      <button type="button" onClick={() => setPage((value) => Math.min(totalPages, value + 1))} disabled={page === totalPages} style={pageButtonStyle(page === totalPages)}>
                        下一页
                      </button>
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
