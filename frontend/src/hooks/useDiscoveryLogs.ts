// frontend/src/hooks/useDiscoveryLogs.ts
import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../auth";
import type { NewsRunLogEntry } from "../types";

const DISCOVERY_RELATED_STAGES = new Set([
  "任务",
  "抓首页",
  "抓网络请求",
  "路由",
  "命名",
  "探查",
  "验证URL",
  "写配方",
  "审计",
  "存库",
  "抓方式",
  "定时抓取",
]);

function isDiscoveryLog(log: NewsRunLogEntry, includeAll: boolean, runId: number | null) {
  if (!includeAll) {
    return Number((log as Record<string, unknown>).run_id) === runId;
  }
  if (DISCOVERY_RELATED_STAGES.has(log.stage)) {
    return true;
  }
  return log.stage === "process" && typeof log.method_id === "number";
}

export function useDiscoveryLogs(runId: number | null, enabled: boolean, includeAll = false) {
  const [logs, setLogs] = useState<NewsRunLogEntry[]>([]);
  const lastId = useRef(0);
  const inFlight = useRef(false);
  useEffect(() => {
    if (!enabled || (!includeAll && runId == null)) return;
    setLogs([]);
    lastId.current = 0;
    inFlight.current = false;
    let stop = false;
    async function poll() {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const configuredBase = import.meta.env.VITE_API_BASE?.trim();
        const base = configuredBase ? configuredBase.replace(/\/+$/, "") : "";
        const r = await fetch(`${base}/news-run/logs?after_id=${lastId.current}`, { headers: authHeaders() });
        if (!r.ok) return;
        const data = (await r.json()) as { logs: NewsRunLogEntry[] };
        if (data.logs.length) {
          lastId.current = Math.max(lastId.current, ...data.logs.map((l) => l.id));
        }
        const nextLogs = data.logs.filter((l) => isDiscoveryLog(l, includeAll, runId));
        if (!stop) {
          setLogs((prev) => {
            if (nextLogs.length === 0) return prev;
            const seen = new Set(prev.map((log) => log.id));
            const deduped = nextLogs.filter((log) => !seen.has(log.id));
            return deduped.length > 0 ? [...prev, ...deduped] : prev;
          });
        }
      } catch { /* ignore */ }
      finally {
        inFlight.current = false;
      }
    }
    poll();
    const t = window.setInterval(poll, 1500);
    return () => { stop = true; window.clearInterval(t); };
  }, [runId, enabled, includeAll]);
  return logs;
}
