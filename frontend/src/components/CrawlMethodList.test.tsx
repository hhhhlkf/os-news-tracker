// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CrawlMethodList } from "./CrawlMethodList";
import type { NewsRunFormState } from "./runLimits";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const listDiscoveryMethods = vi.fn();
const fetchDiscoveryMethod = vi.fn();
const cancelDiscoveryMethodFetch = vi.fn();

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
  cancelDiscoveryMethodFetch: (...args: unknown[]) => cancelDiscoveryMethodFetch(...args),
  deleteDiscoveryMethod: vi.fn(),
}));

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

function method(id: number, sourceName: string, status = "active") {
  return {
    id,
    source_name: sourceName,
    domain: `${sourceName.toLowerCase().replaceAll(" ", "-")}.example.com`,
    entry_url: `https://${sourceName.toLowerCase().replaceAll(" ", "-")}.example.com/news`,
    status,
    signature: `sig-${id}`,
    last_run_at: null,
    last_run_status: null,
  };
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
    window.sessionStorage.clear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    listDiscoveryMethods.mockResolvedValue([method(7, "LangChain Blog")]);
    fetchDiscoveryMethod.mockResolvedValue({
      discovered_count: 2,
      stored_count: 1,
      items: [],
      stats: {},
      message: "ok",
    });
    cancelDiscoveryMethodFetch.mockResolvedValue({ cancelled: true, killed: true, method_id: 7 });
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    queryClient.clear();
    window.sessionStorage.clear();
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
        trigger_type: "manual_method",
        batch_id: null,
      },
      expect.any(AbortSignal),
    );
  });

  it("unlocks stale batchRunning state restored after refresh", async () => {
    window.sessionStorage.setItem(
      "crawl-method-list-state:v1",
      JSON.stringify({
        selectedIds: [7],
        rowStates: { 7: { kind: "running" } },
        summary: { text: "正在取消当前抓取批次…", tone: "danger", showItemsLink: false },
        batchRunning: true,
        batchCancelling: true,
        batchDeleting: false,
        page: 1,
        pageSize: 10,
      }),
    );

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });
    await flush();

    expect(container.textContent).toContain("已自动解锁");
    expect(container.textContent).not.toContain("取消中…");
    const fetchButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("抓取选中"),
    ) as HTMLButtonElement | undefined;
    expect(fetchButton).toBeTruthy();
    expect(fetchButton?.disabled).toBe(false);
  });

  it("stops batch fetching when the user cancels the crawl", async () => {
    listDiscoveryMethods.mockResolvedValue([method(7, "LangChain Blog"), method(8, "Second Blog")]);

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

    const checkboxes = Array.from(container.querySelectorAll("input[aria-label^='选择 ']")) as HTMLInputElement[];
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
    expect(cancelDiscoveryMethodFetch).toHaveBeenCalledWith(7);
    expect(container.textContent).toContain("已强制取消");
  });

  it("renders source_name as the primary crawl method label when available", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });

    await flush();

    expect(container.textContent).toContain("LangChain Blog");
    expect(container.textContent).toContain("langchain-blog.example.com");
  });

  it("paginates crawl methods and navigates between pages", async () => {
    listDiscoveryMethods.mockResolvedValue(Array.from({ length: 12 }, (_, index) => method(index + 1, `Method ${index + 1}`)));

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });

    await flush();

    expect(container.textContent).toContain("第 1-10 条 / 共 12 条");
    expect(container.textContent).toContain("Method 1");
    expect(container.querySelector("input[aria-label='选择 Method 12']")).toBeNull();

    const nextButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("下一页"),
    );
    expect(nextButton).toBeTruthy();

    await act(async () => {
      nextButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("第 11-12 条 / 共 12 条");
    expect(container.textContent).toContain("Method 12");
    expect(container.querySelector("input[aria-label='选择 Method 1']")).toBeNull();
  });

  it("selects all enabled methods on the current page only", async () => {
    listDiscoveryMethods.mockResolvedValue([
      ...Array.from({ length: 10 }, (_, index) => method(index + 1, `Method ${index + 1}`)),
      method(11, "Disabled Method", "disabled"),
      method(12, "Enabled Method 12"),
    ]);

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodList runLimitState={runLimitState} />
        </QueryClientProvider>,
      );
    });

    await flush();

    const selectAll = container.querySelector("input[aria-label='全选当前页爬取方式']") as HTMLInputElement | null;
    expect(selectAll).toBeTruthy();

    await act(async () => {
      selectAll?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("已选 10 个");

    const nextButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("下一页"),
    );
    await act(async () => {
      nextButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("已选 10 个");

    const secondPageSelectAll = container.querySelector("input[aria-label='全选当前页爬取方式']") as HTMLInputElement | null;
    await act(async () => {
      secondPageSelectAll?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("已选 11 个");
  });
});
