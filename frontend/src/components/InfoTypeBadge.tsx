export function InfoTypeBadge({ value }: { value: string | null }) {
  if (!value) return null;
  return (
    <span style={{
      border: "1px solid #d0d5dd", color: "#344054",
      padding: "2px 8px", borderRadius: 6, fontSize: 12,
    }}>{value}</span>
  );
}
