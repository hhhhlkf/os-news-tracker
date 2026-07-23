import type { CSSProperties } from "react";
import type { MailNoticeBlock } from "../mail/types";

export function MailNoticeBox({
  notice,
  style,
}: {
  notice: MailNoticeBlock | null | undefined;
  style?: CSSProperties;
}) {
  if (!notice) return null;
  const docText = notice.doc_text?.trim() ?? "";
  const websiteUrl = notice.website_url?.trim() ?? "";
  if (!docText && !websiteUrl) return null;

  return (
    <section
      style={{
        background: "#fff",
        border: "1px solid #d0d5dd",
        borderRadius: 12,
        padding: "14px 16px",
        display: "grid",
        gap: 12,
        ...style,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 800,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "#667085",
        }}
      >
        说明与链接
      </div>
      <div style={{ display: "grid", gap: 10 }}>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#475467", marginBottom: 4 }}>说明文档</div>
          <div
            style={{
              fontSize: 13,
              lineHeight: 1.7,
              color: docText ? "#344054" : "#98a2b3",
              whiteSpace: "pre-wrap",
            }}
          >
            {docText || "暂无说明文档"}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#475467", marginBottom: 4 }}>网站链接</div>
          {websiteUrl ? (
            <a
              href={websiteUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                color: "#175cd3",
                fontSize: 13,
                fontWeight: 700,
                textDecoration: "none",
                wordBreak: "break-all",
              }}
            >
              {websiteUrl}
            </a>
          ) : (
            <div style={{ fontSize: 13, color: "#98a2b3" }}>暂无网站链接</div>
          )}
        </div>
      </div>
    </section>
  );
}
