import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchTrendIdentityTemplates } from "../features/trends/api";
import { clampInput, INPUT_LIMITS } from "../inputLimits";
import { emailListError, parseEmailList } from "../mail/emailValidation";
import {
  createMailTemplate,
  fetchMailNoticeConfig,
  previewTrendDistributionMail,
  sendTrendDistributionMail,
  updateMailNoticeConfig,
} from "../mail/api";
import {
  mailProviderLabel,
  type MailPreviewResponse,
  type MailProviderKind,
  type MailTrendPreviewRequest,
  type MailTemplate,
} from "../mail/types";
import { MailNoticeBox } from "./MailNoticeBox";
import { MailTrendPreviewGroups } from "./MailTrendPreviewGroups";

export function MailTrendDistributionPanel(props: { onTemplateSaved?: (template: MailTemplate) => void }) {
  const { onTemplateSaved } = props;
  const queryClient = useQueryClient();
  const [templateId, setTemplateId] = useState("");
  const [subject, setSubject] = useState("技术趋势与热点分发");
  const [recipientsText, setRecipientsText] = useState("");
  const [mailProvider, setMailProvider] = useState<MailProviderKind>("tof4");
  const [previewData, setPreviewData] = useState<MailPreviewResponse | null>(null);
  const [previewHeight, setPreviewHeight] = useState(620);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [docText, setDocText] = useState("");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [includeNotice, setIncludeNotice] = useState(false);
  const leftColumnRef = useRef<HTMLDivElement | null>(null);
  const noticeHydratedRef = useRef(false);

  const templatesQuery = useQuery({
    queryKey: ["trends", "identity-templates"],
    queryFn: fetchTrendIdentityTemplates,
    retry: false,
  });
  const templates = templatesQuery.data ?? [];
  const selectedTemplate = templates.find((template) => template.template_id === templateId) ?? null;

  const noticeQuery = useQuery({
    queryKey: ["mail-notice-config"],
    queryFn: fetchMailNoticeConfig,
    retry: false,
  });

  useEffect(() => {
    if (!noticeQuery.data || noticeHydratedRef.current) return;
    noticeHydratedRef.current = true;
    setDocText(noticeQuery.data.doc_text ?? "");
    setWebsiteUrl(noticeQuery.data.website_url ?? "");
    setIncludeNotice(Boolean(noticeQuery.data.include_on_send || noticeQuery.data.include_on_template));
  }, [noticeQuery.data]);

  const noticeSaveMutation = useMutation({
    mutationFn: updateMailNoticeConfig,
    onSuccess: (data) => queryClient.setQueryData(["mail-notice-config"], data),
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "说明配置保存失败"),
  });

  function persistNotice(patch: {
    doc_text?: string;
    website_url?: string;
    include_notice?: boolean;
  }) {
    const include = patch.include_notice ?? includeNotice;
    noticeSaveMutation.mutate({
      doc_text: patch.doc_text ?? docText,
      website_url: patch.website_url ?? websiteUrl,
      include_on_send: include,
      include_on_template: include,
    });
  }

  const liveNotice = useMemo(() => {
    const text = docText.trim();
    const url = websiteUrl.trim();
    if (!includeNotice || (!text && !url)) return null;
    return { doc_text: text, website_url: url };
  }, [docText, websiteUrl, includeNotice]);

  useEffect(() => {
    if (templateId && templates.some((template) => template.template_id === templateId)) return;
    setTemplateId(templates[0]?.template_id ?? "");
  }, [templateId, templates]);

  useEffect(() => {
    setPreviewData(null);
  }, [templateId, subject, recipientsText, mailProvider, docText, websiteUrl, includeNotice]);

  useEffect(() => {
    const element = leftColumnRef.current;
    if (!element) return;

    const updateHeight = () => setPreviewHeight(Math.ceil(element.getBoundingClientRect().height));
    updateHeight();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateHeight);
      return () => window.removeEventListener("resize", updateHeight);
    }

    const observer = new ResizeObserver(updateHeight);
    observer.observe(element);
    window.addEventListener("resize", updateHeight);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateHeight);
    };
  }, [templateId, subject, recipientsText, mailProvider, docText, websiteUrl, includeNotice, statusMessage, previewData?.item_count]);

  const parsedRecipients = useMemo(() => parseEmailList(recipientsText), [recipientsText]);
  const recipients = parsedRecipients.valid;
  const recipientsError = useMemo(() => emailListError(parsedRecipients), [parsedRecipients]);
  const requestPayload: MailTrendPreviewRequest = {
    template_id: templateId,
    subject: subject.trim() || "技术趋势与热点分发",
    recipients,
    provider: mailProvider,
  };
  const canPreview = Boolean(templateId) && subject.trim().length > 0 && !recipientsError;
  const canSend = canPreview && recipients.length > 0;

  async function flushNoticeConfig() {
    await updateMailNoticeConfig({
      doc_text: docText,
      website_url: websiteUrl,
      include_on_send: includeNotice,
      include_on_template: includeNotice,
    });
  }

  const previewMutation = useMutation({
    mutationFn: async (request: MailTrendPreviewRequest) => {
      await flushNoticeConfig();
      return previewTrendDistributionMail(request);
    },
    onSuccess: (data) => {
      setPreviewData(data);
      setStatusMessage(`预览已生成，共 ${data.item_count} 条趋势，通道：${mailProviderLabel(data.provider)}。`);
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "预览生成失败"),
  });
  const sendMutation = useMutation({
    mutationFn: async (request: MailTrendPreviewRequest) => {
      await flushNoticeConfig();
      return sendTrendDistributionMail(request);
    },
    onSuccess: (data) => {
      setStatusMessage(
        data.status === "sent"
          ? `发送成功，共 ${data.item_count} 条趋势，通道：${mailProviderLabel(data.provider)}。`
          : data.status === "查询空" || data.status === "skipped_empty"
            ? "当前模板最近成功发布中没有可分发的趋势。"
            : `发送失败（${mailProviderLabel(data.provider)}）：${data.error_message ?? "未知错误"}`,
      );
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "发送失败"),
  });
  const saveTemplateMutation = useMutation({
    mutationFn: async () => {
      await flushNoticeConfig();
      return createMailTemplate({
        name: subject.trim() || "趋势与热点分发",
        subject: subject.trim() || "技术趋势与热点分发",
        recipients,
        filter_snapshot: {},
        content_type: "trend_distribution",
        trend_identity_template_id: templateId,
        is_active: true,
      });
    },
    onSuccess: (template) => {
      setStatusMessage(`模板已保存：${template.name}`);
      void queryClient.invalidateQueries({ queryKey: ["mail-templates"] });
      onTemplateSaved?.(template);
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "模板保存失败"),
  });

  return (
    <div style={{ display: "grid", gridTemplateColumns: "0.8fr 1.2fr", gap: 12, alignItems: "start" }}>
      <div ref={leftColumnRef} style={{ display: "grid", gap: 12, alignContent: "start" }}>
        <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14, background: "#fff", height: 145, boxSizing: "border-box" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 10 }}>
            趋势来源
          </div>
          <div style={{ display: "grid", gap: 8 }}>
            <select
              value={templateId}
              onChange={(event) => setTemplateId(event.target.value)}
              disabled={templatesQuery.isLoading || templates.length === 0}
              style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054", background: "#fff" }}
            >
              {templates.length === 0 ? <option value="">暂无身份模板</option> : null}
              {templates.map((template) => <option key={template.template_id} value={template.template_id}>{template.name}</option>)}
            </select>
            <div style={{ fontSize: 12, color: templatesQuery.isError ? "#b42318" : "#667085", lineHeight: 1.55 }}>
              {templatesQuery.isLoading
                ? "正在读取身份模板…"
                : templatesQuery.isError
                  ? "身份模板读取失败，请刷新后重试。"
                  : selectedTemplate
                    ? `按当前模板方向分组：${selectedTemplate.directions.length ? selectedTemplate.directions.join(" / ") : "未配置方向"}`
                    : "选择身份模板后，将按其最近成功发布的趋势轮播结果发送。"}
            </div>
          </div>
        </section>

        <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: "10px 12px", background: "#fff" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 8 }}>
            发送设置
          </div>
          <div style={{ display: "grid", gap: 8 }}>
            <input
              value={subject}
              maxLength={INPUT_LIMITS.subject}
              onChange={(event) => setSubject(clampInput(event.target.value, INPUT_LIMITS.subject))}
              placeholder="邮件标题"
              style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054" }}
            />
            <textarea
              value={recipientsText}
              maxLength={INPUT_LIMITS.emailListMultiline}
              onChange={(event) => setRecipientsText(clampInput(event.target.value, INPUT_LIMITS.emailListMultiline))}
              placeholder="收件人邮箱，支持换行、逗号或空格分隔"
              rows={3}
              style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054", resize: "vertical" }}
            />
            <div style={{ display: "grid", gap: 5 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送通道</span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {(["tof4", "smtp"] as const).map((provider) => {
                  const active = mailProvider === provider;
                  return (
                    <button
                      key={provider}
                      type="button"
                      onClick={() => setMailProvider(provider)}
                      style={{
                        border: active ? "1px solid #175cd3" : "1px solid #d0d5dd",
                        borderRadius: 999,
                        padding: "6px 11px",
                        fontSize: 12,
                        fontWeight: 700,
                        color: active ? "#175cd3" : "#475467",
                        background: active ? "#eff8ff" : "#fff",
                        cursor: "pointer",
                      }}
                    >
                      {mailProviderLabel(provider)}
                    </button>
                  );
                })}
              </div>
            </div>
            <div style={{ borderTop: "1px solid #eaecf0", paddingTop: 8, display: "grid", gap: 7 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>
                说明文档与网站链接
                <span style={{ marginLeft: 8, fontWeight: 400, color: "#98a2b3" }}>题头下、新闻前</span>
              </div>
              <textarea
                value={docText}
                maxLength={INPUT_LIMITS.mailDocText}
                onChange={(event) => setDocText(clampInput(event.target.value, INPUT_LIMITS.mailDocText))}
                onBlur={() => persistNotice({ doc_text: docText })}
                placeholder="说明文档内容，例如使用说明、订阅须知"
                rows={2}
                style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054", resize: "vertical" }}
              />
              <input
                value={websiteUrl}
                maxLength={INPUT_LIMITS.mailWebsiteUrl}
                onChange={(event) => setWebsiteUrl(clampInput(event.target.value, INPUT_LIMITS.mailWebsiteUrl))}
                onBlur={() => persistNotice({ website_url: websiteUrl })}
                placeholder="网站链接，例如 https://example.com/docs"
                style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "8px 10px", fontSize: 13, color: "#344054" }}
              />
              <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "#344054", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={includeNotice}
                  onChange={(event) => {
                    const next = event.target.checked;
                    setIncludeNotice(next);
                    persistNotice({ include_notice: next });
                  }}
                />
                附加此说明框
              </label>
              {noticeSaveMutation.isPending ? <div style={{ fontSize: 12, color: "#98a2b3" }}>说明配置保存中…</div> : null}
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <button
                type="button"
                onClick={() => previewMutation.mutate(requestPayload)}
                disabled={previewMutation.isPending || !canPreview}
                style={{
                  border: "1px solid #b2ddff", borderRadius: 10, padding: "9px 14px", fontSize: 12, fontWeight: 700,
                  color: "#175cd3", background: "linear-gradient(180deg, #f5faff 0%, #eff8ff 100%)",
                  cursor: previewMutation.isPending || !canPreview ? "not-allowed" : "pointer",
                  opacity: previewMutation.isPending || !canPreview ? 0.55 : 1,
                }}
              >
                {previewMutation.isPending ? "生成中..." : "生成预览"}
              </button>
              <button
                type="button"
                onClick={() => sendMutation.mutate(requestPayload)}
                disabled={sendMutation.isPending || !canSend}
                style={{
                  border: "1px solid #84caff", borderRadius: 10, padding: "9px 16px", fontSize: 12, fontWeight: 700,
                  color: "#fff", background: "linear-gradient(180deg, #53b1fd 0%, #2e90fa 100%)",
                  cursor: sendMutation.isPending || !canSend ? "not-allowed" : "pointer",
                  opacity: sendMutation.isPending || !canSend ? 0.55 : 1,
                }}
              >
                {sendMutation.isPending ? "发送中..." : "立即发送"}
              </button>
              <button
                type="button"
                onClick={() => saveTemplateMutation.mutate()}
                disabled={saveTemplateMutation.isPending || !canPreview}
                style={{
                  border: "1px solid #1849a9", borderRadius: 10, padding: "9px 14px", fontSize: 12, fontWeight: 700,
                  color: "#fff", background: "linear-gradient(180deg, #175cd3 0%, #1849a9 100%)",
                  cursor: saveTemplateMutation.isPending || !canPreview ? "not-allowed" : "pointer",
                  opacity: saveTemplateMutation.isPending || !canPreview ? 0.55 : 1,
                }}
              >
                {saveTemplateMutation.isPending ? "保存中..." : "保存为模板"}
              </button>
            </div>
            {recipientsError ? <div style={{ fontSize: 12, color: "#b42318" }}>{recipientsError}</div> : null}
            {!recipientsError && !canSend ? <div style={{ fontSize: 12, color: "#667085" }}>立即发送前请至少填写一个有效收件人邮箱。</div> : null}
            {statusMessage ? (
              <div style={{ display: "flex", alignItems: "flex-start", gap: 6, fontSize: 12, color: statusMessage.includes("失败") ? "#b42318" : "#027a48" }}>
                <span style={{ flex: 1 }}>{statusMessage}</span>
                <button type="button" onClick={() => setStatusMessage(null)} aria-label="关闭提示" title="关闭" style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", fontSize: 14, lineHeight: 1, padding: 0, opacity: 0.7 }}>×</button>
              </div>
            ) : null}
          </div>
        </section>
      </div>

      <section style={{ border: "1px solid #d0d5dd", borderRadius: 16, overflow: "hidden", background: "#f8fafc", alignSelf: "start", height: `${previewHeight}px`, maxHeight: `${previewHeight}px`, minHeight: 0, display: "grid", gridTemplateRows: "auto minmax(0, 1fr)" }}>
        <div style={{ background: "linear-gradient(135deg,#101828 0%,#1d2939 100%)", color: "#f8fafc", padding: "18px 20px" }}>
          <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>HTML 邮件预览</div>
          <div style={{ fontSize: 22, fontWeight: 800, marginBottom: 8 }}>{previewData?.subject ?? subject ?? "技术趋势与热点分发"}</div>
          <div style={{ fontSize: 13, lineHeight: 1.7, color: "#d0d5dd" }}>
            {selectedTemplate ? `身份模板：${selectedTemplate.name}` : "请选择身份模板"}
            <br />
            {previewData ? `共 ${previewData.item_count} 条趋势，准备发送给 ${previewData.recipients.length} 个收件人。` : "趋势按方向分点；每个趋势和其新闻卡片均默认收起。"}
          </div>
        </div>
        <div style={{ padding: 14, display: "grid", gap: 12, minHeight: 0, overflowY: "auto", alignContent: "start" }}>
          {previewData?.trend_groups.length ? (
            <MailTrendPreviewGroups groups={previewData.trend_groups} />
          ) : (
            <div style={{ border: "1px dashed #d0d5dd", background: "#fff", color: "#667085", borderRadius: 12, padding: 18 }}>
              还没有生成趋势分发预览。
            </div>
          )}
          <MailNoticeBox notice={previewData?.notice ?? liveNotice} />
        </div>
      </section>
    </div>
  );
}
