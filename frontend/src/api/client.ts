import type {
  AgentRunRecord,
  AgentRunTriggerResponse,
  AgentSource,
  ItemListResponse,
  ItemDetail,
  Facets,
  ManualNewsRunRequest,
  ManualNewsRunStatus,
  NewsRunLogsResponse,
} from "../types";
import { authHeaders } from "../auth";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

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
  const r = await fetch(`${BASE}/sources/agent`, {
    headers: authHeaders(),
  });
  return expectOk<AgentSource[]>(r, "failed to load agent sources");
}

export async function fetchAgentRuns(sourceId: number): Promise<AgentRunRecord[]> {
  const r = await fetch(`${BASE}/sources/agent/${sourceId}/runs`, {
    headers: authHeaders(),
  });
  return expectOk<AgentRunRecord[]>(r, "failed to load agent runs");
}

export async function triggerAgentRun(sourceId: number): Promise<AgentRunTriggerResponse> {
  const r = await fetch(`${BASE}/sources/agent/${sourceId}/run`, {
    method: "POST",
    headers: authHeaders(),
  });
  return expectOk<AgentRunTriggerResponse>(r, "failed to trigger agent run");
}
