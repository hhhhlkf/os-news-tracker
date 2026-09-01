import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { listDiscoveryQueue } from "../api/client";
import { fetchDiscussionPipelineEvents } from "../discussions/api";
import type { NewsRunLogEntry } from "../types";
import { useDiscoveryLogs } from "./useDiscoveryLogs";

const QUEUE_QUERY_KEY = ["discovery-queue"] as const;

export function useUnifiedDiscoveryLogs(
  selectedQueueItemId: number | null,
  discussionRunId: string | null,
): NewsRunLogEntry[] {
  const queueQuery = useQuery({
    queryKey: QUEUE_QUERY_KEY,
    queryFn: listDiscoveryQueue,
    refetchInterval: 1500,
  });

  const pending = queueQuery.data?.pending ?? [];
  const failedItems = queueQuery.data?.failed ?? [];
  const completedItems = queueQuery.data?.completed ?? [];
  const selected = pending.find((item) => item.id === selectedQueueItemId)
    ?? failedItems.find((item) => item.id === selectedQueueItemId)
    ?? completedItems.find((item) => item.id === selectedQueueItemId)
    ?? null;
  const watchRunId = selected?.run_id ?? null;

  const discoveryLogs = useDiscoveryLogs(watchRunId, watchRunId != null, "run");
  const manualMethodLogs = useDiscoveryLogs(0, true, true);

  // The unified drawer follows only the run explicitly selected by this
  // browser session.  Falling back to the globally latest run resurrected
  // scheduled mail/GitHub history immediately after "重置状态".
  const visibleRunId = discussionRunId;
  const runStream = useQuery({
    queryKey: ["discussion-run-events", visibleRunId],
    queryFn: () => fetchDiscussionPipelineEvents(visibleRunId as string),
    enabled: Boolean(visibleRunId),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status === "running" || status === "stopping" ? 1000 : false;
    },
  });

  return useMemo(() => {
    const methodLogs = manualMethodLogs
      .filter(
        (log) =>
          typeof log.method_id === "number" &&
          (log.trigger_type === "manual" || log.trigger_type === "manual_method"),
      )
      .map((log) => ({ ...log, id: -log.id, sequence: -log.sequence }));

    const events = runStream.data?.events ?? [];
    const discussionLogs: NewsRunLogEntry[] = events.map((event, index) => {
      const sequence = "sequence" in event && typeof event.sequence === "number" ? event.sequence : index + 1;
      return {
        id: 1_000_000 + sequence,
        ts: event.at,
        level: event.level === "success" ? "info" : event.level,
        stage: event.stage,
        source: event.source ?? null,
        message: event.message,
        provider: event.provider ?? "organizer",
      };
    });

    return [...discoveryLogs, ...methodLogs, ...discussionLogs]
      .sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts));
  }, [discoveryLogs, manualMethodLogs, runStream.data?.events]);
}
