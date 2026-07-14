// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDiscoveryLogs } from "./useDiscoveryLogs";
import type { NewsRunLogEntry } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function flush() {
  return act(async () => {
    await Promise.resolve();
  });
}

function Harness({ runId, enabled, includeAll = false }: { runId: number | null; enabled: boolean; includeAll?: boolean }) {
  const logs = useDiscoveryLogs(runId, enabled, includeAll);
  return <div data-testid="logs">{logs.map((log) => log.message).join("|")}</div>;
}

describe("useDiscoveryLogs", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("advances after_id using the highest shared log id while only storing current run logs", async () => {
    const firstBatch: NewsRunLogEntry[] = [
      { id: 4, ts: "2026-07-01T00:00:00Z", level: "info", stage: "other", source: null, message: "other run", run_id: 99 },
      { id: 2, ts: "2026-07-01T00:00:01Z", level: "info", stage: "discovery", source: null, message: "mine-1", run_id: 7 },
    ];
    const secondBatch: NewsRunLogEntry[] = [
      { id: 5, ts: "2026-07-01T00:00:02Z", level: "info", stage: "discovery", source: null, message: "mine-2", run_id: 7 },
    ];
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ logs: firstBatch }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ logs: secondBatch }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => {
      root.render(<Harness runId={7} enabled />);
    });
    await flush();

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    await flush();

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining("/run-logs?after_id=4"),
      expect.any(Object),
    );
    expect(container.textContent).toContain("mine-1|mine-2");
    expect(container.textContent).not.toContain("other run");
  });

  it("keeps only discovery and crawl-method logs when includeAll is enabled", async () => {
    const batch: NewsRunLogEntry[] = [
      { id: 4, ts: "2026-07-01T00:00:00Z", level: "info", stage: "process", source: null, message: "other run", run_id: 99 },
      { id: 5, ts: "2026-07-01T00:00:01Z", level: "info", stage: "探查", source: null, message: "mine-1", run_id: 7 },
      { id: 6, ts: "2026-07-01T00:00:02Z", level: "info", stage: "抓方式", source: null, message: "method-run", method_id: 5 },
    ];
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify({ logs: batch }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => {
      root.render(<Harness runId={7} enabled includeAll />);
    });
    await flush();

    expect(container.textContent).toContain("mine-1|method-run");
    expect(container.textContent).not.toContain("other run");
  });
});
