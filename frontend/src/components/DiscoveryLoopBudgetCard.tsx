import { useEffect, useMemo, useState, type CSSProperties } from "react";

export type DiscoveryLoopPresetId = "save" | "balanced" | "deep";

export interface DiscoveryLoopBudget {
  contextMemory: number;
  toolKb: number;
  depth: number;
  tokenBudget: number;
}

const STORAGE_KEY = "os-news-tracker.discovery-loop-budget";

const LIMITS = {
  contextMemory: { min: 20, max: 80, step: 1, unit: "条" },
  toolKb: { min: 8, max: 64, step: 1, unit: "KB" },
  depth: { min: 20, max: 150, step: 1, unit: "轮" },
  tokenBudget: { min: 50_000, max: 200_000, step: 10_000, unit: "万" },
} as const;

export const DISCOVERY_LOOP_PRESETS: Record<DiscoveryLoopPresetId, DiscoveryLoopBudget> = {
  save: { contextMemory: 20, toolKb: 24, depth: 48, tokenBudget: 50_000 },
  balanced: { contextMemory: 40, toolKb: 48, depth: 80, tokenBudget: 100_000 },
  deep: { contextMemory: 80, toolKb: 64, depth: 150, tokenBudget: 200_000 },
};

const PRESET_META: Array<{ id: DiscoveryLoopPresetId; label: string; hint: string }> = [
  { id: "save", label: "节省", hint: "轻量站点，少轮次少上下文" },
  { id: "balanced", label: "均衡", hint: "日常探查默认" },
  { id: "deep", label: "深度", hint: "复杂站点，更深轮次" },
];

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function formatTokens(value: number): string {
  const wan = value / 10_000;
  return Number.isInteger(wan) ? `${wan} 万` : `${wan.toFixed(1)} 万`;
}

function budgetsEqual(left: DiscoveryLoopBudget, right: DiscoveryLoopBudget): boolean {
  return (
    left.contextMemory === right.contextMemory
    && left.toolKb === right.toolKb
    && left.depth === right.depth
    && left.tokenBudget === right.tokenBudget
  );
}

export function readStoredDiscoveryLoopBudget(): DiscoveryLoopBudget {
  if (typeof window === "undefined") return DISCOVERY_LOOP_PRESETS.balanced;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return DISCOVERY_LOOP_PRESETS.balanced;
    const parsed = JSON.parse(raw) as Partial<DiscoveryLoopBudget>;
    return {
      contextMemory: clamp(Number(parsed.contextMemory) || DISCOVERY_LOOP_PRESETS.balanced.contextMemory, LIMITS.contextMemory.min, LIMITS.contextMemory.max),
      toolKb: clamp(Number(parsed.toolKb) || DISCOVERY_LOOP_PRESETS.balanced.toolKb, LIMITS.toolKb.min, LIMITS.toolKb.max),
      depth: clamp(Number(parsed.depth) || DISCOVERY_LOOP_PRESETS.balanced.depth, LIMITS.depth.min, LIMITS.depth.max),
      tokenBudget: clamp(Number(parsed.tokenBudget) || DISCOVERY_LOOP_PRESETS.balanced.tokenBudget, LIMITS.tokenBudget.min, LIMITS.tokenBudget.max),
    };
  } catch {
    return DISCOVERY_LOOP_PRESETS.balanced;
  }
}

function writeStoredBudget(budget: DiscoveryLoopBudget): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(budget));
}

export function DiscoveryLoopBudgetCard() {
  const [budget, setBudget] = useState<DiscoveryLoopBudget>(readStoredDiscoveryLoopBudget);
  const [advancedOpen, setAdvancedOpen] = useState(true);

  useEffect(() => {
    const normalized = readStoredDiscoveryLoopBudget();
    if (!budgetsEqual(budget, normalized)) {
      setBudget(normalized);
      writeStoredBudget(normalized);
    }
  }, []); // Reconcile values saved under an older set of backend limits.

  const activePreset = useMemo(
    () => (Object.keys(DISCOVERY_LOOP_PRESETS) as DiscoveryLoopPresetId[]).find((id) => budgetsEqual(budget, DISCOVERY_LOOP_PRESETS[id])) ?? null,
    [budget],
  );

  function commit(next: DiscoveryLoopBudget): void {
    setBudget(next);
    writeStoredBudget(next);
  }

  function applyPreset(id: DiscoveryLoopPresetId): void {
    commit(DISCOVERY_LOOP_PRESETS[id]);
  }

  function patch<K extends keyof DiscoveryLoopBudget>(key: K, raw: string): void {
    const limit = LIMITS[key];
    commit({ ...budget, [key]: clamp(Number(raw) || limit.min, limit.min, limit.max) });
  }

  return (
    <section style={card}>
      <style>{RANGE_TONE_CSS}</style>
      <div style={head}>
        <div>
          <div style={title}>探查资源</div>
          <div style={kicker}>用预设控制探查消耗；复杂站点再加大深度与 Token。</div>
        </div>
        <button type="button" onClick={() => setAdvancedOpen((open) => !open)} style={toggle}>
          {advancedOpen ? "收起滑杆" : "高级滑杆"}
        </button>
      </div>

      <div style={presetRow} role="group" aria-label="探查资源预设">
        {PRESET_META.map((preset) => {
          const active = activePreset === preset.id;
          return (
            <button
              key={preset.id}
              type="button"
              onClick={() => applyPreset(preset.id)}
              style={active ? presetActive : presetIdle}
              aria-pressed={active}
            >
              <span style={presetLabel}>{preset.label}</span>
              <span style={presetHint}>{preset.hint}</span>
            </button>
          );
        })}
      </div>

      {!activePreset && <div style={customHint}>当前为自定义组合，不再对应某个预设。</div>}

      {advancedOpen && (
        <div style={sliderStack}>
          <SliderRow
            tone="memory"
            label="上下文记忆"
            hint="保留最近对话与工具结果的条数"
            valueLabel={`${budget.contextMemory} 条`}
            minLabel="20 条"
            maxLabel="80 条"
            min={LIMITS.contextMemory.min}
            max={LIMITS.contextMemory.max}
            step={LIMITS.contextMemory.step}
            value={budget.contextMemory}
            onChange={(value) => patch("contextMemory", value)}
          />
          <SliderRow
            tone="tool"
            label="工具信息量"
            hint="单次工具结果写入上下文的长度"
            valueLabel={`${budget.toolKb} KB`}
            minLabel="8 KB"
            maxLabel="64 KB"
            min={LIMITS.toolKb.min}
            max={LIMITS.toolKb.max}
            step={LIMITS.toolKb.step}
            value={budget.toolKb}
            onChange={(value) => patch("toolKb", value)}
          />
          <SliderRow
            tone="depth"
            label="探查深度"
            hint="单次探查最多迭代轮数"
            valueLabel={`${budget.depth} 轮`}
            minLabel="20 轮"
            maxLabel="150 轮"
            min={LIMITS.depth.min}
            max={LIMITS.depth.max}
            step={LIMITS.depth.step}
            value={budget.depth}
            onChange={(value) => patch("depth", value)}
          />
          <SliderRow
            tone="token"
            label="Token 预算"
            hint="达到阈值后压缩上下文，不硬停"
            valueLabel={formatTokens(budget.tokenBudget)}
            minLabel="5 万"
            maxLabel="20 万"
            min={LIMITS.tokenBudget.min}
            max={LIMITS.tokenBudget.max}
            step={LIMITS.tokenBudget.step}
            value={budget.tokenBudget}
            onChange={(value) => patch("tokenBudget", value)}
          />
        </div>
      )}

    </section>
  );
}

function SliderRow({
  tone,
  label,
  hint,
  valueLabel,
  minLabel,
  maxLabel,
  min,
  max,
  step,
  value,
  onChange,
}: {
  tone: SliderTone;
  label: string;
  hint: string;
  valueLabel: string;
  minLabel: string;
  maxLabel: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: string) => void;
}) {
  const color = SLIDER_TONES[tone];
  const filled = ((value - min) / (max - min)) * 100;
  return (
    <label style={sliderRow}>
      <div style={sliderHead}>
        <div>
          <div style={{ ...sliderLabel, color: color.ink }}>{label}</div>
          <div style={sliderHint}>{hint}</div>
        </div>
        <div style={{ ...sliderValue, color: color.ink }}>{valueLabel}</div>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={`discovery-budget-range discovery-budget-range--${tone}`}
        style={{
          ...range,
          accentColor: color.ink,
          background: `linear-gradient(to right, ${color.ink} 0%, ${color.ink} ${filled}%, ${color.track} ${filled}%, ${color.track} 100%)`,
        }}
      />
      <div style={sliderEnds}>
        <span>{minLabel}</span>
        <span>{maxLabel}</span>
      </div>
    </label>
  );
}

type SliderTone = "memory" | "tool" | "depth" | "token";

const SLIDER_TONES: Record<SliderTone, { ink: string; track: string }> = {
  memory: { ink: "#2f7d6d", track: "#d7ebe6" },
  tool: { ink: "#3d6eb5", track: "#d8e4f4" },
  depth: { ink: "#6b5ea8", track: "#e4dff2" },
  token: { ink: "#c17a2e", track: "#f3e6d2" },
};

const RANGE_TONE_CSS = `
.discovery-budget-range {
  -webkit-appearance: none;
  appearance: none;
  height: 8px;
  border-radius: 999px;
  outline: none;
}
.discovery-budget-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  border: 2px solid currentColor;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.18);
  cursor: pointer;
}
.discovery-budget-range--memory { color: #2f7d6d; }
.discovery-budget-range--tool { color: #3d6eb5; }
.discovery-budget-range--depth { color: #6b5ea8; }
.discovery-budget-range--token { color: #c17a2e; }
`;

const card: CSSProperties = {
  border: "1px solid #dbe3f0",
  borderRadius: 8,
  background: "#ffffff",
  padding: 12,
  display: "grid",
  gap: 12,
  boxShadow: "0 1px 2px rgba(16, 24, 40, 0.04)",
};
const head: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" };
const title: CSSProperties = { fontSize: 14, fontWeight: 700, color: "#101828" };
const kicker: CSSProperties = { marginTop: 4, fontSize: 12, color: "#667085", lineHeight: 1.5 };
const toggle: CSSProperties = {
  flexShrink: 0,
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  background: "#fff",
  color: "#344054",
  padding: "6px 10px",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
};
const presetRow: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8 };
const presetIdle: CSSProperties = {
  display: "grid",
  gap: 4,
  textAlign: "left",
  border: "1px solid #e4e7ec",
  borderRadius: 8,
  background: "#f8fafc",
  padding: "8px 9px",
  cursor: "pointer",
};
const presetActive: CSSProperties = {
  ...presetIdle,
  borderColor: "#b9d4ff",
  background: "#eff6ff",
};
const presetLabel: CSSProperties = { fontSize: 13, fontWeight: 800, color: "#101828" };
const presetHint: CSSProperties = { fontSize: 11, color: "#667085", lineHeight: 1.4 };
const customHint: CSSProperties = { fontSize: 12, color: "#b54708" };
const sliderStack: CSSProperties = { display: "grid", gap: 12 };
const sliderRow: CSSProperties = { display: "grid", gap: 6 };
const sliderHead: CSSProperties = { display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start" };
const sliderLabel: CSSProperties = { fontSize: 12, fontWeight: 700, color: "#344054" };
const sliderHint: CSSProperties = { marginTop: 2, fontSize: 11, color: "#667085", lineHeight: 1.4 };
const sliderValue: CSSProperties = { flexShrink: 0, fontSize: 12, fontWeight: 800, fontFamily: "JetBrains Mono, ui-monospace, monospace" };
const range: CSSProperties = { width: "100%", margin: 0 };
const sliderEnds: CSSProperties = { display: "flex", justifyContent: "space-between", fontSize: 10, color: "#98a2b3" };
