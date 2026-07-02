// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HomePage } from "./HomePage";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../api/client", () => ({
  fetchItems: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 }),
  fetchFacets: vi.fn().mockResolvedValue({ main_category: [], info_type: [], importance: [], sub_tags: [] }),
  fetchNewsRunStatus: vi.fn().mockResolvedValue({ state: "idle" }),
  fetchNewsRunLogs: vi.fn().mockResolvedValue({ logs: [] }),
  fetchAgentSources: vi.fn().mockResolvedValue([]),
  fetchAgentSourceCandidates: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 5, total_pages: 1 }),
  fetchAgentRuns: vi.fn().mockResolvedValue([]),
  startNewsRun: vi.fn(),
  stopNewsRun: vi.fn(),
  triggerAgentRun: vi.fn(),
  triggerAgentRunFromCandidate: vi.fn(),
  cancelAgentRun: vi.fn(),
  deleteAgentSource: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

vi.mock("../components/FacetSidebar", () => ({
  FacetSidebar: () => <div>侧边栏</div>,
}));

vi.mock("../components/ItemList", () => ({
  ItemList: () => <div>新闻条目列表</div>,
}));

vi.mock("../components/ItemDetail", () => ({
  ItemDetail: () => <div>详情</div>,
}));

vi.mock("../components/NewsRunControl", () => ({
  NewsRunControl: () => <div>新闻处理控制</div>,
  buildNewsRunFormState: () => ({
    timeMode: "relative",
    relativeRange: "7d",
    startDate: "",
    endDate: "",
    targetCount: "20",
  }),
  toAbsoluteDateTime: () => null,
}));

vi.mock("../components/NewsRunLogPanel", () => ({
  NewsRunLogPanel: () => <div>运行日志</div>,
}));

vi.mock("../components/SourceManager", () => ({
  SourceManager: () => <div>抓取来源</div>,
}));

vi.mock("../components/AgentRunControl", () => ({
  AgentRunControl: () => <div>Agent控制</div>,
}));

describe("HomePage", () => {
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
    vi.clearAllMocks();
  });

  it("keeps the title, stats cards, search tools, and news items without rendering news flow controls", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <HomePage />
        </QueryClientProvider>,
      );
    });

    expect(container.textContent).toContain("技术新闻追踪");
    expect(container.textContent).toContain("当前数据源");
    expect(container.textContent).toContain("当前条目数");
    expect(container.textContent).toContain("激活筛选");
    expect(container.querySelector("input[placeholder='搜索标题、摘要、分类…']")).toBeTruthy();
    expect(container.textContent).toContain("发布时间 最新优先");
    expect(container.textContent).toContain("清空筛选");
    expect(container.textContent).toContain("新闻条目列表");
    expect(container.textContent).not.toContain("新闻处理控制");
    expect(container.textContent).not.toContain("运行日志");
  });
});
