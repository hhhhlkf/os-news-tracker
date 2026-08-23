import { authHeaders } from "../auth";
import { ApiError } from "../api/client";
import type { AgentCrawlRunRequest } from "../types";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = `${configuredBase ? configuredBase.replace(/\/+$/, "") : ""}/discussions`;

export interface DiscussionConnection {
  id: number;
  name: string;
  host: string;
  port: number;
  folder: string;
  enabled: boolean;
  last_uid: number;
  health_status: string;
  last_success_at: string | null;
  last_error: string | null;
}

export interface DiscussionSource { id: number; name: string; enabled: boolean; review_status: "pending" | "approved"; }
export interface DiscussionRule { id: number; source_id: number; rule_type: string; match_value: string; header_name: string | null; enabled: boolean; review_status: "pending" | "approved"; }
export interface DiscussionPipelineEvent {
  at: string;
  stage: string;
  level: "info" | "success" | "warning" | "error";
  message: string;
  provider?: "mail" | "github" | "organizer";
  source?: string | null;
}
export interface DiscussionPipelineResult {
  received?: Array<{ connection_id: number; connection_name?: string; status: string; headers?: number; matched?: number; unknown?: number; inserted?: number; duplicates?: number; error?: string }>;
  github?: Array<{ repository_id: number; repository: string; status: string; issues?: { roots?: number; comments?: number; inserted?: number }; discussions?: { roots?: number; comments?: number; replies?: number; inserted?: number }; error?: string }>;
  threads?: { messages: number; threads: number; repaired: number };
  groups?: { created: number; updated: number };
  requeued?: number;
  organized?: { processed: number; promoted: number; waiting: number; failed: number };
  published_items?: Array<{ item_id: number; title: string; group_id: number }>;
}
export interface DiscussionPipelineRun {
  id: string;
  trigger_type: "manual" | "scheduled" | "patrol_resend";
  status: "running" | "stopping" | "cancelled" | "succeeded" | "partial" | "failed";
  source_ids: number[] | null;
  github_repository_ids?: number[] | null;
  events: DiscussionPipelineEvent[];
  result: DiscussionPipelineResult | null;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
}
export type DiscussionPipelineRunState = Omit<DiscussionPipelineRun, "events">;
export interface DiscussionPipelineStreamEvent extends DiscussionPipelineEvent { sequence: number; }
export interface DiscussionPipelineEventStream {
  run: DiscussionPipelineRunState;
  events: DiscussionPipelineStreamEvent[];
  next_after: number;
}
export interface DiscussionScheduleConfig {
  enabled: boolean;
  run_time: string;
  patrol_interval_hours: number;
  last_run_at: string | null;
  last_run_status: "running" | "stopping" | "cancelled" | "succeeded" | "partial" | "failed" | null;
  last_success_date: string | null;
  next_run_at: string | null;
}
export interface DiscussionGroupSummary {
  id: number;
  item_id: number | null;
  title: string;
  processing_status: "pending" | "published" | "waiting" | "failed";
  activity_status: string;
  resolution_status: string;
  message_count: number;
  participant_count: number;
  pending_message_count: number;
  first_activity_at: string | null;
  last_activity_at: string | null;
  last_processed_at: string | null;
  rejection_reason: string | null;
  summary: string | null;
}
export type DiscussionGroupStatus = "all" | "published" | "waiting";
export type DiscussionGroupProvider = "mail" | "github";
export interface DiscussionGroupPage {
  groups: DiscussionGroupSummary[];
  next_offset: number | null;
}
export interface DiscussionGroupCounts { all: number; published: number; waiting: number; }
export interface DiscussionTopologyMessage {
  id: number;
  message_id: string;
  provider: "mail" | "github";
  external_id: string | null;
  external_parent_id: string | null;
  kind: string;
  public_url: string | null;
  parent_message_id: number | null;
  thread_id: number | null;
  subject: string;
  author_name: string | null;
  author_email: string | null;
  platform_user_id: string | null;
  sent_at: string | null;
  updated_at: string | null;
  body_text: string | null;
  authored_text: string | null;
  translated_body_text: string | null;
  translation_summary: string | null;
  translation_phrase: string | null;
  translation_status: "pending" | "succeeded" | "failed";
  metadata: Record<string, unknown>;
  context_incomplete: boolean;
}
export interface DiscussionTopologySnapshot {
  id: number;
  content_revision: number;
  new_message_ids: string[];
  new_reply_count: number;
  progress_summary: string | null;
  created_at: string | null;
}
export interface DiscussionDetail {
  group_id: number;
  activity_status: string;
  resolution_status: string;
  message_count: number;
  participant_count: number;
  pending_message_count: number;
  last_activity_at: string | null;
  structured_result: Record<string, unknown> | null;
  heat_score: number | null;
  messages: DiscussionTopologyMessage[];
  snapshots: DiscussionTopologySnapshot[];
}
export interface DiscussionConnectionCreate {
  name: string;
  host: string;
  port: number;
  folder: string;
  username_env_key: string;
  password_env_key: string;
  enabled: boolean;
}
export interface DiscussionSourceCreate { name: string; rule_type: string; match_value: string; header_name?: string | null; }
export interface GitHubDiscussionRepository {
  id: number;
  source_id: number;
  owner: string;
  repo: string;
  display_name: string;
  token_env_key: string;
  token_configured: boolean;
  enabled: boolean;
  review_status: "pending" | "approved";
  include_issues: boolean;
  include_discussions: boolean;
  issue_label_allowlist: string[];
  issue_label_blocklist: string[];
  discussion_category_allowlist: string[];
  discussion_category_blocklist: string[];
  backfill_start_at: string | null;
  backfill_end_at: string | null;
  issue_watermark: { updated_at: string | null; external_id: string | null };
  discussion_watermark: { updated_at: string | null; external_id: string | null };
  last_success_at: string | null;
  last_error: string | null;
}
export interface GitHubDiscussionRepositoryCreate {
  owner: string; repo: string; display_name: string; token_env_key: string;
  include_issues: boolean; include_discussions: boolean;
  issue_label_allowlist?: string[]; issue_label_blocklist?: string[];
  discussion_category_allowlist?: string[]; discussion_category_blocklist?: string[];
  time_window: AgentCrawlRunRequest;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { ...init, headers: { ...authHeaders(), "content-type": "application/json", ...(init?.headers ?? {}) } });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = response.status === 401
      ? "管理登录已失效，请先退出后重新登录"
      : response.status === 403
        ? "当前账号没有系统管理权限"
        : typeof body?.detail === "string" ? body.detail : "技术讨论服务请求失败";
    throw new ApiError(response.status, message, body);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const fetchDiscussionConnections = () => request<DiscussionConnection[]>("/connections");
export const fetchDiscussionSources = () => request<DiscussionSource[]>("/sources");
export const fetchDiscussionRules = () => request<DiscussionRule[]>("/rules");
export const createDiscussionConnection = (payload: DiscussionConnectionCreate) => request<DiscussionConnection>("/connections", { method: "POST", body: JSON.stringify(payload) });
export const createDiscussionSource = (payload: DiscussionSourceCreate) => request<DiscussionSource>("/sources", { method: "POST", body: JSON.stringify(payload) });
export const approveDiscussionSources = (sourceIds: number[]) => request<{ approved_count: number }>("/sources/review/approve", { method: "POST", body: JSON.stringify({ source_ids: sourceIds }) });
export const deletePendingDiscussionSources = (sourceIds: number[]) => request<{ deleted_count: number }>("/sources/review/delete", { method: "POST", body: JSON.stringify({ source_ids: sourceIds }) });
export const deleteDiscussionSource = (sourceId: number) => request<void>(`/sources/${sourceId}`, { method: "DELETE" });
export const patchDiscussionSource = (sourceId: number, enabled: boolean) => request<{ id: number; enabled: boolean }>(`/sources/${sourceId}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
export const runDiscussionPipeline = (channel: "mail" | "github", sourceIds?: number[], githubRepositoryIds?: number[]) => request<DiscussionPipelineRun>("/runs", { method: "POST", body: JSON.stringify({ channel, source_ids: sourceIds?.length ? sourceIds : null, github_repository_ids: githubRepositoryIds?.length ? githubRepositoryIds : null, organize: true }) });
export const stopDiscussionPipeline = (runId: string) => request<DiscussionPipelineRun>(`/runs/${runId}/stop`, { method: "POST" });
export const fetchLatestDiscussionPipelineRun = () => request<DiscussionPipelineRun | null>("/runs/latest");
export const fetchDiscussionPipelineEvents = (runId: string, after = 0) => request<DiscussionPipelineEventStream>(`/runs/${runId}/events?after=${after}`);
export const fetchDiscussionSchedule = () => request<DiscussionScheduleConfig>("/schedule");
export const updateDiscussionSchedule = (payload: Partial<Pick<DiscussionScheduleConfig, "enabled" | "run_time" | "patrol_interval_hours">>) => request<DiscussionScheduleConfig>("/schedule", { method: "PUT", body: JSON.stringify(payload) });
export const fetchDiscussionGroups = (status: DiscussionGroupStatus, offset = 0, provider?: DiscussionGroupProvider) => request<DiscussionGroupPage>(`/groups?status=${status}&offset=${offset}&limit=10${provider ? `&provider=${provider}` : ""}`);
export const fetchDiscussionGroupCounts = (provider?: DiscussionGroupProvider) => request<DiscussionGroupCounts>(`/groups/summary${provider ? `?provider=${provider}` : ""}`);
export const fetchDiscussionDetail = (itemId: number) => request<DiscussionDetail>(`/items/${itemId}`);
export const fetchGitHubDiscussionRepositories = () => request<GitHubDiscussionRepository[]>("/github/repositories");
export const fetchGitHubTokenStatus = (tokenEnvKey: string) => request<{ token_env_key: string; configured: boolean }>(`/github/token-status?token_env_key=${encodeURIComponent(tokenEnvKey)}`);
export const createGitHubDiscussionRepository = (payload: GitHubDiscussionRepositoryCreate) => request<GitHubDiscussionRepository>("/github/repositories", { method: "POST", body: JSON.stringify(payload) });
export const estimateGitHubDiscussionBackfill = (repositoryId: number) => request<{ issues: number; discussions: number }>(`/github/repositories/${repositoryId}/estimate`, { method: "POST" });
export const approveGitHubDiscussionRepository = (repositoryId: number) => request<GitHubDiscussionRepository>(`/github/repositories/${repositoryId}/approve`, { method: "POST" });
export const approveGitHubDiscussionRepositories = (repositoryIds: number[]) => request<{ approved_count: number }>("/github/repositories/review/approve", { method: "POST", body: JSON.stringify({ repository_ids: repositoryIds }) });
export const deleteGitHubDiscussionRepository = (repositoryId: number) => request<void>(`/github/repositories/${repositoryId}`, { method: "DELETE" });
export const deletePendingGitHubDiscussionRepositories = (repositoryIds: number[]) => request<{ deleted_count: number }>("/github/repositories/review/delete", { method: "POST", body: JSON.stringify({ repository_ids: repositoryIds }) });
export const patchGitHubDiscussionRepository = (repositoryId: number, enabled: boolean) => request<GitHubDiscussionRepository>(`/github/repositories/${repositoryId}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
