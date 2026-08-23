import { useEffect, useRef, useState } from "react";
import { ApiError, extractDiscoverySseFrames, openDiscoveryEventStream, parseDiscoverySseFrame } from "../api/client";
import { authHeaders } from "../auth";
import type { DiscoveryRunEvent, NewsRunLogEntry } from "../types";

const UI_BATCH_MS = 75;
const INITIAL_RETRY_MS = 500;
const MAX_RETRY_MS = 8_000;

const LEGACY_DISCOVERY_STAGES = new Set([
  "任务", "抓首页", "抓网络请求", "路由", "命名", "探查", "验证URL", "写配方", "审计", "存库", "抓方式", "定时抓取",
]);

type DiscoveryLogMode = "run" | "all_discovery" | "run_plus_methods";

export type DiscoveryEventLogEntry = NewsRunLogEntry & {
  event_type: string;
  phase: string | null;
  round: number | null;
  payload: Record<string, unknown> | null;
  run_id: number;
  sequence: number;
};

function asLog(event: DiscoveryRunEvent): DiscoveryEventLogEntry {
  return {
    id: event.sequence,
    sequence: event.sequence,
    run_id: event.run_id,
    ts: event.created_at ?? new Date().toISOString(),
    level: event.level,
    stage: event.phase ?? event.event_type,
    source: null,
    message: event.summary,
    event_type: event.event_type,
    phase: event.phase,
    round: event.round,
    payload: event.payload,
  };
}

function asLegacyLog(log: NewsRunLogEntry): DiscoveryEventLogEntry {
  return {
    ...log,
    sequence: log.id,
    run_id: Number(log.run_id ?? 0),
    event_type: "legacy_run_log",
    phase: typeof log.phase === "string" ? log.phase : log.stage,
    round: null,
    payload: null,
  };
}

function isLegacyMethodLog(log: NewsRunLogEntry): boolean {
  return typeof log.method_id === "number";
}

function includeLegacyLog(log: NewsRunLogEntry, includeAll: boolean, runId: number): boolean {
  if (includeAll) return LEGACY_DISCOVERY_STAGES.has(log.stage) || isLegacyMethodLog(log);
  return Number(log.run_id) === runId;
}

function appendOrdered(
  previous: DiscoveryEventLogEntry[],
  incoming: DiscoveryEventLogEntry[],
): DiscoveryEventLogEntry[] {
  if (incoming.length === 0) return previous;
  const bySequence = new Map(previous.map((entry) => [entry.sequence, entry]));
  for (const entry of incoming) bySequence.set(entry.sequence, entry);
  return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
}

/** Tail one run's persisted event stream using authenticated fetch-based SSE. */
export function useDiscoveryLogs(
  runId: number | null,
  enabled: boolean,
  mode: boolean | DiscoveryLogMode = false,
): DiscoveryEventLogEntry[] {
  const [logs, setLogs] = useState<DiscoveryEventLogEntry[]>([]);
  const cursorRef = useRef(0);

  useEffect(() => {
    setLogs([]);
    cursorRef.current = 0;
    if (!enabled || runId == null) return;

    if (typeof mode === "boolean") {
      let stopped = false;
      let inFlight = false;
      let lastId = 0;
      let epoch: string | null = null;
      const poll = async () => {
        if (stopped || inFlight) return;
        inFlight = true;
        try {
          const configuredBase = import.meta.env.VITE_API_BASE?.trim();
          const base = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
          const response = await fetch(`${base}/run-logs?after_id=${lastId}`, {
            headers: authHeaders(),
          });
          if (!response.ok) return;
          const data = await response.json() as { epoch?: string; logs: NewsRunLogEntry[] };
          if (data.epoch && epoch && data.epoch !== epoch) {
            epoch = data.epoch;
            lastId = 0;
            if (!stopped) setLogs([]);
            return;
          }
          if (data.epoch) epoch = data.epoch;
          if (data.logs.length > 0) lastId = Math.max(lastId, ...data.logs.map((log) => log.id));
          const next = data.logs.filter((log) => includeLegacyLog(log, mode, runId)).map(asLegacyLog);
          if (!stopped) setLogs((previous) => appendOrdered(previous, next));
        } catch {
          // The persisted SSE path replaces this compatibility poll for new callers.
        } finally {
          inFlight = false;
        }
      };
      void poll();
      const timer = window.setInterval(poll, 1500);
      return () => {
        stopped = true;
        window.clearInterval(timer);
      };
    }

    let stopped = false;
    let retryMs = INITIAL_RETRY_MS;
    let reconnectTimer: number | undefined;
    let flushTimer: number | undefined;
    let controller: AbortController | null = null;
    let terminalReached = false;
    const pending: DiscoveryEventLogEntry[] = [];

    const flush = () => {
      flushTimer = undefined;
      if (stopped || pending.length === 0) return;
      const batch = pending.splice(0, pending.length);
      setLogs((previous) => appendOrdered(previous, batch));
    };

    const queue = (event: DiscoveryRunEvent) => {
      if (event.run_id !== runId || event.sequence <= cursorRef.current) return;
      cursorRef.current = event.sequence;
      pending.push(asLog(event));
      retryMs = INITIAL_RETRY_MS;
      if (flushTimer === undefined) flushTimer = window.setTimeout(flush, UI_BATCH_MS);
    };

    const connect = async () => {
      if (stopped) return;
      controller = new AbortController();
      let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
      try {
        const response = await openDiscoveryEventStream(runId, cursorRef.current, controller.signal);
        const reader = response.body?.getReader();
        if (!reader) throw new Error("Discovery SSE response has no body");
        activeReader = reader;
        const decoder = new TextDecoder();
        let buffer = "";
        const consumeFrames = (endOfStream = false) => {
          const extracted = extractDiscoverySseFrames(buffer, endOfStream);
          buffer = extracted.remainder;
          for (const frame of extracted.frames) {
            if (frame.split(/\r?\n/).some((line) => line.trim() === "event: end")) {
              terminalReached = true;
              continue;
            }
            const event = parseDiscoverySseFrame(frame);
            if (event) queue(event);
          }
        };
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) {
            buffer += decoder.decode();
            consumeFrames(true);
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          consumeFrames();
        }
      } catch (error) {
        if (stopped || controller?.signal.aborted) return;
        await activeReader?.cancel().catch(() => undefined);
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
          terminalReached = true;
        }
        if (import.meta.env.DEV) console.debug("Discovery SSE reconnecting", error);
      } finally {
        activeReader?.releaseLock();
        controller = null;
        flush();
      }
      if (!stopped && !terminalReached) {
        reconnectTimer = window.setTimeout(connect, retryMs);
        retryMs = Math.min(MAX_RETRY_MS, retryMs * 2);
      }
    };

    void connect();
    return () => {
      stopped = true;
      controller?.abort();
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (flushTimer !== undefined) window.clearTimeout(flushTimer);
    };
  }, [enabled, mode, runId]);

  return logs;
}
