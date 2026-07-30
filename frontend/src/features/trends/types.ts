export interface TrendIdentityTemplate {
  template_id: string;
  name: string;
  identity_text: string;
  created_at: string;
}

export interface TrendCarouselTemplate {
  template_id: string;
  name: string;
}

export type TrendTriggerMode = "manual" | "scheduled";
export type TrendWindowMode = "date_range" | "relative";
export type TrendRelativeWindowUnit = "week" | "month";

export interface TrendSettings {
  window_mode: TrendWindowMode;
  window_start_date: string | null;
  window_end_date: string | null;
  relative_window_unit: TrendRelativeWindowUnit;
  relative_window_value: number;
  trend_count: number;
  storyline_candidate_goal: number;
  trigger_mode: TrendTriggerMode;
  schedule_rule: string | null;
  scheduled_template_id: string | null;
}

export type TrendEmbeddingProvider = "local_qwen" | "openai_compatible";
export type TrendEmbeddingStatusValue =
  | "not_installed"
  | "downloading"
  | "ready"
  | "loading"
  | "processing"
  | "failed";

export interface TrendEmbeddingStatus {
  provider: TrendEmbeddingProvider;
  model_id: string;
  model_revision: string;
  model_version: string | null;
  embedding_version: string;
  dimension: number;
  normalization: string;
  cache_dir: string;
  cache_installed: boolean;
  status: TrendEmbeddingStatusValue;
  error_message: string | null;
  remedy: string | null;
  worker_base_url: string;
  worker_reachable: boolean;
  worker_active: boolean;
  worker_error: string | null;
  status_updated_at: string;
}

export interface TrendCardStageStatus {
  is_running: boolean;
  start_date: string;
  end_date: string;
  pending_count: number;
  generated_count: number;
  skipped_count: number;
  failed_count: number;
  card_prompt_version: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  retry_guidance: string | null;
  message: string | null;
}

export interface TrendCardBackfillRequest {
  start_date?: string;
  end_date?: string;
}

export interface TrendVectorStageStatus {
  is_running: boolean;
  start_date: string;
  end_date: string;
  embedding_version: string;
  pending_count: number;
  generated_count: number;
  failed_count: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  retry_guidance: string | null;
  message: string | null;
}

export interface TrendClusterStageStatus {
  is_running: boolean;
  start_date: string;
  end_date: string;
  embedding_version: string;
  candidate_goal: number;
  max_cluster_size: number;
  pending_vector_count: number;
  generated_vector_count: number;
  vector_failed_count: number;
  candidate_cluster_count: number;
  pending_match_count: number;
  used_threshold: number | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  retry_guidance: string | null;
  message: string | null;
}

export interface TrendCandidateClusterListItem {
  candidate_cluster_id: string;
  member_count: number;
  cohesion_score: number;
  threshold: number;
}

export interface TrendCandidateClusterList {
  start_date: string;
  end_date: string;
  total: number;
  offset: number;
  limit: number;
  items: TrendCandidateClusterListItem[];
}

export interface FetchTrendCandidateClusterListRequest {
  offset?: number;
  limit?: number;
}

export type TrendCardListStatus = "pending" | "ready" | "skipped" | "failed";

export interface TrendCardListItem {
  item_id: number;
  title: string;
  published_at: string | null;
  fetched_at: string;
  status: TrendCardListStatus;
  news_actor: string | null;
  action: string | null;
  result: string | null;
  potential_impact: string | null;
  cause: string | null;
  skip_reason: string | null;
  error_message: string | null;
  attempt_count: number;
  card_updated_at: string | null;
}

export interface TrendCardList {
  start_date: string;
  end_date: string;
  total: number;
  offset: number;
  limit: number;
  items: TrendCardListItem[];
}

export interface FetchTrendCardListRequest {
  status?: TrendCardListStatus;
  offset?: number;
  limit?: number;
}

export interface CreateTrendIdentityTemplateRequest {
  name: string;
  identity_text: string;
}

export interface UpdateTrendSettingsRequest extends TrendSettings {}

export type StorylineReviewStatus = "pending" | "running" | "accepted" | "split" | "rejected" | "failed";
export type StorylineReviewDecision = "accept" | "split" | "reject";
export type StorylineMembership = "core" | "supporting" | "duplicate";

export interface TrendStorylineStageStatus {
  is_running: boolean;
  start_date: string;
  end_date: string;
  embedding_version: string;
  pending_count: number;
  accepted_count: number;
  split_count: number;
  rejected_count: number;
  failed_count: number;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  retry_guidance: string | null;
  message: string | null;
}

export interface TrendStorylineReview {
  review_id: string;
  candidate_cluster_id: string | null;
  status: StorylineReviewStatus;
  decision: StorylineReviewDecision | null;
  member_card_ids: string[];
  removed_card_ids: string[];
  agent_review: string | null;
  error_message: string | null;
  attempt_count: number;
  created_at: string;
  completed_at: string | null;
}

export interface TrendStorylineReviewList {
  total: number;
  offset: number;
  limit: number;
  items: TrendStorylineReview[];
}

export type TrendStorylineReviewFilter = "accepted" | "split" | "rejected" | "unreviewed";

export interface TrendStorylineMember {
  card_id: string;
  at: string;
  membership: StorylineMembership;
}

export interface TrendStoryline {
  storyline_id: string;
  title: string;
  overall_start_date: string;
  overall_end_date: string;
  overall_influence_score: number;
  cohesion_score: number;
  decision: "accept" | "split";
  status: "active" | "archived";
  agent_review: string;
  members: TrendStorylineMember[];
}

export interface TrendStorylineList {
  total: number;
  offset: number;
  limit: number;
  items: TrendStoryline[];
}

export type TrendCategory =
  | "emerging_trend"
  | "hot_event"
  | "periodic_activity"
  | "attention_declining"
  | "unverified_change";

export type TrendRunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface TrendRunStageStatus {
  run_id: string | null;
  template_id: string | null;
  is_running: boolean;
  status: TrendRunStatus | null;
  window_start_date: string | null;
  window_end_date: string | null;
  trend_count: number | null;
  storyline_candidate_goal: number | null;
  candidate_count: number;
  completed_candidate_count: number;
  result_count: number;
  unverified_count: number;
  reusable: boolean;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  retry_guidance: string | null;
  message: string | null;
}

export interface TrendRunListItem {
  run_id: string;
  template_id: string;
  status: TrendRunStatus;
  window_start_date: string;
  window_end_date: string;
  trend_count: number;
  candidate_count: number;
  completed_candidate_count: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface TrendRunList {
  total: number;
  offset: number;
  limit: number;
  items: TrendRunListItem[];
}

/** One exact news reference; `title` is display-only, filtering uses `item_id`. */
export interface TrendResultSource {
  item_id: number;
  title: string;
}

export interface TrendResult {
  result_id: string;
  run_id: string;
  storyline_id: string;
  overall_start_date: string;
  overall_end_date: string;
  window_start_date: string;
  window_end_date: string;
  overall_score: number;
  window_score: number;
  template_relevance_score: number;
  trend_rank_score: number;
  category: TrendCategory;
  topic: string | null;
  trend_summary: string | null;
  agent_review: string;
  item_ids: number[];
  sources: TrendResultSource[];
}

export interface TrendLatestResults {
  template_id: string;
  run_id: string | null;
  window_start_date: string | null;
  window_end_date: string | null;
  trend_count: number | null;
  status: TrendRunStatus | null;
  finished_at: string | null;
  items: TrendResult[];
  message: string | null;
}

export interface TrendCarouselItem {
  result_id: string;
  storyline_id: string;
  category: TrendCategory;
  category_label: string;
  topic: string;
  trend_summary: string;
  trend_rank_score: number;
  window_start_date: string;
  window_end_date: string;
  window_item_count: number;
  item_ids: number[];
  sources: TrendResultSource[];
}

export interface TrendCarousel {
  template_id: string;
  run_id: string | null;
  window_start_date: string | null;
  window_end_date: string | null;
  trend_count: number;
  finished_at: string | null;
  items: TrendCarouselItem[];
  message: string | null;
}
