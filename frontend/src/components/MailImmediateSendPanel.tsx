import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { fetchItems, type ItemQueryParams } from "../api/client";
import {
  buildMailFilterSnapshot,
  createMailTemplate,
  previewImmediateMail,
  sendImmediateMail,
} from "../mail/api";
import type { MailImmediateSendRequest, MailPreviewResponse, MailProviderKind, MailTemplate } from "../mail/types";
import { ImportanceBadge } from "./ImportanceBadge";
import { formatDateYmd, HotspotTags, SourceCta, SourceQualityMeta, TechHighlightsList } from "./ItemMetaBlocks";

export function MailImmediateSendPanel(props: {
  homeFilters: ItemQueryParams;
  onTemplateSaved?: (template: MailTemplate) => void;
}) {
  const { homeFilters, onTemplateSaved } = props;
  const [subject, setSubject] = useState("技术新闻筛选简报");
  const [recipientsText, setRecipientsText] = useState("");
  const [sendTime, setSendTime] = useState("09:00");
  const [sendFrequency, setSendFrequency] = useState<"once" | "daily" | "weekly">("once");
  const [mailProvider, setMailProvider] = useState<MailProviderKind>("tof4");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<MailPreviewResponse | null>(null);
  const [previewHeight, setPreviewHeight] = useState<number>(620);
  const leftColumnRef = useRef<HTMLDivElement | null>(null);

  const filterSnapshot = useMemo(() => buildMailFilterSnapshot(homeFilters), [homeFilters]);

  const estimateParams = useMemo(
    () => ({
      ...homeFilters,
      limit: 1,
      offset: 0,
    }),
    [homeFilters],
  );

  const estimateQuery = useQuery({
    queryKey: ["mail-immediate-estimate", estimateParams],
    queryFn: () => fetchItems(estimateParams),
    retry: false,
  });

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
  }, [homeFilters, subject, recipientsText, mailProvider]);

  useEffect(() => {
    const element = leftColumnRef.current;
    if (!element) return;

    const updateHeight = () => {
      setPreviewHeight(Math.ceil(element.getBoundingClientRect().height));
    };

    updateHeight();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateHeight);
      return () => window.removeEventListener("resize", updateHeight);
    }

    const observer = new ResizeObserver(() => updateHeight());
    observer.observe(element);
    window.addEventListener("resize", updateHeight);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateHeight);
    };
  }, [estimateQuery.data?.total, recipientsText, subject, statusMessage, previewData?.item_count, homeFilters, mailProvider]);

  const previewMutation = useMutation({
    mutationFn: async (request: MailImmediateSendRequest) => previewImmediateMail(request),
    onSuccess: (data) => {
      setStatusMessage(`预览已生成，共 ${data.item_count} 条，通道：${data.provider.toUpperCase()}。`);
      setPreviewData(data);
    },
    onError: (error) => {
      setStatusMessage(error instanceof Error ? error.message : "预览生成失败");
    },
  });

  const sendMutation = useMutation({
    mutationFn: async (request: MailImmediateSendRequest) => sendImmediateMail(request),
    onSuccess: (data) => {
      setStatusMessage(
        data.status === "sent"
          ? `发送成功，共 ${data.item_count} 条，通道：${data.provider.toUpperCase()}。`
          : `发送失败（${data.provider.toUpperCase()}）：${data.error_message ?? "未知错误"}`,
      );
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
    provider: mailProvider,
  };
  const canSend = recipients.length > 0 && subject.trim().length > 0;
  const canSaveTemplate = subject.trim().length > 0;
  const relativeWindow = useMemo(() => {
    if (filterSnapshot.published_after_mode !== "relative") return null;
    if (filterSnapshot.published_after_value === "24h") return "最近 24h";
    if (filterSnapshot.published_after_value === "7d") return "最近 7d";
    if (filterSnapshot.published_after_value === "30d") return "最近 30d";
    return null;
  }, [filterSnapshot.published_after_mode, filterSnapshot.published_after_value]);

  const queryTimeWindow = useMemo(() => {
    if (filterSnapshot.fetched_after_mode !== "relative") return null;
    if (filterSnapshot.fetched_after_value === "24h") return "最近 24h";
    if (filterSnapshot.fetched_after_value === "7d") return "最近 7天";
    if (filterSnapshot.fetched_after_value === "30d") return "最近 30天";
    return null;
  }, [filterSnapshot.fetched_after_mode, filterSnapshot.fetched_after_value]);

  const snapshotSummary = useMemo(() => {
    const rows: Array<{ label: string; value: string }> = [];
    if (filterSnapshot.q) {
      rows.push({ label: "关键词", value: filterSnapshot.q });
    }
    const formatMulti = (raw: string | null | undefined) =>
      (raw ?? "").split(",").map((part) => part.trim()).filter(Boolean).join(" / ");
    const mainCategory = formatMulti(filterSnapshot.main_category);
    const importance = formatMulti(filterSnapshot.importance);
    const subTag = formatMulti(filterSnapshot.sub_tag);
    if (mainCategory) {
      rows.push({ label: "主分类", value: mainCategory });
    }
    if (filterSnapshot.info_type) {
      rows.push({ label: "信息类型", value: filterSnapshot.info_type });
    }
    if (importance) {
      rows.push({ label: "重要程度", value: importance });
    }
    if (subTag) {
      rows.push({ label: "技术热点", value: subTag });
    }

    let publishedLabel = "全部时间";
    if (relativeWindow) {
      publishedLabel = relativeWindow;
    } else if (filterSnapshot.published_after || filterSnapshot.published_before) {
      const afterLabel = filterSnapshot.published_after ? `从 ${filterSnapshot.published_after}` : "";
      const beforeLabel = filterSnapshot.published_before ? `到 ${filterSnapshot.published_before}` : "";
      publishedLabel = `${afterLabel}${afterLabel && beforeLabel ? " " : ""}${beforeLabel}`.trim();
    }
    rows.push({ label: "发布时间", value: publishedLabel });

    if (relativeWindow) {
      rows.push({ label: "窗口说明", value: `发送时按当下时间重算 ${relativeWindow.replace("最近 ", "")} 窗口` });
    }

    let fetchedLabel = "全部时间";
    if (queryTimeWindow) {
      fetchedLabel = queryTimeWindow;
    } else if (filterSnapshot.fetched_after || filterSnapshot.fetched_before) {
      const afterLabel = filterSnapshot.fetched_after ? `从 ${filterSnapshot.fetched_after}` : "";
      const beforeLabel = filterSnapshot.fetched_before ? `到 ${filterSnapshot.fetched_before}` : "";
      fetchedLabel = `${afterLabel}${afterLabel && beforeLabel ? " " : ""}${beforeLabel}`.trim();
    }
    rows.push({ label: "查询时间", value: fetchedLabel });

    if (queryTimeWindow) {
      rows.push({ label: "查询说明", value: `发送时按当下时间重算 ${queryTimeWindow.replace("最近 ", "")} 入库窗口` });
    }

    const sortByLabel = filterSnapshot.sort_by === "fetched_at" ? "入库时间" : "发布时间";
    const sortDirLabel = filterSnapshot.sort_dir === "asc" ? "最早优先" : "最新优先";
    rows.push({ label: "排序方式", value: `${sortByLabel} / ${sortDirLabel}` });

    return rows;
  }, [filterSnapshot, queryTimeWindow, relativeWindow]);

  const previewHeaderSummary = useMemo(() => {
    const formatMulti = (raw: string | null | undefined) =>
      (raw ?? "").split(",").map((part) => part.trim()).filter(Boolean).join(" / ");
    const segments = [
      formatMulti(filterSnapshot.main_category) || null,
      formatMulti(filterSnapshot.importance) || null,
      formatMulti(filterSnapshot.sub_tag) || null,
      filterSnapshot.q?.trim(),
      relativeWindow ??
        (filterSnapshot.published_after || filterSnapshot.published_before
          ? snapshotSummary.find((row) => row.label === "发布时间")?.value
          : null),
      queryTimeWindow ? `查询${queryTimeWindow}` : null,
    ].filter((value): value is string => !!value);

    if (segments.length > 0) {
      return segments.join(" · ");
    }
    return "依据当前筛选快照生成";
  }, [
    filterSnapshot.main_category,
    filterSnapshot.importance,
    filterSnapshot.sub_tag,
    filterSnapshot.q,
    filterSnapshot.published_after,
    filterSnapshot.published_after_mode,
    filterSnapshot.published_after_value,
    filterSnapshot.published_before,
    queryTimeWindow,
    relativeWindow,
    snapshotSummary,
  ]);

  const sendFrequencyLabel = useMemo(() => {
    if (sendFrequency === "daily") return "每日";
    if (sendFrequency === "weekly") return "每周";
    return "单次发送";
  }, [sendFrequency]);

  const providerLabel = useMemo(() => (mailProvider === "tof4" ? "TOF4 API" : "SMTP"), [mailProvider]);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "0.8fr 1.2fr", gap: 12, alignItems: "start" }}>
      <div ref={leftColumnRef} style={{ display: "grid", gap: 12 }}>
        <section style={{ border: "1px solid #eaecf0", borderRadius: 12, padding: 14, background: "#fff" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", color: "#667085", fontWeight: 700, marginBottom: 10 }}>
            当前筛选快照
          </div>
          <div style={{ display: "grid", gap: 8 }}>
            {snapshotSummary.map((row) => (
              <div
                key={row.label}
                style={{
                  display: "grid",
                  gridTemplateColumns: "84px minmax(0,1fr)",
                  gap: 10,
                  alignItems: "start",
                  fontSize: 12,
                  lineHeight: 1.45,
                }}
              >
                <div style={{ color: "#667085", fontWeight: 700 }}>{row.label}</div>
                <div style={{ color: "#344054" }}>{row.value}</div>
              </div>
            ))}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "84px minmax(0,1fr)",
                gap: 10,
                alignItems: "start",
                fontSize: 12,
                lineHeight: 1.45,
              }}
            >
              <div style={{ color: "#667085", fontWeight: 700 }}>预计纳入</div>
              <div style={{ color: estimateQuery.isError ? "#b42318" : "#344054" }}>
                {estimateQuery.isLoading
                  ? "计算中..."
                  : estimateQuery.isError
                    ? "查询失败"
                    : `${estimateQuery.data?.total ?? 0} 条`}
              </div>
            </div>
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
              <label style={{ display: "grid", gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送时间</span>
                <input
                  type="time"
                  value={sendTime}
                  onChange={(e) => setSendTime(e.target.value)}
                  style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054", background: "#fff" }}
                />
              </label>
              <label style={{ display: "grid", gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>频率</span>
                <select
                  value={sendFrequency}
                  onChange={(e) => setSendFrequency(e.target.value as "once" | "daily" | "weekly")}
                  style={{ border: "1px solid #d0d5dd", borderRadius: 10, padding: "10px 12px", fontSize: 13, color: "#344054", background: "#fff" }}
                >
                  <option value="once">单次发送</option>
                  <option value="daily">每日</option>
                  <option value="weekly">每周</option>
                </select>
              </label>
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>发送通道</span>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {([
                  { value: "tof4", label: "TOF4 API" },
                  { value: "smtp", label: "SMTP" },
                ] as const).map((option) => {
                  const active = mailProvider === option.value;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setMailProvider(option.value)}
                      style={{
                        border: active ? "1px solid #175cd3" : "1px solid #d0d5dd",
                        borderRadius: 999,
                        padding: "8px 12px",
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
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <button
                onClick={() => previewMutation.mutate(requestPayload)}
                disabled={previewMutation.isPending}
                style={{
                  border: "1px solid #b2ddff",
                  borderRadius: 10,
                  padding: "9px 14px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#175cd3",
                  background: "linear-gradient(180deg, #f5faff 0%, #eff8ff 100%)",
                  cursor: previewMutation.isPending ? "wait" : "pointer",
                  opacity: previewMutation.isPending ? 0.7 : 1,
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.9)",
                }}
              >
                {previewMutation.isPending ? "生成中..." : "生成预览"}
              </button>
              <button
                onClick={() => sendMutation.mutate(requestPayload)}
                disabled={sendMutation.isPending || !canSend}
                style={{
                  border: "1px solid #84caff",
                  borderRadius: 10,
                  padding: "9px 16px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#fff",
                  background: "linear-gradient(180deg, #53b1fd 0%, #2e90fa 100%)",
                  cursor: sendMutation.isPending ? "wait" : canSend ? "pointer" : "not-allowed",
                  opacity: sendMutation.isPending || !canSend ? 0.55 : 1,
                  boxShadow: "0 1px 0 rgba(255,255,255,0.25) inset, 0 4px 12px rgba(46, 144, 250, 0.22)",
                }}
              >
                {sendMutation.isPending ? "发送中..." : "立即发送"}
              </button>
              <button
                onClick={() => saveTemplateMutation.mutate()}
                disabled={saveTemplateMutation.isPending || !canSaveTemplate}
                style={{
                  border: "1px solid #1849a9",
                  borderRadius: 10,
                  padding: "9px 14px",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#fff",
                  background: "linear-gradient(180deg, #175cd3 0%, #1849a9 55%, #194185 100%)",
                  cursor: saveTemplateMutation.isPending ? "wait" : canSaveTemplate ? "pointer" : "not-allowed",
                  opacity: saveTemplateMutation.isPending || !canSaveTemplate ? 0.55 : 1,
                  boxShadow: "0 1px 0 rgba(255,255,255,0.18) inset, 0 6px 14px rgba(24, 73, 169, 0.28)",
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
      </div>

      <section
        style={{
          border: "1px solid #d0d5dd",
          borderRadius: 16,
          overflow: "hidden",
          background: "#f8fafc",
          alignSelf: "start",
          height: `${previewHeight}px`,
          maxHeight: `${previewHeight}px`,
          minHeight: 0,
          display: "grid",
          gridTemplateRows: "auto minmax(0, 1fr)",
        }}
      >
        <div style={{ background: "linear-gradient(135deg,#101828 0%,#1d2939 100%)", color: "#f8fafc", padding: "18px 20px" }}>
          <div style={{ fontSize: 12, color: "#98a2b3", marginBottom: 6 }}>HTML 邮件预览</div>
          <div style={{ fontSize: 22, fontWeight: 800, marginBottom: 8 }}>{previewData?.subject ?? subject ?? "技术新闻筛选简报"}</div>
          <div style={{ fontSize: 13, lineHeight: 1.7, color: "#d0d5dd" }}>
            筛选条件：{previewHeaderSummary}
            <br />
            发送时间：{sendTime} · 频率：{sendFrequencyLabel} · 通道：{previewData?.provider?.toUpperCase() ?? providerLabel}
            <br />
            {previewData ? `共 ${previewData.item_count} 条，准备发送给 ${previewData.recipients.length || 0} 个收件人。` : "点击“生成预览”后展示邮件内容。"}
          </div>
        </div>
        <div style={{ padding: 14, display: "grid", gap: 12, minHeight: 0, overflowY: "auto" }}>
          {previewData?.items.length ? (
            previewData.items.map((item, index) => (
              <div key={`${item.source_url}-${index}`} style={{ background: "#fff", border: "1px solid #eaecf0", borderRadius: 12, padding: "14px 16px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", marginBottom: 8 }}>
                  <div style={{ fontSize: 16, fontWeight: 800, color: "#101828" }}>{item.title}</div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", whiteSpace: "nowrap" }}>
                    <ImportanceBadge value={item.importance} />
                    <div style={{ fontSize: 11, color: "#667085" }}>{formatDateYmd(item.published_at)}</div>
                  </div>
                </div>
                <div style={{ display: "grid", gap: 10, fontSize: 13, color: "#475467", lineHeight: 1.7 }}>
                  <div><strong style={{ color: "#101828" }}>摘要：</strong>{item.summary ?? "暂无"}</div>
                  <div>
                    <strong style={{ color: "#101828", display: "block", marginBottom: 6 }}>技术要点</strong>
                    <TechHighlightsList items={item.key_points} compact />
                  </div>
                  <div>
                    <strong style={{ color: "#101828", display: "block", marginBottom: 6 }}>技术热点</strong>
                    <HotspotTags tags={item.hotspots} />
                  </div>
                  <div>
                    <strong style={{ color: "#101828", display: "block", marginBottom: 6 }}>来源链接</strong>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <SourceCta url={item.source_url} />
                      <SourceQualityMeta
                        sourceName={item.source_name}
                        score={item.source_quality_score}
                        grade={item.source_quality_grade}
                        status={item.source_quality_status}
                      />
                    </div>
                  </div>
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
