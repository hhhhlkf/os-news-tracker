import { useMemo, useState, type CSSProperties } from "react";
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

const SECTION: CSSProperties = {
  background: "#fff",
  border: "1px solid #d0d5dd",
  borderRadius: 10,
  padding: 16,
  marginTop: 14,
};

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

  const editorTitle = selection?.kind === "new" ? "新建套餐" : "保存套餐";

  return (
    <section style={SECTION}>
      <div style={stack}>
        <div style={statusRow}>
          {activeSet ? (
            <span style={{ ...pill, background: "#ecf2f1", color: "#3d6b62", border: "1px solid #c5d6d3" }}>
              当前生效：{activeSet.name}
            </span>
          ) : (
            <span style={{ ...pill, background: "#f2f4f7", color: "#475467", border: "1px solid #e4e7ec" }}>
              当前使用内置默认
            </span>
          )}
        </div>

        {statusMessage && (
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 8,
              fontSize: 12,
              borderRadius: 8,
              padding: "7px 10px",
              color: statusMessage.tone === "error" ? "#b42318" : "#175cd3",
              background: statusMessage.tone === "error" ? "#fef3f2" : "#eff6ff",
              border: `1px solid ${statusMessage.tone === "error" ? "#fecdca" : "#d3e3fb"}`,
            }}
          >
            <span style={{ flex: 1, minWidth: 0 }}>{statusMessage.text}</span>
            <button type="button" onClick={() => setStatusMessage(null)} title="关闭" style={closeButton}>×</button>
          </div>
        )}

        {(stagesQuery.isError || setsQuery.isError) && (
          <div style={infoBox}>加载失败，请确认后端与登录状态。</div>
        )}

        <div style={block}>
          <div style={blockHead}>
            <div style={blockTitle}>已保存套餐（{sets.length}）</div>
            <button type="button" onClick={openNew} disabled={stages.length === 0} style={btnPrimary}>
              + 新建套餐
            </button>
          </div>
          {sets.length === 0 ? (
            <div style={infoBox}>还没有自定义套餐。点「新建套餐」会用内置默认模版预填每个阶段。</div>
          ) : (
            <div style={setList} data-home-scroll="true">
              {sets.map((s) => {
                const isSel = selection?.kind === "existing" && selection.id === s.id;
                return (
                  <div
                    key={s.id}
                    onClick={() => openExisting(s)}
                    style={{
                      ...setRow,
                      borderColor: isSel ? "#c5d0dc" : "#eaecf0",
                      background: isSel ? "#f7f9fb" : "#fff",
                    }}
                  >
                    <div style={setMain}>
                      <div style={setTitleLine}>
                        <span style={setName}>{s.name}</span>
                        {s.is_active && <span style={{ ...badge, background: "#ecf2f1", color: "#3d6b62" }}>使用中</span>}
                      </div>
                      <div style={setMeta}>覆盖 {Object.keys(s.prompts || {}).length}/{stages.length} 阶段</div>
                    </div>
                    {s.is_active ? (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); deactivateMutation.mutate(s.id); }}
                        style={btnGhost}
                      >停用</button>
                    ) : (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); activateMutation.mutate(s.id); }}
                        style={btnSoft}
                      >启用</button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div style={block}>
          <div style={blockHead}>
            <div style={blockTitle}>{draft && selection ? editorTitle : "保存套餐"}</div>
          </div>
          {!draft || !selection ? (
            <div style={infoBox}>从上方选择一套编辑，或点「新建套餐」开始。</div>
          ) : (
            <div style={editorBody}>
              <label style={nameLabel}>
                套餐名称
                <input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="给这套 prompt 起个名字"
                  style={nameInput}
                />
              </label>

              <div style={stageList}>
                {stages.map((stage) => {
                  const isOpen = expanded === stage.key;
                  const text = draft.prompts[stage.key] ?? "";
                  const overridden = !!(text && text.trim());
                  return (
                    <div key={stage.key} style={stageCard}>
                      <div
                        onClick={() => setExpanded(isOpen ? null : stage.key)}
                        style={stageHead}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div style={stageTitle}>
                            {stage.label}
                            <span style={{ ...badge, marginLeft: 6, background: overridden ? "#eef2f6" : "#f2f4f7", color: overridden ? "#3d5a80" : "#98a2b3" }}>
                              {overridden ? "自定义" : "用默认"}
                            </span>
                          </div>
                          <div style={stageDesc}>{stage.description}</div>
                        </div>
                      </div>
                      {isOpen && (
                        <div style={stageBody}>
                          {stage.required_tokens.length > 0 && (
                            <div style={tokenRow}>
                              <span style={tokenHint}>必需占位符：</span>
                              {stage.required_tokens.map((tok) => (
                                <code key={tok} style={tokenChip}>{tok}</code>
                              ))}
                            </div>
                          )}
                          <textarea
                            value={text}
                            onChange={(e) => updateStageText(stage.key, e.target.value)}
                            placeholder="留空 = 使用内置默认模版"
                            spellCheck={false}
                            style={textarea}
                          />
                          <div style={stageActions}>
                            <button type="button" onClick={() => resetStageToDefault(stage)} style={btnSoft}>载入默认</button>
                            <button type="button" onClick={() => clearStage(stage.key)} style={btnGhost}>清空</button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div style={editorActions}>
                <button type="button" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending} style={btnPrimary}>
                  {saveMutation.isPending ? "保存中…" : selection.kind === "new" ? "创建套餐" : "保存修改"}
                </button>
                {selection.kind === "existing" && (
                  <button
                    type="button"
                    onClick={() => { if (confirm("确认删除该套餐？")) deleteMutation.mutate(selection.id); }}
                    disabled={deleteMutation.isPending}
                    style={btnDanger}
                  >删除</button>
                )}
                <button type="button" onClick={closeEditor} style={btnGhost}>取消</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

const stack: CSSProperties = { display: "grid", gap: 14, minWidth: 0 };
const statusRow: CSSProperties = { display: "flex", alignItems: "center", minWidth: 0 };
const block: CSSProperties = { minWidth: 0 };
const blockHead: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 8, minWidth: 0 };
const blockTitle: CSSProperties = { fontSize: 12, fontWeight: 800, color: "#101828", lineHeight: 1.4 };
const setList: CSSProperties = { display: "grid", alignContent: "start", gap: 6, minWidth: 0, maxHeight: 180, overflowY: "auto" };
const setRow: CSSProperties = { display: "flex", alignItems: "center", gap: 8, minWidth: 0, overflow: "hidden", border: "1px solid #eaecf0", borderRadius: 8, padding: "7px 9px", cursor: "pointer" };
const setMain: CSSProperties = { flex: 1, minWidth: 0, overflow: "hidden" };
const setTitleLine: CSSProperties = { display: "flex", alignItems: "center", gap: 6, minWidth: 0 };
const setName: CSSProperties = { minWidth: 0, flex: 1, fontSize: 12, fontWeight: 700, color: "#101828", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const setMeta: CSSProperties = { fontSize: 10, color: "#667085", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const editorBody: CSSProperties = { display: "grid", gap: 10, minWidth: 0 };
const nameLabel: CSSProperties = { display: "grid", gap: 6, fontSize: 11, fontWeight: 700, color: "#475467", minWidth: 0 };
const nameInput: CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid #d0d7e2", borderRadius: 8, padding: "7px 9px", fontSize: 13, color: "#101828", fontWeight: 500 };
const stageList: CSSProperties = { display: "grid", gap: 6, minWidth: 0 };
const stageCard: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 8, overflow: "hidden", minWidth: 0 };
const stageHead: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "8px 10px", background: "#fafbfc", cursor: "pointer" };
const stageTitle: CSSProperties = { fontSize: 12, fontWeight: 700, color: "#101828", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const stageDesc: CSSProperties = { fontSize: 10, color: "#98a2b3", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const stageBody: CSSProperties = { padding: 10, display: "grid", gap: 8, minWidth: 0 };
const tokenRow: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" };
const tokenHint: CSSProperties = { fontSize: 10, color: "#667085" };
const tokenChip: CSSProperties = { fontFamily: MONO, fontSize: 10, background: "#f2f4f7", color: "#344054", borderRadius: 4, padding: "1px 6px" };
const textarea: CSSProperties = {
  width: "100%",
  minHeight: 140,
  resize: "vertical",
  boxSizing: "border-box",
  fontFamily: MONO,
  fontSize: 12,
  lineHeight: 1.55,
  color: "#1d2939",
  border: "1px solid #d0d7e2",
  borderRadius: 8,
  padding: 8,
  background: "#fff",
};
const stageActions: CSSProperties = { display: "flex", gap: 6, flexWrap: "wrap" };
const editorActions: CSSProperties = { display: "flex", gap: 6, flexWrap: "wrap" };
const btnPrimary: CSSProperties = { flexShrink: 0, border: "none", borderRadius: 999, padding: "6px 12px", background: "#3d5a80", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const btnDanger: CSSProperties = { ...btnPrimary, background: "#fff", color: "#b42318", border: "1px solid #fecdca" };
const btnSoft: CSSProperties = { flexShrink: 0, border: "1px solid #c5d0dc", borderRadius: 999, padding: "4px 10px", background: "#eef2f6", color: "#3d5a80", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const btnGhost: CSSProperties = { flexShrink: 0, border: "1px solid #d0d5dd", borderRadius: 999, padding: "4px 10px", background: "#fff", color: "#344054", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const pill: CSSProperties = { display: "inline-block", maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11, fontWeight: 700, borderRadius: 999, padding: "3px 10px" };
const badge: CSSProperties = { flexShrink: 0, fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 999 };
const infoBox: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: "10px 12px", color: "#667085", fontSize: 12 };
const closeButton: CSSProperties = { border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 };
