import { authHeaders } from "../auth";
import type { ItemQueryParams } from "../api/client";
import { ApiError } from "../api/client";
import type { MailTemplate, MailTemplateCreateRequest } from "./types";

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

export function buildMailFilterSnapshot(params: ItemQueryParams): MailTemplateCreateRequest["filter_snapshot"] {
  return {
    q: params.q ?? null,
    main_category: params.main_category ?? null,
    info_type: params.info_type ?? null,
    importance: params.importance ?? null,
    sub_tag: params.sub_tag ?? null,
    sort_by: params.sort_by ?? "published_at",
    sort_dir: params.sort_dir ?? "desc",
    published_after_mode: params.published_after ? "absolute" : "none",
    published_after_value: null,
    published_after: params.published_after ?? null,
    published_before_mode: params.published_before ? "absolute" : "none",
    published_before_value: null,
    published_before: params.published_before ?? null,
  };
}

export async function fetchMailTemplates(): Promise<MailTemplate[]> {
  const r = await fetch(`${BASE}/mail/templates`, {
    headers: { ...authHeaders() },
  });
  return expectOk<MailTemplate[]>(r, "failed to load mail templates");
}

export async function createMailTemplate(request: MailTemplateCreateRequest): Promise<MailTemplate> {
  const r = await fetch(`${BASE}/mail/templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailTemplate>(r, "failed to create mail template");
}
