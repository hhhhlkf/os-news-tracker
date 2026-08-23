import { authHeaders } from "../../auth";
import { ApiError } from "../../api/client";
import type {
  CreateTrendIdentityTemplateRequest,
  FetchTrendCandidateClusterListRequest,
  FetchTrendCardListRequest,
  TrendCardBackfillRequest,
  TrendCardList,
  TrendCardStageStatus,
  TrendCandidateClusterList,
  TrendCarousel,
  TrendCarouselTemplate,
  TrendClusterStageStatus,
  TrendEmbeddingStatus,
  TrendIdentityTemplate,
  TrendSettings,
  TrendVectorStageStatus,
  TrendStorylineList,
  TrendStorylineReviewList,
  TrendStorylineReviewFilter,
  TrendStorylineStageStatus,
  TrendLatestResults,
  TrendRunList,
  TrendRunStageStatus,
  UpdateTrendSettingsRequest,
  UpdateTrendIdentityTemplateRequest,
} from "./types";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
const TRENDS_BASE = `${BASE}/trends`;

async function parseErrorBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  try {
    return await response.text();
  } catch {
    return null;
  }
}

async function expectOk<T>(response: Response, fallbackMessage: string): Promise<T> {
  if (!response.ok) {
    const body = await parseErrorBody(response);
    const message =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail?: unknown }).detail === "string"
        ? (body as { detail: string }).detail
        : `${fallbackMessage} (HTTP ${response.status})`;
    throw new ApiError(response.status, message, body);
  }
  return response.json() as Promise<T>;
}

export async function fetchTrendIdentityTemplates(): Promise<TrendIdentityTemplate[]> {
  const response = await fetch(`${TRENDS_BASE}/templates`, { headers: authHeaders() });
  return expectOk<TrendIdentityTemplate[]>(response, "failed to load trend identity templates");
}

export async function fetchTrendCarouselTemplates(): Promise<TrendCarouselTemplate[]> {
  const response = await fetch(`${TRENDS_BASE}/results/carousel/templates`);
  return expectOk<TrendCarouselTemplate[]>(response, "failed to load trend carousel templates");
}

export async function createTrendIdentityTemplate(
  request: CreateTrendIdentityTemplateRequest,
): Promise<TrendIdentityTemplate> {
  const response = await fetch(`${TRENDS_BASE}/templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<TrendIdentityTemplate>(response, "failed to create trend identity template");
}

export async function updateTrendIdentityTemplate(
  templateId: string,
  request: UpdateTrendIdentityTemplateRequest,
): Promise<TrendIdentityTemplate> {
  const response = await fetch(`${TRENDS_BASE}/templates/${encodeURIComponent(templateId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<TrendIdentityTemplate>(response, "failed to update trend identity template");
}

export async function deleteTrendIdentityTemplate(templateId: string): Promise<void> {
  const response = await fetch(`${TRENDS_BASE}/templates/${encodeURIComponent(templateId)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (response.status === 204 || response.ok) return;
  const body = await parseErrorBody(response);
  const message =
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof (body as { detail?: unknown }).detail === "string"
      ? (body as { detail: string }).detail
      : `failed to delete trend identity template (HTTP ${response.status})`;
  throw new ApiError(response.status, message, body);
}

export async function fetchTrendSettings(): Promise<TrendSettings> {
  const response = await fetch(`${TRENDS_BASE}/settings`, { headers: authHeaders() });
  return expectOk<TrendSettings>(response, "failed to load trend settings");
}

export async function updateTrendSettings(request: UpdateTrendSettingsRequest): Promise<TrendSettings> {
  const response = await fetch(`${TRENDS_BASE}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<TrendSettings>(response, "failed to save trend settings");
}

export async function fetchTrendEmbeddingStatus(): Promise<TrendEmbeddingStatus> {
  const response = await fetch(`${TRENDS_BASE}/embedding/status`, { headers: authHeaders() });
  return expectOk<TrendEmbeddingStatus>(response, "failed to load embedding status");
}

export async function prepareTrendEmbeddingModel(): Promise<TrendEmbeddingStatus> {
  const response = await fetch(`${TRENDS_BASE}/embedding/prepare`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk<TrendEmbeddingStatus>(response, "failed to start embedding model preparation");
}

export async function fetchTrendCardStageStatus(): Promise<TrendCardStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/cards/status`, { headers: authHeaders() });
  return expectOk<TrendCardStageStatus>(response, "failed to load news card stage status");
}

export async function fetchTrendCardList(
  request: FetchTrendCardListRequest = {},
): Promise<TrendCardList> {
  const query = new URLSearchParams();
  if (request.status) query.set("status", request.status);
  if (request.offset !== undefined) query.set("offset", String(request.offset));
  if (request.limit !== undefined) query.set("limit", String(request.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(`${TRENDS_BASE}/cards${suffix}`, { headers: authHeaders() });
  return expectOk<TrendCardList>(response, "failed to load news card list");
}

export async function backfillTrendNewsCards(
  request?: TrendCardBackfillRequest,
): Promise<TrendCardStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/cards/backfill`, {
    method: "POST",
    headers: request
      ? { "Content-Type": "application/json", ...authHeaders() }
      : authHeaders(),
    body: request ? JSON.stringify(request) : undefined,
  });
  return expectOk<TrendCardStageStatus>(response, "failed to start news card backfill");
}

export async function fetchTrendVectorStageStatus(): Promise<TrendVectorStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/vectors/status`, { headers: authHeaders() });
  return expectOk<TrendVectorStageStatus>(response, "failed to load trend vector status");
}

export async function backfillTrendVectors(): Promise<TrendVectorStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/vectors/backfill`, { method: "POST", headers: authHeaders() });
  return expectOk<TrendVectorStageStatus>(response, "failed to start trend vector backfill");
}

export async function fetchTrendClusterStageStatus(): Promise<TrendClusterStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/clusters/status`, { headers: authHeaders() });
  return expectOk<TrendClusterStageStatus>(response, "failed to load trend cluster status");
}

export async function fetchTrendCandidateClusterList(
  request: FetchTrendCandidateClusterListRequest = {},
): Promise<TrendCandidateClusterList> {
  const query = new URLSearchParams();
  if (request.offset !== undefined) query.set("offset", String(request.offset));
  if (request.limit !== undefined) query.set("limit", String(request.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(`${TRENDS_BASE}/clusters${suffix}`, { headers: authHeaders() });
  return expectOk<TrendCandidateClusterList>(response, "failed to load trend candidate clusters");
}

export async function runTrendCandidateClustering(): Promise<TrendClusterStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/clusters/run`, { method: "POST", headers: authHeaders() });
  return expectOk<TrendClusterStageStatus>(response, "failed to run trend candidate clustering");
}

export async function fetchTrendStorylineStageStatus(): Promise<TrendStorylineStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/storylines/status`, { headers: authHeaders() });
  return expectOk<TrendStorylineStageStatus>(response, "failed to load storyline review status");
}

export async function reviewTrendCandidateClusters(): Promise<TrendStorylineStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/storylines/review`, { method: "POST", headers: authHeaders() });
  return expectOk<TrendStorylineStageStatus>(response, "failed to start storyline review");
}

export async function fetchTrendStorylineReviews(
  request: { offset?: number; limit?: number; status?: TrendStorylineReviewFilter } = {},
): Promise<TrendStorylineReviewList> {
  const query = new URLSearchParams();
  if (request.offset !== undefined) query.set("offset", String(request.offset));
  if (request.limit !== undefined) query.set("limit", String(request.limit));
  if (request.status !== undefined) query.set("status", request.status);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(`${TRENDS_BASE}/storylines/reviews${suffix}`, { headers: authHeaders() });
  return expectOk<TrendStorylineReviewList>(response, "failed to load storyline reviews");
}

export async function fetchTrendStorylines(
  request: { offset?: number; limit?: number } = {},
): Promise<TrendStorylineList> {
  const query = new URLSearchParams();
  if (request.offset !== undefined) query.set("offset", String(request.offset));
  if (request.limit !== undefined) query.set("limit", String(request.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(`${TRENDS_BASE}/storylines${suffix}`, { headers: authHeaders() });
  return expectOk<TrendStorylineList>(response, "failed to load storylines");
}

export async function fetchTrendRunStatus(templateId: string): Promise<TrendRunStageStatus> {
  const query = new URLSearchParams({ template_id: templateId });
  const response = await fetch(`${TRENDS_BASE}/runs/status?${query.toString()}`, {
    headers: authHeaders(),
  });
  return expectOk<TrendRunStageStatus>(response, "failed to load trend run status");
}

export async function startTrendRun(templateId: string): Promise<TrendRunStageStatus> {
  const response = await fetch(`${TRENDS_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ template_id: templateId }),
  });
  return expectOk<TrendRunStageStatus>(response, "failed to start trend run");
}

export async function fetchTrendRunList(
  templateId: string,
  request: { offset?: number; limit?: number } = {},
): Promise<TrendRunList> {
  const query = new URLSearchParams({ template_id: templateId });
  if (request.offset !== undefined) query.set("offset", String(request.offset));
  if (request.limit !== undefined) query.set("limit", String(request.limit));
  const response = await fetch(`${TRENDS_BASE}/runs?${query.toString()}`, { headers: authHeaders() });
  return expectOk<TrendRunList>(response, "failed to load trend runs");
}

export async function fetchLatestTrendResults(templateId: string): Promise<TrendLatestResults> {
  const query = new URLSearchParams({ template_id: templateId });
  const response = await fetch(`${TRENDS_BASE}/results/latest?${query.toString()}`, {
    headers: authHeaders(),
  });
  return expectOk<TrendLatestResults>(response, "failed to load latest trend results");
}

export async function fetchTrendCarousel(templateId: string, direction?: string): Promise<TrendCarousel> {
  const query = new URLSearchParams({ template_id: templateId });
  if (direction) query.set("direction", direction);
  const response = await fetch(`${TRENDS_BASE}/results/carousel?${query.toString()}`);
  return expectOk<TrendCarousel>(response, "failed to load trend carousel");
}
