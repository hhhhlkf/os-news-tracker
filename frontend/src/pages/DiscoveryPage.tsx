import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CrawlMethodDetail } from "../components/CrawlMethodDetail";
import { CrawlMethodList } from "../components/CrawlMethodList";
import { CrawlMethodReviewList } from "../components/CrawlMethodReviewList";
import { DiscoveryPanel } from "../components/DiscoveryPanel";
import { MainCategoryPanel } from "../components/MainCategoryPanel";
import { buildNewsRunFormState } from "../components/runLimits";
import { PromptStudioPanel } from "../components/PromptStudioPanel";
import { RunLimitCard } from "../components/RunLimitCard";
import { WechatAuthPanel } from "../components/WechatAuthPanel";

export function DiscoveryPage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const queryClient = useQueryClient();
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [openMethod, setOpenMethod] = useState<number | null>(null);
  const [runLimitState, setRunLimitState] = useState(buildNewsRunFormState);
  const reviewSectionRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (highlightId == null) return;
    const timeoutId = window.setTimeout(() => setHighlightId(null), 4000);
    return () => window.clearTimeout(timeoutId);
  }, [highlightId]);

  async function handleMethodAdded(methodId: number) {
    await queryClient.invalidateQueries({ queryKey: ["discovery-methods"] });
    await queryClient.invalidateQueries({ queryKey: ["discovery-methods", "pending-review"] });
    setHighlightId(methodId);
    window.requestAnimationFrame(() => {
      reviewSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  return (
    <div style={pageShell}>
      <div style={pageInner}>
        <header style={heroCard}>
          <div style={eyebrow}>Site Discovery</div>
          <h1 style={heroTitle}>站点发现</h1>
          <p style={heroCopy}>输入网站，AI 探查员摸清爬取门道，沉淀为可复用的爬取方式。</p>
        </header>

        <div style={runLimitSection}>
          <RunLimitCard
            formState={runLimitState}
            onChange={setRunLimitState}
            description="统一限制当前页的抓取时间范围与总条目数，方式库批量抓取、标准抓取、Agent Crawl 共用。"
          />
        </div>
        <DiscoveryPanel onMethodAdded={handleMethodAdded} />
        {hasSystemAccess && (
          <div ref={reviewSectionRef}>
            <CrawlMethodReviewList highlightId={highlightId} onOpenMethod={setOpenMethod} />
          </div>
        )}
        <CrawlMethodList
          onOpenMethod={setOpenMethod}
          highlightId={highlightId}
          runLimitState={runLimitState}
          allowDelete={hasSystemAccess}
        />
        {hasSystemAccess && <PromptStudioPanel />}
        <MainCategoryPanel />
        {hasSystemAccess && <WechatAuthPanel />}

        {openMethod != null && (
          <CrawlMethodDetail methodId={openMethod} onClose={() => setOpenMethod(null)} allowManage={hasSystemAccess} />
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
  minHeight: 160,
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
  fontSize: 32,
  lineHeight: 1.2,
};

const heroCopy: CSSProperties = {
  marginTop: 10,
  marginBottom: 0,
  color: "#d0d5dd",
  fontSize: 14,
};

const runLimitSection: CSSProperties = {
  marginBottom: 18,
};
