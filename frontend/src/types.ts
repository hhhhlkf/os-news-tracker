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
  why_it_matters?: string | null;
}

export interface ItemDetail extends ItemSummary {
  summary: string | null;
  key_points: string[];
  llm_confidence: number | null;
  sub_tags: string[];
  source_links: { source_id: number; url: string }[];
}

export interface FacetValue { value: string; count: number; }
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
  sub_tags: FacetValue[];
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

export interface AgentCrawlRunRequest {
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

export interface NewsRunLogEntry {
  id: number;
  ts: string;
  level: "info" | "warning" | "error" | string;
  stage: string;
  source: string | null;
  message: string;
  [key: string]: unknown;
}

export interface NewsRunLogsResponse {
  logs: NewsRunLogEntry[];
}

export type SourceShape = "rss" | "api" | "page_monitor" | "search" | "agent_crawl";

export const MAIN_CATEGORIES = [
  "OS跟踪来源",
  "友商产品信息",
  "软件包适配",
  "OS性能发展",
  "司内AI工具",
] as const;

export type MainCategory = typeof MAIN_CATEGORIES[number];

export interface CrawlSource {
  id: number;
  name: string;
  url: string;
  type: SourceShape | string;
  main_category: string | null;
  enabled: boolean;
}

export interface SourceDetectResponse {
  detected_type: SourceShape | string;
  name_suggestion: string;
  api_config: Record<string, unknown> | null;
  notes: string[];
}

export interface ProbeFieldsConfig {
  title?: string | null;
  url?: string | null;
  url_template?: string | null;
  published_at?: string | null;
  content?: string[] | string | null;
}

export interface ProbeConfig {
  mode: string;
  method: string;
  url?: string | null;
  headers?: Record<string, string> | null;
  query?: Record<string, string> | null;
  json_body?: Record<string, unknown> | null;
  items_path?: string | null;
  fields: ProbeFieldsConfig;
}

export interface SourceCreateRequest {
  url: string;
  name?: string | null;
  main_category: string;
  type?: string | null;
  adapter?: string | null;
  api_config?: Record<string, unknown> | null;
  link_selector?: string | null;
  title_selector?: string | null;
  date_selector?: string | null;
}

export interface DiscoverSampleItem {
  title: string;
  url: string;
  published_at: string | null;
  content_preview: string;
}

export interface DiscoverCandidate {
  api_url: string;
  method: string;
  status: number;
  items_path: string;
  items_count: number;
  fields: Record<string, unknown>;
  score: number;
}

export interface DiscoverResponse {
  root_url: string;
  success: boolean;
  api_url: string | null;
  method: string;
  items_path: string | null;
  fields: Record<string, unknown>;
  name_suggestion: string;
  sample_items: DiscoverSampleItem[];
  real_content_count: number;
  candidates: DiscoverCandidate[];
  notes: string[];
  created_source?: {
    id: number;
    name: string;
    url: string;
    type: string;
    main_category: string | null;
  };
}

export interface DiscoverRequest {
  url: string;
  create_source?: boolean;
  name?: string | null;
  main_category?: string | null;
}

export interface CreateFromProbeRequest {
  api_url: string;
  method?: string;
  items_path?: string | null;
  fields?: Record<string, unknown>;
  name?: string | null;
  main_category: string;
}

export type AgentRunStage =
  | "planning"
  | "crawling"
  | "quality"
  | "summarizing"
  | "completed"
  | "failed";

export type AgentRunStatus = "running" | "completed" | "failed";

export interface AgentSourceConfig {
  focus_areas: string[];
  topic_groups: string[];
  crawl_depth: number;
  max_urls_per_run: number;
  quality_threshold: number;
  crawl_workers: number;
  quality_workers: number;
  summary_workers: number;
}

export interface AgentSource {
  id: number;
  name: string;
  url: string;
  enabled: boolean;
  config: AgentSourceConfig | null;
}

export interface AgentSourceCandidate {
  id: number;
  name: string;
  url: string;
  source_type: string;
  main_category: string | null;
}

export interface AgentSourceCandidatesResponse {
  items: AgentSourceCandidate[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface AgentRunRecord {
  id: number;
  status: AgentRunStatus;
  current_stage: AgentRunStage;
  stage_message: string | null;
  plan_urls_count: number;
  fetched_count: number;
  quality_passed: number;
  items_created: number;
  target_count?: number | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
}

export interface AgentRunTriggerResponse {
  message: string;
  source_id: number;
  accepted: boolean;
}

export interface AgentCandidateRunResponse {
  accepted: boolean;
  created: boolean;
  candidate_source_id: number;
  agent_source_id: number;
  message: string;
}

// ---- Discovery ----
export interface DiscoveryNodeTraceEntry {
  step: string;
  status: string;
  ts: string;
  summary?: Record<string, unknown>;
}

export interface DiscoveryRun {
  id: number;
  site_url: string;
  status: "running" | "completed" | "failed" | "cancelled";
  resulting_method_id: number | null;
  llm_token_usage: number;
  node_trace: DiscoveryNodeTraceEntry[];
  retry_count: number;
  current_step: string | null;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
}

export interface DiscoveryRunSummary {
  id: number;
  site_url: string;
  status: string;
  resulting_method_id: number | null;
  llm_token_usage: number;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
}

export type CrawlMethodStatus = "active" | "disabled" | "failed";

export interface CrawlMethod {
  id: number;
  source_name?: string | null;
  domain: string;
  entry_url: string;
  status: CrawlMethodStatus;
  signature: string;
  last_run_at: string | null;
  last_run_status: string | null;
  quality_score?: number | null;
  quality_grade?: string | null;
  quality_reason?: string | null;
  quality_sample_count?: number | null;
  density_score?: number | null;
  density_daily_avg?: number | null;
  density_weekly_avg?: number | null;
  quality_audit_status?: string | null;
  quality_audited_at?: string | null;
}

export interface CrawlMethodDetail extends CrawlMethod {
  dsl_recipe: Record<string, unknown>;
}

export interface DiscoveryFetchResult {
  discovered_count: number;
  stored_count: number;
  items: Record<string, unknown>[];
  stats: Record<string, unknown>;
  message: string;
}

export interface SuggestNameResponse { name: string; }

export interface DiscoverRunResponse {
  status: "started" | "duplicate";
  run_id?: number;
  name?: string;
  existing_method?: {
    method_id: number; domain: string; signature: string;
    dsl_recipe: Record<string, unknown>; last_run_at: string | null; last_run_status: string | null;
  };
}

// ---- Multi-Type Discovery (V2) ----
export type DiscoveryRouteType = "website" | "wechat_search" | "wechat_history" | "internal_forum";

export type DiscoveryRouteSource = "explicit" | "inferred";

export interface DiscoveryRouteInfo {
  selected_route_type: DiscoveryRouteType | null;
  resolved_route_type: DiscoveryRouteType | null;
  route_source: DiscoveryRouteSource;
}

export interface MultiDiscoveryStartRequest extends DiscoveryRouteInfo {
  input: string;
  display_input?: string | null;
  force: boolean;
  name?: string | null;
}

export interface MultiDiscoveryNameRequest extends DiscoveryRouteInfo {
  input: string;
  display_input?: string | null;
}

export interface MultiDiscoveryStartResponse {
  status: "started" | "duplicate" | "completed" | "accepted";
  run_id?: number | null;
  method_id?: number | null;
  name?: string;
  route?: {
    kind: string;
    input_type: string;
    normalized_input: string;
  };
  resolved_route_type?: DiscoveryRouteType | null;
  route_source?: DiscoveryRouteSource;
  existing_method?: {
    method_id: number;
    domain: string;
    signature: string;
    dsl_recipe: Record<string, unknown>;
    last_run_at: string | null;
    last_run_status: string | null;
  };
}

export interface MultiDiscoveryNameResponse {
  name: string;
  resolved_route_type: DiscoveryRouteType | null;
}
