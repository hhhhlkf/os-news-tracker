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
        borderRadius: 10,
        padding: "10px 12px",
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        ...style,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 800,
          color: "#667085",
          whiteSpace: "nowrap",
        }}
      >
        说明与链接
      </div>
      {docText && <div style={{ minWidth: 0, flex: "1 1 220px", fontSize: 12, lineHeight: 1.45, color: "#475467", whiteSpace: "pre-wrap" }}>{docText}</div>}
      {websiteUrl && <a href={websiteUrl} target="_blank" rel="noreferrer" style={{ color: "#175cd3", fontSize: 12, fontWeight: 700, textDecoration: "none", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "100%" }}>{websiteUrl}</a>}
    </section>
  );
}
