function parseTechHighlight(text: string): { keyword: string; detail: string } | null {
  const match = text.match(/^\[(.+?)\]\s*(.*)/);
  if (match) {
    return { keyword: match[1], detail: match[2] };
  }
  return null;
}

function domainFromUrl(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function truncateLinkText(text: string): string {
  return text.length > 10 ? `${text.slice(0, 9)}…` : text;
}

export function formatDateYmd(value: string | null | undefined): string {
  if (!value) return "-";
  return value.slice(0, 10);
}

export function SourceCta({ url }: { url: string }) {
  const domain = domainFromUrl(url);

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={url}
      aria-label={`阅读原文：${url}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "6px 9px",
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        background: "#fff",
        color: "#344054",
        fontSize: 12,
        textDecoration: "none",
        lineHeight: 1,
        maxWidth: 168,
        width: "fit-content",
        whiteSpace: "nowrap",
      }}
    >
      <span>阅读原文</span>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width={14}
        height={14}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
        <polyline points="15 3 21 3 21 9" />
        <line x1="10" y1="14" x2="21" y2="3" />
      </svg>
      <span style={{ color: "#98a2b3", maxWidth: 80, overflow: "hidden", textOverflow: "ellipsis" }}>
        {truncateLinkText(domain)}
      </span>
    </a>
  );
}

export function TechHighlightsList(props: { items: string[]; compact?: boolean }) {
  const { items, compact = false } = props;
  const visibleItems = items.filter((item) => item && !item.startsWith("__type:"));

  if (visibleItems.length === 0) {
    return <div style={{ fontSize: 13, color: "#98a2b3" }}>暂无</div>;
  }

  return (
    <div>
      {visibleItems.map((item, index) => {
        const parsed = parseTechHighlight(item);
        return (
          <div
            key={`${item}-${index}`}
            style={{
              border: "1px solid #eaecf0",
              borderRadius: 8,
              padding: compact ? "7px 10px" : "8px 12px",
              marginBottom: 6,
              background: "#fafafa",
              fontSize: 13,
              color: "#475467",
              lineHeight: 1.65,
            }}
          >
            {parsed ? (
              <>
                <strong style={{ color: "#175cd3" }}>[{parsed.keyword}]</strong>{" "}
                {parsed.detail}
              </>
            ) : (
              item
            )}
          </div>
        );
      })}
    </div>
  );
}

export function HotspotTags(props: { tags: string[] }) {
  const { tags } = props;
  if (tags.length === 0) {
    return <div style={{ fontSize: 13, color: "#98a2b3" }}>暂无</div>;
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {tags.map((tag, index) => (
        <span
          key={`${tag}-${index}`}
          style={{
            background: "#f2f4f7",
            borderRadius: 6,
            padding: "2px 8px",
            fontSize: 12,
            color: "#344054",
          }}
        >
          {tag}
        </span>
      ))}
    </div>
  );
}
