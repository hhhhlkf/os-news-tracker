import { useEffect, useState } from "react";

/** Home content column: maxWidth 1280 + 24px horizontal padding on each side. */
const CONTENT_OUTER_WIDTH = 1328;
/** Narrow viewports have no outer margin, so fall back to a fixed edge strip. */
const EDGE_FALLBACK_PX = 72;

type Zone = "left" | "right" | null;

interface Props {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  /** Suppress the arrows while a modal overlay owns the screen. */
  hidden?: boolean;
}

export function EdgePageArrows({ page, totalPages, onPageChange, hidden = false }: Props) {
  const [zone, setZone] = useState<Zone>(null);

  useEffect(() => {
    const handleMove = (event: MouseEvent) => {
      const margin = Math.max((window.innerWidth - CONTENT_OUTER_WIDTH) / 2, 0);
      const trigger = Math.max(margin, EDGE_FALLBACK_PX);
      if (event.clientX <= trigger) {
        setZone("left");
      } else if (event.clientX >= window.innerWidth - trigger) {
        setZone("right");
      } else {
        setZone(null);
      }
    };
    const handleLeave = (event: MouseEvent) => {
      if (!event.relatedTarget) {
        setZone(null);
      }
    };
    window.addEventListener("mousemove", handleMove, { passive: true });
    document.documentElement.addEventListener("mouseleave", handleLeave);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      document.documentElement.removeEventListener("mouseleave", handleLeave);
    };
  }, []);

  const canPrev = page > 1;
  const canNext = page < totalPages;

  return (
    <>
      {canPrev && !hidden && (
        <button
          type="button"
          aria-label="上一页"
          className={`edge-page-arrow edge-page-arrow--left${zone === "left" ? " is-visible" : ""}`}
          onClick={() => onPageChange(page - 1)}
        >
          <span className="edge-page-arrow__ring" />
          <span className="edge-page-arrow__ring edge-page-arrow__ring--delay" />
          <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M14.5 5 L8 12 L14.5 19" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
      {canNext && !hidden && (
        <button
          type="button"
          aria-label="下一页"
          className={`edge-page-arrow edge-page-arrow--right${zone === "right" ? " is-visible" : ""}`}
          onClick={() => onPageChange(page + 1)}
        >
          <span className="edge-page-arrow__ring" />
          <span className="edge-page-arrow__ring edge-page-arrow__ring--delay" />
          <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M9.5 5 L16 12 L9.5 19" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </>
  );
}
