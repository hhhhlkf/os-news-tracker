export type MorningCrawlFrequency = "daily" | "weekly";
export type MorningCrawlLookback = "24h" | "7d" | "30d" | "all";

export interface MorningCrawlConfig {
  enabled: boolean;
  run_time: string;
  frequency: MorningCrawlFrequency;
  lookback_window: MorningCrawlLookback;
  patrol_interval_hours: number;
  last_run_at: string | null;
  last_run_status: string | null;
  last_success_date: string | null;
  next_run_at: string | null;
}

export interface MorningCrawlConfigUpdateRequest {
  enabled?: boolean;
  run_time?: string;
  frequency?: MorningCrawlFrequency;
  lookback_window?: MorningCrawlLookback;
  patrol_interval_hours?: number;
}

export interface MorningCrawlRunSummary {
  id: number;
  trigger_type: string;
  status: string;
  run_date: string | null;
  total_methods: number;
  success_methods: number;
  failed_methods: number;
  stored_count: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

export interface MorningCrawlDashboard {
  config: MorningCrawlConfig;
  active_method_count: number;
  today_status: string;
  today_run: MorningCrawlRunSummary | null;
  recent_runs: MorningCrawlRunSummary[];
  is_running: boolean;
}
