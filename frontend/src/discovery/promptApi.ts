import { authHeaders } from "../auth";
import { ApiError } from "../api/client";

const configuredBase = import.meta.env.VITE_API_BASE?.trim();
const BASE = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
const PROMPT_BASE = `${BASE}/discovery`;

export interface PromptStage {
  key: string;
  label: string;
  description: string;
  required_tokens: string[];
  default_template: string;
}

export interface PromptSet {
  id: number;
  name: string;
  is_active: boolean;
  prompts: Record<string, string>;
  created_at?: string | null;
  updated_at?: string | null;
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

export async function fetchPromptStages(): Promise<PromptStage[]> {
  const r = await fetch(`${PROMPT_BASE}/prompt-stages`, { headers: { ...authHeaders() } });
  return expectOk<PromptStage[]>(r, "failed to load prompt stages");
}

export async function fetchPromptSets(): Promise<PromptSet[]> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets`, { headers: { ...authHeaders() } });
  return expectOk<PromptSet[]>(r, "failed to load prompt sets");
}

export async function createPromptSet(payload: {
  name: string;
  prompts: Record<string, string>;
}): Promise<PromptSet> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  return expectOk<PromptSet>(r, "failed to create prompt set");
}

export async function updatePromptSet(
  id: number,
  payload: { name?: string; prompts?: Record<string, string> },
): Promise<PromptSet> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  return expectOk<PromptSet>(r, "failed to update prompt set");
}

export async function deletePromptSet(id: number): Promise<void> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets/${id}`, {
    method: "DELETE",
    headers: { ...authHeaders() },
  });
  await ensureOk(r, "failed to delete prompt set");
}

export async function activatePromptSet(id: number): Promise<PromptSet> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets/${id}/activate`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<PromptSet>(r, "failed to activate prompt set");
}

export async function deactivatePromptSet(id: number): Promise<PromptSet> {
  const r = await fetch(`${PROMPT_BASE}/prompt-sets/${id}/deactivate`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  return expectOk<PromptSet>(r, "failed to deactivate prompt set");
}
