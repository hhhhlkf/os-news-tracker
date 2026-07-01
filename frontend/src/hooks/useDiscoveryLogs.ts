// frontend/src/hooks/useDiscoveryLogs.ts
import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../auth";
import type { NewsRunLogEntry } from "../types";

export function useDiscoveryLogs(runId: number | null, enabled: boolean) {
  const [logs, setLogs] = useState<NewsRunLogEntry[]>([]);
  const lastId = useRef(0);
  useEffect(() => {
    if (!enabled || runId == null) return;
    setLogs([]);
    lastId.current = 0;
    let stop = false;
    async function poll() {
      try {
        const base = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
        const r = await fetch(`${base}/news-run/logs?after_id=${lastId.current}`, { headers: authHeaders() });
        if (!r.ok) return;
        const data = (await r.json()) as { logs: NewsRunLogEntry[] };
        const mine = data.logs.filter((l) => Number((l as Record<string, unknown>).run_id) === runId);
        if (mine.length) lastId.current = Math.max(lastId.current, ...mine.map((l) => l.id));
        if (!stop) setLogs((prev) => [...prev, ...mine]);
      } catch { /* ignore */ }
    }
    poll();
    const t = window.setInterval(poll, 1500);
    return () => { stop = true; window.clearInterval(t); };
  }, [runId, enabled]);
  return logs;
}
