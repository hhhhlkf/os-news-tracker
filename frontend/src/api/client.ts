import type {
  AgentCandidateRunResponse,
  AgentCrawlRunRequest,
  AgentRunRecord,
  AgentRunTriggerResponse,
  AgentSourceCandidatesResponse,
  AgentSource,
  AgentSourceCandidate,
  CrawlMethod,
  CrawlMethodDetail,
  CrawlMethodStatus,
  CrawlSource,
  ItemListResponse,
  ItemDetail,
  Facets,
  ManualNewsRunRequest,
  SourceCreateRequest,
  SourceDetectResponse,
  DiscoverRequest,
  DiscoverResponse,
  DiscoveryFetchResult,
  DiscoveryRun,
  DiscoveryRunSummary,
  CreateFromProbeRequest,
  MultiDiscoveryStartRequest,
  MultiDiscoveryStartResponse,
  MultiDiscoveryNameRequest,
  MultiDiscoveryNameResponse,
  TokenUsageSummaryResponse,
  QueryRunUsageResponse,
  DiscoveryRunUsageResponse,
  ItemVolumeDailyResponse,
  WechatAuthLogoutResult,
  WechatAuthProfileStatus,
  WechatAuthVerification,
  WechatQrSession,
} from "../types";
import { authHeaders } from "../auth";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
const CRAWL_BASE = `${BASE}/crawl-sources`;
const LEGACY_CRAWL_BASE = `${BASE}/sources/agent`;
const DISCOVERY_BASE = `${BASE}/discovery`;
const SUGGEST_NAME_TIMEOUT_MS = 12000;

export class ApiError extends Error {
  status: number;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export interface ItemQueryParams {
  main_category?: string;
  info_type?: string;
  importance?: string;
  sub_tag?: string;
  source_id?: string;
  q?: string;
  limit?: number | string;
  offset?: number | string;
  sort_by?: "published_at" | "fetched_at";
  sort_dir?: "desc" | "asc";
  published_after_mode?: "none" | "absolute" | "relative";
  published_after_value?: "24h" | "7d" | "30d";
  published_after?: string;
  published_before_mode?: "none" | "absolute" | "relative";
  published_before_value?: "24h" | "7d" | "30d";
  published_before?: string;
  fetched_after_mode?: "none" | "absolute" | "relative";
  fetched_after_value?: "24h" | "7d" | "30d";
  fetched_after?: string;
  fetched_before_mode?: "none" | "absolute" | "relative";
  fetched_before_value?: "24h" | "7d" | "30d";
  fetched_before?: string;
}

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

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit | undefined, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, `请求超时（>${Math.floor(timeoutMs / 1000)}s）`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function fetchJsonWithFallback<T>(
  primaryUrl: string,
  fallbackUrl: string,
  init: RequestInit | undefined,
  fallbackMessage: string,
): Promise<T> {
  async function readJsonOrThrow(url: string): Promise<T> {
    const response = await fetch(url, init);
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

    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("application/json")) {
      throw new SyntaxError(`Expected JSON but received ${contentType || "unknown content type"}`);
    }

    return response.json() as Promise<T>;
  }

  try {
    return await readJsonOrThrow(primaryUrl);
  } catch (error) {
    const shouldFallback =
      error instanceof SyntaxError ||
      (error instanceof ApiError && error.status === 404) ||
      (error instanceof TypeError);
    if (!shouldFallback) {
      throw error;
    }
    return readJsonOrThrow(fallbackUrl);
  }
}

export async function fetchItems(params: ItemQueryParams): Promise<ItemListResponse> {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) qs.set(key, String(value));
  }
  const r = await fetch(`${BASE}/items?${qs}`);
  return expectOk<ItemListResponse>(r, "failed to load items");
}

export async function fetchItemDetail(id: number): Promise<ItemDetail> {
  const r = await fetch(`${BASE}/items/${id}`);
  return expectOk<ItemDetail>(r, "failed to load item");
}

export interface ItemReasonResponse {
  reason: string;
  generated: boolean;
}

export async function generateItemReason(id: number): Promise<ItemReasonResponse> {
  const r = await fetch(`${BASE}/items/${id}/reason`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<ItemReasonResponse>(r, "failed to generate item reason");
}

export async function fetchFacets(): Promise<Facets> {
  const r = await fetch(`${BASE}/facets`);
  return expectOk<Facets>(r, "failed to load facets");
}

export async function fetchSources(): Promise<CrawlSource[]> {
  const r = await fetch(`${BASE}/sources`);
  return expectOk<CrawlSource[]>(r, "failed to load sources");
}

export async function detectSource(url: string): Promise<SourceDetectResponse> {
  const r = await fetch(`${BASE}/sources/detect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return expectOk<SourceDetectResponse>(r, "failed to detect source");
}

export async function createSource(request: SourceCreateRequest): Promise<CrawlSource> {
  const r = await fetch(`${BASE}/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  return expectOk<CrawlSource>(r, "failed to create source");
}

export async function deleteSource(sourceId: number): Promise<void> {
  const r = await fetch(`${BASE}/sources/${sourceId}`, { method: "DELETE" });
  if (r.status === 204 || r.ok) return;
  const body = await parseErrorBody(r);
  const message =
    typeof body === "object" &&
    body !== null &&
    "detail" in body &&
    typeof (body as { detail?: unknown }).detail === "string"
      ? (body as { detail: string }).detail
      : `failed to delete source (HTTP ${r.status})`;
  throw new ApiError(r.status, message, body);
}

export async function discoverSource(req: DiscoverRequest): Promise<DiscoverResponse> {
  const r = await fetch(`${BASE}/sources/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(req),
  });
  return expectOk<DiscoverResponse>(r, "failed to run smart discovery");
}

export async function createSourceFromProbe(req: CreateFromProbeRequest): Promise<CrawlSource> {
  const r = await fetch(`${BASE}/sources/create-from-probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(req),
  });
  return expectOk<CrawlSource>(r, "failed to create source from probe");
}

export async function fetchAgentSources(): Promise<AgentSource[]> {
  return fetchJsonWithFallback<AgentSource[]>(
    CRAWL_BASE,
    LEGACY_CRAWL_BASE,
    { headers: authHeaders() },
    "failed to load agent sources",
  );
}

export async function fetchAgentRuns(sourceId: number): Promise<AgentRunRecord[]> {
  return fetchJsonWithFallback<AgentRunRecord[]>(
    `${CRAWL_BASE}/${sourceId}/runs`,
    `${LEGACY_CRAWL_BASE}/${sourceId}/runs`,
    { headers: authHeaders() },
    "failed to load agent runs",
  );
}

export async function triggerAgentRun(
  sourceId: number,
  request?: AgentCrawlRunRequest,
): Promise<AgentRunTriggerResponse> {
  return fetchJsonWithFallback<AgentRunTriggerResponse>(
    `${CRAWL_BASE}/${sourceId}/run`,
    `${LEGACY_CRAWL_BASE}/${sourceId}/run`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: request ? JSON.stringify(request) : undefined,
    },
    "failed to trigger agent run",
  );
}

export async function deleteAgentSource(sourceId: number): Promise<void> {
  const tryDelete = async (url: string) => {
    const r = await fetch(url, { method: "DELETE", headers: authHeaders() });
    if (r.status === 204 || r.ok) return;
    const body = await parseErrorBody(r);
    throw new ApiError(r.status, `failed to delete agent source (HTTP ${r.status})`, body);
  };

  const doDelete = async () => {
    try {
      await tryDelete(`${CRAWL_BASE}/${sourceId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        await tryDelete(`${LEGACY_CRAWL_BASE}/${sourceId}`);
      } else {
        throw err;
      }
    }
  };

  try {
    await doDelete();
  } catch (firstErr) {
    // 后台采集线程可能仍持有 DB 锁（竞争条件），等待后自动重试一次
    if (firstErr instanceof ApiError && firstErr.status === 500) {
      await new Promise((r) => setTimeout(r, 1500));
      await doDelete();
    } else {
      throw firstErr;
    }
  }
}

export async function cancelAgentRun(sourceId: number, runId: number): Promise<{ cancelled: boolean; run_id: number }> {
  return fetchJsonWithFallback(
    `${CRAWL_BASE}/${sourceId}/runs/${runId}/cancel`,
    `${LEGACY_CRAWL_BASE}/${sourceId}/runs/${runId}/cancel`,
    { method: "POST", headers: authHeaders() },
    "failed to cancel agent run",
  );
}

export async function fetchAgentSourceCandidates(
  page = 1,
  pageSize = 5,
): Promise<AgentSourceCandidatesResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const payload = await fetchJsonWithFallback<AgentSourceCandidatesResponse | AgentSourceCandidate[]>(
    `${CRAWL_BASE}/candidates?${params}`,
    `${LEGACY_CRAWL_BASE}/candidates?${params}`,
    { headers: authHeaders() },
    "failed to load agent source candidates",
  );
  if (Array.isArray(payload)) {
    return {
      items: payload,
      total: payload.length,
      page: 1,
      page_size: payload.length || pageSize,
      total_pages: 1,
    };
  }
  return payload;
}

export async function triggerAgentRunFromCandidate(
  sourceId: number,
  request?: AgentCrawlRunRequest,
): Promise<AgentCandidateRunResponse> {
  return fetchJsonWithFallback<AgentCandidateRunResponse>(
    `${CRAWL_BASE}/candidates/${sourceId}/run`,
    `${LEGACY_CRAWL_BASE}/candidates/${sourceId}/run`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: request ? JSON.stringify(request) : undefined,
    },
    "failed to trigger agent run from candidate",
  );
}

export async function startDiscoveryRun(
  request: MultiDiscoveryStartRequest,
): Promise<MultiDiscoveryStartResponse> {
  const r = await fetch(`${DISCOVERY_BASE}/multi-run`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MultiDiscoveryStartResponse>(r, "failed to start discovery run");
}

export async function getDiscoveryRun(runId: number): Promise<DiscoveryRun> {
  const r = await fetch(`${DISCOVERY_BASE}/runs/${runId}`, { headers: authHeaders() });
  return expectOk<DiscoveryRun>(r, "failed to load discovery run");
}

export async function cancelDiscoveryRun(runId: number): Promise<DiscoveryRun> {
  const r = await fetch(`${DISCOVERY_BASE}/runs/${runId}/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk<DiscoveryRun>(r, "failed to cancel discovery run");
}

export async function listDiscoveryRuns(limit = 20): Promise<DiscoveryRunSummary[]> {
  const r = await fetch(`${DISCOVERY_BASE}/runs?limit=${limit}`, { headers: authHeaders() });
  return expectOk<DiscoveryRunSummary[]>(r, "failed to load discovery runs");
}

export async function suggestDiscoveryName(
  request: MultiDiscoveryNameRequest,
): Promise<MultiDiscoveryNameResponse> {
  const r = await fetchWithTimeout(
    `${DISCOVERY_BASE}/suggest-name`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(request),
    },
    SUGGEST_NAME_TIMEOUT_MS,
  );
  return expectOk<MultiDiscoveryNameResponse>(r, "failed to suggest name");
}

export async function listDiscoveryMethods(): Promise<CrawlMethod[]> {
  const r = await fetch(`${DISCOVERY_BASE}/methods`, { headers: authHeaders() });
  return expectOk<CrawlMethod[]>(r, "failed to load crawl methods");
}

export async function listPendingDiscoveryMethods(): Promise<CrawlMethod[]> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review-pending`, { headers: authHeaders() });
  return expectOk<CrawlMethod[]>(r, "failed to load pending crawl methods");
}

export async function approveDiscoveryMethods(methodIds: number[]): Promise<{ approved_count: number }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ method_ids: methodIds }),
  });
  return expectOk<{ approved_count: number }>(r, "failed to approve crawl methods");
}

export async function deletePendingDiscoveryMethods(methodIds: number[]): Promise<{ deleted_count: number }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ method_ids: methodIds }),
  });
  return expectOk<{ deleted_count: number }>(r, "failed to delete pending crawl methods");
}

export interface CrawlMethodReviewReminderConfig {
  enabled: boolean;
  interval_minutes: number;
  recipients: string[];
  last_sent_at: string | null;
  last_result_status: string | null;
  last_error: string | null;
}

export async function getCrawlMethodReviewReminderConfig(): Promise<CrawlMethodReviewReminderConfig> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review/reminder`, { headers: authHeaders() });
  return expectOk<CrawlMethodReviewReminderConfig>(r, "failed to load review reminder config");
}

export async function updateCrawlMethodReviewReminderConfig(
  request: { enabled?: boolean; interval_minutes?: number; recipients?: string[] },
): Promise<CrawlMethodReviewReminderConfig> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review/reminder`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<CrawlMethodReviewReminderConfig>(r, "failed to update review reminder config");
}

export async function sendCrawlMethodReviewReminderNow(): Promise<{ sent: boolean; reason: string; count: number; error?: string }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/review/reminder/send-now`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk(r, "failed to send review reminder");
}

export async function getDiscoveryMethod(methodId: number): Promise<CrawlMethodDetail> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, { headers: authHeaders() });
  return expectOk<CrawlMethodDetail>(r, "failed to load crawl method");
}

export async function patchDiscoveryMethod(
  methodId: number, status: CrawlMethodStatus,
): Promise<{ id: number; status: string }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ status }),
  });
  return expectOk<{ id: number; status: string }>(r, "failed to patch method");
}

export async function deleteDiscoveryMethod(methodId: number): Promise<void> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (r.status === 204 || r.ok) return;
  const body = await parseErrorBody(r);
  throw new ApiError(r.status, `failed to delete method (HTTP ${r.status})`, body);
}

export async function fetchDiscoveryMethod(
  methodId: number,
  request?: ManualNewsRunRequest | null,
  signal?: AbortSignal,
): Promise<DiscoveryFetchResult> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}/fetch`, {
    method: "POST",
    headers: request ? { "Content-Type": "application/json", ...authHeaders() } : authHeaders(),
    signal,
    body: request ? JSON.stringify(request) : undefined,
  });
  return expectOk<DiscoveryFetchResult>(r, "failed to fetch method");
}

export async function cancelDiscoveryMethodFetch(
  methodId: number,
): Promise<{ cancelled: boolean; killed: boolean; method_id: number }> {
  const r = await fetch(`${DISCOVERY_BASE}/methods/${methodId}/fetch/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk(r, "failed to cancel method fetch");
}

export interface TokenUsageQuery {
  start: string;
  end: string;
  bucket?: "hour" | "day";
  trigger_type?: string;
  limit?: number;
  offset?: number;
}

function tokenUsageParams(query: TokenUsageQuery): string {
  const params = new URLSearchParams({ start: query.start, end: query.end });
  if (query.bucket) params.set("bucket", query.bucket);
  if (query.trigger_type) params.set("trigger_type", query.trigger_type);
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  return params.toString();
}

export async function fetchTokenUsageSummary(query: TokenUsageQuery): Promise<TokenUsageSummaryResponse> {
  const response = await fetch(`${BASE}/statistics/token-usage/summary?${tokenUsageParams(query)}`, {
    headers: authHeaders(),
  });
  return expectOk<TokenUsageSummaryResponse>(response, "failed to load token usage summary");
}

export async function fetchQueryRunUsage(query: TokenUsageQuery): Promise<QueryRunUsageResponse> {
  const response = await fetch(`${BASE}/statistics/token-usage/query-runs?${tokenUsageParams(query)}`, {
    headers: authHeaders(),
  });
  return expectOk<QueryRunUsageResponse>(response, "failed to load query token usage");
}

export async function fetchDiscoveryRunUsage(query: TokenUsageQuery): Promise<DiscoveryRunUsageResponse> {
  const response = await fetch(`${BASE}/statistics/token-usage/discovery-runs?${tokenUsageParams(query)}`, {
    headers: authHeaders(),
  });
  return expectOk<DiscoveryRunUsageResponse>(response, "failed to load discovery token usage");
}

export async function fetchItemVolumeDaily(query: Pick<TokenUsageQuery, "start" | "end">): Promise<ItemVolumeDailyResponse> {
  const params = new URLSearchParams({ start: query.start, end: query.end });
  const response = await fetch(`${BASE}/statistics/item-volume/daily?${params}`, {
    headers: authHeaders(),
  });
  return expectOk<ItemVolumeDailyResponse>(response, "failed to load daily item volume");
}

export async function fetchWechatAuthProfile(): Promise<WechatAuthProfileStatus> {
  const response = await fetch(`${BASE}/wechat-auth/profile`, { headers: authHeaders() });
  return expectOk<WechatAuthProfileStatus>(response, "failed to load WeChat authentication status");
}

export async function verifyWechatAuthProfile(): Promise<WechatAuthVerification> {
  const response = await fetch(`${BASE}/wechat-auth/profile/verify`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk<WechatAuthVerification>(response, "failed to verify WeChat authentication");
}

export async function logoutWechatAuthProfile(): Promise<WechatAuthLogoutResult> {
  const response = await fetch(`${BASE}/wechat-auth/profile/logout`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk<WechatAuthLogoutResult>(response, "failed to log out of WeChat");
}

export async function startWechatQrSession(): Promise<WechatQrSession> {
  const response = await fetch(`${BASE}/wechat-auth/qr-sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({}),
  });
  return expectOk<WechatQrSession>(response, "failed to start WeChat QR login");
}

export async function fetchWechatQrSession(sessionId: string): Promise<WechatQrSession> {
  const response = await fetch(`${BASE}/wechat-auth/qr-sessions/${encodeURIComponent(sessionId)}`, {
    headers: authHeaders(),
  });
  return expectOk<WechatQrSession>(response, "failed to load WeChat QR login status");
}

export async function cancelWechatQrSession(sessionId: string): Promise<WechatQrSession> {
  const response = await fetch(
    `${BASE}/wechat-auth/qr-sessions/${encodeURIComponent(sessionId)}/cancel`,
    { method: "POST", headers: authHeaders() },
  );
  return expectOk<WechatQrSession>(response, "failed to cancel WeChat QR login");
}
