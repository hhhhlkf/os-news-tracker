// @vitest-environment jsdom

import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CrawlMethodDetail } from "./CrawlMethodDetail";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function flush() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

describe("CrawlMethodDetail", () => {
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

  it("refreshes the current detail query after patch succeeds", async () => {
    let getCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";

      if (url.includes("/discovery/methods/3") && method === "PATCH") {
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (url.includes("/discovery/methods/3") && method === "GET") {
        getCount += 1;
        return new Response(
          JSON.stringify({
            id: 3,
            domain: "example.com",
            entry_url: "https://example.com",
            status: getCount >= 2 ? "disabled" : "active",
            signature: "sig",
            last_run_at: null,
            last_run_status: null,
            dsl_recipe: {},
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }

      throw new Error(`unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <CrawlMethodDetail methodId={3} onClose={() => {}} />
        </QueryClientProvider>,
      );
    });
    await flush();

    const patchButton = [...container.querySelectorAll("button")].find((node) => node.textContent === "禁用") as HTMLButtonElement | undefined;
    expect(patchButton).toBeTruthy();
    act(() => {
      patchButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await flush();
    await flush();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/discovery/methods/3"),
      expect.objectContaining({ method: "PATCH" }),
    );
    expect(getCount).toBeGreaterThanOrEqual(2);
    expect(container.textContent).toContain("启用");
  });
});
