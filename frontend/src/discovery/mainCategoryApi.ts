import { authHeaders } from "../auth";
import { ApiError } from "../api/client";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
const CATEGORY_BASE = `${BASE}/discovery/main-categories`;

export interface MainCategory {
  id: number;
  name: string;
  sort_order: number;
  item_count: number;
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

async function ensureOk(response: Response, fallbackMessage: string): Promise<void> {
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
}

async function expectOk<T>(response: Response, fallbackMessage: string): Promise<T> {
  await ensureOk(response, fallbackMessage);
  return response.json() as Promise<T>;
}

export async function fetchMainCategories(): Promise<MainCategory[]> {
  const r = await fetch(CATEGORY_BASE, { headers: { ...authHeaders() } });
  return expectOk<MainCategory[]>(r, "failed to load main categories");
}

export async function createMainCategory(name: string): Promise<MainCategory> {
  const r = await fetch(CATEGORY_BASE, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  return expectOk<MainCategory>(r, "failed to create main category");
}

export async function renameMainCategory(id: number, name: string): Promise<MainCategory> {
  const r = await fetch(`${CATEGORY_BASE}/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  return expectOk<MainCategory>(r, "failed to rename main category");
}

export async function deleteMainCategory(id: number): Promise<void> {
  const r = await fetch(`${CATEGORY_BASE}/${id}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  await ensureOk(r, "failed to delete main category");
}
