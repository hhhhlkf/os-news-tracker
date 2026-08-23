import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTrendCarousel, fetchTrendCarouselTemplates } from "./api";
import { trendCategoryColor, trendDirectionColor } from "./palette";
import type { TrendCarouselItem, TrendResultSource } from "./types";

const carouselTemplatesQueryKey = ["trends", "results", "carousel", "templates"] as const;
const carouselQueryKey = ["trends", "results", "carousel"] as const;
const CARDS_PER_PAGE = 2;
const AUTO_SCROLL_INTERVAL_MS = 7000;
const SOURCE_TITLE_MAX_LENGTH = 18;
/** Duration of the track slide; wrap snap waits this long before teleporting. */
const SLIDE_MS = 520;
const SLIDE_EASE = "cubic-bezier(0.22, 1, 0.36, 1)";

function shortenSourceTitle(title: string): string {
  const normalizedTitle = title.trim();
  if (normalizedTitle.length <= SOURCE_TITLE_MAX_LENGTH) return normalizedTitle;
  return `${normalizedTitle.slice(0, SOURCE_TITLE_MAX_LENGTH)}…`;
}

export interface TrendCarouselProps {
  /** Filter the news list by every news item this trend references. */
  onSelectTrend: (trend: { resultId: string; topic: string; itemIds: number[] }) => void;
  /** Filter by exactly one news item; the title only fills the search box. */
  onSelectSource: (source: { itemId: number; title: string }) => void;
  /** result_id of the trend currently driving the news list, if any. */
  activeResultId?: string | null;
  /** item_id of the source pill currently driving the news list, if any. */
  activeItemId?: number | null;
}

export function TrendCarousel({
  onSelectTrend,
  onSelectSource,
  activeResultId = null,
  activeItemId = null,
}: TrendCarouselProps) {
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [direction, setDirection] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  /** Track index into [cloneLast, ...pages, cloneFirst]; 1 == real page 1. */
  const [slideIndex, setSlideIndex] = useState(1);
  const [transitionOn, setTransitionOn] = useState(true);
  const [isHovered, setIsHovered] = useState(false);
  const [isSliding, setIsSliding] = useState(false);
  const slideResetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const templatesQuery = useQuery({
    queryKey: carouselTemplatesQueryKey,
    queryFn: fetchTrendCarouselTemplates,
    retry: false,
  });
  const templates = templatesQuery.data ?? [];
  const selectedTemplate = templates.find((template) => template.template_id === templateId);
  // Direction choices are authored by the template currently selected above;
  // a historical run can retain labels that were removed from that template.
  const directions = selectedTemplate?.directions ?? [];

  useEffect(() => {
    if (templateId && templates.some((template) => template.template_id === templateId)) return;
    setTemplateId(templates[0]?.template_id ?? null);
  }, [templateId, templates]);

  useEffect(() => {
    setDirection(null);
  }, [templateId]);

  useEffect(() => {
    if (direction !== null && !directions.includes(direction)) {
      setDirection(null);
    }
  }, [direction, directions]);

  const carouselQuery = useQuery({
    queryKey: [...carouselQueryKey, templateId, direction] as const,
    queryFn: () => fetchTrendCarousel(templateId!, direction ?? undefined),
    enabled: Boolean(templateId),
    retry: false,
  });

  const items = carouselQuery.data?.items ?? [];
  const pageCount = Math.max(1, Math.ceil(items.length / CARDS_PER_PAGE));
  const pages = useMemo(() => {
    const chunks: TrendCarouselItem[][] = [];
    for (let i = 0; i < items.length; i += CARDS_PER_PAGE) {
      chunks.push(items.slice(i, i + CARDS_PER_PAGE));
    }
    return chunks;
  }, [items]);

  // Infinite track: [last, ...pages, first]. Single-page carousels skip clones.
  const trackPages = useMemo(() => {
    if (pageCount <= 1) return pages;
    return [pages[pageCount - 1]!, ...pages, pages[0]!];
  }, [pages, pageCount]);

  useEffect(() => {
    const count = Math.max(1, Math.ceil((carouselQuery.data?.items.length ?? 0) / CARDS_PER_PAGE));
    setPage(1);
    setSlideIndex(count <= 1 ? 0 : 1);
    setTransitionOn(false);
    setIsSliding(false);
  }, [templateId, direction, carouselQuery.data?.run_id]);
  useEffect(() => {
    if (page <= pageCount) return;
    setPage(pageCount);
    setSlideIndex(pageCount <= 1 ? 0 : pageCount);
  }, [page, pageCount]);

  useEffect(() => {
    return () => {
      if (slideResetTimer.current) clearTimeout(slideResetTimer.current);
    };
  }, []);

  const settleAfterSlide = (nextIndex: number, count: number) => {
    if (slideResetTimer.current) clearTimeout(slideResetTimer.current);
    slideResetTimer.current = setTimeout(() => {
      const needsSnapToFirst = count > 1 && nextIndex === count + 1;
      const needsSnapToLast = count > 1 && nextIndex === 0;
      if (needsSnapToFirst || needsSnapToLast) {
        setTransitionOn(false);
        const snapped = needsSnapToFirst ? 1 : count;
        setSlideIndex(snapped);
        setPage(snapped);
        // Re-enable transition on the next frame after the snap paint.
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            setTransitionOn(true);
            setIsSliding(false);
          });
        });
        return;
      }
      setIsSliding(false);
    }, SLIDE_MS);
  };

  const goToRelative = (delta: -1 | 1) => {
    if (pageCount <= 1 || isSliding) return;
    const next = slideIndex + delta;
    const logical = next === 0 ? pageCount : next === pageCount + 1 ? 1 : next;
    setIsSliding(true);
    setTransitionOn(true);
    setSlideIndex(next);
    setPage(logical);
    settleAfterSlide(next, pageCount);
  };

  // Rotating away from what the reader is aiming at is worse than not rotating,
  // so hovering, keyboard focus and an active trend filter all hold the page.
  const isHeld = isHovered || activeResultId !== null || activeItemId !== null;
  useEffect(() => {
    if (isHeld || pageCount <= 1 || isSliding) return;
    // Re-armed on every page change, so manual paging restarts the full delay.
    const timer = setTimeout(() => {
      goToRelative(1);
    }, AUTO_SCROLL_INTERVAL_MS);
    return () => clearTimeout(timer);
    // goToRelative closes over latest slideIndex/pageCount; page + flags re-arm the timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: re-arm on page settle
  }, [isHeld, page, pageCount, isSliding, slideIndex]);

  const loadError = (templatesQuery.error ?? carouselQuery.error) as Error | null;
  const carouselMeta = carouselQuery.data;

  return (
    <section
      style={panel}
      aria-label="趋势轮播"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onFocus={() => setIsHovered(true)}
      onBlur={() => setIsHovered(false)}
    >
      <div style={header}>
        <div>
          <div style={eyebrow}>趋势轮播</div>
          <div style={meta}>
            {carouselMeta?.window_start_date && carouselMeta.window_end_date
              ? `窗口 ${carouselMeta.window_start_date} 至 ${carouselMeta.window_end_date} · 最多展示 X=${carouselMeta.trend_count} 条可验证趋势`
              : "选择身份模板后展示该模板最近成功发布的可验证趋势"}
          </div>
        </div>
        <div style={controls}>
          <select
            value={templateId ?? ""}
            onChange={(event) => setTemplateId(event.target.value || null)}
            style={{
              ...select,
              ...(direction
                ? {
                    borderColor: trendDirectionColor(direction).border,
                    background: trendDirectionColor(direction).background,
                    color: trendDirectionColor(direction).color,
                  }
                : {}),
            }}
            aria-label="选择身份模板"
            disabled={templates.length === 0}
          >
            {templates.length === 0 && <option value="">暂无身份模板</option>}
            {templates.map((template) => (
              <option key={template.template_id} value={template.template_id}>
                {template.name}
              </option>
            ))}
          </select>
          <select
            value={direction ?? ""}
            onChange={(event) => setDirection(event.target.value || null)}
            style={select}
            aria-label="筛选趋势方向"
            disabled={!templateId}
          >
            <option value="">全部方向</option>
            {directions.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          {items.length > CARDS_PER_PAGE && (
            <div style={pager}>
              <span style={autoScrollHint}>{isHeld ? "已暂停" : "自动轮播"}</span>
              <button
                type="button"
                style={isSliding ? pagerButtonDisabled : pagerButton}
                disabled={isSliding}
                onClick={() => goToRelative(-1)}
                aria-label="上一组趋势"
              >
                ‹
              </button>
              <span style={pagerLabel}>
                {page} / {pageCount}
              </span>
              <button
                type="button"
                style={isSliding ? pagerButtonDisabled : pagerButton}
                disabled={isSliding}
                onClick={() => goToRelative(1)}
                aria-label="下一组趋势"
              >
                ›
              </button>
            </div>
          )}
        </div>
      </div>

      {loadError ? (
        <div style={emptyState}>{describeLoadError(loadError)}</div>
      ) : templatesQuery.isLoading ? (
        <div style={emptyState}>正在加载身份模板…</div>
      ) : templates.length === 0 ? (
        <div style={emptyState}>尚未创建身份模板，先在趋势总结页新建模板并完成一次趋势运行。</div>
      ) : carouselQuery.isLoading ? (
        <div style={emptyState}>正在加载趋势轮播…</div>
      ) : items.length === 0 ? (
        <div style={emptyState}>{carouselQuery.data?.message ?? "当前范围内暂无可验证趋势"}</div>
      ) : (
        <div style={viewport} aria-live="polite">
          <div
            className="trend-carousel-track"
            style={{
              ...track,
              transform: `translate3d(-${slideIndex * 100}%, 0, 0)`,
              transition: transitionOn ? `transform ${SLIDE_MS}ms ${SLIDE_EASE}` : "none",
            }}
          >
            {trackPages.map((pageItems, index) => (
              <div key={`slide-${index}`} style={slide}>
                <div style={grid}>
                  {pageItems.map((item) => (
                    <TrendCard
                      key={`${index}-${item.result_id}`}
                      item={item}
                      isActive={item.result_id === activeResultId}
                      activeItemId={activeItemId}
                      onSelectTrend={onSelectTrend}
                      onSelectSource={onSelectSource}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function describeLoadError(error: Error): string {
  return `暂时无法读取趋势轮播：${error.message}`;
}

function TrendCard({
  item,
  isActive,
  activeItemId,
  onSelectTrend,
  onSelectSource,
}: {
  item: TrendCarouselItem;
  isActive: boolean;
  activeItemId: number | null;
  onSelectTrend: TrendCarouselProps["onSelectTrend"];
  onSelectSource: TrendCarouselProps["onSelectSource"];
}) {
  const categoryColor = trendCategoryColor(item.category);
  const directionColor = trendDirectionColor(item.direction);

  return (
    <article
      style={isActive ? activeCard : card}
      onClick={() =>
        onSelectTrend({ resultId: item.result_id, topic: item.topic, itemIds: item.item_ids })
      }
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        onSelectTrend({ resultId: item.result_id, topic: item.topic, itemIds: item.item_ids });
      }}
      aria-pressed={isActive}
    >
      <div style={cardHeader}>
        <div style={badges}>
          <span style={{ ...categoryBadge, background: categoryColor.background, color: categoryColor.color, borderColor: categoryColor.border }}>{item.category_label}</span>
          {item.direction && <span style={{ ...directionBadge, background: directionColor.background, color: directionColor.color, borderColor: directionColor.border }}>{item.direction}</span>}
        </div>
        <span style={cardScore}>排序分 {item.trend_rank_score.toFixed(1)}</span>
      </div>
      <div style={cardTopic}>{item.topic}</div>
      <div style={cardSummary}>{item.trend_summary}</div>
      <SourcePillsRow
        sources={item.sources}
        activeItemId={activeItemId}
        onSelectSource={onSelectSource}
      />
    </article>
  );
}

/** Single-row citation pills with end arrows, same idea as the Token chart scroll legend. */
function SourcePillsRow({
  sources,
  activeItemId,
  onSelectSource,
}: {
  sources: TrendResultSource[];
  activeItemId: number | null;
  onSelectSource: TrendCarouselProps["onSelectSource"];
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [needsPager, setNeedsPager] = useState(false);
  const [canPrev, setCanPrev] = useState(false);
  const [canNext, setCanNext] = useState(false);

  const syncPager = () => {
    const el = viewportRef.current;
    if (!el) {
      setNeedsPager(false);
      setCanPrev(false);
      setCanNext(false);
      return;
    }
    const maxScroll = el.scrollWidth - el.clientWidth;
    const overflow = maxScroll > 1;
    setNeedsPager(overflow);
    setCanPrev(overflow && el.scrollLeft > 1);
    setCanNext(overflow && el.scrollLeft < maxScroll - 1);
  };

  useLayoutEffect(() => {
    syncPager();
    const el = viewportRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => syncPager());
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => observer.disconnect();
  }, [sources]);

  const scrollByPage = (direction: -1 | 1) => {
    const el = viewportRef.current;
    if (!el) return;
    const step = Math.max(80, Math.floor(el.clientWidth * 0.85));
    el.scrollBy({ left: direction * step, behavior: "smooth" });
  };

  return (
    <div
      style={sourceRowShell}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <div
        ref={viewportRef}
        style={sourceViewport}
        onScroll={syncPager}
        aria-label="引用新闻"
      >
        <div style={sourceTrack}>
          {sources.map((source) => (
            <button
              key={source.item_id}
              type="button"
              style={source.item_id === activeItemId ? activeSourcePill : sourcePill}
              title={source.title}
              aria-label={`查看引用新闻：${source.title}`}
              onClick={() => onSelectSource({ itemId: source.item_id, title: source.title })}
            >
              {shortenSourceTitle(source.title)}
            </button>
          ))}
        </div>
      </div>
      {needsPager && (
        <div style={sourcePager}>
          <button
            type="button"
            style={canPrev ? sourcePageButton : sourcePageButtonDisabled}
            disabled={!canPrev}
            aria-label="上一组引用"
            onClick={() => scrollByPage(-1)}
          >
            ‹
          </button>
          <button
            type="button"
            style={canNext ? sourcePageButton : sourcePageButtonDisabled}
            disabled={!canNext}
            aria-label="下一组引用"
            onClick={() => scrollByPage(1)}
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}

const panel: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  background: "#fff",
  padding: 16,
  marginBottom: 16,
};
const header: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 12,
  flexWrap: "wrap",
  marginBottom: 12,
};
const eyebrow: CSSProperties = { fontSize: 14, fontWeight: 800, color: "#101828" };
const meta: CSSProperties = { fontSize: 12, color: "#667085", marginTop: 4, lineHeight: 1.55 };
const controls: CSSProperties = { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" };
const select: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "8px 10px",
  fontSize: 13,
  background: "#fff",
  color: "#344054",
  maxWidth: 240,
};
const pager: CSSProperties = { display: "flex", alignItems: "center", gap: 6 };
const pagerButton: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  background: "#fff",
  color: "#344054",
  padding: "6px 12px",
  fontSize: 14,
  fontWeight: 700,
  cursor: "pointer",
  lineHeight: 1,
};
const pagerButtonDisabled: CSSProperties = {
  ...pagerButton,
  opacity: 0.55,
  cursor: "default",
};
const pagerLabel: CSSProperties = { fontSize: 12, color: "#667085", minWidth: 38, textAlign: "center" };
const autoScrollHint: CSSProperties = { fontSize: 11, color: "#98a2b3", whiteSpace: "nowrap", marginRight: 2 };
const viewport: CSSProperties = {
  overflow: "hidden",
  width: "100%",
};
const track: CSSProperties = {
  display: "flex",
  alignItems: "stretch",
  width: "100%",
  willChange: "transform",
};
const slide: CSSProperties = {
  flex: "0 0 100%",
  minWidth: 0,
  boxSizing: "border-box",
  // Stretch with the tallest page in the track, then fill that height.
  alignSelf: "stretch",
};
const grid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  gridTemplateRows: "1fr",
  gap: 12,
  width: "100%",
  height: "100%",
  minHeight: "100%",
  alignItems: "stretch",
};
const card: CSSProperties = {
  border: "1px solid #eaecf0",
  borderRadius: 8,
  padding: 14,
  background: "#fcfcfd",
  display: "flex",
  flexDirection: "column",
  gap: 8,
  cursor: "pointer",
  minWidth: 0,
  height: "100%",
  boxSizing: "border-box",
};
const activeCard: CSSProperties = {
  ...card,
  borderColor: "#84adff",
  background: "#f8fbff",
  boxShadow: "0 0 0 2px rgba(23,92,211,.10)",
};
const cardHeader: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 };
const badges: CSSProperties = { display: "flex", alignItems: "center", gap: 6, minWidth: 0, flexWrap: "wrap" };
const categoryBadge: CSSProperties = {
  border: "1px solid",
  fontSize: 11,
  fontWeight: 700,
  borderRadius: 999,
  padding: "3px 8px",
  whiteSpace: "nowrap",
};
const directionBadge: CSSProperties = { border: "1px solid", borderRadius: 999, padding: "3px 6px", fontSize: 10, fontWeight: 800 };
const cardScore: CSSProperties = { fontSize: 11, color: "#98a2b3", whiteSpace: "nowrap" };
const cardTopic: CSSProperties = { fontSize: 14, fontWeight: 800, color: "#101828", lineHeight: 1.4 };
const cardSummary: CSSProperties = { fontSize: 12, color: "#475467", lineHeight: 1.6 };
const sourceRowShell: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  marginTop: "auto",
  paddingTop: 4,
  minWidth: 0,
  width: "100%",
  flexShrink: 0,
};
const sourceViewport: CSSProperties = {
  flex: "1 1 auto",
  minWidth: 0,
  overflow: "hidden",
};
const sourceTrack: CSSProperties = {
  display: "flex",
  flexWrap: "nowrap",
  gap: 6,
  width: "max-content",
};
const sourcePager: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 2,
  flex: "0 0 auto",
};
const sourcePageButton: CSSProperties = {
  border: "none",
  background: "transparent",
  color: "#98a2b3",
  fontSize: 14,
  fontWeight: 700,
  lineHeight: 1,
  padding: "2px 4px",
  cursor: "pointer",
};
const sourcePageButtonDisabled: CSSProperties = {
  ...sourcePageButton,
  opacity: 0.35,
  cursor: "default",
};
const sourcePill: CSSProperties = {
  border: "1px solid #e4ebf5",
  borderRadius: 999,
  background: "#fff",
  color: "#475467",
  fontSize: 11,
  padding: "3px 9px",
  maxWidth: 150,
  flex: "0 0 auto",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  cursor: "pointer",
};
const activeSourcePill: CSSProperties = {
  ...sourcePill,
  borderColor: "#84adff",
  background: "#eff6ff",
  color: "#175cd3",
  fontWeight: 700,
};
const emptyState: CSSProperties = {
  border: "1px dashed #d0d5dd",
  borderRadius: 8,
  color: "#667085",
  background: "#fcfcfd",
  padding: "20px 16px",
  fontSize: 13,
  textAlign: "center",
};
