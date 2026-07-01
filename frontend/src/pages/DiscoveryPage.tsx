import { useEffect, useState, type CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CrawlMethodDetail } from "../components/CrawlMethodDetail";
import { CrawlMethodList } from "../components/CrawlMethodList";
import { DiscoveryPanel } from "../components/DiscoveryPanel";

export function DiscoveryPage() {
  const queryClient = useQueryClient();
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [openMethod, setOpenMethod] = useState<number | null>(null);

  useEffect(() => {
    if (highlightId == null) return;
    const timeoutId = window.setTimeout(() => setHighlightId(null), 4000);
    return () => window.clearTimeout(timeoutId);
  }, [highlightId]);

  async function handleMethodAdded(methodId: number) {
    await queryClient.invalidateQueries({ queryKey: ["discovery-methods"] });
    setHighlightId(methodId);
  }

  return (
    <div style={pageShell}>
      <div style={pageInner}>
        <header style={heroCard}>
          <div style={eyebrow}>OS News Tracker</div>
          <h1 style={heroTitle}>站点发现</h1>
          <p style={heroCopy}>输入网站，AI 探查员摸清爬取门道，沉淀为可复用的爬取方式。</p>
        </header>

        <DiscoveryPanel onMethodAdded={handleMethodAdded} />
        <CrawlMethodList onOpenMethod={setOpenMethod} highlightId={highlightId} />

        {openMethod != null && (
          <CrawlMethodDetail methodId={openMethod} onClose={() => setOpenMethod(null)} />
        )}
      </div>
    </div>
  );
}

const pageShell: CSSProperties = {
  minHeight: "100vh",
  background:
    "radial-gradient(circle at top left, rgba(23,92,211,0.12), transparent 32%), linear-gradient(180deg, #f7f9fc 0%, #eef2f7 100%)",
};

const pageInner: CSSProperties = {
  maxWidth: 1280,
  margin: "0 auto",
  padding: 24,
};

const heroCard: CSSProperties = {
  background: "linear-gradient(135deg, #0f172a 0%, #101828 55%, #1d2939 100%)",
  color: "#f8fafc",
  borderRadius: 8,
  padding: 24,
  marginBottom: 20,
  boxShadow: "0 18px 40px rgba(15, 23, 42, 0.14)",
};

const eyebrow: CSSProperties = {
  fontSize: 13,
  color: "#98a2b3",
  marginBottom: 10,
};

const heroTitle: CSSProperties = {
  margin: 0,
  fontSize: 28,
  lineHeight: 1.2,
};

const heroCopy: CSSProperties = {
  marginTop: 10,
  marginBottom: 0,
  color: "#d0d5dd",
};
