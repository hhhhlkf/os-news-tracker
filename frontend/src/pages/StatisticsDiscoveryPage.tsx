import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";
import {
  fetchDiscoveryRunUsage,
  fetchItemVolumeDaily,
  fetchQueryItemAvg,
  fetchQueryRunUsage,
  fetchTokenUsageSummary,
} from "../api/client";

type RangePreset = "24h" | "7d" | "30d" | "custom";

/**
 * Token console palette — discovery / query / trend / volume each keep a distinct instrument:
 * 探查 = probe cyan, 查询 = industrial steel spectrum, 趋势 = cold stock → copper burn,
 * 消息量 = importance red/amber/slate. Deliberately avoids purple/rainbow defaults.
 */
const SERIES_COLORS = {
  prompt: "#1d4e89",
  completion: "#c2410c",
  discoveryIn: "#0e7490",
  discoveryOut: "#22d3ee",
  importanceHigh: "#e31b54",
  importanceMedium: "#f79009",
  importanceLow: "#98a2b3",
};

const METHOD_COLORS = [
  "#1e3a8a",
  "#0369a1",
  "#0f766e",
  "#3f6212",
  "#b45309",
  "#9f1239",
  "#155e75",
  "#854d0e",
  "#1e40af",
  "#115e59",
];

type ChartSegment = {
  key: string;
  label: string;
  value: number;
  color: string;
};

type ChartPoint = {
  key: string;
  label: string;
  total: number;
  segments: ChartSegment[];
  tipSubtitle?: string;
};

type LegendItem = {
  key: string;
  label: string;
  color: string;
  title?: string;
};

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function dateLabel(value: string | null): string {
  if (!value) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

/** Compact x-axis labels: day buckets as MM-DD (UTC), datetimes as MM-DD HH:mm (local). */
function axisDateLabel(value: string | null): string {
  if (!value) return "未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  const isUtcDayBucket = /T00:00:00(?:\.0+)?(?:Z|[+-]00:00)$/.test(value);
  if (isUtcDayBucket) {
    const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
    const dd = String(date.getUTCDate()).padStart(2, "0");
    return `${mm}-${dd}`;
  }
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mi = String(date.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function triggerTypeLabel(value: string | null | undefined): string {
  switch (value) {
    case "scheduled":
      return "定时抓取";
    case "patrol_resend":
      return "巡检补发";
    case "manual":
      return "一键手动批量";
    case "manual_method":
      return "单方式手动";
    default:
      return value || "未知";
  }
}

/** SiteDiscoveryRun.status → 中文（与 DiscoveryPanel 口径一致） */
function discoveryStatusLabel(value: string | null | undefined): string {
  switch (value) {
    case "running":
      return "探查中";
    case "completed":
      return "探查完成";
    case "failed":
      return "探查失败";
    case "cancelled":
      return "已取消";
    default:
      return value || "未知";
  }
}

function barWidthForCount(count: number): { barWidth: string | number; barMaxWidth: number } {
  if (count <= 2) return { barWidth: "88%", barMaxWidth: 220 };
  if (count <= 4) return { barWidth: "78%", barMaxWidth: 160 };
  if (count <= 7) return { barWidth: "64%", barMaxWidth: 110 };
  if (count <= 12) return { barWidth: "48%", barMaxWidth: 64 };
  if (count <= 18) return { barWidth: "38%", barMaxWidth: 36 };
  return { barWidth: "30%", barMaxWidth: 22 };
}

/** Compact legend text; API already returns names after 网站：/公众号： prefixes. */
function shortMethodLabel(label: string, maxChars = 14): string {
  const trimmed = label.trim();
  if (!trimmed) return trimmed;
  if ([...trimmed].length <= maxChars) return trimmed;
  return `${[...trimmed].slice(0, maxChars - 1).join("")}…`;
}

function rangeForPreset(preset: RangePreset, customStart: string, customEnd: string) {
  const end = preset === "custom" && customEnd
    ? new Date(`${customEnd}T23:59:59Z`)
    : new Date();
  const start = preset === "custom" && customStart
    ? new Date(`${customStart}T00:00:00Z`)
    : new Date(end.getTime() - (preset === "24h" ? 1 : preset === "7d" ? 7 : 30) * 86_400_000);
  return {
    start: start.toISOString(),
    end: end.toISOString(),
    bucket: preset === "24h" ? "hour" as const : "day" as const,
  };
}

function MetricCard({ label, value, detail }: { label: string; value: number; detail?: string }) {
  return (
    <div style={card}>
      <div style={eyebrow}>{label}</div>
      <div style={{ fontSize: 28, fontWeight: 800, color: "#101828", marginTop: 8 }}>
        {formatTokens(value)}
      </div>
      {detail && <div style={{ color: "#667085", fontSize: 12, marginTop: 6 }}>{detail}</div>}
    </div>
  );
}

function ChartCard({ title, subtitle, children }: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <section style={panel}>
      <div style={{ marginBottom: 16 }}>
        <div style={{ color: "#101828", fontSize: 16, fontWeight: 800 }}>{title}</div>
        <div style={{ color: "#667085", fontSize: 12, marginTop: 4 }}>{subtitle}</div>
      </div>
      {children}
    </section>
  );
}

type ChartTab = {
  key: string;
  title: string;
  subtitle: string;
  content: ReactNode;
};

function TabbedChartCard({
  tabs,
  activeKey,
  onChange,
}: {
  tabs: ChartTab[];
  activeKey: string;
  onChange: (key: string) => void;
}) {
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const active = tabs.find((tab) => tab.key === activeKey) ?? tabs[0];
  if (!active) return null;

  return (
    <section style={panel}>
      {/*
        Underline tabs (Ant / Material / NN/g): peer content panels use text + ink bar,
        not pill buttons. Dual signals — weight/color + bottom indicator — for 2-tab clarity.
      */}
      <div style={chartTabBar} role="tablist" aria-label="图表切换">
        {tabs.map((tab) => {
          const selected = tab.key === active.key;
          const hovered = !selected && hoveredKey === tab.key;
          return (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={selected}
              id={`chart-tab-${tab.key}`}
              aria-controls={`chart-panel-${tab.key}`}
              onClick={() => onChange(tab.key)}
              onMouseEnter={() => setHoveredKey(tab.key)}
              onMouseLeave={() => setHoveredKey(null)}
              style={{
                ...chartTab,
                color: selected ? "#175cd3" : hovered ? "#344054" : "#667085",
                fontWeight: selected ? 800 : 600,
                borderBottomColor: selected ? "#175cd3" : "transparent",
              }}
            >
              {tab.title}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`chart-panel-${active.key}`}
        aria-labelledby={`chart-tab-${active.key}`}
      >
        <div style={{ color: "#667085", fontSize: 12, marginBottom: 14 }}>{active.subtitle}</div>
        {active.content}
      </div>
    </section>
  );
}

function hexToRgba(hex: string, alpha: number): string {
  const raw = hex.replace("#", "").trim();
  const full = raw.length === 3
    ? raw.split("").map((ch) => `${ch}${ch}`).join("")
    : raw.padEnd(6, "0").slice(0, 6);
  const r = Number.parseInt(full.slice(0, 2), 16);
  const g = Number.parseInt(full.slice(2, 4), 16);
  const b = Number.parseInt(full.slice(4, 6), 16);
  if ([r, g, b].some((n) => Number.isNaN(n))) return `rgba(152, 162, 179, ${alpha})`;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function StackedBarChart({
  points,
  legend,
  emptyText,
  formatValue = formatTokens,
  totalLabel = "总量",
  showLines = false,
}: {
  points: ChartPoint[];
  legend: LegendItem[];
  emptyText: string;
  formatValue?: (value: number) => string;
  totalLabel?: string;
  showLines?: boolean;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.EChartsType | null>(null);
  const hasData = points.length > 0;

  const option = useMemo<EChartsOption>(() => {
    const categories = points.map((point) => point.label);
    const { barWidth, barMaxWidth } = barWidthForCount(points.length);
    const dense = points.length > 8;
    const hasZoom = points.length > 18;
    const scrollLegend = legend.length > 4;
    // Few series → soft gradient area; many series → flatter stacked area (less mud).
    const fewSeries = legend.length <= 3;
    const seriesValues = (item: LegendItem) => points.map((point) => {
      const segment = point.segments.find((entry) => entry.key === item.key);
      return segment?.value ?? 0;
    });
    const series = legend.map((item) => {
      const data = seriesValues(item);
      if (showLines) {
        // Stacked area line (ECharts handbook): keeps the same stack semantics as bars,
        // instead of overlapping spaghetti lines that are hard to read.
        return {
          name: item.label,
          type: "line" as const,
          stack: "total",
          data,
          smooth: true,
          symbol: "circle",
          symbolSize: fewSeries ? 7 : 5,
          showSymbol: false,
          connectNulls: true,
          sampling: "lttb" as const,
          emphasis: {
            focus: "series" as const,
            itemStyle: {
              borderColor: "#fff",
              borderWidth: 2,
              shadowBlur: 6,
              shadowColor: hexToRgba(item.color, 0.35),
            },
          },
          lineStyle: {
            width: fewSeries ? 2.5 : 1.8,
            color: item.color,
          },
          itemStyle: { color: item.color },
          areaStyle: fewSeries
            ? {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: hexToRgba(item.color, 0.28) },
                { offset: 1, color: hexToRgba(item.color, 0.02) },
              ]),
            }
            : {
              color: hexToRgba(item.color, 0.18),
              opacity: 1,
            },
        };
      }
      return {
        name: item.label,
        type: "bar" as const,
        stack: "total",
        barWidth,
        barMaxWidth,
        emphasis: { focus: "series" as const },
        itemStyle: { color: item.color, borderRadius: [3, 3, 0, 0] },
        data,
      };
    });

    return {
      color: legend.map((item) => item.color),
      animationDuration: 320,
      animationEasing: "cubicOut",
      grid: {
        left: 52,
        right: showLines ? 20 : 16,
        top: 40,
        // Reserve room for rotated labels and the dataZoom slider when it appears.
        bottom: hasZoom ? 78 : dense ? 56 : 40,
        containLabel: false,
      },
      legend: {
        // Single horizontal row; page with arrows when items overflow.
        type: "scroll",
        orient: "horizontal",
        top: 0,
        left: 0,
        right: 8,
        width: "96%",
        height: 28,
        itemWidth: showLines ? 14 : 10,
        itemHeight: showLines ? 8 : 10,
        itemGap: 10,
        pageButtonPosition: "end",
        pageIconSize: 10,
        pageButtonItemGap: 6,
        pageButtonGap: 8,
        pageTextStyle: { color: "#98a2b3", fontSize: 10 },
        textStyle: { color: "#475467", fontSize: 11 },
        data: legend.map((item) => item.label),
        formatter: (name: string) => shortMethodLabel(name, scrollLegend ? 10 : 14),
        tooltip: { show: true },
        // Click legend to isolate a series — critical when many methods overlap.
        selectedMode: true,
      },
      tooltip: {
        trigger: "axis",
        appendTo: "body",
        confine: false,
        order: "valueDesc",
        axisPointer: showLines
          ? {
            type: "cross",
            snap: true,
            label: {
              backgroundColor: "#344054",
              color: "#fff",
              fontSize: 10,
              formatter: (params) => {
                if (params.axisDimension === "y") {
                  return formatValue(Number(params.value));
                }
                return String(params.value ?? "");
              },
            },
            crossStyle: { color: "#98a2b3", width: 1, type: "dashed" },
            lineStyle: { color: "#98a2b3", width: 1, type: "dashed" },
          }
          : { type: "shadow" },
        backgroundColor: "rgba(255,255,255,0.96)",
        borderColor: "#d0d5dd",
        borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: "#475467", fontSize: 11 },
        extraCssText: "box-shadow: 0 8px 24px rgba(16,24,40,.16); max-width: 340px; backdrop-filter: blur(2px);",
        formatter: (raw) => {
          const items = Array.isArray(raw) ? raw : [raw];
          const index = typeof items[0]?.dataIndex === "number" ? items[0].dataIndex : -1;
          const point = index >= 0 ? points[index] : undefined;
          if (!point) return "";
          const marker = showLines ? "●" : "■";
          const rows = [...items]
            .filter((item) => Number(item.value ?? 0) > 0)
            .sort((a, b) => Number(b.value ?? 0) - Number(a.value ?? 0))
            .map((item) => {
              const color = typeof item.color === "string" ? item.color : "#98a2b3";
              return `<div style="display:flex;justify-content:space-between;gap:18px;margin-top:3px">
                <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                  <span style="color:${color}">${marker}</span> ${String(item.seriesName ?? "")}
                </span>
                <strong style="font-variant-numeric:tabular-nums">${formatValue(Number(item.value ?? 0))}</strong>
              </div>`;
            })
            .join("");
          return `<div>
            <div style="color:#667085;font-size:11px;word-break:break-all">${point.tipSubtitle || point.label}</div>
            <div style="color:#101828;font-size:13px;font-weight:800;margin:4px 0 8px">${totalLabel} ${formatValue(point.total)}</div>
            ${rows}
          </div>`;
        },
      },
      xAxis: {
        type: "category",
        data: categories,
        // Line/area charts read better without category gaps (ECharts basic line).
        boundaryGap: !showLines,
        axisTick: { alignWithLabel: true, show: !showLines },
        axisLine: { lineStyle: { color: "#eaecf0" } },
        axisLabel: {
          color: "#667085",
          fontSize: 10,
          hideOverlap: true,
          interval: dense ? "auto" : 0,
          rotate: dense ? 28 : 0,
          margin: 10,
        },
        splitLine: showLines
          ? { show: true, lineStyle: { color: "#f2f4f7", type: "dashed" } }
          : undefined,
      },
      yAxis: {
        type: "value",
        minInterval: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: "#eaecf0", type: "dashed" } },
        axisLabel: {
          color: "#98a2b3",
          fontSize: 10,
          formatter: (value: number) => formatValue(value),
        },
      },
      dataZoom: hasZoom
        ? [
          { type: "inside", startValue: Math.max(0, points.length - 16), endValue: points.length - 1 },
          {
            type: "slider",
            height: 18,
            bottom: 8,
            borderColor: "#eaecf0",
            fillerColor: "rgba(23, 92, 211, 0.12)",
            handleStyle: { color: "#84adff" },
            textStyle: { color: "#98a2b3", fontSize: 10 },
          },
        ]
        : undefined,
      series,
    };
  }, [points, legend, formatValue, totalLabel, showLines]);

  // 必须在有数据、容器已挂载后再 init；加载中 early-return 会导致 [] effect 永远跳过
  useEffect(() => {
    if (!hasData) {
      chartRef.current?.dispose();
      chartRef.current = null;
      return;
    }
    const host = hostRef.current;
    if (!host) return;

    const chart = echarts.getInstanceByDom(host) ?? echarts.init(host);
    chartRef.current = chart;
    chart.setOption(option, { notMerge: true });
    requestAnimationFrame(() => chart.resize());

    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(onResize) : null;
    observer?.observe(host);
    return () => {
      window.removeEventListener("resize", onResize);
      observer?.disconnect();
    };
  }, [hasData, option]);

  useEffect(() => () => {
    chartRef.current?.dispose();
    chartRef.current = null;
  }, []);

  if (!hasData) {
    return <div style={{ ...chartHeight, ...empty }}>{emptyText}</div>;
  }

  return <div ref={hostRef} style={chartHeight} />;
}

export function StatisticsDiscoveryPage({ hasSystemAccess = false }: { hasSystemAccess?: boolean }) {
  const [preset, setPreset] = useState<RangePreset>("30d");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [triggerType, setTriggerType] = useState("");
  const [showLines, setShowLines] = useState(false);
  const [discoveryTab, setDiscoveryTab] = useState<"discovery" | "trend">("discovery");
  const [queryTab, setQueryTab] = useState<"query" | "volume" | "itemAvg">("query");
  const range = useMemo(
    () => rangeForPreset(preset, customStart, customEnd),
    [preset, customStart, customEnd],
  );
  const query = { ...range, trigger_type: triggerType || undefined, limit: 100 };

  const summaryQuery = useQuery({
    queryKey: ["token-usage-summary", range],
    queryFn: () => fetchTokenUsageSummary(range),
    enabled: hasSystemAccess,
  });
  const runsQuery = useQuery({
    queryKey: ["token-usage-query-runs", query],
    queryFn: () => fetchQueryRunUsage(query),
    enabled: hasSystemAccess,
  });
  const discoveryQuery = useQuery({
    queryKey: ["token-usage-discovery-runs", range],
    queryFn: () => fetchDiscoveryRunUsage({ ...range, limit: 100 }),
    enabled: hasSystemAccess,
  });
  const itemVolumeQuery = useQuery({
    queryKey: ["item-volume-daily", range.start, range.end],
    queryFn: () => fetchItemVolumeDaily({ start: range.start, end: range.end }),
    enabled: hasSystemAccess,
  });
  const itemAvgQuery = useQuery({
    queryKey: ["token-usage-query-item-avg", query],
    queryFn: () => fetchQueryItemAvg(query),
    enabled: hasSystemAccess,
  });

  const trendChart = useMemo(() => {
    const points: ChartPoint[] = (summaryQuery.data?.trend ?? []).map((item, index) => ({
      key: `${item.bucket}-${index}`,
      label: axisDateLabel(item.bucket),
      total: item.total_tokens,
      tipSubtitle: dateLabel(item.bucket),
      segments: [
        { key: "prompt", label: "输入", value: item.prompt_tokens, color: SERIES_COLORS.prompt },
        { key: "completion", label: "输出", value: item.completion_tokens, color: SERIES_COLORS.completion },
      ],
    }));
    const legend: LegendItem[] = [
      { key: "prompt", label: "输入", color: SERIES_COLORS.prompt },
      { key: "completion", label: "输出", color: SERIES_COLORS.completion },
    ];
    return { points, legend };
  }, [summaryQuery.data]);

  const discoveryChart = useMemo(() => {
    const points: ChartPoint[] = [...(discoveryQuery.data?.runs ?? [])].reverse().map((run) => ({
      key: String(run.run_id),
      label: axisDateLabel(run.started_at),
      total: run.total_tokens,
      tipSubtitle: `${dateLabel(run.started_at)} · ${run.site_url}`,
      segments: [
        { key: "input", label: "输入", value: run.prompt_tokens, color: SERIES_COLORS.discoveryIn },
        { key: "output", label: "输出", value: run.completion_tokens, color: SERIES_COLORS.discoveryOut },
      ],
    }));
    const legend: LegendItem[] = [
      { key: "input", label: "输入", color: SERIES_COLORS.discoveryIn },
      { key: "output", label: "输出", color: SERIES_COLORS.discoveryOut },
    ];
    return { points, legend };
  }, [discoveryQuery.data]);

  const queryChart = useMemo(() => {
    const methods = new Map<number, string>();
    for (const run of runsQuery.data?.runs ?? []) {
      for (const method of run.methods) methods.set(method.method_id, method.label);
    }
    const legend: LegendItem[] = [...methods.entries()].map(([methodId, label], index) => ({
      key: String(methodId),
      label,
      title: label,
      color: METHOD_COLORS[index % METHOD_COLORS.length],
    }));
    const colorOf = (methodId: number) => {
      const index = legend.findIndex((item) => item.key === String(methodId));
      return METHOD_COLORS[(index >= 0 ? index : 0) % METHOD_COLORS.length];
    };
    const points: ChartPoint[] = [...(runsQuery.data?.runs ?? [])].reverse().map((run) => ({
      key: run.run_key,
      label: axisDateLabel(run.started_at),
      total: run.total_tokens,
      tipSubtitle: `${dateLabel(run.started_at)} · ${triggerTypeLabel(run.trigger_type)}`,
      segments: run.methods.length > 0
        ? run.methods.map((method) => ({
          key: String(method.method_id),
          label: method.label,
          value: method.total_tokens,
          color: colorOf(method.method_id),
        }))
        : [{ key: "total", label: "总量", value: run.total_tokens, color: METHOD_COLORS[0] }],
    }));
    return { points, legend };
  }, [runsQuery.data]);

  const itemVolumeChart = useMemo(() => {
    const points: ChartPoint[] = (itemVolumeQuery.data?.trend ?? [])
      .filter((item) => item.total > 0)
      .map((item, index) => ({
        key: `${item.bucket}-${index}`,
        label: axisDateLabel(item.bucket),
        total: item.total,
        tipSubtitle: `${axisDateLabel(item.bucket)} · 入库消息`,
        segments: [
          { key: "high", label: "高", value: item.high, color: SERIES_COLORS.importanceHigh },
          { key: "medium", label: "中", value: item.medium, color: SERIES_COLORS.importanceMedium },
          { key: "low", label: "低", value: item.low, color: SERIES_COLORS.importanceLow },
        ],
      }));
    const legend: LegendItem[] = [
      { key: "high", label: "高", color: SERIES_COLORS.importanceHigh },
      { key: "medium", label: "中", color: SERIES_COLORS.importanceMedium },
      { key: "low", label: "低", color: SERIES_COLORS.importanceLow },
    ];
    return { points, legend };
  }, [itemVolumeQuery.data]);

  const itemAvgChart = useMemo(() => {
    const methods = new Map<number, string>();
    for (const run of itemAvgQuery.data?.runs ?? []) {
      for (const method of run.methods) methods.set(method.method_id, method.label);
    }
    const legend: LegendItem[] = [...methods.entries()].map(([methodId, label], index) => ({
      key: String(methodId),
      label,
      title: label,
      color: METHOD_COLORS[index % METHOD_COLORS.length],
    }));
    const colorOf = (methodId: number) => {
      const index = legend.findIndex((item) => item.key === String(methodId));
      return METHOD_COLORS[(index >= 0 ? index : 0) % METHOD_COLORS.length];
    };
    const points: ChartPoint[] = [...(itemAvgQuery.data?.runs ?? [])].reverse().map((run) => ({
      key: run.run_key,
      label: axisDateLabel(run.started_at),
      total: run.avg_tokens_per_item,
      tipSubtitle: `${dateLabel(run.started_at)} · ${triggerTypeLabel(run.trigger_type)}`,
      segments: run.methods.map((method) => ({
        key: String(method.method_id),
        label: method.label,
        value: method.avg_tokens_per_item,
        color: colorOf(method.method_id),
      })),
    }));
    return { points, legend };
  }, [itemAvgQuery.data]);

  if (!hasSystemAccess) {
    return (
      <main style={page}>
        <header style={heroCard}>
          <div style={eyebrow}>Statistics And Discovery</div>
          <h1 style={pageTitle}>统计与发现</h1>
        </header>
        <div style={{ ...panel, color: "#475467" }}>该页面包含系统 Token 用量，请先在顶部使用管理密码登录。</div>
      </main>
    );
  }

  const summary = summaryQuery.data?.summary;
  const loading = summaryQuery.isLoading || runsQuery.isLoading || discoveryQuery.isLoading
    || itemVolumeQuery.isLoading || itemAvgQuery.isLoading;
  const error = summaryQuery.error || runsQuery.error || discoveryQuery.error
    || itemVolumeQuery.error || itemAvgQuery.error;

  return (
    <main style={page}>
      <header style={heroCard}>
        <div style={eyebrow}>Statistics And Discovery</div>
        <h1 style={pageTitle}>统计与发现</h1>
        <p style={heroCopy}>
          精确追踪查询与智能探查的模型输入、输出和总 Token。
        </p>
      </header>

      <div style={toolbar}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {(["24h", "7d", "30d", "custom"] as RangePreset[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setPreset(item)}
              style={preset === item ? activeButton : button}
            >
              {item === "custom" ? "自定义" : item}
            </button>
          ))}
        </div>
        {preset === "custom" && (
          <div style={{ display: "flex", gap: 8 }}>
            <input type="date" value={customStart} onChange={(e) => setCustomStart(e.target.value)} style={input} />
            <input type="date" value={customEnd} onChange={(e) => setCustomEnd(e.target.value)} style={input} />
          </div>
        )}
        <select value={triggerType} onChange={(e) => setTriggerType(e.target.value)} style={input}>
          <option value="">全部查询类型</option>
          <option value="scheduled">定时/巡检</option>
          <option value="manual">手动查询</option>
        </select>
        <button
          type="button"
          onClick={() => setShowLines((value) => !value)}
          style={showLines ? activeButton : button}
          title={showLines ? "切换为堆叠柱状图" : "切换为堆叠面积曲线图"}
        >
          {showLines ? "面积曲线" : "柱状图"}
        </button>
      </div>

      {error && <div style={errorBox}>{error instanceof Error ? error.message : "统计加载失败"}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        <MetricCard label="总 TOKEN" value={summary?.total_tokens ?? 0} detail={`${summary?.call_count ?? 0} 次模型调用`} />
        <MetricCard label="输入 TOKEN" value={summary?.prompt_tokens ?? 0} />
        <MetricCard label="输出 TOKEN" value={summary?.completion_tokens ?? 0} />
        <MetricCard label="查询消耗" value={summary?.query_tokens ?? 0} />
        <MetricCard label="探查消耗" value={summary?.discovery_tokens ?? 0} />
      </div>

      <div style={chartGrid}>
        <TabbedChartCard
          activeKey={discoveryTab}
          onChange={(key) => setDiscoveryTab(key as "discovery" | "trend")}
          tabs={[
            {
              key: "discovery",
              title: "每次探查 Token",
              subtitle: "每次站点探查的输入与输出消耗；悬停查看明细",
              content: (
                <StackedBarChart
                  points={discoveryChart.points}
                  legend={discoveryChart.legend}
                  emptyText="当前范围暂无探查数据"
                  showLines={showLines}
                />
              ),
            },
            {
              key: "trend",
              title: "时间段 Token 趋势",
              subtitle: "输入与输出 Token 按时间分系列统计",
              content: (
                <StackedBarChart
                  points={trendChart.points}
                  legend={trendChart.legend}
                  emptyText="当前范围暂无趋势数据"
                  showLines={showLines}
                />
              ),
            },
          ]}
        />
        <TabbedChartCard
          activeKey={queryTab}
          onChange={(key) => setQueryTab(key as "query" | "volume" | "itemAvg")}
          tabs={[
            {
              key: "query",
              title: "每次查询 Token",
              subtitle: "一根柱 / 一条线代表一次任务中各爬取方式的消耗；图例单行可左右翻页",
              content: (
                <StackedBarChart
                  points={queryChart.points}
                  legend={queryChart.legend}
                  emptyText="当前范围暂无查询数据"
                  showLines={showLines}
                />
              ),
            },
            {
              key: "itemAvg",
              title: "单条均 Token",
              subtitle: "与「每次查询 Token」同时间刻度；一根柱一次查询，按信息源堆叠单条均耗；查出 0 条时用模型调用次数填充",
              content: (
                <StackedBarChart
                  points={itemAvgChart.points}
                  legend={itemAvgChart.legend}
                  emptyText="当前范围暂无单条均 Token 数据"
                  totalLabel="单条均合计"
                  showLines={showLines}
                />
              ),
            },
            {
              key: "volume",
              title: "每日消息量",
              subtitle: "按入库日期分高 / 中 / 低重要性统计",
              content: (
                <StackedBarChart
                  points={itemVolumeChart.points}
                  legend={itemVolumeChart.legend}
                  emptyText="当前范围暂无消息数据"
                  formatValue={formatCount}
                  totalLabel="合计"
                  showLines={showLines}
                />
              ),
            },
          ]}
        />
      </div>

      <div style={chartGrid}>
        <ChartCard title="查询任务明细" subtitle={`共 ${runsQuery.data?.total ?? 0} 次有精确数据的任务`}>
          <div style={tableWrap}>
            {(runsQuery.data?.runs ?? []).map((run) => (
              <div
                key={run.run_key}
                style={row}
                title={run.methods.map((method) => `${method.label}: ${formatTokens(method.total_tokens)}`).join("\n") || undefined}
              >
                <div>
                  <div style={rowTitle}>{dateLabel(run.started_at)} · {triggerTypeLabel(run.trigger_type)}</div>
                  <div style={rowMeta}>
                    {run.methods.length > 0
                      ? run.methods.map((method) => `${shortMethodLabel(method.label)} ${formatTokens(method.total_tokens)}`).join(" · ")
                      : "未知方式"}
                  </div>
                </div>
                <div style={rowValue}>{formatTokens(run.total_tokens)}</div>
              </div>
            ))}
            {!loading && (runsQuery.data?.runs.length ?? 0) === 0 && <div style={empty}>当前范围暂无查询数据</div>}
          </div>
        </ChartCard>
        <ChartCard title="探查任务明细" subtitle={`共 ${discoveryQuery.data?.total ?? 0} 次有精确消耗的已完成探查`}>
          <div style={tableWrap}>
            {(discoveryQuery.data?.runs ?? []).map((run) => (
              <div key={run.run_id} style={row}>
                <div style={{ minWidth: 0 }}>
                  <div style={rowTitle}>{dateLabel(run.started_at)} · {discoveryStatusLabel(run.status)}</div>
                  <div style={{ ...rowMeta, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{run.site_url}</div>
                </div>
                <div style={rowValue}>{formatTokens(run.total_tokens)}</div>
              </div>
            ))}
            {!loading && (discoveryQuery.data?.runs.length ?? 0) === 0 && <div style={empty}>当前范围暂无探查数据</div>}
          </div>
        </ChartCard>
      </div>
    </main>
  );
}

const page: CSSProperties = { maxWidth: 1280, margin: "0 auto", padding: "28px 24px 56px" };
const heroCard: CSSProperties = {
  background: "linear-gradient(135deg, #0f172a 0%, #101828 55%, #1d2939 100%)",
  color: "#f8fafc",
  borderRadius: 8,
  padding: 24,
  minHeight: 160,
  marginBottom: 22,
  boxShadow: "0 18px 40px rgba(15, 23, 42, 0.14)",
};
const pageTitle: CSSProperties = { margin: 0, fontSize: 32, lineHeight: 1.2, color: "#f8fafc" };
const eyebrow: CSSProperties = { fontSize: 13, color: "#98a2b3", marginBottom: 10 };
const heroCopy: CSSProperties = { marginTop: 10, marginBottom: 0, color: "#d0d5dd", fontSize: 14 };
const card: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 12, padding: 18, background: "#fff", boxShadow: "0 1px 2px rgba(16,24,40,.04)" };
const panel: CSSProperties = { ...card, marginTop: 14, minWidth: 0 };
const toolbar: CSSProperties = { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 14 };
const button: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, background: "#fff", color: "#475467", padding: "7px 12px", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const activeButton: CSSProperties = { ...button, borderColor: "#84adff", background: "#eff6ff", color: "#175cd3" };
const input: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, padding: "7px 10px", background: "#fff", color: "#344054", fontSize: 12 };
const errorBox: CSSProperties = { border: "1px solid #fecdca", background: "#fef3f2", color: "#b42318", borderRadius: 8, padding: "9px 12px", fontSize: 12, marginBottom: 14 };
const chartGrid: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 14 };
const chartTabBar: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "stretch",
  gap: 4,
  // Bleed to card edges so the ink bar reads as a header rail, not a floating button row.
  margin: "-2px -18px 12px",
  padding: "0 18px",
  borderBottom: "1px solid #eaecf0",
};
const chartTab: CSSProperties = {
  appearance: "none",
  border: "none",
  borderBottom: "2px solid transparent",
  borderRadius: 0,
  marginBottom: -1,
  background: "transparent",
  color: "#667085",
  padding: "10px 14px 12px",
  fontSize: 14,
  fontWeight: 600,
  fontFamily: "inherit",
  lineHeight: 1.3,
  cursor: "pointer",
  transition: "color 160ms ease, border-color 160ms ease",
};
const chartHeight: CSSProperties = { width: "100%", height: 340 };
const tableWrap: CSSProperties = { display: "grid", maxHeight: 360, overflowY: "auto" };
const row: CSSProperties = { display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", alignItems: "center", gap: 14, padding: "11px 0", borderBottom: "1px solid #f2f4f7" };
const rowTitle: CSSProperties = { color: "#344054", fontSize: 13, fontWeight: 700 };
const rowMeta: CSSProperties = { color: "#667085", fontSize: 11, marginTop: 3 };
const rowValue: CSSProperties = { color: "#101828", fontSize: 13, fontWeight: 800, fontFamily: "var(--font-mono)" };
const empty: CSSProperties = { color: "#98a2b3", textAlign: "center", padding: 28, fontSize: 13 };
