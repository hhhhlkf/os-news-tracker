import { useState, type CSSProperties, type Dispatch, type SetStateAction } from "react";
import { clampDigitInput, INPUT_LIMITS } from "../inputLimits";
import { TimeRangePicker } from "./TimeRangePicker";
import type { NewsRunFormState } from "./runLimits";

interface RunLimitCardProps {
  formState: NewsRunFormState;
  disabled?: boolean;
  description?: string;
  defaultExpanded?: boolean;
  onChange: Dispatch<SetStateAction<NewsRunFormState>>;
}

export function RunLimitCard(props: RunLimitCardProps) {
  const {
    formState,
    disabled = false,
    description = "统一限制当前页面的抓取时间范围与总条目数。",
    defaultExpanded = true,
    onChange,
  } = props;
  const [expanded, setExpanded] = useState(defaultExpanded);

  function commit(next: SetStateAction<NewsRunFormState>) {
    const resolved = typeof next === "function" ? next(formState) : next;
    onChange(resolved);
  }

  return (
    <section
      style={{
        border: "1px solid #dbe3f0",
        borderRadius: 8,
        background: "#ffffff",
        padding: 12,
        display: "grid",
        gap: 10,
        boxShadow: "0 1px 2px rgba(16, 24, 40, 0.04)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#101828" }}>抓取限制</div>
          <div style={{ fontSize: 12, color: "#667085", lineHeight: 1.5 }}>{description}</div>
        </div>
        <button type="button" onClick={() => setExpanded((value) => !value)} style={sectionToggleStyle}>
          {expanded ? "收起" : "展开"}
        </button>
      </div>
      {expanded && (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end", paddingTop: 2 }}>
          <div style={{ display: "grid", gap: 8, minWidth: 300, flex: "1 1 360px" }}>
            <TimeRangePicker
              timeMode={formState.timeMode}
              relativeRange={formState.relativeRange}
              startDate={formState.startDate}
              endDate={formState.endDate}
              disabled={disabled}
              onTimeModeChange={(mode) => commit((state) => ({ ...state, timeMode: mode }))}
              onRelativeRangeChange={(range) => commit((state) => ({ ...state, relativeRange: range }))}
              onStartDateChange={(value) => commit((state) => ({ ...state, startDate: value }))}
              onEndDateChange={(value) => commit((state) => ({ ...state, endDate: value }))}
            />
          </div>
          <label style={{ display: "grid", gap: 6, minWidth: 172 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "#344054" }}>总条目数上限</span>
            <input
              value={formState.targetCount}
              disabled={disabled}
              maxLength={INPUT_LIMITS.countDigits}
              onChange={(event) => commit((state) => ({
                ...state,
                targetCount: clampDigitInput(event.target.value, INPUT_LIMITS.countDigits),
              }))}
              inputMode="numeric"
              style={inputStyle}
            />
          </label>
        </div>
      )}
    </section>
  );
}

const sectionToggleStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  padding: "8px 14px",
  background: "#fff",
  color: "#344054",
  fontSize: 13,
  fontWeight: 700,
  cursor: "pointer",
};

const inputStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 8,
  padding: "9px 12px",
  fontSize: 14,
  color: "#101828",
  background: "#ffffff",
};
