import { useEffect, useState, type CSSProperties } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { CrawlMethodDetail } from "../components/CrawlMethodDetail";
import { CrawlMethodList } from "../components/CrawlMethodList";
import { CrawlMethodReviewList } from "../components/CrawlMethodReviewList";
import { DiscoveryPanel } from "../components/DiscoveryPanel";
import { BatchDiscoveryQueue } from "../components/BatchDiscoveryQueue";
import {
  DiscoveryBookmarkDrawers,
  type DiscoveryLeftDrawerId,
} from "../components/DiscoveryBookmarkDrawers";
import { DiscussionDiscoveryPanel, type TechnicalDiscoveryTab } from "../components/DiscussionDiscoveryPanel";
import type { DiscussionPipelineRun } from "../discussions/api";
import { GitHubRepositoryLibraryList } from "../components/GitHubRepositoryLibraryList";
import { GitHubRepositoryReviewList } from "../components/GitHubRepositoryReviewList";
import { DiscussionSourceLibraryList } from "../components/DiscussionSourceLibraryList";
import { DiscussionSourceReviewList } from "../components/DiscussionSourceReviewList";
import { MainCategoryPanel } from "../components/MainCategoryPanel";
import { buildNewsRunFormState } from "../components/runLimits";
import { PromptStudioPanel } from "../components/PromptStudioPanel";
import { DiscoveryLoopBudgetCard } from "../components/DiscoveryLoopBudgetCard";
import { RunLimitCard } from "../components/RunLimitCard";
import { ReviewReminderSettings } from "../components/ReviewReminderSettings";
import { WechatAuthPanel } from "../components/WechatAuthPanel";
import {
  DISCOVERY_MODULES,
  HomeModuleDeck,
  discoveryModuleIndex,
} from "../components/HomeModuleDeck";
import { useUnifiedDiscoveryLogs } from "../hooks/useUnifiedDiscoveryLogs";

const DISCUSSION_RUN_STORAGE_KEY = "os-news-tracker.discussion-run-id";
const DISCOVERY_MODULE_PATHS = ["/discover/probe", "/discover/crawl"] as const;

function discoveryPathFromIndex(index: number): (typeof DISCOVERY_MODULE_PATHS)[number] {
  return index === discoveryModuleIndex("library") ? "/discover/crawl" : "/discover/probe";
}

function discoveryIndexFromPath(pathname: string): number {
  return pathname === "/discover/crawl" ? discoveryModuleIndex("library") : discoveryModuleIndex("probe");
}

function readDiscussionRunId(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(DISCUSSION_RUN_STORAGE_KEY);
}

export function DiscoveryPage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const moduleIndex = discoveryIndexFromPath(pathname);
  const [leftOpen, setLeftOpen] = useState<DiscoveryLeftDrawerId | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [openMethod, setOpenMethod] = useState<number | null>(null);
  const [runLimitState, setRunLimitState] = useState(buildNewsRunFormState);
  const [reviewTab, setReviewTab] = useState<"crawl" | "discussion" | "github">("crawl");
  const [libraryTab, setLibraryTab] = useState<"crawl" | "discussion" | "github">("crawl");
  const [technicalTab, setTechnicalTab] = useState<TechnicalDiscoveryTab>("email");
  const [discussionRunId, setDiscussionRunId] = useState<string | null>(readDiscussionRunId);
  const [selectedQueueItemId, setSelectedQueueItemId] = useState<number | null>(null);
  const logs = useUnifiedDiscoveryLogs(selectedQueueItemId, discussionRunId);

  useEffect(() => {
    if (highlightId == null) return;
    const timeoutId = window.setTimeout(() => setHighlightId(null), 4000);
    return () => window.clearTimeout(timeoutId);
  }, [highlightId]);

  function closeSideDrawers() {
    setLeftOpen(null);
    setLogOpen(false);
  }

  async function handleMethodAdded(methodId: number) {
    await queryClient.invalidateQueries({ queryKey: ["discovery-methods"] });
    await queryClient.invalidateQueries({ queryKey: ["discovery-methods", "pending-review"] });
    setHighlightId(methodId);
    if (hasSystemAccess) {
      setReviewTab("crawl");
      setLeftOpen("review");
      return;
    }
    setLibraryTab("crawl");
    navigate("/discover/crawl");
  }

  function followDiscussionRun(run: DiscussionPipelineRun, tab: TechnicalDiscoveryTab): void {
    queryClient.setQueryData(["discussion-latest-run"], run);
    window.sessionStorage.setItem(DISCUSSION_RUN_STORAGE_KEY, run.id);
    setDiscussionRunId(run.id);
    setTechnicalTab(tab);
    navigate("/discover/probe");
    setLogOpen(true);
  }

  function resetDiscussionRun(): void {
    window.sessionStorage.removeItem(DISCUSSION_RUN_STORAGE_KEY);
    setDiscussionRunId(null);
  }

  const titleCard = (copy: string) => (
    <header className="home-title-card">
      <div style={{ fontSize: 13, color: "#98a2b3", marginBottom: 10 }}>Site Discovery</div>
      <div style={{ maxWidth: 720, minWidth: 0 }}>
        <h1 style={{ margin: 0, fontSize: 32, lineHeight: 1.2 }}>站点发现</h1>
        <p style={{ marginTop: 10, marginBottom: 0, color: "#d0d5dd" }}>{copy}</p>
      </div>
    </header>
  );

  return (
    <div className="home-shell">
      <div className="home-deck-host">
        <HomeModuleDeck
          className="home-deck--discovery"
          modules={DISCOVERY_MODULES}
          railLabel="发现模块"
          index={moduleIndex}
          onIndexChange={(next) => {
            navigate(discoveryPathFromIndex(next));
            closeSideDrawers();
          }}
          locked={openMethod != null}
        >
          <div className="home-module__frame home-module__frame--discovery">
            <div className="home-module__stack">
              {titleCard("智能探查摸清站点爬取门道，技术探查收取邮件与 GitHub 讨论。")}
              <div className="home-module__probe">
                <div className="home-module__probe-pane" data-home-scroll="true">
                  <DiscoveryPanel
                    hideLogs
                    selectedQueueItemId={selectedQueueItemId}
                    onSelectQueueItem={setSelectedQueueItemId}
                    onMethodAdded={handleMethodAdded}
                  />
                </div>
                <div className="home-module__probe-pane" data-home-scroll="true">
                  <DiscussionDiscoveryPanel
                    hideLogs
                    runLimitState={runLimitState}
                    runId={discussionRunId}
                    technicalTab={technicalTab}
                    onTechnicalTabChange={setTechnicalTab}
                    canManage={hasSystemAccess}
                    onResetRun={resetDiscussionRun}
                  />
                </div>
              </div>
            </div>
          </div>

          <div className="home-module__frame home-module__frame--discovery">
            <div className="home-module__stack">
              {titleCard("已沉淀的爬取方式、邮件列表和 GitHub 仓库。")}
              <section className="home-module__library" data-home-scroll="true" style={sourceModule}>
                <div style={tabHeader}>
                  <span style={tabHeaderLabel}>方式库</span>
                  <button type="button" onClick={() => setLibraryTab("crawl")} style={libraryTab === "crawl" ? tabActive : tabIdle}>爬取方式</button>
                  <button type="button" onClick={() => setLibraryTab("discussion")} style={libraryTab === "discussion" ? tabActive : tabIdle}>邮件探查</button>
                  <button type="button" onClick={() => setLibraryTab("github")} style={libraryTab === "github" ? tabActive : tabIdle}>GitHub 探查</button>
                </div>
                {libraryTab === "crawl" ? (
                  <CrawlMethodList
                    onOpenMethod={setOpenMethod}
                    highlightId={highlightId}
                    runLimitState={runLimitState}
                    allowDelete={hasSystemAccess}
                    readOnly={!hasSystemAccess}
                  />
                ) : libraryTab === "discussion" ? (
                  <DiscussionSourceLibraryList
                    onRunStarted={(run) => followDiscussionRun(run, "email")}
                    canManage={hasSystemAccess}
                  />
                ) : (
                  <GitHubRepositoryLibraryList
                    onRunStarted={(run) => followDiscussionRun(run, "github")}
                    canManage={hasSystemAccess}
                  />
                )}
              </section>
            </div>
          </div>
        </HomeModuleDeck>

        <DiscoveryBookmarkDrawers
          leftOpen={leftOpen}
          logOpen={logOpen}
          hasSystemAccess={hasSystemAccess}
          logs={logs}
          suppressOutsideClose={openMethod != null}
          onOpenLeft={(id) => {
            setLogOpen(false);
            setLeftOpen(id);
          }}
          onCloseLeft={() => setLeftOpen(null)}
          onOpenLog={() => {
            setLeftOpen(null);
            setLogOpen(true);
          }}
          onCloseLog={() => setLogOpen(false)}
          leftPanels={{
            limits: (
              <RunLimitCard
                formState={runLimitState}
                onChange={setRunLimitState}
                description="统一限制当前页的抓取时间范围与总条目数，方式库批量抓取、标准抓取、Agent Crawl 与 GitHub 技术探查共用。"
              />
            ),
            budget: <DiscoveryLoopBudgetCard />,
            queue: (
              <BatchDiscoveryQueue
                canManage={hasSystemAccess}
                selectedId={selectedQueueItemId}
                onSelect={(id, item) => {
                  setSelectedQueueItemId(id);
                  if (item && ["starting", "running", "cancelling", "failed"].includes(item.status)) {
                    setLogOpen(true);
                  }
                }}
              />
            ),
            review: hasSystemAccess ? (
              <div style={reviewStack}>
                <div style={tabHeader}>
                  <button type="button" onClick={() => setReviewTab("crawl")} style={reviewTab === "crawl" ? tabActive : tabIdle}>爬取方式</button>
                  <button type="button" onClick={() => setReviewTab("discussion")} style={reviewTab === "discussion" ? tabActive : tabIdle}>邮件探查</button>
                  <button type="button" onClick={() => setReviewTab("github")} style={reviewTab === "github" ? tabActive : tabIdle}>GitHub 探查</button>
                </div>
                <ReviewReminderSettings />
                {reviewTab === "crawl" ? (
                  <CrawlMethodReviewList highlightId={highlightId} onOpenMethod={setOpenMethod} />
                ) : reviewTab === "discussion" ? (
                  <DiscussionSourceReviewList />
                ) : (
                  <GitHubRepositoryReviewList />
                )}
              </div>
            ) : null,
            prompt: hasSystemAccess ? <PromptStudioPanel /> : null,
            category: <MainCategoryPanel />,
            wechat: hasSystemAccess ? <WechatAuthPanel /> : null,
          }}
        />
      </div>

      {openMethod != null && (
        <CrawlMethodDetail methodId={openMethod} onClose={() => setOpenMethod(null)} allowManage={hasSystemAccess} />
      )}
    </div>
  );
}

const sourceModule: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 12 };
const reviewStack: CSSProperties = { display: "grid", gap: 16, minWidth: 0 };
const tabHeader: CSSProperties = { display: "flex", alignItems: "center", gap: 5, padding: "0 2px 8px", borderBottom: "1px solid #d0d5dd" };
const tabHeaderLabel: CSSProperties = { marginRight: 10, color: "#101828", fontSize: 15, fontWeight: 700 };
const tabIdle: CSSProperties = { border: "none", borderBottom: "2px solid transparent", padding: "8px 11px", background: "transparent", color: "#667085", fontSize: 13, cursor: "pointer" };
const tabActive: CSSProperties = { ...tabIdle, borderBottomColor: "#175cd3", color: "#175cd3", fontWeight: 800 };
