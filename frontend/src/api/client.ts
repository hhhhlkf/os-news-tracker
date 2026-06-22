import type {
  AgentCandidateRunResponse,
  AgentRunRecord,
  AgentRunTriggerResponse,
  AgentSourceCandidatesResponse,
  AgentSource,
  AgentSourceCandidate,
  ItemListResponse,
  ItemDetail,
  Facets,
  ManualNewsRunRequest,
  ManualNewsRunStatus,
  NewsRunLogsResponse,
} from "../types";
import { authHeaders } from "../auth";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
const CRAWL_BASE = `${BASE}/crawl-sources`;
const LEGACY_CRAWL_BASE = `${BASE}/sources/agent`;

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
  q?: string;
  limit?: number | string;
  offset?: number | string;
  sort_by?: "published_at" | "fetched_at";
  sort_dir?: "desc" | "asc";
  published_after?: string;
  published_before?: string;
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

export async function fetchFacets(): Promise<Facets> {
  const r = await fetch(`${BASE}/facets`);
  return expectOk<Facets>(r, "failed to load facets");
}

export async function fetchNewsRunStatus(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run`);
  return expectOk<ManualNewsRunStatus>(r, "failed to load news run status");
}

export async function fetchNewsRunLogs(): Promise<NewsRunLogsResponse> {
  const r = await fetch(`${BASE}/news-run/logs`);
  return expectOk<NewsRunLogsResponse>(r, "failed to load news run logs");
}

export async function startNewsRun(request: ManualNewsRunRequest): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  return expectOk<ManualNewsRunStatus>(r, "failed to start news run");
}

export async function stopNewsRun(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/stop`, {
    method: "POST",
  });
  return expectOk<ManualNewsRunStatus>(r, "failed to stop news run");
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

export async function triggerAgentRun(sourceId: number): Promise<AgentRunTriggerResponse> {
  return fetchJsonWithFallback<AgentRunTriggerResponse>(
    `${CRAWL_BASE}/${sourceId}/run`,
    `${LEGACY_CRAWL_BASE}/${sourceId}/run`,
    { method: "POST", headers: authHeaders() },
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
  try {
    await tryDelete(`${CRAWL_BASE}/${sourceId}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      await tryDelete(`${LEGACY_CRAWL_BASE}/${sourceId}`);
    } else {
      throw err;
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

export async function triggerAgentRunFromCandidate(sourceId: number): Promise<AgentCandidateRunResponse> {
  return fetchJsonWithFallback<AgentCandidateRunResponse>(
    `${CRAWL_BASE}/candidates/${sourceId}/run`,
    `${LEGACY_CRAWL_BASE}/candidates/${sourceId}/run`,
    { method: "POST", headers: authHeaders() },
    "failed to trigger agent run from candidate",
  );
}
