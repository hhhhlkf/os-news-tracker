import { authHeaders } from "../auth";
import type { ItemQueryParams } from "../api/client";
import { ApiError } from "../api/client";
import type {
  MailDeliveryLog,
  MailImmediateSendRequest,
  MailImmediateSendResponse,
  MailPreviewResponse,
  MailProviderKind,
  MailSchedule,
  MailScheduleCreateRequest,
  MailScheduleUpdateRequest,
  MailTemplate,
  MailTemplateCreateRequest,
  MailTemplateUpdateRequest,
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

export async function updateMailTemplate(
  templateId: number,
  request: MailTemplateUpdateRequest,
): Promise<MailTemplate> {
  const r = await fetch(`${BASE}/mail/templates/${templateId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailTemplate>(r, "failed to update mail template");
}

export async function deleteMailTemplate(templateId: number): Promise<void> {
  const r = await fetch(`${BASE}/mail/templates/${templateId}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  if (!r.ok) {
    const body = await parseErrorBody(r);
    const message =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail?: unknown }).detail === "string"
        ? (body as { detail: string }).detail
        : `failed to delete mail template (HTTP ${r.status})`;
    throw new ApiError(r.status, message, body);
  }
}

export async function previewMailTemplate(
  templateId: number,
  provider?: MailProviderKind | null,
): Promise<MailPreviewResponse> {
  const r = await fetch(`${BASE}/mail/templates/${templateId}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ provider: provider ?? null }),
  });
  return expectOk<MailPreviewResponse>(r, "failed to preview mail template");
}

export async function sendMailTemplate(
  templateId: number,
  provider?: MailProviderKind | null,
): Promise<MailImmediateSendResponse> {
  const r = await fetch(`${BASE}/mail/templates/${templateId}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ provider: provider ?? null }),
  });
  return expectOk<MailImmediateSendResponse>(r, "failed to send mail template");
}

export async function previewImmediateMail(request: MailImmediateSendRequest): Promise<MailPreviewResponse> {
  const r = await fetch(`${BASE}/mail/immediate/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailPreviewResponse>(r, "failed to preview immediate mail");
}

export async function sendImmediateMail(request: MailImmediateSendRequest): Promise<MailImmediateSendResponse> {
  const r = await fetch(`${BASE}/mail/immediate/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailImmediateSendResponse>(r, "failed to send immediate mail");
}

export async function fetchMailSchedules(): Promise<MailSchedule[]> {
  const r = await fetch(`${BASE}/mail/schedules`, { headers: { ...authHeaders() } });
  return expectOk<MailSchedule[]>(r, "failed to load mail schedules");
}

export async function createMailSchedule(request: MailScheduleCreateRequest): Promise<MailSchedule> {
  const r = await fetch(`${BASE}/mail/schedules`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailSchedule>(r, "failed to create mail schedule");
}

export async function updateMailSchedule(
  scheduleId: number,
  request: MailScheduleUpdateRequest,
): Promise<MailSchedule> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(request),
  });
  return expectOk<MailSchedule>(r, "failed to update mail schedule");
}

export async function fetchTemplateSchedules(templateId: number): Promise<MailSchedule[]> {
  const r = await fetch(`${BASE}/mail/templates/${templateId}/schedules`, {
    headers: { ...authHeaders() },
  });
  return expectOk<MailSchedule[]>(r, "failed to load template schedules");
}

export async function deleteMailSchedule(scheduleId: number): Promise<void> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  if (!r.ok) {
    const body = await parseErrorBody(r);
    const message =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof (body as { detail?: unknown }).detail === "string"
        ? (body as { detail: string }).detail
        : `failed to delete mail schedule (HTTP ${r.status})`;
    throw new ApiError(r.status, message, body);
  }
}

export async function pauseMailSchedule(scheduleId: number): Promise<MailSchedule> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}/pause`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MailSchedule>(r, "failed to pause mail schedule");
}

export async function resumeMailSchedule(scheduleId: number): Promise<MailSchedule> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}/resume`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MailSchedule>(r, "failed to resume mail schedule");
}

export async function sendNowMailSchedule(scheduleId: number): Promise<MailImmediateSendResponse> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}/send-now`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<MailImmediateSendResponse>(r, "failed to send mail schedule now");
}

export async function fetchMailScheduleLogs(scheduleId: number): Promise<MailDeliveryLog[]> {
  const r = await fetch(`${BASE}/mail/schedules/${scheduleId}/logs`, { headers: { ...authHeaders() } });
  return expectOk<MailDeliveryLog[]>(r, "failed to load mail schedule logs");
}
