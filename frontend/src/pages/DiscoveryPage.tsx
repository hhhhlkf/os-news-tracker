import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CrawlMethodDetail } from "../components/CrawlMethodDetail";
import { CrawlMethodList } from "../components/CrawlMethodList";
import { CrawlMethodReviewList } from "../components/CrawlMethodReviewList";
import { DiscoveryPanel } from "../components/DiscoveryPanel";
import { DiscussionDiscoveryPanel, type TechnicalDiscoveryTab } from "../components/DiscussionDiscoveryPanel";
import type { DiscussionPipelineRun } from "../discussions/api";
import { GitHubRepositoryLibraryList } from "../components/GitHubRepositoryLibraryList";
import { GitHubRepositoryReviewList } from "../components/GitHubRepositoryReviewList";
import { DiscussionSourceLibraryList } from "../components/DiscussionSourceLibraryList";
import { DiscussionSourceReviewList } from "../components/DiscussionSourceReviewList";
import { MainCategoryPanel } from "../components/MainCategoryPanel";
import { buildNewsRunFormState } from "../components/runLimits";
import { PromptStudioPanel } from "../components/PromptStudioPanel";
import { RunLimitCard } from "../components/RunLimitCard";
import { ReviewReminderSettings } from "../components/ReviewReminderSettings";
import { WechatAuthPanel } from "../components/WechatAuthPanel";

const DISCUSSION_RUN_STORAGE_KEY = "os-news-tracker.discussion-run-id";

function readDiscussionRunId(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(DISCUSSION_RUN_STORAGE_KEY);
}

export function DiscoveryPage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const queryClient = useQueryClient();
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [openMethod, setOpenMethod] = useState<number | null>(null);
  const [runLimitState, setRunLimitState] = useState(buildNewsRunFormState);
  const [reviewTab, setReviewTab] = useState<"crawl" | "discussion" | "github">("crawl");
  const [reviewExpanded, setReviewExpanded] = useState(true);
  const [libraryTab, setLibraryTab] = useState<"crawl" | "discussion" | "github">("crawl");
  const [libraryExpanded, setLibraryExpanded] = useState(true);
  const reviewSectionRef = useRef<HTMLDivElement | null>(null);
  const technicalPanelRef = useRef<HTMLDivElement | null>(null);
  const [technicalTab, setTechnicalTab] = useState<TechnicalDiscoveryTab>("email");
  const [discussionRunId, setDiscussionRunId] = useState<string | null>(readDiscussionRunId);

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

  function openGitHubRunPanel(run: DiscussionPipelineRun): void {
    queryClient.setQueryData(["discussion-latest-run"], run);
    window.sessionStorage.setItem(DISCUSSION_RUN_STORAGE_KEY, run.id);
    setDiscussionRunId(run.id);
    setTechnicalTab("github");
    window.requestAnimationFrame(() => {
      technicalPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function openMailRunPanel(run: DiscussionPipelineRun): void {
    queryClient.setQueryData(["discussion-latest-run"], run);
    window.sessionStorage.setItem(DISCUSSION_RUN_STORAGE_KEY, run.id);
    setDiscussionRunId(run.id);
    setTechnicalTab("email");
    window.requestAnimationFrame(() => {
      technicalPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
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
            description="统一限制当前页的抓取时间范围与总条目数，方式库批量抓取、标准抓取、Agent Crawl 与 GitHub 技术探查共用。"
          />
        </div>
        <DiscoveryPanel onMethodAdded={handleMethodAdded} />
        <div ref={technicalPanelRef}><DiscussionDiscoveryPanel runLimitState={runLimitState} runId={discussionRunId} technicalTab={technicalTab} onTechnicalTabChange={setTechnicalTab} canManage={hasSystemAccess} /></div>
        {hasSystemAccess && (
          <section ref={reviewSectionRef} style={sourceModule}>
            <div style={tabHeader}>
              <span style={tabHeaderLabel}>待审核方式</span>
              <button type="button" onClick={() => setReviewTab("crawl")} style={reviewTab === "crawl" ? tabActive : tabIdle}>爬取方式</button>
              <button type="button" onClick={() => setReviewTab("discussion")} style={reviewTab === "discussion" ? tabActive : tabIdle}>邮件探查</button>
              <button type="button" onClick={() => setReviewTab("github")} style={reviewTab === "github" ? tabActive : tabIdle}>GitHub 探查</button>
              <button type="button" onClick={() => setReviewExpanded((value) => !value)} style={moduleToggle}>{reviewExpanded ? "收起" : "展开"}</button>
            </div>
            {reviewExpanded && <>
              <ReviewReminderSettings />
              {reviewTab === "crawl" ? <CrawlMethodReviewList highlightId={highlightId} onOpenMethod={setOpenMethod} /> : reviewTab === "discussion" ? <DiscussionSourceReviewList /> : <GitHubRepositoryReviewList />}
            </>}
          </section>
        )}
        <section style={sourceModule}>
          <div style={tabHeader}>
            <span style={tabHeaderLabel}>方式库</span>
            <button type="button" onClick={() => setLibraryTab("crawl")} style={libraryTab === "crawl" ? tabActive : tabIdle}>爬取方式</button>
            <button type="button" onClick={() => setLibraryTab("discussion")} style={libraryTab === "discussion" ? tabActive : tabIdle}>邮件探查</button>
            <button type="button" onClick={() => setLibraryTab("github")} style={libraryTab === "github" ? tabActive : tabIdle}>GitHub 探查</button>
            <button type="button" onClick={() => setLibraryExpanded((value) => !value)} style={moduleToggle}>{libraryExpanded ? "收起" : "展开"}</button>
          </div>
          {libraryExpanded && (libraryTab === "crawl" ? <CrawlMethodList onOpenMethod={setOpenMethod} highlightId={highlightId} runLimitState={runLimitState} allowDelete={hasSystemAccess} readOnly={!hasSystemAccess} /> : libraryTab === "discussion" ? <DiscussionSourceLibraryList onRunStarted={openMailRunPanel} canManage={hasSystemAccess} /> : <GitHubRepositoryLibraryList onRunStarted={openGitHubRunPanel} canManage={hasSystemAccess} />)}
        </section>
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

const sourceModule: CSSProperties = { marginTop: 18, background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16 };
const tabHeader: CSSProperties = { display: "flex", alignItems: "center", gap: 5, padding: "0 2px", borderBottom: "1px solid #d0d5dd" };
const tabHeaderLabel: CSSProperties = { marginRight: 10, color: "#101828", fontSize: 15, fontWeight: 700 };
const tabIdle: CSSProperties = { border: "none", borderBottom: "2px solid transparent", padding: "8px 11px", background: "transparent", color: "#667085", fontSize: 13, cursor: "pointer" };
const tabActive: CSSProperties = { ...tabIdle, borderBottomColor: "#175cd3", color: "#175cd3", fontWeight: 800 };
const moduleToggle: CSSProperties = { marginLeft: "auto", border: "1px solid #d0d5dd", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#344054", fontSize: 13, fontWeight: 700, cursor: "pointer" };

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
