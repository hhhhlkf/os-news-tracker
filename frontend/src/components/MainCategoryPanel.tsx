import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  createMainCategory,
  deleteMainCategory,
  fetchMainCategories,
  renameMainCategory,
  type MainCategory,
} from "../discovery/mainCategoryApi";
import { clampInput, INPUT_LIMITS } from "../inputLimits";

const SECTION: React.CSSProperties = {
  background: "#fff",
  border: "1px solid #d0d5dd",
  borderRadius: 10,
  padding: 16,
  marginTop: 14,
};
const STORAGE_KEY = "discovery.main-category-panel.expanded";

function readExpandedState() {
  if (typeof window === "undefined") return true;
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw == null ? true : raw === "true";
}

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : error instanceof Error ? error.message : "操作失败";
}

export function MainCategoryPanel() {
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState("");
  const [statusMessage, setStatusMessage] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [expanded, setExpanded] = useState(readExpandedState);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY, String(expanded));
  }, [expanded]);

  const categoriesQuery = useQuery({
    queryKey: ["main-categories"],
    queryFn: fetchMainCategories,
    retry: false,
  });
  const categories = categoriesQuery.data ?? [];

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["main-categories"] });
    // 分类改名会影响信息流 facet 与条目分类，一并刷新
    void queryClient.invalidateQueries({ queryKey: ["facets"] });
    void queryClient.invalidateQueries({ queryKey: ["items"] });
  };

  const createMutation = useMutation({
    mutationFn: async (name: string) => createMainCategory(name),
    onSuccess: (c) => {
      setStatusMessage({ text: `已添加「${c.name}」。`, tone: "info" });
      setNewName("");
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  const renameMutation = useMutation({
    mutationFn: async ({ id, name }: { id: number; name: string }) => renameMainCategory(id, name),
    onSuccess: (c) => {
      setStatusMessage({ text: `已改名为「${c.name}」，相关条目分类已同步更新。`, tone: "info" });
      setEditingId(null);
      setEditingName("");
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: number) => deleteMainCategory(id),
    onSuccess: () => {
      setStatusMessage({ text: "已删除。", tone: "info" });
      invalidate();
    },
    onError: (e) => setStatusMessage({ text: messageFrom(e), tone: "error" }),
  });

  function submitCreate() {
    const name = newName.trim();
    if (!name) return;
    createMutation.mutate(name);
  }

  function startEdit(c: MainCategory) {
    setEditingId(c.id);
    setEditingName(c.name);
    setStatusMessage(null);
  }

  function submitRename(c: MainCategory) {
    const name = editingName.trim();
    if (!name || name === c.name) {
      setEditingId(null);
      return;
    }
    if (c.item_count > 0 && !confirm(`「${c.name}」下有 ${c.item_count} 条条目，改名后这些条目会一并改成「${name}」。确认？`)) {
      return;
    }
    renameMutation.mutate({ id: c.id, name });
  }

  return (
    <section style={SECTION}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>抓取模块 · 主分类修改</div>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 2 }}>
            管理富集阶段可用的主分类。改名会同步更新原属该分类的所有条目；删除仅限无条目的分类。信息流的分类筛选按条目聚合，空分类不会出现在那里。
          </div>
        </div>
        <button onClick={() => setExpanded((value) => !value)} style={btnGhost}>
          {expanded ? "收起" : "展开"}
        </button>
      </div>

      {!expanded ? null : (
        <>

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

      {categoriesQuery.isError && <div style={infoBox}>加载失败，请确认后端与登录状态。</div>}

      {/* 添加 */}
      <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
        <input
          value={newName}
          maxLength={INPUT_LIMITS.shortName}
          onChange={(e) => setNewName(clampInput(e.target.value, INPUT_LIMITS.shortName))}
          onKeyDown={(e) => { if (e.key === "Enter") submitCreate(); }}
          placeholder="新主分类名称"
          style={{ flex: 1, minWidth: 200, border: "1px solid #d0d7e2", borderRadius: 8, padding: "8px 10px", fontSize: 14, color: "#101828" }}
        />
        <button onClick={submitCreate} disabled={createMutation.isPending || !newName.trim()} style={btnPrimary}>
          {createMutation.isPending ? "添加中…" : "+ 添加分类"}
        </button>
      </div>

      {/* 列表 */}
      {categories.length === 0 ? (
        <div style={infoBox}>还没有主分类。</div>
      ) : (
        <div style={{ display: "grid", gap: 8 }}>
          {categories.map((c) => {
            const isEditing = editingId === c.id;
            return (
              <div
                key={c.id}
                style={{ display: "flex", alignItems: "center", gap: 10, border: "1px solid #eaecf0", borderRadius: 8, padding: "10px 12px", background: "#fff" }}
              >
                {isEditing ? (
                  <input
                    value={editingName}
                    autoFocus
                    maxLength={INPUT_LIMITS.shortName}
                    onChange={(e) => setEditingName(clampInput(e.target.value, INPUT_LIMITS.shortName))}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") submitRename(c);
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    style={{ flex: 1, border: "1px solid #175cd3", borderRadius: 6, padding: "6px 8px", fontSize: 14, color: "#101828" }}
                  />
                ) : (
                  <span style={{ flex: 1, fontSize: 14, fontWeight: 600, color: "#101828" }}>{c.name}</span>
                )}

                <span style={{ ...countBadge, background: c.item_count > 0 ? "#eff6ff" : "#f2f4f7", color: c.item_count > 0 ? "#175cd3" : "#98a2b3" }}>
                  {c.item_count} 条
                </span>

                {isEditing ? (
                  <>
                    <button onClick={() => submitRename(c)} disabled={renameMutation.isPending} style={btnSoft}>保存</button>
                    <button onClick={() => setEditingId(null)} style={btnGhost}>取消</button>
                  </>
                ) : (
                  <>
                    <button onClick={() => startEdit(c)} style={btnGhost}>改名</button>
                    {c.item_count === 0 ? (
                      <button
                        onClick={() => { if (confirm(`确认删除主分类「${c.name}」？`)) deleteMutation.mutate(c.id); }}
                        disabled={deleteMutation.isPending}
                        style={btnDangerGhost}
                      >删除</button>
                    ) : (
                      <button disabled title="仅无条目的分类可删除" style={{ ...btnGhost, color: "#d0d5dd", cursor: "not-allowed" }}>删除</button>
                    )}
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
        </>
      )}
    </section>
  );
}

const btnPrimary: React.CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnSoft: React.CSSProperties = { border: "1px solid #d3e3fb", borderRadius: 999, padding: "5px 12px", background: "#eff6ff", color: "#175cd3", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const btnGhost: React.CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#344054", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDangerGhost: React.CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "5px 12px", background: "#fff", color: "#b42318", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const countBadge: React.CSSProperties = { fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap" };
const infoBox: React.CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
