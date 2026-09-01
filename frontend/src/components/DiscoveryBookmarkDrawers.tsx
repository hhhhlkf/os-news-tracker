import { useEffect, useRef, type ReactNode } from "react";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import type { NewsRunLogEntry } from "../types";

export type DiscoveryLeftDrawerId =
  | "limits"
  | "budget"
  | "queue"
  | "review"
  | "prompt"
  | "category"
  | "wechat";

type LeftBookmark = {
  id: DiscoveryLeftDrawerId;
  label: string;
  title: string;
  meta: string;
  tone: string;
  visible: boolean;
};

const LEFT_BOOKMARKS: LeftBookmark[] = [
  { id: "limits", label: "限制", title: "抓取限制", meta: "时间范围与总条目数", tone: "limits", visible: true },
  { id: "budget", label: "资源", title: "探查资源", meta: "记忆、工具量、深度与 Token", tone: "budget", visible: true },
  { id: "queue", label: "队列", title: "批量探查", meta: "探查队列与并行任务", tone: "queue", visible: true },
  { id: "review", label: "审核", title: "待审核方式", meta: "爬取、邮件与 GitHub", tone: "review", visible: false },
  { id: "prompt", label: "Prompt", title: "抓取模块 · Prompt 工作室", meta: "探查提示词", tone: "prompt", visible: false },
  { id: "category", label: "分类", title: "抓取模块 · 主分类修改", meta: "来源主分类", tone: "category", visible: true },
  { id: "wechat", label: "微信", title: "抓取模块 · 微信认证", meta: "兼容登录（已休眠）", tone: "wechat", visible: false },
];

interface Props {
  leftOpen: DiscoveryLeftDrawerId | null;
  logOpen: boolean;
  hasSystemAccess: boolean;
  logs: NewsRunLogEntry[];
  suppressOutsideClose?: boolean;
  onOpenLeft: (id: DiscoveryLeftDrawerId) => void;
  onCloseLeft: () => void;
  onOpenLog: () => void;
  onCloseLog: () => void;
  leftPanels: Partial<Record<DiscoveryLeftDrawerId, ReactNode>>;
}

export function DiscoveryBookmarkDrawers({
  leftOpen,
  logOpen,
  hasSystemAccess,
  logs,
  suppressOutsideClose = false,
  onOpenLeft,
  onCloseLeft,
  onOpenLog,
  onCloseLog,
  leftPanels,
}: Props) {
  const leftRef = useRef<HTMLElement>(null);
  const rightRef = useRef<HTMLElement>(null);
  const closeLeftRef = useRef(onCloseLeft);
  const closeLogRef = useRef(onCloseLog);
  closeLeftRef.current = onCloseLeft;
  closeLogRef.current = onCloseLog;

  const bookmarks = LEFT_BOOKMARKS.filter((item) => item.visible || (hasSystemAccess && (item.id === "review" || item.id === "prompt" || item.id === "wechat")));
  const activeLeft = bookmarks.find((item) => item.id === leftOpen) ?? null;
  const leftHandleHidden = logOpen && leftOpen == null;
  const rightHandleHidden = leftOpen != null && !logOpen;

  useEffect(() => {
    if ((!leftOpen && !logOpen) || suppressOutsideClose) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (leftRef.current?.contains(target) || rightRef.current?.contains(target)) return;
      if (leftOpen) closeLeftRef.current();
      if (logOpen) closeLogRef.current();
    };

    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [leftOpen, logOpen, suppressOutsideClose]);

  return (
    <>
      <aside
        ref={leftRef}
        className={`discovery-side-drawers discovery-side-drawers--left${leftOpen ? " is-open" : ""}${leftHandleHidden ? " is-handle-hidden" : ""}${activeLeft ? ` is-${activeLeft.tone}` : ""}`}
        aria-label="站点发现侧栏"
      >
        <div className="discovery-side-drawers__stage">
          {bookmarks.map((bookmark) => (
            <div
              key={bookmark.id}
              className={`discovery-side-drawers__panel discovery-side-drawers__panel--${bookmark.tone}${leftOpen === bookmark.id ? " is-active" : ""}`}
              aria-hidden={leftOpen !== bookmark.id}
              aria-label={bookmark.title}
            >
              <div className={`home-ops-card home-ops-card--${bookmark.tone}`}>
                {bookmark.id === "queue" && (
                  <div className="home-ops-card__head">
                    <div>
                      <div className="home-ops-card__title">{bookmark.title}</div>
                      <div className="home-ops-card__kicker">{bookmark.meta}</div>
                    </div>
                    <button type="button" className="home-side-drawers__close" onClick={onCloseLeft}>
                      关闭
                    </button>
                  </div>
                )}
                <div className="discovery-side-card__body" data-home-scroll="true">
                  {leftPanels[bookmark.id]}
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="home-side-drawers__bookmarks" aria-hidden={false}>
          {bookmarks.map((bookmark) => {
            const active = leftOpen === bookmark.id;
            const away = leftOpen != null ? !active : logOpen;
            return (
              <button
                key={bookmark.id}
                type="button"
                className={`home-bookmark home-bookmark--${bookmark.tone}${active ? " is-active" : ""}${away ? " is-away" : ""}`}
                aria-label={active ? `收起${bookmark.title}` : `打开${bookmark.title}`}
                aria-expanded={active}
                aria-hidden={away}
                tabIndex={away ? -1 : 0}
                onClick={() => (active ? onCloseLeft() : onOpenLeft(bookmark.id))}
              >
                <span className="home-bookmark__eyelet" aria-hidden="true" />
                <span className="home-bookmark__label">{bookmark.label}</span>
              </button>
            );
          })}
        </div>
      </aside>

      <aside
        ref={rightRef}
        className={`discovery-side-drawers discovery-side-drawers--right${logOpen ? " is-open" : ""}${rightHandleHidden ? " is-handle-hidden" : ""}`}
        aria-label="运行日志侧栏"
      >
        <div className="home-side-drawers__bookmarks" aria-hidden={false}>
          <button
            type="button"
            className={`home-bookmark home-bookmark--logs home-bookmark--right${logOpen ? " is-active" : ""}${leftOpen != null && !logOpen ? " is-away" : ""}`}
            aria-label={logOpen ? "收起运行日志" : "打开运行日志"}
            aria-expanded={logOpen}
            aria-hidden={leftOpen != null && !logOpen}
            tabIndex={leftOpen != null && !logOpen ? -1 : 0}
            onClick={() => (logOpen ? onCloseLog() : onOpenLog())}
          >
            <span className="home-bookmark__eyelet" aria-hidden="true" />
            <span className="home-bookmark__label">日志</span>
          </button>
        </div>
        <div className="discovery-side-drawers__stage">
          <div
            className={`discovery-side-drawers__panel discovery-side-drawers__panel--logs${logOpen ? " is-active" : ""}`}
            aria-hidden={!logOpen}
            aria-label="运行日志"
          >
            <div className="home-ops-card home-ops-card--logs">
              <div className="discovery-side-drawers__log-body">
                <DiscoveryLogPanel logs={logs} variant="unified" />
              </div>
            </div>
          </div>
        </div>
      </aside>
    </>
  );
}
