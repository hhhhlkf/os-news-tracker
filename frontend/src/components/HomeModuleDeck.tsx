import {
  Children,
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

const EDGE_Y_PX = 88;
type ArrowZone = "up" | "down" | null;

export const HOME_MODULES = [
  { id: "news", label: "新闻", hint: "条目列表" },
  { id: "trends", label: "趋势", hint: "热点轮播" },
  { id: "ops", label: "订阅", hint: "邮件与抓取" },
] as const;

export function homeModuleIndex(id: (typeof HOME_MODULES)[number]["id"]): number {
  return HOME_MODULES.findIndex((module) => module.id === id);
}

export const HOME_DECK_SLIDE_MS = 620;

interface Props {
  index: number;
  onIndexChange: (index: number) => void;
  /** Freeze wheel/keyboard paging while a modal owns the screen. */
  locked?: boolean;
  children: ReactNode;
}

function clampIndex(value: number, count: number): number {
  return Math.min(Math.max(value, 0), count - 1);
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function findScrollableAncestor(start: EventTarget | null, root: HTMLElement | null): HTMLElement | null {
  let node = start instanceof HTMLElement ? start : null;
  while (node && node !== root) {
    if (node.dataset.homeScroll === "true") return node;
    const overflowY = window.getComputedStyle(node).overflowY;
    if ((overflowY === "auto" || overflowY === "scroll") && node.scrollHeight > node.clientHeight + 1) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

function isLocalWheelRegion(start: EventTarget | null, root: HTMLElement | null): boolean {
  let node = start instanceof HTMLElement ? start : null;
  while (node && node !== root) {
    if (node.dataset.homeScroll === "true") return true;
    node = node.parentElement;
  }
  return false;
}

function canScrollFurther(el: HTMLElement, deltaY: number): boolean {
  if (deltaY > 0) return el.scrollTop + el.clientHeight < el.scrollHeight - 1;
  return el.scrollTop > 1;
}

export function HomeModuleDeck({ index, onIndexChange, locked = false, children }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const lockUntilRef = useRef(0);
  const indexRef = useRef(index);
  const lockedRef = useRef(locked);
  const [zone, setZone] = useState<ArrowZone>(null);
  const panels = Children.toArray(children);
  const count = panels.length;
  const safeIndex = clampIndex(index, count);

  indexRef.current = safeIndex;
  lockedRef.current = locked;

  const goTo = useCallback((next: number) => {
    const clamped = clampIndex(next, count);
    if (clamped === indexRef.current) return;
    lockUntilRef.current = performance.now() + HOME_DECK_SLIDE_MS + 80;
    onIndexChange(clamped);
  }, [count, onIndexChange]);

  const step = useCallback((delta: -1 | 1) => {
    goTo(indexRef.current + delta);
  }, [goTo]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const onWheel = (event: WheelEvent) => {
      if (lockedRef.current || Math.abs(event.deltaY) < 8) return;
      if (performance.now() < lockUntilRef.current) {
        event.preventDefault();
        return;
      }
      // Only the news list (and other data-home-scroll regions) own the wheel.
      // Left/right gutters around the column still page the module deck.
      if (isLocalWheelRegion(event.target, root)) return;
      const scrollable = findScrollableAncestor(event.target, root);
      if (scrollable && canScrollFurther(scrollable, event.deltaY)) return;
      event.preventDefault();
      step(event.deltaY > 0 ? 1 : -1);
    };

    const onKey = (event: KeyboardEvent) => {
      if (lockedRef.current || isTypingTarget(event.target)) return;
      if (event.key === "ArrowDown" || event.key === "PageDown") {
        event.preventDefault();
        step(1);
      } else if (event.key === "ArrowUp" || event.key === "PageUp") {
        event.preventDefault();
        step(-1);
      }
    };

    const onMove = (event: MouseEvent) => {
      if (event.clientY <= EDGE_Y_PX) {
        setZone("up");
      } else if (event.clientY >= window.innerHeight - EDGE_Y_PX) {
        setZone("down");
      } else {
        setZone(null);
      }
    };
    const onLeave = (event: MouseEvent) => {
      if (!event.relatedTarget) setZone(null);
    };

    root.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousemove", onMove, { passive: true });
    document.documentElement.addEventListener("mouseleave", onLeave);
    return () => {
      root.removeEventListener("wheel", onWheel);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousemove", onMove);
      document.documentElement.removeEventListener("mouseleave", onLeave);
    };
  }, [step]);

  const handleArrowKey = (event: ReactKeyboardEvent<HTMLButtonElement>, delta: -1 | 1) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      step(delta);
    }
  };

  const prev = HOME_MODULES[safeIndex - 1];
  const next = HOME_MODULES[safeIndex + 1];

  return (
    <div ref={rootRef} className="home-deck" aria-roledescription="竖向轮播">
      <div
        className="home-deck-track"
        style={{
          height: `${Math.max(count, 1) * 100}%`,
          transform: `translate3d(0, -${safeIndex * (100 / Math.max(count, 1))}%, 0)`,
        }}
      >
        {panels.map((panel, panelIndex) => (
          <section
            key={HOME_MODULES[panelIndex]?.id ?? panelIndex}
            className="home-module"
            style={{ flex: `1 0 ${100 / Math.max(count, 1)}%`, height: `${100 / Math.max(count, 1)}%` }}
            aria-hidden={panelIndex !== safeIndex}
            aria-label={HOME_MODULES[panelIndex]?.label}
          >
            {panel}
          </section>
        ))}
      </div>

      <div className="home-module-rail" aria-label="首页模块">
        {HOME_MODULES.slice(0, count).map((module, moduleIndex) => (
          <button
            key={module.id}
            type="button"
            className={`home-module-rail__dot${moduleIndex === safeIndex ? " is-active" : ""}`}
            aria-label={`切换到${module.label}`}
            aria-current={moduleIndex === safeIndex ? "true" : undefined}
            onClick={() => goTo(moduleIndex)}
          >
            <span className="home-module-rail__mark" />
            <span className="home-module-rail__label">{module.label}</span>
          </button>
        ))}
      </div>

      {prev && (
        <button
          type="button"
          className={`home-module-arrow home-module-arrow--up${zone === "up" ? " is-visible" : ""}`}
          aria-label={`上滚到${prev.label}`}
          onClick={() => step(-1)}
          onKeyDown={(event) => handleArrowKey(event, -1)}
        >
          <span className="home-module-arrow__ring" />
          <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M6 14.5 L12 8 L18 14.5" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="home-module-arrow__hint">上滚 · {prev.label}</span>
        </button>
      )}

      {next && (
        <button
          type="button"
          className={`home-module-arrow home-module-arrow--down${zone === "down" ? " is-visible" : ""}`}
          aria-label={`下滚到${next.label}`}
          onClick={() => step(1)}
          onKeyDown={(event) => handleArrowKey(event, 1)}
        >
          <span className="home-module-arrow__ring" />
          <span className="home-module-arrow__hint">下滚 · {next.label}</span>
          <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M6 9.5 L12 16 L18 9.5" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </div>
  );
}
