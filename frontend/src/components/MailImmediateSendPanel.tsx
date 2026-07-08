import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { ItemQueryParams } from "../api/client";
import {
  buildMailFilterSnapshot,
  createMailTemplate,
  previewImmediateMail,
  sendImmediateMail,
} from "../mail/api";
import type { MailImmediateSendRequest, MailPreviewResponse, MailTemplate } from "../mail/types";

export function MailImmediateSendPanel(props: {
  homeFilters: ItemQueryParams;
  onTemplateSaved?: (template: MailTemplate) => void;
}) {
  const { homeFilters, onTemplateSaved } = props;
  const filterSnapshot = useMemo(() => buildMailFilterSnapshot(homeFilters), [homeFilters]);
  const [subject, setSubject] = useState("技术新闻筛选简报");
  const [recipientsText, setRecipientsText] = useState("");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<MailPreviewResponse | null>(null);

  const recipients = useMemo(
    () =>
      recipientsText
        .split(/[\n,;，；\s]+/)
        .map((value) => value.trim())
        .filter(Boolean),
    [recipientsText],
  );

  useEffect(() => {
    setPreviewData(null);
  }, [homeFilters, subject, recipientsText]);

  const previewMutation = useMutation({
    mutationFn: async (request: MailImmediateSendRequest) => previewImmediateMail(request),
    onSuccess: (data) => {
      setStatusMessage(`预览已生成，共 ${data.item_count} 条。`);
      setPreviewData(data);
    },
    onError: (error) => {
      setStatusMessage(error instanceof Error ? error.message : "预览生成失败");
    },
  });

  const sendMutation = useMutation({
    mutationFn: async (request: MailImmediateSendRequest) => sendImmediateMail(request),
    onSuccess: (data) => {
      setStatusMessage(data.status === "sent" ? `发送成功，共 ${data.item_count} 条。` : `发送失败：${data.error_message ?? "未知错误"}`);
    },
    onError: (error) => {
      setStatusMessage(error instanceof Error ? error.message : "发送失败");
    },
  });

  const saveTemplateMutation = useMutation({
    mutationFn: async () =>
      createMailTemplate({
        name: subject.trim() || "未命名模板",
        subject: subject.trim() || "未命名模板",
        recipients,
        filter_snapshot: filterSnapshot,
        is_active: true,
      }),
    onSuccess: (template) => {
      setStatusMessage(`模板已保存：${template.name}`);
      onTemplateSaved?.(template);
    },
    onError: (error) => {
      setStatusMessage(error instanceof Error ? error.message : "模板保存失败");
    },
  });

  const requestPayload: MailImmediateSendRequest = {
    subject: subject.trim() || "技术新闻筛选简报",
    recipients,
    filter_snapshot: filterSnapshot,
  };
  const canSend = recipients.length > 0 && subject.trim().length > 0;
  const canSaveTemplate = subject.trim().length > 0;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "0.92fr 1.08fr", gap: 12, alignItems: "start" }}>
      <div style={{ display: "grid", gap: 12 }}>
        <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14, background: "#fff" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 10 }}>
            当前筛选快照
          </div>
          <div style={{ display: "grid", gap: 8, fontSize: 12, color: "#475467", lineHeight: 1.45 }}>
            {Object.entries(filterSnapshot).map(([key, value]) => (
              <div key={key}>
                {key}: {value === null || value === undefined || value === "" ? "-" : String(value)}
              </div>
            ))}
          </div>
        </section>

        <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14, background: "#fff" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 10 }}>
            发送设置
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            <input
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="邮件标题"
              style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054" }}
            />
            <textarea
              value={recipientsText}
              onChange={(e) => setRecipientsText(e.target.value)}
              placeholder="收件人邮箱，支持换行、逗号或空格分隔"
              rows={4}
              style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054", resize: "vertical" }}
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <div style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054", background: "#fff" }}>发送时间：保存模板后在后续任务页配置</div>
              <div style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054", background: "#fff" }}>频率：保存模板后在后续任务页配置</div>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button
                onClick={() => previewMutation.mutate(requestPayload)}
                disabled={previewMutation.isPending}
                style={{ border: "none", borderRadius: 999, padding: "8px 14px", fontSize: 12, fontWeight: 700, color: "#fff", background: "#344054", cursor: previewMutation.isPending ? "wait" : "pointer", opacity: previewMutation.isPending ? 0.7 : 1 }}
              >
                {previewMutation.isPending ? "生成中..." : "生成预览"}
              </button>
              <button
                onClick={() => sendMutation.mutate(requestPayload)}
                disabled={sendMutation.isPending || !canSend}
                style={{
                  border: "none",
                  borderRadius: 999,
                  padding: "8px 14px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#fff",
                  background: "#175cd3",
                  cursor: sendMutation.isPending ? "wait" : canSend ? "pointer" : "not-allowed",
                  opacity: sendMutation.isPending || !canSend ? 0.6 : 1,
                }}
              >
                {sendMutation.isPending ? "发送中..." : "立即发送"}
              </button>
              <button
                onClick={() => saveTemplateMutation.mutate()}
                disabled={saveTemplateMutation.isPending || !canSaveTemplate}
                style={{
                  border: "none",
                  borderRadius: 999,
                  padding: "8px 14px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#fff",
                  background: "#0f766e",
                  cursor: saveTemplateMutation.isPending ? "wait" : canSaveTemplate ? "pointer" : "not-allowed",
                  opacity: saveTemplateMutation.isPending || !canSaveTemplate ? 0.6 : 1,
                }}
              >
                {saveTemplateMutation.isPending ? "保存中..." : "保存为模板"}
              </button>
            </div>
            {!canSend && (
              <div style={{ fontSize: 12, color: "#667085" }}>
                立即发送前请至少填写一个收件人邮箱。
              </div>
            )}
            {statusMessage && (
              <div style={{ fontSize: 12, color: statusMessage.includes("失败") ? "#b42318" : "#027a48" }}>{statusMessage}</div>
            )}
          </div>
        </section>
      </div>

      <section style={{ border: "1px solid #d0d5dd", borderRadius: 16, overflow: "hidden", background: "#f8fafc" }}>
        <div style={{ background: "linear-gradient(135deg,#101828 0%,#1d2939 100%)", color: "#f8fafc", padding: "18px 20px" }}>
          <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>HTML 邮件预览</div>
          <div style={{ fontSize: 22, fontWeight: 800, marginBottom: 8 }}>{previewData?.subject ?? subject ?? "技术新闻筛选简报"}</div>
          <div style={{ fontSize: 13, lineHeight: 1.7, color: "#d0d5dd" }}>
            当前筛选生成的即时预览
            <br />
            {previewData ? `共 ${previewData.item_count} 条，准备发送给 ${previewData.recipients.length || 0} 个收件人。` : "点击“生成预览”后展示邮件内容。"}
          </div>
        </div>
        <div style={{ padding: 14, display: "grid", gap: 12, maxHeight: "64vh", overflowY: "auto" }}>
          {previewData?.items.length ? (
            previewData.items.map((item, index) => (
              <div key={`${item.source_url}-${index}`} style={{ background: "#fff", border: "1px solid #eaecf0", borderRadius: 12, padding: "14px 16px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", marginBottom: 8 }}>
                  <div style={{ fontSize: 16, fontWeight: 800, color: "#101828" }}>{item.title}</div>
                  <div style={{ fontSize: 11, color: "#667085", whiteSpace: "nowrap" }}>{item.published_at ?? "-"}</div>
                </div>
                <div style={{ display: "grid", gap: 6, fontSize: 13, color: "#475467", lineHeight: 1.7 }}>
                  <div><strong style={{ color: "#101828" }}>推荐理由：</strong>{item.reason ?? "暂无"}</div>
                  <div><strong style={{ color: "#101828" }}>摘要：</strong>{item.summary ?? "暂无"}</div>
                  <div><strong style={{ color: "#101828" }}>技术要点：</strong>{item.key_points.length ? item.key_points.join("；") : "暂无"}</div>
                  <div><strong style={{ color: "#101828" }}>技术热点：</strong>{item.hotspots.length ? item.hotspots.join(" / ") : "暂无"}</div>
                  <div><strong style={{ color: "#101828" }}>来源链接：</strong><span style={{ color: "#175cd3", wordBreak: "break-all" }}>{item.source_url}</span></div>
                </div>
              </div>
            ))
          ) : (
            <div style={{ border: "1px dashed #d0d5dd", background: "#fff", color: "#667085", borderRadius: 12, padding: 18 }}>
              还没有生成预览内容。
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
