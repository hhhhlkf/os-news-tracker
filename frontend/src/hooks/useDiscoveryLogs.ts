// frontend/src/hooks/useDiscoveryLogs.ts
import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../auth";
import type { NewsRunLogEntry } from "../types";

const DISCOVERY_RELATED_STAGES = new Set([
  "任务",
  "抓首页",
  "抓网络请求",
  "路由",
  "探查",
  "验证URL",
  "写配方",
  "审计",
  "存库",
  "抓方式",
]);

export function useDiscoveryLogs(runId: number | null, enabled: boolean, includeAll = false) {
  const [logs, setLogs] = useState<NewsRunLogEntry[]>([]);
  const lastId = useRef(0);
  useEffect(() => {
    if (!enabled || (!includeAll && runId == null)) return;
    setLogs([]);
    lastId.current = 0;
    let stop = false;
    async function poll() {
      try {
        const base = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
        const r = await fetch(`${base}/news-run/logs?after_id=${lastId.current}`, { headers: authHeaders() });
        if (!r.ok) return;
        const data = (await r.json()) as { logs: NewsRunLogEntry[] };
        if (data.logs.length) {
          lastId.current = Math.max(lastId.current, ...data.logs.map((l) => l.id));
        }
        const nextLogs = includeAll
          ? data.logs.filter((l) => DISCOVERY_RELATED_STAGES.has(l.stage))
          : data.logs.filter((l) => Number((l as Record<string, unknown>).run_id) === runId);
        if (!stop) setLogs((prev) => [...prev, ...nextLogs]);
      } catch { /* ignore */ }
    }
    poll();
    const t = window.setInterval(poll, 1500);
    return () => { stop = true; window.clearInterval(t); };
  }, [runId, enabled, includeAll]);
  return logs;
}
