export interface ItemSummary {
  id: number;
  title: string;
  title_tldr: string | null;
  main_category: string | null;
  info_type: string | null;
  importance: string | null;
  published_at: string | null;
  fetched_at: string | null;
  url: string;
}

export interface ItemDetail extends ItemSummary {
  summary: string | null;
  key_points: string[];
  why_it_matters: string | null;
  llm_confidence: number | null;
  sub_tags: string[];
  entities: { type: string; name: string }[];
  source_links: { source_id: number; url: string }[];
}

export interface FacetValue { value: string; count: number; }
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
}
export interface ItemListResponse { total: number; items: ItemSummary[]; }

export type ManualNewsRunState =
  | "idle"
  | "collecting"
  | "processing"
  | "stopping"
  | "completed"
  | "failed"
  | "stopped";

export type ManualNewsTimeMode = "relative" | "absolute";
export type ManualNewsRelativeRange = "24h" | "7d" | "30d";

export interface ManualNewsRunRequest {
  time_mode: ManualNewsTimeMode;
  relative_range?: ManualNewsRelativeRange | null;
  start_at?: string | null;
  end_at?: string | null;
  target_count: number;
}

export interface TimeFilterStats {
  missing_published_at: number;
  before_start: number;
  after_end: number;
  matched: number;
}

export interface ManualNewsRunStatus {
  state: ManualNewsRunState;
  time_mode: ManualNewsTimeMode | null;
  relative_range: ManualNewsRelativeRange | null;
  start_at: string | null;
  end_at: string | null;
  target_count: number | null;
  discovered_count: number;
  queued_count: number;
  processed_count: number;
  saved_count: number;
  fulfilled: boolean;
  gap_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
  time_filter_stats: TimeFilterStats | null;
}
