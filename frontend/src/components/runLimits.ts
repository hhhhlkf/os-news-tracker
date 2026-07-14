import type {
  ManualNewsRelativeRange,
  ManualNewsRunRequest,
  ManualNewsTimeMode,
} from "../types";

export interface NewsRunFormState {
  timeMode: ManualNewsTimeMode;
  relativeRange: ManualNewsRelativeRange;
  startDate: string;
  endDate: string;
  targetCount: string;
}

export function buildNewsRunFormState(): NewsRunFormState {
  return {
    timeMode: "relative",
    relativeRange: "7d",
    startDate: "",
    endDate: "",
    targetCount: "50",
  };
}

export function toAbsoluteDateTime(value: string, endOfDay: boolean): string | null {
  if (!value) {
    return null;
  }
  return `${value}T${endOfDay ? "23:59:59.999" : "00:00:00.000"}Z`;
}

export function buildManualNewsRunRequest(formState: NewsRunFormState): ManualNewsRunRequest | null {
  if (formState.timeMode === "absolute" && (!formState.startDate || !formState.endDate)) {
    return null;
  }
  const targetCount = Number(formState.targetCount);
  if (!Number.isFinite(targetCount) || targetCount <= 0) {
    return null;
  }
  return {
    time_mode: formState.timeMode,
    relative_range: formState.timeMode === "relative" ? formState.relativeRange : null,
    start_at: formState.timeMode === "absolute" ? toAbsoluteDateTime(formState.startDate, false) : null,
    end_at: formState.timeMode === "absolute" ? toAbsoluteDateTime(formState.endDate, true) : null,
    target_count: targetCount,
  };
}
