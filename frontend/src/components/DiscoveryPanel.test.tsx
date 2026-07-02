// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiscoveryPanel } from "./DiscoveryPanel";

const startDiscoveryRun = vi.fn();
const getDiscoveryRun = vi.fn();
const cancelDiscoveryRun = vi.fn();
const suggestDiscoveryName = vi.fn();

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {},
  getDiscoveryRun: (...args: unknown[]) => getDiscoveryRun(...args),
  startDiscoveryRun: (...args: unknown[]) => startDiscoveryRun(...args),
  cancelDiscoveryRun: (...args: unknown[]) => cancelDiscoveryRun(...args),
  suggestDiscoveryName: (...args: unknown[]) => suggestDiscoveryName(...args),
}));

vi.mock("./DiscoveryFlowChart", () => ({
  DiscoveryFlowChart: () => <div>flow</div>,
}));

vi.mock("./DiscoveryNodeDetail", () => ({
  DiscoveryNodeDetail: () => <div>detail</div>,
}));

vi.mock("./DiscoveryLogPanel", () => ({
  DiscoveryLogPanel: () => <div data-testid="discovery-logs">logs</div>,
}));

vi.mock("../hooks/useDiscoveryLogs", () => ({
  useDiscoveryLogs: () => [],
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

function changeInput(element: HTMLInputElement, value: string) {
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

describe("DiscoveryPanel", () => {
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
    getDiscoveryRun.mockReset();
    startDiscoveryRun.mockReset();
    cancelDiscoveryRun.mockReset();
    suggestDiscoveryName.mockReset();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    queryClient.clear();
    vi.restoreAllMocks();
  });

  it("disables the start button as soon as the start request is pending", async () => {
    let resolveStart: ((value: { status: "started"; run_id: number }) => void) | null = null;
    startDiscoveryRun.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve as typeof resolveStart;
        }),
    );

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    await flush();

    const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "开始探查") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(startDiscoveryRun).toHaveBeenCalledOnce();
    const startButtonAfterClick = [...container.querySelectorAll("button")].find((node) =>
      node.textContent === "开始探查" || node.textContent === "启动中…",
    ) as HTMLButtonElement;
    expect(startButtonAfterClick.disabled).toBe(true);

    await act(async () => {
      resolveStart?.({ status: "started", run_id: 11 });
    });
  });

  it("prevents duplicate override starts while the force restart request is pending", async () => {
    let callCount = 0;
    let resolveOverride: ((value: { status: "started"; run_id: number }) => void) | null = null;
    startDiscoveryRun.mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          status: "duplicate",
          existing_method: {
            method_id: 5,
            domain: "example.com",
            signature: "sig",
            dsl_recipe: {},
            last_run_at: null,
            last_run_status: null,
          },
        });
      }
      return new Promise((resolve) => {
        resolveOverride = resolve as typeof resolveOverride;
      });
    });

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    await flush();

    const startButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "开始探查") as HTMLButtonElement;
    act(() => {
      startButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    const overrideButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "覆盖重探") as HTMLButtonElement;
    expect(overrideButton.disabled).toBe(false);

    act(() => {
      overrideButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      overrideButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(startDiscoveryRun).toHaveBeenCalledTimes(2);
    const pendingOverrideButton = [...container.querySelectorAll("button")].find((node) =>
      node.textContent === "覆盖重探" || node.textContent === "启动中…",
    ) as HTMLButtonElement;
    expect(pendingOverrideButton.disabled).toBe(true);

    await act(async () => {
      resolveOverride?.({ status: "started", run_id: 12 });
    });
  });

  it("prevents duplicate retry starts while the rerun request is pending after a failed run", async () => {
    let callCount = 0;
    let resolveRetry: ((value: { status: "started"; run_id: number }) => void) | null = null;
    startDiscoveryRun.mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({ status: "started", run_id: 21 });
      }
      return new Promise((resolve) => {
        resolveRetry = resolve as typeof resolveRetry;
      });
    });
    getDiscoveryRun.mockResolvedValue({
      id: 21,
      site_url: "https://example.com",
      status: "failed",
      resulting_method_id: null,
      llm_token_usage: 0,
      node_trace: [],
      retry_count: 0,
      current_step: "writer",
      started_at: null,
      ended_at: null,
      error_message: "boom",
    });

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    await flush();

    const startButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "开始探查") as HTMLButtonElement;
    act(() => {
      startButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    await flush();
    await flush();

    const retryButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "重新探查") as HTMLButtonElement;
    expect(retryButton.disabled).toBe(false);

    act(() => {
      retryButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      retryButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(startDiscoveryRun).toHaveBeenCalledTimes(2);
    const pendingRetryButton = [...container.querySelectorAll("button")].find((node) =>
      node.textContent === "重新探查" || node.textContent === "启动中…",
    ) as HTMLButtonElement;
    expect(pendingRetryButton.disabled).toBe(true);

    await act(async () => {
      resolveRetry?.({ status: "started", run_id: 22 });
    });
  });

  it("uses equal-width columns for the discovery workspace layout", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    startDiscoveryRun.mockResolvedValueOnce({ status: "started", run_id: 31 });
    getDiscoveryRun.mockResolvedValue({
      id: 31,
      site_url: "https://example.com",
      status: "running",
      resulting_method_id: null,
      llm_token_usage: 0,
      node_trace: [],
      retry_count: 0,
      current_step: "explorer",
      started_at: null,
      ended_at: null,
      error_message: null,
    });

    const startButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "开始探查") as HTMLButtonElement;
    act(() => {
      startButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    await flush();

    const grid = container.querySelector("[data-testid='discovery-layout-grid']");
    expect(grid?.getAttribute("style")).toContain("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)");
  });

  it("shows a cancel button while running and sends cancel request", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    startDiscoveryRun.mockResolvedValueOnce({ status: "started", run_id: 41 });
    getDiscoveryRun.mockResolvedValue({
      id: 41,
      site_url: "https://example.com",
      status: "running",
      resulting_method_id: null,
      llm_token_usage: 0,
      node_trace: [],
      retry_count: 0,
      current_step: "explorer",
      started_at: null,
      ended_at: null,
      error_message: null,
    });
    cancelDiscoveryRun.mockResolvedValue({
      id: 41,
      site_url: "https://example.com",
      status: "cancelled",
      resulting_method_id: null,
      llm_token_usage: 0,
      node_trace: [],
      retry_count: 0,
      current_step: "explorer",
      started_at: null,
      ended_at: null,
      error_message: "已手动取消",
    });

    const startButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "开始探查") as HTMLButtonElement;
    act(() => {
      startButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    await flush();

    const cancelButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "取消探查") as HTMLButtonElement;
    expect(cancelButton).toBeTruthy();

    act(() => {
      cancelButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(cancelDiscoveryRun).toHaveBeenCalledWith(41);
  });

  it("shows a visible error when auto naming fails", async () => {
    suggestDiscoveryName.mockRejectedValue(new Error("自动命名超时"));

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='站点 URL，如 openanolis.cn/blog']")!, "https://example.com");
    await flush();

    const autoButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "✨ 自动") as HTMLButtonElement;
    act(() => {
      autoButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(container.textContent).toContain("自动命名超时");
  });
});
