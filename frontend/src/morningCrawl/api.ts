import { authHeaders } from "../auth";
import { ApiError } from "../api/client";
import type {
  MorningCrawlConfig,
  MorningCrawlConfigUpdateRequest,
  MorningCrawlDashboard,
  MorningCrawlRunDetail,
  MorningCrawlRunSummary,
  MorningCrawlRunsResponse,
} from "./types";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";

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

export async function fetchMorningCrawlDashboard(): Promise<MorningCrawlDashboard> {
  const r = await fetch(`${BASE}/system-morning-crawl`, { headers: { ...authHeaders() } });
  return expectOk<MorningCrawlDashboard>(r, "failed to load morning crawl dashboard");
}

export async function updateMorningCrawlConfig(
  request: MorningCrawlConfigUpdateRequest,
): Promise<MorningCrawlConfig> {
  const r = await fetch(`${BASE}/system-morning-crawl`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MorningCrawlConfig>(r, "failed to update morning crawl config");
}

export async function runMorningCrawlNow(): Promise<MorningCrawlRunSummary> {
  const r = await fetch(`${BASE}/system-morning-crawl/run-now`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MorningCrawlRunSummary>(r, "failed to trigger morning crawl");
}

export async function retryTodayFailedMorningCrawl(): Promise<MorningCrawlRunSummary> {
  const r = await fetch(`${BASE}/system-morning-crawl/retry-today`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MorningCrawlRunSummary>(r, "failed to retry unfinished morning crawl methods");
}

export async function stopMorningCrawl(): Promise<MorningCrawlDashboard> {
  const r = await fetch(`${BASE}/system-morning-crawl/stop`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MorningCrawlDashboard>(r, "failed to stop morning crawl");
}

export async function fetchMorningCrawlRuns(limit = 20): Promise<MorningCrawlRunsResponse> {
  const r = await fetch(`${BASE}/system-morning-crawl/runs?limit=${limit}`, { headers: { ...authHeaders() } });
  return expectOk<MorningCrawlRunsResponse>(r, "failed to load morning crawl runs");
}

export async function fetchMorningCrawlRunDetail(runId: number): Promise<MorningCrawlRunDetail> {
  const r = await fetch(`${BASE}/system-morning-crawl/runs/${runId}`, { headers: { ...authHeaders() } });
  return expectOk<MorningCrawlRunDetail>(r, "failed to load morning crawl run detail");
}
