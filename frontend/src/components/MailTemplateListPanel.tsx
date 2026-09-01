import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteMailTemplate,
  fetchMailTemplates,
  fetchTemplateSchedules,
  previewMailTemplate,
  sendMailTemplate,
  updateMailTemplate,
} from "../mail/api";
import {
  mailProviderLabel,
  type MailFilterSnapshot,
  type MailPreviewResponse,
  type MailProviderKind,
  type MailTemplate,
} from "../mail/types";
import { clampInput, INPUT_LIMITS } from "../inputLimits";
import { emailListError, parseEmailList } from "../mail/emailValidation";
import { DeleteIconButton } from "./DeleteIconButton";
import { MailNoticeBox } from "./MailNoticeBox";
import { MailPreviewItemGroups } from "./MailPreviewItemGroups";
import { MailTrendPreviewGroups } from "./MailTrendPreviewGroups";

function formatMultiFilter(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw.split(",").map((part) => part.trim()).filter(Boolean).join(" / ");
}

function summarizeFilter(snapshot: MailFilterSnapshot): string {
  const segments: string[] = [];
  if (snapshot.item_kind === "discussion") segments.push("条目类型：技术讨论");
  if (snapshot.item_kind === "news") segments.push("条目类型：新闻");
  const mainCategory = formatMultiFilter(snapshot.main_category);
  const importance = formatMultiFilter(snapshot.importance);
  const subTag = formatMultiFilter(snapshot.sub_tag);
  if (mainCategory) segments.push(`主分类：${mainCategory}`);
  if (snapshot.info_type) segments.push(`信息类型：${snapshot.info_type}`);
  if (importance) segments.push(`重要程度：${importance}`);
  if (subTag) segments.push(`技术热点：${subTag}`);
  if (snapshot.keywords?.length) segments.push(`关键词筛选${snapshot.strict_title ? "（仅标题）" : ""}：${snapshot.keywords.join(" / ")}`);
  else if (snapshot.q) segments.push(`“${snapshot.q}”`);

  if (snapshot.published_after_mode === "relative" && snapshot.published_after_value) {
    segments.push(`发布时间最近 ${snapshot.published_after_value}`);
  } else if (snapshot.published_after || snapshot.published_before) {
    const after = snapshot.published_after ? `从 ${snapshot.published_after}` : "";
    const before = snapshot.published_before ? `到 ${snapshot.published_before}` : "";
    segments.push(`发布时间${after}${after && before ? " " : ""}${before}`.trim());
  }

  if (snapshot.fetched_after_mode === "relative" && snapshot.fetched_after_value) {
    segments.push(`查询时间最近 ${snapshot.fetched_after_value}`);
  } else if (snapshot.fetched_after || snapshot.fetched_before) {
    const after = snapshot.fetched_after ? `从 ${snapshot.fetched_after}` : "";
    const before = snapshot.fetched_before ? `到 ${snapshot.fetched_before}` : "";
    segments.push(`查询时间${after}${after && before ? " " : ""}${before}`.trim());
  }

  return segments.length > 0 ? segments.join(" · ") : "全部时间 · 无附加筛选";
}

function templateContentLabel(template: MailTemplate): string {
  return template.content_type === "trend_distribution" ? "趋势与热点分发" : summarizeFilter(template.filter_snapshot);
}

// 后端时间均为北京时间墙钟值，原样展示，不做时区换算。
function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const [datePart, timePartRaw = ""] = value.split("T");
  if (!datePart) return value;
  const timePart = timePartRaw.replace("Z", "").split(".")[0].slice(0, 5);
  return timePart ? `${datePart} ${timePart}` : datePart;
}

function lastResultText(template: MailTemplate): { text: string; color: string } {
  if (!template.last_send_status) return { text: "尚未发送", color: "#98a2b3" };
  if (template.last_send_status === "sent") return { text: "发送成功", color: "#027a48" };
  return { text: "发送失败", color: "#b42318" };
}

const PANEL_TITLE: React.CSSProperties = {
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: "#667085",
  fontWeight: 700,
  marginBottom: 10,
};

const PANEL: React.CSSProperties = {
  border: "1px solid #eaecf0",
  borderRadius: 12,
  padding: 14,
  background: "#fff",
};

export function MailTemplateListPanel(props: {
  collapsedNav: boolean;
  onCreateSchedule: (template: MailTemplate) => void;
}) {
  const { onCreateSchedule } = props;
  const queryClient = useQueryClient();
  const templatesQuery = useQuery({
    queryKey: ["mail-templates"],
    queryFn: fetchMailTemplates,
    retry: false,
  });

  const templates = useMemo(() => templatesQuery.data ?? [], [templatesQuery.data]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<MailPreviewResponse | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [sendProvider, setSendProvider] = useState<MailProviderKind>("tof4");

  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editSubject, setEditSubject] = useState("");
  const [editRecipients, setEditRecipients] = useState("");
  const [editActive, setEditActive] = useState(true);

  useEffect(() => {
    if (templates.length === 0) {
      setSelectedId(null);
      return;
    }
    if (selectedId === null || !templates.some((template) => template.id === selectedId)) {
      setSelectedId(templates[0].id);
    }
  }, [templates, selectedId]);

  const selected = useMemo(
    () => templates.find((template) => template.id === selectedId) ?? null,
    [templates, selectedId],
  );

  const linkedSchedulesQuery = useQuery({
    queryKey: ["mail-template-schedules", selectedId],
    queryFn: () => fetchTemplateSchedules(selectedId as number),
    enabled: selectedId !== null,
    retry: false,
  });
  const linkedScheduleCount = linkedSchedulesQuery.data?.length ?? 0;

  useEffect(() => {
    setPreviewData(null);
    setPreviewOpen(false);
    setEditing(false);
    setStatusMessage(null);
  }, [selectedId]);

  const parsedEditRecipients = useMemo(() => parseEmailList(editRecipients), [editRecipients]);
  const editRecipientList = parsedEditRecipients.valid;
  const editRecipientsError = useMemo(
    () => emailListError(parsedEditRecipients),
    [parsedEditRecipients],
  );

  const startEditing = () => {
    if (!selected) return;
    setEditName(selected.name);
    setEditSubject(selected.subject);
    setEditRecipients((selected.recipients ?? []).join("\n"));
    setEditActive(selected.is_active);
    setEditing(true);
    setStatusMessage(null);
  };

  const previewMutation = useMutation({
    mutationFn: async (templateId: number) => previewMailTemplate(templateId, sendProvider),
    onSuccess: (data) => {
      setPreviewData(data);
      setPreviewOpen(true);
      setStatusMessage(`预览已生成，共 ${data.item_count} 条。`);
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "预览生成失败"),
  });

  const sendMutation = useMutation({
    mutationFn: async (templateId: number) => sendMailTemplate(templateId, sendProvider),
    onSuccess: (data) => {
      setStatusMessage(
        data.status === "sent"
          ? `发送成功，共 ${data.item_count} 条，通道：${mailProviderLabel(data.provider)}。`
          : data.status === "查询空" || data.status === "skipped_empty"
            ? "当前筛选没有匹配到条目，未发送。"
          : `发送失败（${mailProviderLabel(data.provider)}）：${data.error_message ?? "未知错误"}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["mail-templates"] });
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "发送失败"),
  });

  const updateMutation = useMutation({
    mutationFn: async (templateId: number) => {
      if (editRecipientsError) {
        throw new Error(editRecipientsError);
      }
      return updateMailTemplate(templateId, {
        name: editName.trim() || "未命名模板",
        subject: editSubject.trim() || "未命名模板",
        recipients: editRecipientList,
        is_active: editActive,
      });
    },
    onSuccess: (template) => {
      setStatusMessage(`模板已更新：${template.name}`);
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["mail-templates"] });
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "模板更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: async (templateId: number) => deleteMailTemplate(templateId),
    onSuccess: (_data, templateId) => {
      setStatusMessage("模板已删除。");
      if (selectedId === templateId) setSelectedId(null);
      void queryClient.invalidateQueries({ queryKey: ["mail-templates"] });
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "模板删除失败"),
  });

  const handleDelete = (template: MailTemplate) => {
    if (typeof window !== "undefined" && !window.confirm(`确认删除模板「${template.name}」？此操作不可撤销。`)) {
      return;
    }
    deleteMutation.mutate(template.id);
  };

  const btn = (background: string, disabled: boolean): React.CSSProperties => ({
    border: "none",
    borderRadius: 999,
    padding: "8px 14px",
    fontSize: 12,
    fontWeight: 700,
    color: "#fff",
    background,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
  });

  const actionBtn = (opts: { bg: string; color: string; border: string; disabled?: boolean }): React.CSSProperties => ({
    border: `1px solid ${opts.border}`,
    borderRadius: 10,
    padding: "9px 8px",
    fontSize: 12.5,
    fontWeight: 700,
    lineHeight: 1.1,
    color: opts.color,
    background: opts.bg,
    cursor: opts.disabled ? "not-allowed" : "pointer",
    opacity: opts.disabled ? 0.6 : 1,
    whiteSpace: "nowrap",
    textAlign: "center",
  });

  const showPreview = previewOpen && previewData !== null;

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 0.8fr)",
        gap: 12,
        alignItems: "stretch",
        height: "64vh",
        minHeight: 0,
      }}
    >
      <section style={{ ...PANEL, display: "grid", gridTemplateRows: "auto minmax(0, 1fr)", minHeight: 0, overflow: "hidden" }}>
        {showPreview && previewData ? (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
              <div style={PANEL_TITLE}>内容预览</div>
              <div style={{ fontSize: 12, color: "#667085" }}>
                共 {previewData.item_count} 条 · {mailProviderLabel(previewData.provider)}
              </div>
            </div>
            <div style={{ overflowY: "auto", minHeight: 0, display: "grid", gap: 10, alignContent: "start" }}>
              <MailNoticeBox notice={previewData.notice} />
              {previewData.item_count === 0 ? (
                <div style={{ border: "1px dashed #d0d5dd", color: "#667085", borderRadius: 12, padding: 14, fontSize: 13 }}>
                    {selected?.content_type === "trend_distribution" ? "当前模板最近成功发布中没有可分发趋势。" : "当前筛选没有匹配到条目。"}
                </div>
              ) : selected?.content_type === "trend_distribution" ? (
                <MailTrendPreviewGroups groups={previewData.trend_groups} />
              ) : (
                <MailPreviewItemGroups items={previewData.items} compact />
              )}
            </div>
          </>
        ) : (
          <>
            <div style={PANEL_TITLE}>模板列表</div>
            <div style={{ overflowY: "auto", minHeight: 0, display: "grid", gap: 10, alignContent: "start" }}>
              {templatesQuery.isLoading && <div style={{ color: "#667085", fontSize: 13 }}>正在加载模板…</div>}
              {templatesQuery.isError && <div style={{ color: "#b42318", fontSize: 13 }}>模板列表加载失败</div>}
              {!templatesQuery.isLoading && !templatesQuery.isError && templates.length === 0 && (
                <div style={{ color: "#667085", fontSize: 13 }}>还没有邮件模板。到「立即发送」页保存一个模板试试。</div>
              )}
              {templates.map((template) => {
                const active = template.id === selectedId;
                const result = lastResultText(template);
                return (
                  <div
                    key={template.id}
                    onClick={() => setSelectedId(template.id)}
                    style={{
                      border: active ? "1px solid #bfd7ff" : "1px solid #dde4ec",
                      borderRadius: 12,
                      padding: "12px 14px",
                      background: active ? "#f8fbff" : "#fff",
                      cursor: "pointer",
                      display: "grid",
                      gap: 6,
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
                      <div style={{ fontWeight: 800, color: "#101828", fontSize: 14, wordBreak: "break-word" }}>{template.name}</div>
                      <DeleteIconButton
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(template);
                        }}
                        disabled={deleteMutation.isPending}
                        title="删除模板"
                      />
                    </div>
                    <div style={{ fontSize: 12, color: "#667085", lineHeight: 1.45 }}>
                      <strong style={{ color: "#475467" }}>{template.content_type === "trend_distribution" ? "内容类型：" : "筛选条件："}</strong>
                      {templateContentLabel(template)}
                    </div>
                    <div style={{ fontSize: 12, color: "#667085" }}>
                      收件人 {(template.recipients ?? []).length} 个 · <span style={{ color: result.color, fontWeight: 700 }}>{result.text}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </section>

      <div style={{ display: "grid", gap: 12, minWidth: 0, overflowY: "auto", minHeight: 0, alignContent: "start" }}>
        {!selected ? (
          <section style={PANEL}>
            <div style={{ color: "#667085", fontSize: 13 }}>从左侧选择一个模板查看详情。</div>
          </section>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 10 }}>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>收件人</div>
                <div style={{ fontSize: 22, fontWeight: 800, color: "#101828" }}>{(selected.recipients ?? []).length}</div>
                <div style={{ fontSize: 12, color: "#667085" }}>固定发送对象</div>
              </div>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>关联任务</div>
                <div style={{ fontSize: 22, fontWeight: 800, color: "#101828" }}>{linkedScheduleCount}</div>
                <div style={{ fontSize: 12, color: "#667085" }}>已有定时发送</div>
              </div>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>最近结果</div>
                <div style={{ fontSize: 15, fontWeight: 800, color: lastResultText(selected).color }}>{lastResultText(selected).text}</div>
                <div style={{ fontSize: 12, color: "#667085" }}>
                  {selected.last_send_count != null ? `${selected.last_send_count} 条${selected.content_type === "trend_distribution" ? "趋势" : "新闻"}` : formatDateTime(selected.last_send_at)}
                </div>
              </div>
            </div>

            <section style={PANEL}>
              <div style={PANEL_TITLE}>{editing ? "编辑模板" : "已选模板详情"}</div>
              {editing ? (
                <div style={{ display: "grid", gap: 10 }}>
                  <label style={{ display: "grid", gap: 4 }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>模板名</span>
                    <input
                      value={editName}
                      maxLength={INPUT_LIMITS.shortName}
                      onChange={(e) => setEditName(clampInput(e.target.value, INPUT_LIMITS.shortName))}
                      style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054" }}
                    />
                  </label>
                  <label style={{ display: "grid", gap: 4 }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>邮件标题</span>
                    <input
                      value={editSubject}
                      maxLength={INPUT_LIMITS.subject}
                      onChange={(e) => setEditSubject(clampInput(e.target.value, INPUT_LIMITS.subject))}
                      style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054" }}
                    />
                  </label>
                  <label style={{ display: "grid", gap: 4 }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>收件人</span>
                    <textarea
                      value={editRecipients}
                      maxLength={INPUT_LIMITS.emailListMultiline}
                      onChange={(e) => setEditRecipients(clampInput(e.target.value, INPUT_LIMITS.emailListMultiline))}
                      rows={3}
                      placeholder="收件人邮箱，支持换行、逗号或空格分隔"
                      style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054", resize: "vertical" }}
                    />
                    {editRecipientsError && (
                      <span style={{ fontSize: 12, color: "#b42318" }}>{editRecipientsError}</span>
                    )}
                  </label>
                  <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: "#475467", fontWeight: 700 }}>
                    <input type="checkbox" checked={editActive} onChange={(e) => setEditActive(e.target.checked)} />
                    启用该模板
                  </label>
                  <div style={{ fontSize: 11, color: "#98a2b3" }}>
                    {selected.content_type === "trend_distribution"
                      ? "趋势方向由保存时选择的身份模板决定；如需更换身份模板，请重新保存一个趋势模板。"
                      : "筛选条件沿用保存时的快照，如需修改请在「立即发送」页重新保存。"}
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button
                      onClick={() => updateMutation.mutate(selected.id)}
                      disabled={updateMutation.isPending || Boolean(editRecipientsError)}
                      style={btn("#175cd3", updateMutation.isPending || Boolean(editRecipientsError))}
                    >
                      {updateMutation.isPending ? "保存中..." : "保存修改"}
                    </button>
                    <button
                      onClick={() => setEditing(false)}
                      style={{ border: "1px solid #d0d5dd", background: "#fff", color: "#475467", borderRadius: 999, padding: "8px 14px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}
                    >
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <div style={{ display: "grid", gap: 8, fontSize: 13, color: "#475467", lineHeight: 1.6 }}>
                  <div><strong style={{ color: "#101828" }}>模板名：</strong>{selected.name}</div>
                  <div><strong style={{ color: "#101828" }}>{selected.content_type === "trend_distribution" ? "内容类型：" : "筛选条件："}</strong>{templateContentLabel(selected)}</div>
                  <div>
                    <strong style={{ color: "#101828" }}>收件人：</strong>
                    {(selected.recipients ?? []).length > 0 ? (selected.recipients ?? []).join("，") : "未设置"}
                  </div>
                  <div><strong style={{ color: "#101828" }}>邮件标题：</strong>{selected.subject}</div>
                  <div>
                    <strong style={{ color: "#101828" }}>状态：</strong>
                    <span style={{ color: selected.is_active ? "#027a48" : "#98a2b3", fontWeight: 700 }}>{selected.is_active ? "已启用" : "已停用"}</span>
                  </div>
                </div>
              )}
            </section>

            {!editing && (
              <section style={{ ...PANEL, background: "#f8fafc" }}>
                <div style={PANEL_TITLE}>模板动作</div>
                <div style={{ display: "grid", gap: 10 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送通道</span>
                    {([
                      { value: "tof4", label: mailProviderLabel("tof4") },
                      { value: "smtp", label: mailProviderLabel("smtp") },
                    ] as const).map((option) => {
                      const active = sendProvider === option.value;
                      return (
                        <button
                          key={option.value}
                          type="button"
                          onClick={() => setSendProvider(option.value)}
                          style={{
                            border: active ? "1px solid #175cd3" : "1px solid #d0d5dd",
                            borderRadius: 999,
                            padding: "6px 12px",
                            fontSize: 12,
                            fontWeight: 700,
                            color: active ? "#175cd3" : "#475467",
                            background: active ? "#eff8ff" : "#fff",
                            cursor: "pointer",
                          }}
                        >
                          {option.label}
                        </button>
                      );
                    })}
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0,1fr))", gap: 8 }}>
                    <button
                      onClick={() => sendMutation.mutate(selected.id)}
                      disabled={sendMutation.isPending}
                      style={actionBtn({ bg: "#2563eb", color: "#fff", border: "#2563eb", disabled: sendMutation.isPending })}
                    >
                      {sendMutation.isPending ? "发送中..." : "发送一次"}
                    </button>
                    <button
                      onClick={() => {
                        if (showPreview) {
                          setPreviewOpen(false);
                          setPreviewData(null);
                        } else {
                          previewMutation.mutate(selected.id);
                        }
                      }}
                      disabled={previewMutation.isPending}
                      style={
                        showPreview
                          ? actionBtn({ bg: "#fef3f2", color: "#b42318", border: "#f7cec8", disabled: previewMutation.isPending })
                          : actionBtn({ bg: "#eff4ff", color: "#1d4ed8", border: "#cdddfb", disabled: previewMutation.isPending })
                      }
                    >
                      {previewMutation.isPending ? "生成中..." : showPreview ? "取消显示" : "预览内容"}
                    </button>
                    <button onClick={startEditing} style={actionBtn({ bg: "#f8fafc", color: "#475467", border: "#e4e7ec" })}>
                      编辑模板
                    </button>
                    <button onClick={() => onCreateSchedule(selected)} style={actionBtn({ bg: "#ecfdf3", color: "#067647", border: "#bbe9cd" })}>
                      创建预定
                    </button>
                  </div>
                </div>
              </section>
            )}

            <section style={PANEL}>
              <div style={PANEL_TITLE}>最近一次发送摘要</div>
              <div style={{ display: "grid", gap: 8 }}>
                <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#fff", padding: "10px 12px", fontSize: 12, color: "#475467" }}>
                  发送时间：{formatDateTime(selected.last_send_at)}
                </div>
                <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#fff", padding: "10px 12px", fontSize: 12, color: "#475467" }}>
                  发送状态：{selected.last_send_status ?? "尚未发送"}
                </div>
                <div style={{ border: "1px solid #eaecf0", borderRadius: 10, background: "#fff", padding: "10px 12px", fontSize: 12, color: "#475467" }}>
                  命中条数：{selected.last_send_count ?? "—"} 条
                </div>
              </div>
            </section>

            {statusMessage && (
              <div style={{ display: "flex", alignItems: "flex-start", gap: 6, fontSize: 12, color: statusMessage.includes("失败") ? "#b42318" : "#027a48" }}>
                <span style={{ flex: 1 }}>{statusMessage}</span>
                <button
                  onClick={() => setStatusMessage(null)}
                  aria-label="关闭提示"
                  title="关闭"
                  style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", fontSize: 14, lineHeight: 1, padding: 0, opacity: 0.7 }}
                >
                  ×
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
