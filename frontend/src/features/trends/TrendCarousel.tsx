import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTrendCarousel, fetchTrendCarouselTemplates } from "./api";
import { trendCategoryColor, trendDirectionColor } from "./palette";
import type { TrendCarouselItem, TrendResultSource } from "./types";

const carouselTemplatesQueryKey = ["trends", "results", "carousel", "templates"] as const;
const carouselQueryKey = ["trends", "results", "carousel"] as const;
const CARDS_PER_PAGE = 2;
const AUTO_SCROLL_INTERVAL_MS = 7000;
const SOURCE_TITLE_MAX_LENGTH = 14;
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
  const [viewportHeight, setViewportHeight] = useState<number | null>(null);
  const slideResetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const viewportRef = useRef<HTMLDivElement>(null);

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

  const measureActiveSlide = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const slide = viewport.querySelectorAll(".trend-carousel__slide")[slideIndex] as HTMLElement | undefined;
    if (!slide) return;
    const pageEl = slide.querySelector(".trend-carousel__page");
    const height = (pageEl instanceof HTMLElement ? pageEl : slide).offsetHeight;
    if (height > 0) setViewportHeight(height);
  };

  useLayoutEffect(() => {
    measureActiveSlide();
  }, [slideIndex, items, templateId, direction]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return;
    const slide = viewport.querySelectorAll(".trend-carousel__slide")[slideIndex] as HTMLElement | undefined;
    if (!slide) return;
    const observer = new ResizeObserver(() => measureActiveSlide());
    observer.observe(slide);
    const pageEl = slide.querySelector(".trend-carousel__page");
    if (pageEl) observer.observe(pageEl);
    return () => observer.disconnect();
  }, [slideIndex, items]);

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
  const selectTone = direction ? trendDirectionColor(direction) : null;

  return (
    <section
      className="home-ops-card home-ops-card--trends trend-carousel"
      aria-label="趋势轮播"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onFocus={() => setIsHovered(true)}
      onBlur={() => setIsHovered(false)}
    >
      <div className="home-ops-card__head">
        <div>
          <div className="home-ops-card__title">趋势轮播</div>
          <div className="home-ops-card__kicker">
            {carouselMeta?.window_start_date && carouselMeta.window_end_date
              ? `${carouselMeta.window_start_date} 至 ${carouselMeta.window_end_date}`
              : "最近成功发布的可验证趋势"}
          </div>
        </div>
        {items.length > CARDS_PER_PAGE && (
          <div className="trend-carousel__pager">
            <span className="trend-carousel__hint">{isHeld ? "已暂停" : "自动"}</span>
            <button
              type="button"
              className="trend-carousel__page-btn"
              disabled={isSliding}
              onClick={() => goToRelative(-1)}
              aria-label="上一组趋势"
            >
              ‹
            </button>
            <span className="trend-carousel__page-label">
              {page}/{pageCount}
            </span>
            <button
              type="button"
              className="trend-carousel__page-btn"
              disabled={isSliding}
              onClick={() => goToRelative(1)}
              aria-label="下一组趋势"
            >
              ›
            </button>
          </div>
        )}
      </div>

      <div className="trend-carousel__controls">
        <select
          value={templateId ?? ""}
          onChange={(event) => setTemplateId(event.target.value || null)}
          className="trend-carousel__select"
          style={
            selectTone
              ? {
                  borderColor: selectTone.border,
                  background: selectTone.background,
                  color: selectTone.color,
                }
              : undefined
          }
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
          className="trend-carousel__select trend-carousel__select--direction"
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
      </div>

      {loadError ? (
        <div className="trend-carousel__empty">{describeLoadError(loadError)}</div>
      ) : templatesQuery.isLoading ? (
        <div className="trend-carousel__empty">正在加载身份模板…</div>
      ) : templates.length === 0 ? (
        <div className="trend-carousel__empty">尚未创建身份模板，先在趋势总结页完成一次运行。</div>
      ) : carouselQuery.isLoading ? (
        <div className="trend-carousel__empty">正在加载趋势轮播…</div>
      ) : items.length === 0 ? (
        <div className="trend-carousel__empty">{carouselQuery.data?.message ?? "当前范围内暂无可验证趋势"}</div>
      ) : (
        <div
          ref={viewportRef}
          className="trend-carousel__viewport"
          aria-live="polite"
          style={{
            height: viewportHeight ?? undefined,
            transition: transitionOn ? `height ${SLIDE_MS}ms ${SLIDE_EASE}` : "none",
          }}
        >
          <div
            className="trend-carousel-track"
            style={{
              transform: `translate3d(-${slideIndex * 100}%, 0, 0)`,
              transition: transitionOn ? `transform ${SLIDE_MS}ms ${SLIDE_EASE}` : "none",
            }}
          >
            {trackPages.map((pageItems, index) => (
              <div key={`slide-${index}`} className="trend-carousel__slide">
                <div className="trend-carousel__page">
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
      className={`trend-mini-card${isActive ? " is-active" : ""}`}
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
      <div className="trend-mini-card__header">
        <div className="trend-mini-card__badges">
          <span
            className="trend-mini-card__badge"
            style={{ background: categoryColor.background, color: categoryColor.color, borderColor: categoryColor.border }}
          >
            {item.category_label}
          </span>
          {item.direction && (
            <span
              className="trend-mini-card__badge trend-mini-card__badge--direction"
              style={{ background: directionColor.background, color: directionColor.color, borderColor: directionColor.border }}
            >
              {item.direction}
            </span>
          )}
        </div>
        <span className="trend-mini-card__score">{item.trend_rank_score.toFixed(1)}</span>
      </div>
      <div className="trend-mini-card__topic">{item.topic}</div>
      <div className="trend-mini-card__summary">{item.trend_summary}</div>
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
      className="trend-mini-card__sources"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <div
        ref={viewportRef}
        className="trend-mini-card__source-viewport"
        onScroll={syncPager}
        aria-label="引用新闻"
      >
        <div className="trend-mini-card__source-track">
          {sources.map((source) => (
            <button
              key={source.item_id}
              type="button"
              className={`trend-mini-card__source${source.item_id === activeItemId ? " is-active" : ""}`}
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
        <div className="trend-mini-card__source-pager">
          <button
            type="button"
            className="trend-mini-card__source-btn"
            disabled={!canPrev}
            aria-label="上一组引用"
            onClick={() => scrollByPage(-1)}
          >
            ‹
          </button>
          <button
            type="button"
            className="trend-mini-card__source-btn"
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
