export interface ItemSummary {
  id: number;
  title: string;
  title_tldr: string | null;
  main_category: string | null;
  info_type: string | null;
  importance: string | null;
  published_at: string | null;
  fetched_at: string | null;
  url: string | null;
  item_kind?: "news" | "discussion";
  last_activity_at?: string | null;
  content_revision?: number;
  heat_score?: number | null;
  why_it_matters?: string | null;
  os_insight?: string | null;
}

export interface ItemDetail extends ItemSummary {
  summary: string | null;
  key_points: string[];
  llm_confidence: number | null;
  sub_tags: string[];
  source_links: { source_id: number; url: string }[];
  original_title?: string | null;
}

export interface FacetValue { value: string; count: number; }
export interface Facets {
  main_category: FacetValue[];
  info_type: FacetValue[];
  importance: FacetValue[];
  sub_tags: FacetValue[];
}
export interface ItemListResponse { total: number; items: ItemSummary[]; }

export type ManualNewsTimeMode = "relative" | "absolute";
export type ManualNewsRelativeRange = "24h" | "7d" | "30d";

export interface ManualNewsRunRequest {
  time_mode: ManualNewsTimeMode;
  relative_range?: ManualNewsRelativeRange | null;
  start_at?: string | null;
  end_at?: string | null;
  target_count: number;
  /** manual = 批量抓取选中；manual_method = 单方式 */
  trigger_type?: "manual" | "manual_method" | null;
  /** 同一次「抓取选中」共享，用于统计页按批次堆叠 */
  batch_id?: string | null;
}

export interface AgentCrawlRunRequest {
  time_mode: ManualNewsTimeMode;
  relative_range?: ManualNewsRelativeRange | null;
  start_at?: string | null;
  end_at?: string | null;
  target_count: number;
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
  status: "queued" | "running" | "repairing" | "interrupted" | "completed" | "failed" | "cancelled";
  resulting_method_id: number | null;
  llm_token_usage: number;
  node_trace: DiscoveryNodeTraceEntry[];
  retry_count: number;
  current_step: string | null;
  started_at: string | null;
  ended_at: string | null;
  error_message: string | null;
  trigger_type?: string;
  phase?: string | null;
  round?: number;
  queue_position?: number | null;
  runtime_version?: string | null;
  repair_method_id?: number | null;
  source_kind?: "website" | "wechat" | string;
  review_status?: "pending" | "approved" | "rejected" | null;
  elapsed_seconds?: number;
  remaining_seconds?: number;
}

export interface DiscoveryRunEvent {
  id: number;
  sequence: number;
  run_id: number;
  event_type: string;
  phase: string | null;
  round: number | null;
  level: "info" | "warning" | "error" | string;
  summary: string;
  payload: Record<string, unknown> | null;
  created_at: string | null;
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
  source_id?: number;
  source_name?: string | null;
  domain: string;
  entry_url: string;
  status: CrawlMethodStatus;
  review_status?: "pending" | "approved" | "rejected";
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  review_note?: string | null;
  created_at?: string | null;
  signature: string;
  last_run_at: string | null;
  last_run_status: string | null;
  overall_score?: number | null;
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
  execution_steps?: CrawlExecutionStep[];
}

export interface CrawlExecutionStep {
  icon: string;
  title: string;
  detail?: string;
  tags?: string[];
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

// ---- Token usage statistics ----
export interface TokenTotals {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface TokenTrendPoint extends TokenTotals {
  bucket: string;
}

export interface TokenUsageSummaryResponse {
  start: string;
  end: string;
  bucket: "hour" | "day";
  exact_since: string | null;
  summary: TokenTotals & {
    query_tokens: number;
    discovery_tokens: number;
    call_count: number;
  };
  trend: TokenTrendPoint[];
}

export interface QueryMethodTokenUsage extends TokenTotals {
  method_id: number;
  label: string;
}

export interface QueryRunTokenUsage extends TokenTotals {
  run_key: string;
  run_id: number;
  kind: "morning" | "method" | "batch";
  trigger_type: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  methods: QueryMethodTokenUsage[];
}

export interface QueryRunUsageResponse {
  exact_since: string | null;
  total: number;
  runs: QueryRunTokenUsage[];
}

/** Same time grain as query-runs; each method segment is avg tokens per item. */
export interface QueryItemAvgMethod extends TokenTotals {
  method_id: number;
  label: string;
  item_count: number;
  divisor: number;
  avg_tokens_per_item: number;
}

export interface QueryItemAvgRun extends TokenTotals {
  run_key: string;
  run_id: number;
  kind: "morning" | "method" | "batch";
  trigger_type: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  avg_tokens_per_item: number;
  methods: QueryItemAvgMethod[];
}

export interface QueryItemAvgResponse {
  exact_since: string | null;
  total: number;
  runs: QueryItemAvgRun[];
}

export interface DiscoveryRunTokenUsage extends TokenTotals {
  run_id: number;
  site_url: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  exact?: boolean;
}

export interface DiscoveryRunUsageResponse {
  exact_since: string | null;
  total: number;
  runs: DiscoveryRunTokenUsage[];
}

export interface ItemVolumePoint {
  bucket: string;
  high: number;
  medium: number;
  low: number;
  total: number;
}

export interface ItemVolumeDailyResponse {
  start: string;
  end: string;
  summary: {
    high: number;
    medium: number;
    low: number;
    total: number;
  };
  trend: ItemVolumePoint[];
}

export type WechatQrSessionStatus =
  | "pending"
  | "qr_ready"
  | "scanned"
  | "success"
  | "expired"
  | "failed"
  | "cancelled";

export interface WechatAuthProfileStatus {
  profile_name: string;
  status: string;
  configured: boolean;
  source: "database" | "environment" | "none";
  last_error: string | null;
  updated_at: string | null;
  last_verified_at: string | null;
}

export type WechatAuthVerificationResult = "valid" | "expired" | "unknown" | "unconfigured";

/** Outcome of a live check against WeChat, not a read of the stored status. */
export interface WechatAuthVerification {
  result: WechatAuthVerificationResult;
  reason: string | null;
  checked_at: string;
  profile: WechatAuthProfileStatus;
}

export interface WechatAuthLogoutResult {
  /** Whether the persistent browser profile was actually removed. */
  browser_data_cleared: boolean;
  profile: WechatAuthProfileStatus;
}

export interface WechatQrSession {
  session_id: string;
  profile_name: string;
  status: WechatQrSessionStatus;
  qr_image_data_url: string | null;
  message: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
}
