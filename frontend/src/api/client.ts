import type {
  ItemListResponse,
  ItemDetail,
  Facets,
  ManualNewsRunRequest,
  ManualNewsRunStatus,
  NewsRunLogsResponse,
} from "../types";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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

export async function fetchItems(params: ItemQueryParams): Promise<ItemListResponse> {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) qs.set(key, String(value));
  }
  const r = await fetch(`${BASE}/items?${qs}`);
  if (!r.ok) throw new ApiError(r.status, `failed to load items (HTTP ${r.status})`);
  return r.json();
}

export async function fetchItemDetail(id: number): Promise<ItemDetail> {
  const r = await fetch(`${BASE}/items/${id}`);
  if (!r.ok) throw new ApiError(r.status, `failed to load item (HTTP ${r.status})`);
  return r.json();
}

export async function fetchFacets(): Promise<Facets> {
  const r = await fetch(`${BASE}/facets`);
  if (!r.ok) throw new ApiError(r.status, `failed to load facets (HTTP ${r.status})`);
  return r.json();
}

export async function fetchNewsRunStatus(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run`);
  if (!r.ok) throw new ApiError(r.status, `failed to load news run status (HTTP ${r.status})`);
  return r.json();
}

export async function fetchNewsRunLogs(): Promise<NewsRunLogsResponse> {
  const r = await fetch(`${BASE}/news-run/logs`);
  if (!r.ok) throw new ApiError(r.status, `failed to load news run logs (HTTP ${r.status})`);
  return r.json();
}

export async function startNewsRun(request: ManualNewsRunRequest): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!r.ok) throw new ApiError(r.status, `failed to start news run (HTTP ${r.status})`);
  return r.json();
}

export async function stopNewsRun(): Promise<ManualNewsRunStatus> {
  const r = await fetch(`${BASE}/news-run/stop`, {
    method: "POST",
  });
  if (!r.ok) throw new ApiError(r.status, `failed to stop news run (HTTP ${r.status})`);
  return r.json();
}
