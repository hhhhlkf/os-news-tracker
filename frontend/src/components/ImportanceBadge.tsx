const COLORS: Record<string, { bg: string; fg: string }> = {
  "高": { bg: "#fde2e1", fg: "#b42318" },
  "中": { bg: "#fef3c7", fg: "#92400e" },
  "低": { bg: "#eceef1", fg: "#475467" },
};

export function ImportanceBadge({ value }: { value: string | null }) {
  if (!value) return null;
  const c = COLORS[value] ?? COLORS["低"];
  return (
    <span style={{
      background: c.bg, color: c.fg, padding: "2px 8px",
      borderRadius: 12, fontSize: 12, fontWeight: 600,
    }}>{value}</span>
  );
}
