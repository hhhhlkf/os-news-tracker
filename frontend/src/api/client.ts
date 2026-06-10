import type { ItemListResponse, ItemDetail, Facets } from "../types";

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
  q?: string;
  limit?: number | string;
  offset?: number | string;
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
