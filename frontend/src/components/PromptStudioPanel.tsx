import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  activatePromptSet,
  createPromptSet,
  deactivatePromptSet,
  deletePromptSet,
  fetchPromptSets,
  fetchPromptStages,
  updatePromptSet,
  type PromptSet,
  type PromptStage,
} from "../discovery/promptApi";

type Draft = { name: string; prompts: Record<string, string> };
type Selection = { kind: "new" } | { kind: "existing"; id: number } | null;

const MONO = "var(--font-mono, 'JetBrains Mono', ui-monospace, monospace)";

const SECTION: React.CSSProperties = {
  background: "#fff",
  border: "1px solid #d0d5dd",
  borderRadius: 10,
  padding: 16,
  marginTop: 14,
};
const STORAGE_KEY = "discovery.prompt-studio-panel.expanded";

function readExpandedState() {
  if (typeof window === "undefined") return true;
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw == null ? true : raw === "true";
}

function buildDefaultDraft(stages: PromptStage[]): Draft {
  const prompts: Record<string, string> = {};
  for (const s of stages) prompts[s.key] = s.default_template;
  return { name: "新建 Prompt 套餐", prompts };
}

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : error instanceof Error ? error.message : "操作失败";
}

export function PromptStudioPanel() {
  const queryClient = useQueryClient();
  const [selection, setSelection] = useState<Selection>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [panelExpanded, setPanelExpanded] = useState(readExpandedState);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY, String(panelExpanded));
  }, [panelExpanded]);

  const stagesQuery = useQuery({ queryKey: ["prompt-stages"], queryFn: fetchPromptStages, retry: false });
  const setsQuery = useQuery({ queryKey: ["prompt-sets"], queryFn: fetchPromptSets, retry: false });

  const stages = useMemo(() => stagesQuery.data ?? [], [stagesQuery.data]);
  const sets = setsQuery.data ?? [];
  const activeSet = sets.find((s) => s.is_active) ?? null;

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["prompt-sets"] });
  };

  function openNew() {
    if (stages.length === 0) return;
    setSelection({ kind: "new" });
    setDraft(buildDefaultDraft(stages));
    setExpanded(stages[0]?.key ?? null);
    setStatusMessage(null);
  }

  function openExisting(set: PromptSet) {
    setSelection({ kind: "existing", id: set.id });
    setDraft({ name: set.name, prompts: { ...set.prompts } });
    setExpanded(stages[0]?.key ?? null);
    setStatusMessage(null);
  }

  function closeEditor() {
    setSelection(null);
    setDraft(null);
    setStatusMessage(null);
  }

  function updateStageText(key: string, value: string) {
    setDraft((prev) => (prev ? { ...prev, prompts: { ...prev.prompts, [key]: value } } : prev));
  }

  function resetStageToDefault(stage: PromptStage) {
    updateStageText(stage.key, stage.default_template);
  }

  function clearStage(key: string) {
    setDraft((prev) => {
      if (!prev) return prev;
      const next = { ...prev.prompts };
      delete next[key];
      return { ...prev, prompts: next };
    });
  }

  // 保存前：仅保留非空阶段（空 = 使用内置默认）
  function normalizedPrompts(d: Draft): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(d.prompts)) {
      if (v && v.trim()) out[k] = v;
    }
    return out;
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!draft || !selection) throw new Error("no draft");
      const payload = { name: draft.name.trim() || "未命名套餐", prompts: normalizedPrompts(draft) };
      if (selection.kind === "new") return createPromptSet(payload);
      return updatePromptSet(selection.id, payload);
    },
    onSuccess: (saved) => {
      setStatusMessage({ text: "已保存。", tone: "info" });
      setSelection({ kind: "existing", id: saved.id });
      setDraft({ name: saved.name, prompts: { ...saved.prompts } });
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  const activateMutation = useMutation({
    mutationFn: async (id: number) => activatePromptSet(id),
    onSuccess: (s) => {
      setStatusMessage({ text: `已启用「${s.name}」，发现流程将使用该套 prompt。`, tone: "info" });
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  const deactivateMutation = useMutation({
    mutationFn: async (id: number) => deactivatePromptSet(id),
    onSuccess: () => {
      setStatusMessage({ text: "已停用，发现流程回退到内置默认 prompt。", tone: "info" });
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: number) => deletePromptSet(id),
    onSuccess: () => {
      setStatusMessage({ text: "已删除。", tone: "info" });
      closeEditor();
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  return (
    <section style={SECTION}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · Prompt 工作室</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2 }}>
            自定义抓取各 LLM 环节的 prompt（站点发现的探查/推断/写配方/审计/命名，以及入库管线的富集·总结·筛选）。可存多套、随时切换；启用哪套就用哪套，未启用则用内置默认。
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button onClick={() => setPanelExpanded((value) => !value)} style={btnGhost}>
            {panelExpanded ? "收起" : "展开"}
          </button>
        </div>
      </div>

      {!panelExpanded ? null : (
        <>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <button onClick={openNew} disabled={stages.length === 0} style={btnPrimary}>
          + 新建套餐
        </button>
      </div>
      {/* 当前生效状态 */}
      <div style={{ marginBottom: 12 }}>
        {activeSet ? (
          <span style={{ ...pill, background: "#ecfdf3", color: "#027a48", border: "1px solid #a6f4c5" }}>
            当前生效：{activeSet.name}
          </span>
        ) : (
          <span style={{ ...pill, background: "#f2f4f7", color: "#475467", border: "1px solid #e4e7ec" }}>
            当前使用内置默认（未启用任何套餐）
          </span>
        )}
      </div>

      {statusMessage && (
        <div
          style={{
            display: "flex", alignItems: "flex-start", gap: 8, fontSize: 13, marginBottom: 12,
            borderRadius: 8, padding: "8px 12px",
            color: statusMessage.tone === "error" ? "#b42318" : "#175cd3",
            background: statusMessage.tone === "error" ? "#fef3f2" : "#eff6ff",
            border: `1px solid ${statusMessage.tone === "error" ? "#fecdca" : "#d3e3fb"}`,
          }}
        >
          <span style={{ flex: 1 }}>{statusMessage.text}</span>
          <button onClick={() => setStatusMessage(null)} title="关闭" style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", fontSize: 15, lineHeight: 1, padding: 0, opacity: 0.7 }}>×</button>
        </div>
      )}

      {(stagesQuery.isError || setsQuery.isError) && (
        <div style={infoBox}>加载失败，请确认后端与登录状态。</div>
      )}

      <div style={{ display: "flex", gap: 14, alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* 左：套餐列表 */}
        <div style={{ flex: "0 0 260px", minWidth: 240 }}>
          <div style={listTitle}>已保存套餐（{sets.length}）</div>
          {sets.length === 0 ? (
            <div style={infoBox}>还没有自定义套餐。点右上角「新建套餐」，会用内置默认模版预填每个阶段。</div>
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {sets.map((s) => {
                const isSel = selection?.kind === "existing" && selection.id === s.id;
                return (
                  <div
                    key={s.id}
                    onClick={() => openExisting(s)}
                    style={{
                      border: `1px solid ${isSel ? "#175cd3" : "#e4e7ec"}`,
                      background: isSel ? "#f5f9ff" : "#fff",
                      borderRadius: 8, padding: "10px 12px", cursor: "pointer",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 13, fontWeight: 700, color: "#101828", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.name}</span>
                      {s.is_active && <span style={{ ...badge, background: "#ecfdf3", color: "#027a48" }}>使用中</span>}
                    </div>
                    <div style={{ fontSize: 11, color: "#98a2b3", marginTop: 4 }}>
                      覆盖 {Object.keys(s.prompts || {}).length}/{stages.length} 阶段
                    </div>
                    <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                      {s.is_active ? (
                        <button
                          onClick={(e) => { e.stopPropagation(); deactivateMutation.mutate(s.id); }}
                          style={btnGhost}
                        >停用</button>
                      ) : (
                        <button
                          onClick={(e) => { e.stopPropagation(); activateMutation.mutate(s.id); }}
                          style={btnSoft}
                        >设为使用中</button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 右：编辑器 */}
        <div style={{ flex: "1 1 420px", minWidth: 320 }}>
          {!draft || !selection ? (
            <div style={{ ...infoBox, minHeight: 120, display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center" }}>
              从左侧选择一套编辑，或点「新建套餐」开始。
            </div>
          ) : (
            <div style={{ border: "1px solid #e4e7ec", borderRadius: 10, padding: 14 }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
                <label style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>套餐名称</label>
                <input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="给这套 prompt 起个名字"
                  style={{ flex: 1, minWidth: 200, border: "1px solid #d0d7e2", borderRadius: 8, padding: "8px 10px", fontSize: 14, color: "#101828" }}
                />
              </div>

              <div style={{ display: "grid", gap: 8 }}>
                {stages.map((stage) => {
                  const isOpen = expanded === stage.key;
                  const text = draft.prompts[stage.key] ?? "";
                  const overridden = !!(text && text.trim());
                  return (
                    <div key={stage.key} style={{ border: "1px solid #eaecf0", borderRadius: 8, overflow: "hidden" }}>
                      <div
                        onClick={() => setExpanded(isOpen ? null : stage.key)}
                        style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "10px 12px", background: "#fafbfc", cursor: "pointer" }}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 700, color: "#101828" }}>
                            {stage.label}
                            <span style={{ ...badge, marginLeft: 8, background: overridden ? "#eff6ff" : "#f2f4f7", color: overridden ? "#175cd3" : "#98a2b3" }}>
                              {overridden ? "自定义" : "用默认"}
                            </span>
                          </div>
                          <div style={{ fontSize: 11, color: "#98a2b3", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{stage.description}</div>
                        </div>
                        <span style={{ fontSize: 12, color: "#98a2b3" }}>{isOpen ? "收起" : "展开"}</span>
                      </div>
                      {isOpen && (
                        <div style={{ padding: 12 }}>
                          {stage.required_tokens.length > 0 && (
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8, alignItems: "center" }}>
                              <span style={{ fontSize: 11, color: "#667085" }}>必需占位符：</span>
                              {stage.required_tokens.map((tok) => (
                                <code key={tok} style={{ fontFamily: MONO, fontSize: 11, background: "#f2f4f7", color: "#344054", borderRadius: 4, padding: "1px 6px" }}>{tok}</code>
                              ))}
                            </div>
                          )}
                          <textarea
                            value={text}
                            onChange={(e) => updateStageText(stage.key, e.target.value)}
                            placeholder="留空 = 使用内置默认模版"
                            spellCheck={false}
                            style={{
                              width: "100%", minHeight: 220, resize: "vertical", boxSizing: "border-box",
                              fontFamily: MONO, fontSize: 12.5, lineHeight: 1.6, color: "#1d2939",
                              border: "1px solid #d0d7e2", borderRadius: 8, padding: 10, background: "#fff",
                            }}
                          />
                          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                            <button onClick={() => resetStageToDefault(stage)} style={btnSoft}>载入默认模版</button>
                            <button onClick={() => clearStage(stage.key)} style={btnGhost}>清空(用默认)</button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending} style={{ ...btnPrimary, flex: 1 }}>
                  {saveMutation.isPending ? "保存中…" : selection.kind === "new" ? "创建套餐" : "保存修改"}
                </button>
                {selection.kind === "existing" && (
                  <button
                    onClick={() => { if (confirm("确认删除该套餐？")) deleteMutation.mutate(selection.id); }}
                    disabled={deleteMutation.isPending}
                    style={btnDanger}
                  >删除</button>
                )}
                <button onClick={closeEditor} style={btnGhost}>关闭</button>
              </div>
            </div>
          )}
        </div>
      </div>
        </>
      )}
    </section>
  );
}

const btnPrimary: React.CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDanger: React.CSSProperties = { ...btnPrimary, background: "#dc2626" };
const btnSoft: React.CSSProperties = { border: "1px solid #d3e3fb", borderRadius: 999, padding: "5px 12px", background: "#eff6ff", color: "#175cd3", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const btnGhost: React.CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#344054", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const pill: React.CSSProperties = { display: "inline-block", fontSize: 12, fontWeight: 700, borderRadius: 999, padding: "4px 12px" };
const badge: React.CSSProperties = { fontSize: 11, fontWeight: 700, padding: "1px 8px", borderRadius: 999 };
const listTitle: React.CSSProperties = { fontSize: 12, fontWeight: 700, color: "#98a2b3", marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.3 };
const infoBox: React.CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
