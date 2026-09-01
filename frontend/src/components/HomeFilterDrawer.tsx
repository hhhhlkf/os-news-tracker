import { useEffect, useRef, type ReactNode } from "react";

export type HomeSideDrawerId = "filter" | "subscribe" | "trends";

interface SideDrawerProps {
  filterOpen: boolean;
  subscribeOpen: boolean;
  trendsOpen: boolean;
  filterTitle?: string;
  filterActiveCount?: number;
  trendsActiveCount?: number;
  /** Keep drawers open while a modal (mail / morning crawl) owns the click. */
  suppressOutsideClose?: boolean;
  onOpenFilter: () => void;
  onCloseFilter: () => void;
  onOpenSubscribe: () => void;
  onCloseSubscribe: () => void;
  onOpenTrends: () => void;
  onCloseTrends: () => void;
  filter: ReactNode;
  subscribe: ReactNode;
  trends: ReactNode;
}

function resolveMode(filterOpen: boolean, subscribeOpen: boolean, trendsOpen: boolean): HomeSideDrawerId | "closed" {
  if (filterOpen) return "filter";
  if (subscribeOpen) return "subscribe";
  if (trendsOpen) return "trends";
  return "closed";
}

export function HomeSideDrawers({
  filterOpen,
  subscribeOpen,
  trendsOpen,
  filterTitle = "条目筛选",
  filterActiveCount = 0,
  trendsActiveCount = 0,
  suppressOutsideClose = false,
  onOpenFilter,
  onCloseFilter,
  onOpenSubscribe,
  onCloseSubscribe,
  onOpenTrends,
  onCloseTrends,
  filter,
  subscribe,
  trends,
}: SideDrawerProps) {
  const rootRef = useRef<HTMLElement>(null);
  const closeFilterRef = useRef(onCloseFilter);
  const closeSubscribeRef = useRef(onCloseSubscribe);
  const closeTrendsRef = useRef(onCloseTrends);
  const open = filterOpen || subscribeOpen || trendsOpen;
  const mode = resolveMode(filterOpen, subscribeOpen, trendsOpen);
  closeFilterRef.current = onCloseFilter;
  closeSubscribeRef.current = onCloseSubscribe;
  closeTrendsRef.current = onCloseTrends;

  useEffect(() => {
    if (!open || suppressOutsideClose) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest(".home-filter-trigger")) return;
      if (filterOpen) closeFilterRef.current();
      if (subscribeOpen) closeSubscribeRef.current();
      if (trendsOpen) closeTrendsRef.current();
    };

    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open, filterOpen, subscribeOpen, trendsOpen, suppressOutsideClose]);

  return (
    <aside
      ref={rootRef}
      className={`home-side-drawers${open ? " is-open" : ""} is-${mode}`}
      aria-label="首页侧栏"
    >
      <div className="home-side-drawers__stage">
        <div
          className={`home-side-drawers__panel home-side-drawers__panel--filter${filterOpen ? " is-active" : ""}`}
          aria-hidden={!filterOpen}
          aria-label={filterTitle}
        >
          <div className="home-ops-card home-ops-card--filter">
            <div className="home-side-drawers__filter-body" data-home-scroll="true">
              {filter}
            </div>
          </div>
        </div>
        <div
          className={`home-side-drawers__panel home-side-drawers__panel--subscribe${subscribeOpen ? " is-active" : ""}`}
          aria-hidden={!subscribeOpen}
          aria-label="订阅"
          data-home-scroll="true"
        >
          {subscribe}
        </div>
        <div
          className={`home-side-drawers__panel home-side-drawers__panel--trends${trendsOpen ? " is-active" : ""}`}
          aria-hidden={!trendsOpen}
          aria-label="趋势"
          data-home-scroll="true"
        >
          {trends}
        </div>
      </div>
      <div className="home-side-drawers__bookmarks" aria-hidden={false}>
        <button
          type="button"
          className={`home-bookmark home-bookmark--filter${filterOpen ? " is-active" : ""}${subscribeOpen || trendsOpen ? " is-away" : ""}`}
          aria-label={filterOpen ? "收起条目筛选" : "打开条目筛选"}
          aria-expanded={filterOpen}
          aria-hidden={subscribeOpen || trendsOpen}
          tabIndex={subscribeOpen || trendsOpen ? -1 : 0}
          onClick={() => (filterOpen ? onCloseFilter() : onOpenFilter())}
        >
          <span className="home-bookmark__eyelet" aria-hidden="true" />
          <span className="home-bookmark__label">筛选</span>
          {filterActiveCount > 0 && (
            <span className="home-bookmark__count">{filterActiveCount}</span>
          )}
        </button>
        <button
          type="button"
          className={`home-bookmark home-bookmark--subscribe${subscribeOpen ? " is-active" : ""}${filterOpen || trendsOpen ? " is-away" : ""}`}
          aria-label={subscribeOpen ? "收起订阅" : "打开订阅"}
          aria-expanded={subscribeOpen}
          aria-hidden={filterOpen || trendsOpen}
          tabIndex={filterOpen || trendsOpen ? -1 : 0}
          onClick={() => (subscribeOpen ? onCloseSubscribe() : onOpenSubscribe())}
        >
          <span className="home-bookmark__eyelet" aria-hidden="true" />
          <span className="home-bookmark__label">订阅</span>
        </button>
        <button
          type="button"
          className={`home-bookmark home-bookmark--trends${trendsOpen ? " is-active" : ""}${filterOpen || subscribeOpen ? " is-away" : ""}`}
          aria-label={trendsOpen ? "收起趋势轮播" : "打开趋势轮播"}
          aria-expanded={trendsOpen}
          aria-hidden={filterOpen || subscribeOpen}
          tabIndex={filterOpen || subscribeOpen ? -1 : 0}
          onClick={() => (trendsOpen ? onCloseTrends() : onOpenTrends())}
        >
          <span className="home-bookmark__eyelet" aria-hidden="true" />
          <span className="home-bookmark__label">趋势</span>
          {trendsActiveCount > 0 && (
            <span className="home-bookmark__count">{trendsActiveCount}</span>
          )}
        </button>
      </div>
    </aside>
  );
}

interface TriggerProps {
  activeCount: number;
  onClick: () => void;
}

export function HomeFilterTrigger({ activeCount, onClick }: TriggerProps) {
  return (
    <button type="button" className="home-filter-trigger" onClick={onClick}>
      筛选
      {activeCount > 0 && <span className="home-filter-trigger__count">{activeCount}</span>}
    </button>
  );
}
