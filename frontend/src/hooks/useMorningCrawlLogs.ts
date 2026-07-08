import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../auth";
import type { NewsRunLogEntry } from "../types";

const MORNING_CRAWL_STAGE = "定时抓取";
const MAX_LOGS = 500;

/** 轮询共享运行日志缓冲，过滤出「定时抓取」阶段的日志，用于实时时间线展示。 */
export function useMorningCrawlLogs(enabled: boolean) {
  const [logs, setLogs] = useState<NewsRunLogEntry[]>([]);
  const lastId = useRef(0);
  const inFlight = useRef(false);

  useEffect(() => {
    if (!enabled) return;
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
        const next = data.logs.filter((l) => l.stage === MORNING_CRAWL_STAGE);
        if (!stop && next.length) {
          setLogs((prev) => {
            const seen = new Set(prev.map((log) => log.id));
            const deduped = next.filter((log) => !seen.has(log.id));
            if (deduped.length === 0) return prev;
            const merged = [...prev, ...deduped];
            return merged.length > MAX_LOGS ? merged.slice(merged.length - MAX_LOGS) : merged;
          });
        }
      } catch {
        /* ignore */
      } finally {
        inFlight.current = false;
      }
    }

    poll();
    const t = window.setInterval(poll, 1500);
    return () => {
      stop = true;
      window.clearInterval(t);
    };
  }, [enabled]);

  return logs;
}
