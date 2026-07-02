// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CrawlMethodList } from "./CrawlMethodList";
import type { NewsRunFormState } from "./NewsRunControl";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const listDiscoveryMethods = vi.fn();
const fetchDiscoveryMethod = vi.fn();

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  listDiscoveryMethods: (...args: unknown[]) => listDiscoveryMethods(...args),
  fetchDiscoveryMethod: (...args: unknown[]) => fetchDiscoveryMethod(...args),
}));

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

describe("CrawlMethodList", () => {
  let container: HTMLDivElement;
  let root: Root;
  let queryClient: QueryClient;

  const runLimitState: NewsRunFormState = {
    timeMode: "relative",
    relativeRange: "7d",
    startDate: "",
    endDate: "",
    targetCount: "12",
  };

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    listDiscoveryMethods.mockResolvedValue([
      {
        id: 7,
        domain: "example.com",
        entry_url: "https://example.com/news",
        status: "active",
        signature: "sig",
        last_run_at: null,
        last_run_status: null,
      },
    ]);
    fetchDiscoveryMethod.mockResolvedValue({
      discovered_count: 2,
      stored_count: 1,
      items: [],
      stats: {},
      message: "ok",
    });
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    queryClient.clear();
    vi.clearAllMocks();
  });

  it("sends shared run limits when batch fetching selected discovery methods", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    const checkbox = container.querySelector("input[type='checkbox']") as HTMLInputElement | null;
    expect(checkbox).toBeTruthy();

    await act(async () => {
      checkbox?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    const fetchButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("抓取选中"),
    );
    expect(fetchButton).toBeTruthy();

    await act(async () => {
      fetchButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(fetchDiscoveryMethod).toHaveBeenCalledWith(
      7,
      {
        time_mode: "relative",
        relative_range: "7d",
        start_at: null,
        end_at: null,
        target_count: 12,
      },
      expect.any(AbortSignal),
    );
  });

  it("stops batch fetching when the user cancels the crawl", async () => {
    listDiscoveryMethods.mockResolvedValue([
      {
        id: 7,
        domain: "example.com",
        entry_url: "https://example.com/news",
        status: "active",
        signature: "sig",
        last_run_at: null,
        last_run_status: null,
      },
      {
        id: 8,
        domain: "second.example.com",
        entry_url: "https://second.example.com/news",
        status: "active",
        signature: "sig-2",
        last_run_at: null,
        last_run_status: null,
      },
    ]);

    let resolveFirst: ((value: { discovered_count: number; stored_count: number; items: []; stats: {}; message: string }) => void) | null = null;
    fetchDiscoveryMethod.mockImplementation((_id: number, _request?: unknown, signal?: AbortSignal) => {
      if (resolveFirst == null) {
        return new Promise((resolve, reject) => {
          resolveFirst = resolve as typeof resolveFirst;
          signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          }, { once: true });
        });
      }
      return Promise.resolve({
        discovered_count: 1,
        stored_count: 1,
        items: [],
        stats: {},
        message: "ok",
      });
    });

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });

    await flush();

    const checkboxes = Array.from(container.querySelectorAll("input[type='checkbox']")) as HTMLInputElement[];
    expect(checkboxes).toHaveLength(2);

    await act(async () => {
      checkboxes[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
      checkboxes[1].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    const fetchButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("抓取选中"),
    ) as HTMLButtonElement | undefined;
    expect(fetchButton).toBeTruthy();

    await act(async () => {
      fetchButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    const cancelButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("取消抓取"),
    ) as HTMLButtonElement | undefined;
    expect(cancelButton).toBeTruthy();

    await act(async () => {
      cancelButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(fetchDiscoveryMethod).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("已取消");
  });
});
