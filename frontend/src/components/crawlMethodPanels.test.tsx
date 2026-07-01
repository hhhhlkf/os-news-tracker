// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CrawlMethodDetail } from "./CrawlMethodDetail";
import { CrawlMethodList } from "./CrawlMethodList";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

function renderWithQueryClient(root: Root, queryClient: QueryClient, node: import("react").ReactNode) {
  return act(async () => {
    root.render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
  });
}

describe("crawl method query states", () => {
  let container: HTMLDivElement;
  let root: Root;
  let queryClient: QueryClient;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    queryClient.clear();
    vi.restoreAllMocks();
  });

  it("shows a loading state for the method list", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));

    await renderWithQueryClient(root, queryClient, <CrawlMethodList />);

    expect(container.textContent).toContain("加载爬取方式中");
  });

  it("shows an error state for the method list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "boom" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await renderWithQueryClient(root, queryClient, <CrawlMethodList />);
    await flush();

    expect(container.textContent).toContain("加载爬取方式失败");
  });

  it("shows a loading state for the method detail drawer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));

    await renderWithQueryClient(root, queryClient, <CrawlMethodDetail methodId={3} onClose={() => {}} />);

    expect(container.textContent).toContain("加载爬取方式详情中");
  });

  it("shows an error state for the method detail drawer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "boom" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await renderWithQueryClient(root, queryClient, <CrawlMethodDetail methodId={3} onClose={() => {}} />);
    await flush();

    expect(container.textContent).toContain("加载爬取方式详情失败");
  });
});
