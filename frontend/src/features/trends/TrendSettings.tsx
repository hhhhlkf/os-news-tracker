import { useEffect, useState, type CSSProperties, type FormEvent } from "react";
import type { TrendIdentityTemplate, TrendSettings as TrendSettingsData } from "./types";

interface TrendSettingsProps {
  settings: TrendSettingsData | undefined;
  templates: TrendIdentityTemplate[];
  isLoading: boolean;
  isSaving: boolean;
  error: Error | null;
  onSave: (settings: TrendSettingsData) => Promise<void>;
}

const emptySettings: TrendSettingsData = {
  window_mode: "relative",
  window_start_date: null,
  window_end_date: null,
  relative_window_unit: "week",
  relative_window_value: 4,
  trend_count: 5,
  storyline_candidate_goal: 20,
  trigger_mode: "manual",
  schedule_rule: null,
  scheduled_template_id: null,
};

const weekdays = [
  { value: "monday", label: "星期一" },
  { value: "tuesday", label: "星期二" },
  { value: "wednesday", label: "星期三" },
  { value: "thursday", label: "星期四" },
  { value: "friday", label: "星期五" },
  { value: "saturday", label: "星期六" },
  { value: "sunday", label: "星期日" },
];
const scheduleTimes = Array.from({ length: 48 }, (_, index) => {
  const hour = Math.floor(index / 2);
  const minute = index % 2 === 0 ? "00" : "30";
  return `${String(hour).padStart(2, "0")}:${minute}`;
});

function scheduleParts(rule: string | null): { weekday: string; time: string } {
  const match = rule?.match(/^weekly_(monday|tuesday|wednesday|thursday|friday|saturday|sunday)_(\d{2}:\d{2})$/);
  return match ? { weekday: match[1], time: match[2] } : { weekday: "monday", time: "09:00" };
}

function weeklyScheduleRule(weekday: string, time: string): string {
  return `weekly_${weekday}_${time}`;
}

export function TrendSettings({ settings, templates, isLoading, isSaving, error, onSave }: TrendSettingsProps) {
  const [form, setForm] = useState<TrendSettingsData>(emptySettings);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState(false);
  const schedule = scheduleParts(form.schedule_rule);

  useEffect(() => {
    if (settings) setForm(settings);
  }, [settings]);

  function update<K extends keyof TrendSettingsData>(key: K, value: TrendSettingsData[K]): void {
    setSavedNotice(false);
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setValidationError(null);
    if (form.window_mode === "date_range") {
      if (!form.window_start_date || !form.window_end_date) {
        setValidationError("请选择完整的日期窗口区间。");
        return;
      }
      if (form.window_start_date > form.window_end_date) {
        setValidationError("日期窗口的开始日期不能晚于结束日期。");
        return;
      }
    } else if (form.relative_window_value < 1) {
      setValidationError("近 N 周或近 N 月中的 N 必须至少为 1。");
      return;
    }
    if (form.trend_count < 1 || form.storyline_candidate_goal < 1) {
      setValidationError("趋势数量 X 与候选目标 G 都必须至少为 1。");
      return;
    }
    if (form.trigger_mode === "scheduled" && !form.scheduled_template_id) {
      setValidationError("定时触发需要选择当前定时模板。");
      return;
    }
    await onSave({
      ...form,
      schedule_rule: form.trigger_mode === "scheduled"
        ? weeklyScheduleRule(schedule.weekday, schedule.time)
        : form.schedule_rule?.trim() || null,
      scheduled_template_id: form.scheduled_template_id || null,
    });
    setSavedNotice(true);
  }

  return (
    <section style={panel}>
      <div style={sectionTitle}>全局趋势设置</div>
      <div style={sectionCopy}>这些设置属于趋势工作台，不绑定某一个身份模板。G 默认值为 20。</div>
      {error && <div style={errorBox}>{error.message}</div>}
      <form onSubmit={(event) => void handleSubmit(event)} style={formStyle}>
        <label style={field}>
          <span style={labelText}>时间窗口</span>
          <select
            value={form.window_mode}
            onChange={(event) => update("window_mode", event.target.value as TrendSettingsData["window_mode"])}
            style={input}
            disabled={isLoading}
          >
            <option value="relative">近 N 周或近 N 月</option>
            <option value="date_range">自定义日期区间</option>
          </select>
        </label>
        {form.window_mode === "relative" ? (
          <label style={field}>
            <span style={labelText}>分析范围</span>
            <div style={relativeWindowFields}>
              <span style={relativePrefix}>近</span>
              <input type="number" min={1} max={104} value={form.relative_window_value} onChange={(event) => update("relative_window_value", Number(event.target.value) || 0)} style={input} disabled={isLoading} />
              <select value={form.relative_window_unit} onChange={(event) => update("relative_window_unit", event.target.value as TrendSettingsData["relative_window_unit"])} style={input} disabled={isLoading}>
                <option value="week">周</option>
                <option value="month">月</option>
              </select>
            </div>
          </label>
        ) : (
          <div style={dateRangeFields}>
            <label style={field}>
              <span style={labelText}>开始日期</span>
              <input type="date" value={form.window_start_date ?? ""} onChange={(event) => update("window_start_date", event.target.value || null)} style={input} disabled={isLoading} />
            </label>
            <label style={field}>
              <span style={labelText}>结束日期</span>
              <input type="date" value={form.window_end_date ?? ""} onChange={(event) => update("window_end_date", event.target.value || null)} style={input} disabled={isLoading} />
            </label>
          </div>
        )}
        <label style={field}>
          <span style={labelText}>趋势数量 X</span>
          <input type="number" min={1} value={form.trend_count} onChange={(event) => update("trend_count", Number(event.target.value) || 0)} style={input} disabled={isLoading} />
        </label>
        <label style={field}>
          <span style={labelText}>故事线候选目标 G</span>
          <input type="number" min={1} value={form.storyline_candidate_goal} onChange={(event) => update("storyline_candidate_goal", Number(event.target.value) || 0)} style={input} disabled={isLoading} />
        </label>
        <label style={field}>
          <span style={labelText}>当前定时模板</span>
          <select value={form.scheduled_template_id ?? ""} onChange={(event) => update("scheduled_template_id", event.target.value || null)} style={input} disabled={isLoading || form.trigger_mode !== "scheduled"}>
            <option value="">请选择模板</option>
            {templates.map((template) => <option key={template.template_id} value={template.template_id}>{template.name}</option>)}
          </select>
        </label>
        <label style={{ ...field, gridColumn: "1 / -1" }}>
          <span style={labelText}>定时规则</span>
          <div style={scheduleFields}>
            <select
              value={schedule.weekday}
              onChange={(event) => update("schedule_rule", weeklyScheduleRule(event.target.value, schedule.time))}
              style={input}
              disabled={isLoading || form.trigger_mode !== "scheduled"}
            >
              {weekdays.map((day) => <option key={day.value} value={day.value}>{day.label}</option>)}
            </select>
            <select
              value={schedule.time}
              onChange={(event) => update("schedule_rule", weeklyScheduleRule(schedule.weekday, event.target.value))}
              style={input}
              disabled={isLoading || form.trigger_mode !== "scheduled"}
            >
              {scheduleTimes.map((time) => <option key={time} value={time}>{time}</option>)}
            </select>
          </div>
          <span style={hint}>
            每周在所选星期和时刻按北京时间执行一次趋势分析：先复用或顺序补齐事实层，再运行当前定时模板；
            同一计划时刻只会执行一次，执行结果可在第 4 步查看。
          </span>
        </label>
        {validationError && <div style={{ ...errorBox, gridColumn: "1 / -1", margin: 0 }}>{validationError}</div>}
        <div style={actions}>
          <button type="submit" style={primaryButton} disabled={isLoading || isSaving}>
            {isSaving ? "保存中…" : "保存全局设置"}
          </button>
          <button
            type="button"
            aria-pressed={form.trigger_mode === "scheduled"}
            onClick={() => update("trigger_mode", form.trigger_mode === "scheduled" ? "manual" : "scheduled")}
            style={{
              ...scheduleToggle,
              ...(form.trigger_mode === "scheduled" ? scheduleToggleEnabled : scheduleToggleDisabled),
            }}
            disabled={isLoading || isSaving}
          >
            定时更新：{form.trigger_mode === "scheduled" ? "已启用" : "已关闭"}
          </button>
          {savedNotice && <span style={saved}>已保存</span>}
        </div>
      </form>
    </section>
  );
}

const panel: CSSProperties = { border: "1px solid #eaecf0", borderRadius: 12, padding: 20, background: "#fff", boxShadow: "0 1px 2px rgba(16,24,40,.04)" };
const sectionTitle: CSSProperties = { color: "#101828", fontSize: 16, fontWeight: 800 };
const sectionCopy: CSSProperties = { color: "#667085", fontSize: 12, marginTop: 5, lineHeight: 1.6 };
const formStyle: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, marginTop: 18 };
const field: CSSProperties = { display: "grid", gap: 6, minWidth: 0 };
const relativeWindowFields: CSSProperties = { display: "grid", gridTemplateColumns: "auto minmax(70px, 1fr) minmax(70px, 1fr)", alignItems: "center", gap: 8 };
const relativePrefix: CSSProperties = { color: "#344054", fontSize: 13, fontWeight: 700 };
const dateRangeFields: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, gridColumn: "1 / -1" };
const scheduleFields: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 };
const labelText: CSSProperties = { color: "#344054", fontSize: 12, fontWeight: 700 };
const input: CSSProperties = { width: "100%", boxSizing: "border-box", border: "1px solid #d0d5dd", borderRadius: 8, padding: "8px 10px", color: "#344054", background: "#fff", fontSize: 13 };
const scheduleToggle: CSSProperties = { borderRadius: 8, padding: "8px 12px", fontSize: 12, fontWeight: 800, cursor: "pointer" };
const scheduleToggleEnabled: CSSProperties = { border: "1px solid #175cd3", background: "#eff8ff", color: "#175cd3" };
const scheduleToggleDisabled: CSSProperties = { border: "1px solid #d0d5dd", background: "#fff", color: "#475467" };
const hint: CSSProperties = { color: "#98a2b3", fontSize: 11, lineHeight: 1.5 };
const actions: CSSProperties = { gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 10, marginTop: 4 };
const primaryButton: CSSProperties = { border: "1px solid #175cd3", borderRadius: 8, background: "#175cd3", color: "#fff", padding: "8px 12px", fontSize: 12, fontWeight: 800, cursor: "pointer" };
const saved: CSSProperties = { color: "#027a48", fontSize: 12, fontWeight: 700 };
const errorBox: CSSProperties = { border: "1px solid #fecdca", borderRadius: 8, background: "#fef3f2", color: "#b42318", padding: "8px 10px", marginTop: 12, fontSize: 12 };
