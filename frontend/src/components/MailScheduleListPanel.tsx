import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createMailSchedule,
  deleteMailSchedule,
  fetchMailScheduleLogs,
  fetchMailSchedules,
  pauseMailSchedule,
  resumeMailSchedule,
  sendNowMailSchedule,
  updateMailSchedule,
} from "../mail/api";
import type { MailDeliveryLog, MailFilterSnapshot, MailFrequency, MailSchedule } from "../mail/types";

export interface ScheduleDraft {
  templateId: number | null;
  name: string;
  subject: string;
  recipients: string[];
  filter_snapshot: MailFilterSnapshot;
}

function summarizeFilter(snapshot: MailFilterSnapshot): string {
  const segments: string[] = [];
  if (snapshot.main_category) segments.push(snapshot.main_category);
  if (snapshot.info_type) segments.push(snapshot.info_type);
  if (snapshot.importance) segments.push(snapshot.importance);
  if (snapshot.sub_tag) segments.push(snapshot.sub_tag);
  if (snapshot.q) segments.push(`“${snapshot.q}”`);
  if (snapshot.published_after_mode === "relative" && snapshot.published_after_value) {
    segments.push(`最近 ${snapshot.published_after_value}`);
  } else if (snapshot.published_after || snapshot.published_before) {
    const after = snapshot.published_after ? `从 ${snapshot.published_after}` : "";
    const before = snapshot.published_before ? `到 ${snapshot.published_before}` : "";
    segments.push(`${after}${after && before ? " " : ""}${before}`.trim());
  }
  return segments.length > 0 ? segments.join(" · ") : "全部时间 · 无附加筛选";
}

// 后端所有邮件时间均以北京时间墙钟值存储，前端原样展示，不做任何时区换算。
function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const [datePart, timePartRaw = ""] = value.split("T");
  if (!datePart) return value;
  const timePart = timePartRaw.replace("Z", "").split(".")[0].slice(0, 5);
  return timePart ? `${datePart} ${timePart}` : datePart;
}

function freqLabel(freq: MailFrequency): string {
  return freq === "weekly" ? "每周" : "每日";
}

/** send_time 已是北京时间 HH:MM，直接展示。 */
function displaySendTime(hhmm: string | null | undefined): string {
  if (!hhmm) return "—";
  return hhmm.slice(0, 5);
}

function logTag(trigger: string): { text: string; bg: string; color: string } {
  if (trigger === "patrol_resend") return { text: "CHECK", bg: "#3b2318", color: "#ffb38f" };
  return { text: "SEND", bg: "#173328", color: "#8ce0b6" };
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

const FIELD: React.CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 10,
  padding: "8px 10px",
  fontSize: 13,
  color: "#344054",
  background: "#fff",
};

export function MailScheduleListPanel(props: {
  initialDraft: ScheduleDraft | null;
  onDraftConsumed: () => void;
}) {
  const { initialDraft, onDraftConsumed } = props;
  const queryClient = useQueryClient();
  const schedulesQuery = useQuery({
    queryKey: ["mail-schedules"],
    queryFn: fetchMailSchedules,
    retry: false,
  });
  const schedules = useMemo(() => schedulesQuery.data ?? [], [schedulesQuery.data]);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editSendTime, setEditSendTime] = useState("09:00");
  const [editFrequency, setEditFrequency] = useState<MailFrequency>("daily");

  const [creating, setCreating] = useState(false);
  const [draftTemplateId, setDraftTemplateId] = useState<number | null>(null);
  const [draftFilter, setDraftFilter] = useState<MailFilterSnapshot>({});
  const [cName, setCName] = useState("");
  const [cSubject, setCSubject] = useState("");
  const [cRecipients, setCRecipients] = useState("");
  const [cFrequency, setCFrequency] = useState<MailFrequency>("daily");
  const [cSendTime, setCSendTime] = useState("09:00");

  const openCreate = (draft: ScheduleDraft | null) => {
    setDraftTemplateId(draft?.templateId ?? null);
    setDraftFilter(draft?.filter_snapshot ?? {});
    setCName(draft?.name ?? "");
    setCSubject(draft?.subject ?? "");
    setCRecipients((draft?.recipients ?? []).join("\n"));
    setCFrequency("daily");
    setCSendTime("09:00");
    setCreating(true);
    setStatusMessage(null);
  };

  useEffect(() => {
    if (initialDraft) {
      openCreate(initialDraft);
      onDraftConsumed();
    }
  }, [initialDraft, onDraftConsumed]);

  useEffect(() => {
    if (creating) return;
    if (schedules.length === 0) {
      setSelectedId(null);
      return;
    }
    if (selectedId === null || !schedules.some((s) => s.id === selectedId)) {
      setSelectedId(schedules[0].id);
    }
  }, [schedules, selectedId, creating]);

  const selected = useMemo(() => schedules.find((s) => s.id === selectedId) ?? null, [schedules, selectedId]);

  useEffect(() => {
    setEditing(false);
    setStatusMessage(null);
  }, [selectedId]);

  const logsQuery = useQuery({
    queryKey: ["mail-schedule-logs", selectedId],
    queryFn: () => fetchMailScheduleLogs(selectedId as number),
    enabled: selectedId !== null && !creating,
    retry: false,
  });

  const cRecipientList = useMemo(
    () => cRecipients.split(/[\n,;，；\s]+/).map((v) => v.trim()).filter(Boolean),
    [cRecipients],
  );

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["mail-schedules"] });
    if (selectedId !== null) void queryClient.invalidateQueries({ queryKey: ["mail-schedule-logs", selectedId] });
  };

  const createMutation = useMutation({
    mutationFn: async () =>
      createMailSchedule({
        name: cName.trim() || "未命名预定",
        subject: cSubject.trim() || "未命名预定",
        recipients: cRecipientList,
        filter_snapshot: draftFilter,
        frequency: cFrequency,
        send_time: cSendTime,
        enabled: true,
        template_id: draftTemplateId,
      }),
    onSuccess: (schedule) => {
      setCreating(false);
      setStatusMessage(`预定已创建：${schedule.name}`);
      setSelectedId(schedule.id);
      void queryClient.invalidateQueries({ queryKey: ["mail-schedules"] });
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "创建预定失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: async (schedule: MailSchedule) =>
      schedule.enabled ? pauseMailSchedule(schedule.id) : resumeMailSchedule(schedule.id),
    onSuccess: (schedule) => {
      setStatusMessage(schedule.enabled ? "任务已恢复。" : "任务已暂停。");
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "操作失败"),
  });

  const sendNowMutation = useMutation({
    mutationFn: async (scheduleId: number) => sendNowMailSchedule(scheduleId),
    onSuccess: (data) => {
      setStatusMessage(
        data.status === "sent"
          ? `补发成功，共 ${data.item_count} 条。`
          : `补发失败（${data.provider.toUpperCase()}）：${data.error_message ?? "未知错误"}`,
      );
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "补发失败"),
  });

  const updateMutation = useMutation({
    mutationFn: async (scheduleId: number) =>
      updateMailSchedule(scheduleId, { send_time: editSendTime, frequency: editFrequency }),
    onSuccess: () => {
      setStatusMessage("发送时间与频率已更新。");
      setEditing(false);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: async (scheduleId: number) => deleteMailSchedule(scheduleId),
    onSuccess: (_data, scheduleId) => {
      setStatusMessage("预定已删除。");
      if (selectedId === scheduleId) setSelectedId(null);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "删除失败"),
  });

  const handleDelete = (schedule: MailSchedule) => {
    if (typeof window !== "undefined" && !window.confirm(`确认删除预定「${schedule.name}」？此操作不可撤销。`)) {
      return;
    }
    deleteMutation.mutate(schedule.id);
  };

  const startEditing = () => {
    if (!selected) return;
    setEditSendTime(displaySendTime(selected.send_time) || "09:00");
    setEditFrequency(selected.frequency);
    setEditing(true);
    setStatusMessage(null);
  };

  const solidBtn = (bg: string, disabled: boolean): React.CSSProperties => ({
    border: "none",
    borderRadius: 10,
    padding: "9px 12px",
    fontSize: 12.5,
    fontWeight: 700,
    color: "#fff",
    background: bg,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
    whiteSpace: "nowrap",
  });

  const softBtn = (bg: string, color: string, border: string): React.CSSProperties => ({
    border: `1px solid ${border}`,
    borderRadius: 10,
    padding: "9px 12px",
    fontSize: 12.5,
    fontWeight: 700,
    color,
    background: bg,
    cursor: "pointer",
    whiteSpace: "nowrap",
  });

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 0.85fr) minmax(0, 1.15fr)",
        gap: 12,
        alignItems: "stretch",
        height: "64vh",
        minHeight: 0,
      }}
    >
      <section style={{ ...PANEL, display: "grid", gridTemplateRows: "auto minmax(0,1fr)", minHeight: 0, overflow: "hidden" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
          <div style={PANEL_TITLE}>已预定发送列表</div>
          <button
            onClick={() => openCreate(null)}
            style={{ border: "1px solid #cddffb", background: "#eff4ff", color: "#1d4ed8", borderRadius: 8, padding: "4px 10px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}
          >
            新建预定
          </button>
        </div>
        <div style={{ overflowY: "auto", minHeight: 0, display: "grid", gap: 10, alignContent: "start" }}>
          {schedulesQuery.isLoading && <div style={{ color: "#667085", fontSize: 13 }}>正在加载预定任务…</div>}
          {schedulesQuery.isError && <div style={{ color: "#b42318", fontSize: 13 }}>预定任务加载失败</div>}
          {!schedulesQuery.isLoading && !schedulesQuery.isError && schedules.length === 0 && (
            <div style={{ color: "#667085", fontSize: 13 }}>还没有预定发送任务。可从模板页「创建预定」或点右上角「新建预定」。</div>
          )}
          {schedules.map((schedule) => {
            const active = schedule.id === selectedId && !creating;
            const statusColor = !schedule.enabled ? "#98a2b3" : schedule.last_result_status === "failed" ? "#b42318" : "#027a48";
            const statusText = !schedule.enabled
              ? "已暂停"
              : schedule.last_result_status
                ? schedule.last_result_status === "sent"
                  ? `最近成功 · ${schedule.last_result_count ?? 0} 条`
                  : "最近失败"
                : "待发送";
            return (
              <div
                key={schedule.id}
                onClick={() => {
                  setCreating(false);
                  setSelectedId(schedule.id);
                }}
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
                  <div style={{ fontWeight: 800, color: "#101828", fontSize: 14, wordBreak: "break-word" }}>
                    {schedule.name} · {freqLabel(schedule.frequency)} {displaySendTime(schedule.send_time)}
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(schedule);
                    }}
                    disabled={deleteMutation.isPending}
                    title="删除预定"
                    aria-label="删除预定"
                    style={{
                      border: "1px solid #f0c6c2",
                      background: "#fff",
                      color: "#b42318",
                      borderRadius: 8,
                      cursor: deleteMutation.isPending ? "wait" : "pointer",
                      fontSize: 13,
                      lineHeight: 1,
                      padding: "4px 7px",
                      flexShrink: 0,
                    }}
                  >
                    🗑
                  </button>
                </div>
                <div style={{ fontSize: 12, color: "#667085" }}>
                  {schedule.template_id
                    ? `来源模板：${schedule.template_name ?? `#${schedule.template_id}`}`
                    : "独立预定（无模板）"}
                </div>
                <div style={{ fontSize: 12, color: statusColor, fontWeight: 700 }}>{statusText}</div>
              </div>
            );
          })}
        </div>
      </section>

      <div style={{ display: "grid", gap: 12, minWidth: 0, overflowY: "auto", minHeight: 0, alignContent: "start" }}>
        {creating ? (
          <section style={PANEL}>
            <div style={PANEL_TITLE}>创建预定发送</div>
            <div style={{ display: "grid", gap: 10 }}>
              <label style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>任务名</span>
                <input value={cName} onChange={(e) => setCName(e.target.value)} style={FIELD} />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>邮件标题</span>
                <input value={cSubject} onChange={(e) => setCSubject(e.target.value)} style={FIELD} />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>收件人</span>
                <textarea
                  value={cRecipients}
                  onChange={(e) => setCRecipients(e.target.value)}
                  rows={3}
                  placeholder="收件人邮箱，支持换行、逗号或空格分隔"
                  style={{ ...FIELD, resize: "vertical" }}
                />
              </label>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送时间 (北京时间)</span>
                  <input type="time" value={cSendTime} onChange={(e) => setCSendTime(e.target.value)} style={FIELD} />
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>频率</span>
                  <select value={cFrequency} onChange={(e) => setCFrequency(e.target.value as MailFrequency)} style={FIELD}>
                    <option value="daily">每日</option>
                    <option value="weekly">每周</option>
                  </select>
                </label>
              </div>
              <div style={{ fontSize: 12, color: "#667085" }}>筛选条件：{summarizeFilter(draftFilter)}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <button onClick={() => createMutation.mutate()} disabled={createMutation.isPending} style={solidBtn("#2563eb", createMutation.isPending)}>
                  {createMutation.isPending ? "创建中..." : "创建预定"}
                </button>
                <button
                  onClick={() => setCreating(false)}
                  style={{ border: "1px solid #d0d5dd", background: "#fff", color: "#475467", borderRadius: 10, padding: "9px 12px", fontSize: 12.5, fontWeight: 700, cursor: "pointer" }}
                >
                  取消
                </button>
              </div>
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
            </div>
          </section>
        ) : !selected ? (
          <section style={PANEL}>
            <div style={{ color: "#667085", fontSize: 13 }}>从左侧选择一个预定任务查看详情。</div>
          </section>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 10 }}>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>任务状态</div>
                <div style={{ fontSize: 15, fontWeight: 800, color: selected.enabled ? "#027a48" : "#98a2b3" }}>
                  {selected.enabled ? "运行中" : "已暂停"}
                </div>
                <div style={{ fontSize: 12, color: "#667085" }}>
                  {selected.last_sent_marker_date ? `今天标记：${selected.last_sent_marker_date}` : "今日尚未发送"}
                </div>
              </div>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>下次发送</div>
                <div style={{ fontSize: 22, fontWeight: 800, color: "#101828" }}>{displaySendTime(selected.send_time)}</div>
                <div style={{ fontSize: 12, color: "#667085" }}>{freqLabel(selected.frequency)} · {formatDateTime(selected.next_run_at)}</div>
              </div>
              <div style={{ border: "1px solid #e1e7ef", borderRadius: 12, background: "#f8fafc", padding: "12px 14px" }}>
                <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>巡检状态</div>
                <div style={{ fontSize: 15, fontWeight: 800, color: "#175cd3" }}>{selected.patrol_status ?? "正常"}</div>
                <div style={{ fontSize: 12, color: "#667085" }}>每 6h 兜底补发</div>
              </div>
            </div>

            <section style={PANEL}>
              <div style={PANEL_TITLE}>已选任务详情</div>
              <div style={{ display: "grid", gap: 8, fontSize: 13, color: "#475467", lineHeight: 1.6 }}>
                <div><strong style={{ color: "#101828" }}>任务名：</strong>{selected.name}</div>
                <div><strong style={{ color: "#101828" }}>来源模板：</strong>{selected.template_id ? (selected.template_name ?? `#${selected.template_id}`) : "独立预定"}</div>
                <div><strong style={{ color: "#101828" }}>筛选条件：</strong>{summarizeFilter(selected.filter_snapshot)}</div>
                <div><strong style={{ color: "#101828" }}>收件人：</strong>{(selected.recipients ?? []).length > 0 ? (selected.recipients ?? []).join("，") : "未设置"}</div>
                <div><strong style={{ color: "#101828" }}>发送时间：</strong>{displaySendTime(selected.send_time)}（北京时间）</div>
                <div><strong style={{ color: "#101828" }}>固定频率：</strong>{freqLabel(selected.frequency)}</div>
                <div><strong style={{ color: "#101828" }}>当天标记：</strong>{selected.last_sent_marker_date ?? "未发送"}</div>
              </div>
            </section>

            <section style={{ ...PANEL, background: "#f8fafc" }}>
              <div style={PANEL_TITLE}>任务操作</div>
              {editing ? (
                <div style={{ display: "grid", gap: 10 }}>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                    <label style={{ display: "grid", gap: 4 }}>
                      <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送时间 (北京时间)</span>
                      <input type="time" value={editSendTime} onChange={(e) => setEditSendTime(e.target.value)} style={FIELD} />
                    </label>
                    <label style={{ display: "grid", gap: 4 }}>
                      <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>频率</span>
                      <select value={editFrequency} onChange={(e) => setEditFrequency(e.target.value as MailFrequency)} style={FIELD}>
                        <option value="daily">每日</option>
                        <option value="weekly">每周</option>
                      </select>
                    </label>
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button onClick={() => updateMutation.mutate(selected.id)} disabled={updateMutation.isPending} style={solidBtn("#2563eb", updateMutation.isPending)}>
                      {updateMutation.isPending ? "保存中..." : "保存"}
                    </button>
                    <button onClick={() => setEditing(false)} style={softBtn("#fff", "#475467", "#d0d5dd")}>取消</button>
                  </div>
                </div>
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 8 }}>
                  <button onClick={() => toggleMutation.mutate(selected)} disabled={toggleMutation.isPending} style={softBtn("#f8fafc", "#475467", "#e4e7ec")}>
                    {selected.enabled ? "暂停任务" : "恢复任务"}
                  </button>
                  <button onClick={() => sendNowMutation.mutate(selected.id)} disabled={sendNowMutation.isPending} style={solidBtn("#2563eb", sendNowMutation.isPending)}>
                    {sendNowMutation.isPending ? "补发中..." : "立即补发"}
                  </button>
                  <button onClick={startEditing} style={softBtn("#ecfdf3", "#067647", "#bbe9cd")}>编辑时间与频率</button>
                </div>
              )}
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

            <section style={{ border: "1px solid #d7dee7", borderRadius: 14, background: "#0f1722", overflow: "hidden" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "10px 12px",
                  borderBottom: "1px solid rgba(255,255,255,0.08)",
                  background: "#131d29",
                  fontSize: 11,
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  color: "#94a6b8",
                }}
              >
                <span>最近执行记录</span>
                <span>Delivery Log</span>
              </div>
              <div style={{ fontFamily: '"JetBrains Mono", ui-monospace, monospace', fontSize: 12 }}>
                {logsQuery.isLoading && <div style={{ padding: "10px 12px", color: "#8ea0b2" }}>加载中…</div>}
                {logsQuery.isError && <div style={{ padding: "10px 12px", color: "#ff9ca4" }}>日志加载失败</div>}
                {!logsQuery.isLoading && !logsQuery.isError && (logsQuery.data ?? []).length === 0 && (
                  <div style={{ padding: "10px 12px", color: "#8ea0b2" }}>暂无执行记录。</div>
                )}
                {(logsQuery.data ?? []).map((log: MailDeliveryLog) => {
                  const tag = logTag(log.trigger_type);
                  const failed = log.status === "failed";
                  return (
                    <div
                      key={log.id}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "150px 1fr",
                        gap: 10,
                        padding: "10px 12px",
                        borderBottom: "1px solid rgba(255,255,255,0.06)",
                        lineHeight: 1.55,
                      }}
                    >
                      <div style={{ color: "#7cc6f5" }}>{formatDateTime(log.finished_at ?? log.started_at)}</div>
                      <div style={{ color: "#d7e2ec" }}>
                        <span
                          style={{
                            display: "inline-block",
                            marginRight: 8,
                            padding: "2px 7px",
                            borderRadius: 999,
                            fontSize: 10,
                            fontWeight: 700,
                            background: failed ? "#402126" : tag.bg,
                            color: failed ? "#ff9ca4" : tag.color,
                          }}
                        >
                          {failed ? "ERR" : tag.text}
                        </span>
                        {failed ? "发送失败" : "正常发送"}
                        <span style={{ color: "#8ea0b2" }}> · {log.item_count} 条 · {failed ? (log.error_message ?? "失败") : "成功"}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
