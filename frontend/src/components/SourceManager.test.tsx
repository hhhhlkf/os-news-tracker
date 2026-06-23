// @vitest-environment jsdom

import { act } from "react";
import type { ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SourceManager } from "./SourceManager";
import type { CrawlSource, SourceDetectResponse } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function makeApi(overrides: Partial<ComponentProps<typeof SourceManager>["api"]> = {}) {
  return {
    fetchSources: vi.fn<() => Promise<CrawlSource[]>>().mockResolvedValue([]),
    detectSource: vi.fn<(url: string) => Promise<SourceDetectResponse>>().mockResolvedValue({
      detected_type: "rss",
      name_suggestion: "Detected Feed",
      api_config: null,
      notes: ["识别为 RSS/Atom 订阅源。"],
    }),
    createSource: vi.fn().mockResolvedValue({
      id: 2,
      name: "Detected Feed",
      url: "https://example.com/feed.xml",
      type: "rss",
      main_category: "软件包适配",
      enabled: true,
    }),
    deleteSource: vi.fn<(sourceId: number) => Promise<void>>().mockResolvedValue(undefined),
    ...overrides,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

function click(element: Element) {
  act(() => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

function input(element: HTMLInputElement, value: string) {
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("SourceManager", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    root.unmount();
    container.remove();
    vi.restoreAllMocks();
  });

  it("validates url before detection", async () => {
    const api = makeApi();
    await act(async () => {
      root.render(<SourceManager api={api} />);
    });
    await flush();

    click(container.querySelector("button")!);
    click([...container.querySelectorAll("button")].find((button) => button.textContent === "识别链接形态")!);

    expect(container.textContent).toContain("请先填写网址");
    expect(api.detectSource).not.toHaveBeenCalled();
  });

  it("shows detect result and creates a source", async () => {
    const api = makeApi({
      detectSource: vi.fn().mockResolvedValue({
        detected_type: "api",
        name_suggestion: "API News",
        api_config: { probe: { mode: "json_list" } },
        notes: ["识别为 JSON API。"],
      }),
    });
    const onSourcesChanged = vi.fn();
    await act(async () => {
      root.render(<SourceManager api={api} onSourcesChanged={onSourcesChanged} />);
    });
    await flush();

    click(container.querySelector("button")!);
    input(container.querySelector("input[placeholder='https://example.com/feed.xml']")!, "https://api.example.com/news");
    click([...container.querySelectorAll("button")].find((button) => button.textContent === "识别链接形态")!);
    await flush();

    expect(container.textContent).toContain("识别结果：API");
    expect(container.textContent).toContain("识别为 JSON API。");

    click([...container.querySelectorAll("button")].find((button) => button.textContent === "确认添加")!);
    await flush();

    expect(api.createSource).toHaveBeenCalledWith({
      url: "https://api.example.com/news",
      name: "API News",
      main_category: "OS跟踪来源",
    });
    expect(onSourcesChanged).toHaveBeenCalledOnce();
  });

  it("deletes a source after confirmation", async () => {
    const api = makeApi({
      fetchSources: vi.fn().mockResolvedValue([
        {
          id: 7,
          name: "Old Feed",
          url: "https://example.com/feed.xml",
          type: "rss",
          main_category: "软件包适配",
          enabled: true,
        },
      ]),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => {
      root.render(<SourceManager api={api} />);
    });
    await flush();

    click([...container.querySelectorAll("button")].find((button) => button.textContent === "删除")!);
    await flush();

    expect(api.deleteSource).toHaveBeenCalledWith(7);
  });
});
