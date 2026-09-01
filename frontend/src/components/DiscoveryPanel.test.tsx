// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DiscoveryPanel } from "./DiscoveryPanel";

const enqueueDiscoveryQueue = vi.fn();
const suggestDiscoveryName = vi.fn();
const DISCOVERY_PANEL_STORAGE_KEY = "os-news-tracker.discovery-panel";

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {},
  enqueueDiscoveryQueue: (...args: unknown[]) => enqueueDiscoveryQueue(...args),
  suggestDiscoveryName: (...args: unknown[]) => suggestDiscoveryName(...args),
  listDiscoveryQueue: () => Promise.resolve({
    pending: [],
    failed: [],
    completed: [],
    running_count: 0,
    max_parallel: 2,
  }),
  getBatchDiscoveryRun: () => Promise.reject(new Error("no discovery run in test")),
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
    enqueueDiscoveryQueue.mockReset();
    suggestDiscoveryName.mockReset();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    queryClient.clear();
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("disables the enqueue button as soon as the queue request is pending", async () => {
    let resolveEnqueue: ((value: { status: "queued" }) => void) | null = null;
    enqueueDiscoveryQueue.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveEnqueue = resolve as typeof resolveEnqueue;
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

    changeInput(container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']")!, "https://example.com");
    await flush();

    const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "加入队列") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    act(() => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(enqueueDiscoveryQueue).toHaveBeenCalledOnce();
    const enqueueButtonAfterClick = [...container.querySelectorAll("button")].find((node) =>
      node.textContent === "加入队列" || node.textContent === "加入中…",
    ) as HTMLButtonElement;
    expect(enqueueButtonAfterClick.disabled).toBe(true);

    await act(async () => {
      resolveEnqueue?.({ status: "queued" });
    });
  });

  it("prevents duplicate override enqueue while the force request is pending", async () => {
    let callCount = 0;
    let resolveOverride: ((value: { status: "queued" }) => void) | null = null;
    enqueueDiscoveryQueue.mockImplementation(() => {
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

    changeInput(container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']")!, "https://example.com");
    await flush();

    const enqueueButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "加入队列") as HTMLButtonElement;
    act(() => {
      enqueueButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    const overrideButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "覆盖重探") as HTMLButtonElement;
    expect(overrideButton.disabled).toBe(false);

    act(() => {
      overrideButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      overrideButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(enqueueDiscoveryQueue).toHaveBeenCalledTimes(2);
    const pendingOverrideButton = [...container.querySelectorAll("button")].find((node) =>
      node.textContent === "覆盖重探" || node.textContent === "加入中…",
    ) as HTMLButtonElement;
    expect(pendingOverrideButton.disabled).toBe(true);

    await act(async () => {
      resolveOverride?.({ status: "queued" });
    });
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

    changeInput(container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']")!, "https://example.com");
    await flush();

    const autoButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "✨ 自动") as HTMLButtonElement;
    act(() => {
      autoButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    expect(container.textContent).toContain("自动命名超时");
  });

  it("stays expanded and does not offer a collapse control", async () => {
    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    expect(container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']")).toBeTruthy();
    expect([...container.querySelectorAll("button")].some((node) => node.textContent === "展开")).toBe(false);
  });

  it("restores persisted discovery form state", async () => {
    window.sessionStorage.setItem(DISCOVERY_PANEL_STORAGE_KEY, JSON.stringify({
      rawInput: "https://persisted.example.com",
      name: "Persisted Run",
      expanded: true,
    }));

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    expect((container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']") as HTMLInputElement).value)
      .toBe("https://persisted.example.com");
    expect((container.querySelector("input[placeholder='名称（选填）']") as HTMLInputElement).value)
      .toBe("Persisted Run");
  });

  it("persists latest discovery panel form state after user interactions", async () => {
    enqueueDiscoveryQueue.mockResolvedValueOnce({ status: "queued" });

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DiscoveryPanel />
        </QueryClientProvider>,
      );
    });
    await flush();

    changeInput(container.querySelector("input[placeholder='输入网页 URL 或搜索关键词']")!, "https://example.com");
    changeInput(container.querySelector("input[placeholder='名称（选填）']")!, "Example Run");
    await flush();

    const enqueueButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "加入队列") as HTMLButtonElement;
    act(() => {
      enqueueButton.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();

    const persisted = JSON.parse(window.sessionStorage.getItem(DISCOVERY_PANEL_STORAGE_KEY) ?? "{}");
    expect(persisted).toMatchObject({
      rawInput: "https://example.com",
      name: "Example Run",
      expanded: true,
    });
  });
});
