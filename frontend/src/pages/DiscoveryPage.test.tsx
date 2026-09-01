// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiscoveryPage } from "./DiscoveryPage";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../components/DiscoveryPanel", () => ({
  DiscoveryPanel: () => <div>智能探查模块</div>,
}));

vi.mock("../components/BatchDiscoveryQueue", () => ({
  BatchDiscoveryQueue: () => <div>批量探查队列</div>,
}));

vi.mock("../components/RunLimitCard", () => ({
  RunLimitCard: () => <div>抓取限制</div>,
}));

vi.mock("../components/CrawlMethodList", () => ({
  CrawlMethodList: () => <div>抓取模块 · 爬取方式库</div>,
}));

vi.mock("../components/CrawlMethodDetail", () => ({
  CrawlMethodDetail: () => <div>方法详情</div>,
}));

describe("DiscoveryPage", () => {
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
  });

  it("renders the discovery deck and bookmark drawers without news flow controls", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/discover/probe"]}>
            <DiscoveryPage hasSystemAccess />
          </MemoryRouter>
        </QueryClientProvider>,
      );
    });

    const text = container.textContent ?? "";
    expect(text).toContain("智能探查模块");
    expect(text).toContain("抓取模块 · 爬取方式库");
    expect(text).toContain("抓取限制");
    expect(text).toContain("批量探查队列");
    expect(container.querySelector("[aria-label='打开抓取限制']")).toBeTruthy();
    expect(container.querySelector("[aria-label='打开运行日志']")).toBeTruthy();
    expect(text).not.toContain("新闻流控制区");
    expect(text).not.toContain("新闻处理控制");
    expect(container.querySelector("[data-testid='discover-methods-divider']")).toBeFalsy();
  });
});
