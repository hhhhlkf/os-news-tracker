import type { ManualNewsRelativeRange, ManualNewsTimeMode } from "../types";

interface TimeRangePickerProps {
  timeMode: ManualNewsTimeMode;
  relativeRange: ManualNewsRelativeRange;
  startDate: string;
  endDate: string;
  disabled?: boolean;
  onTimeModeChange: (value: ManualNewsTimeMode) => void;
  onRelativeRangeChange: (value: ManualNewsRelativeRange) => void;
  onStartDateChange: (value: string) => void;
  onEndDateChange: (value: string) => void;
}

const timeModes: { value: ManualNewsTimeMode; label: string }[] = [
  { value: "relative", label: "相对范围" },
  { value: "absolute", label: "绝对范围" },
];

const relativeRanges: { value: ManualNewsRelativeRange; label: string }[] = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

function segmentButton(active: boolean, disabled: boolean) {
  return {
    border: "1px solid transparent",
    background: active ? "#175cd3" : "#ffffff",
    color: active ? "#ffffff" : "#344054",
    borderRadius: 8,
    padding: "10px 12px",
    fontSize: 13,
    fontWeight: 600,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
    minWidth: 72,
  } as const;
}

export function TimeRangePicker(props: TimeRangePickerProps) {
  const {
    timeMode,
    relativeRange,
    startDate,
    endDate,
    disabled = false,
    onTimeModeChange,
    onRelativeRangeChange,
    onStartDateChange,
    onEndDateChange,
  } = props;

  return (
    <div style={{ display: "grid", gap: 10, minWidth: 320, flex: "1 1 420px" }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {timeModes.map((option) => (
          <button
            key={option.value}
            type="button"
            disabled={disabled}
            onClick={() => onTimeModeChange(option.value)}
            style={segmentButton(timeMode === option.value, disabled)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {timeMode === "relative" ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {relativeRanges.map((option) => (
            <button
              key={option.value}
              type="button"
              disabled={disabled}
              onClick={() => onRelativeRangeChange(option.value)}
              style={segmentButton(relativeRange === option.value, disabled)}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <label style={{ display: "grid", gap: 6, color: "#475467", fontSize: 13 }}>
            <span>开始日期</span>
            <input
              type="date"
              value={startDate}
              disabled={disabled}
              onChange={(event) => onStartDateChange(event.target.value)}
              style={{
                border: "1px solid #d0d5dd",
                borderRadius: 8,
                padding: "10px 12px",
                minWidth: 160,
                fontSize: 14,
                color: "#101828",
                background: "#fff",
              }}
            />
          </label>
          <label style={{ display: "grid", gap: 6, color: "#475467", fontSize: 13 }}>
            <span>结束日期</span>
            <input
              type="date"
              value={endDate}
              disabled={disabled}
              onChange={(event) => onEndDateChange(event.target.value)}
              style={{
                border: "1px solid #d0d5dd",
                borderRadius: 8,
                padding: "10px 12px",
                minWidth: 160,
                fontSize: 14,
                color: "#101828",
                background: "#fff",
              }}
            />
          </label>
        </div>
      )}
    </div>
  );
}
