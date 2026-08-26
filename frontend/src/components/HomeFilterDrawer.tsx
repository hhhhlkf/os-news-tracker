import type { ReactNode } from "react";

interface DrawerProps {
  open: boolean;
  title?: string;
  activeCount?: number;
  /** Skip the dimming overlay so another modal (mail) stays usable. */
  quiet?: boolean;
  onOpen: () => void;
  onClose: () => void;
  children: ReactNode;
}

export function HomeFilterDrawer({
  open,
  title = "条目筛选",
  activeCount = 0,
  quiet = false,
  onOpen,
  onClose,
  children,
}: DrawerProps) {
  return (
    <>
      <div
        className={`home-filter-backdrop${open && !quiet ? " is-open" : ""}`}
        onClick={onClose}
        aria-hidden={!open || quiet}
      />
      <aside
        className={`home-filter-drawer${open ? " is-open" : ""}`}
        aria-label={title}
      >
        <div className="home-filter-drawer__panel" aria-hidden={!open}>
          <div className="home-filter-drawer__head">
            <div>
              <div className="home-filter-drawer__title">{title}</div>
              <div className="home-filter-drawer__meta">
                {activeCount > 0 ? `${activeCount} 项已启用` : "未设置筛选"}
              </div>
            </div>
            <button type="button" className="home-filter-drawer__close" onClick={onClose}>
              关闭
            </button>
          </div>
          <div className="home-filter-drawer__body" data-home-scroll="true">
            {children}
          </div>
        </div>
        <button
          type="button"
          className="home-filter-drawer__handle"
          aria-label={open ? "收起条目筛选" : "打开条目筛选"}
          aria-expanded={open}
          onClick={() => (open ? onClose() : onOpen())}
        >
          <span>筛选</span>
          {activeCount > 0 && <span className="home-filter-drawer__handle-count">{activeCount}</span>}
        </button>
      </aside>
    </>
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
