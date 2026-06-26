// @vitest-environment jsdom

import { act } from "react";
import type { ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SourceManager } from "./SourceManager";
import type { CrawlSource } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function makeApi(overrides: Partial<ComponentProps<typeof SourceManager>["api"]> = {}) {
  return {
    fetchSources: vi.fn<() => Promise<CrawlSource[]>>().mockResolvedValue([]),
    createSource: vi.fn().mockResolvedValue({
      id: 2,
      name: "Detected Feed",
      url: "https://example.com/feed.xml",
      type: "rss",
      main_category: "软件包适配",
      enabled: true,
    }),
    deleteSource: vi.fn<(sourceId: number) => Promise<void>>().mockResolvedValue(undefined),
    discoverSource: vi.fn().mockResolvedValue({
      root_url: "https://example.com/blog",
      success: false,
      api_url: null,
      method: "GET",
      items_path: null,
      fields: {},
      name_suggestion: "Example Blog",
      sample_items: [],
      real_content_count: 0,
      candidates: [],
      notes: ["未捕获到任何 JSON XHR/Fetch 响应"],
    }),
    createSourceFromProbe: vi.fn().mockResolvedValue({
      id: 1,
      name: "Example Blog",
      url: "https://api.example.com/blog/list",
      type: "api",
      main_category: "技术博客",
      enabled: true,
    }),
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

  function findButton(text: string): Element | undefined {
    return [...container.querySelectorAll("button")].find((b) => b.textContent === text);
  }

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

  it("does not expose automatic source detection", async () => {
    const api = makeApi();
    await act(async () => {
      root.render(<SourceManager api={api} />);
    });
    await flush();

    // Expand the section, then open the add form
    click(container.querySelector("button")!);
    await flush();
    click(findButton("添加来源")!);
    await flush();

    expect(container.textContent).not.toContain("自动识别");
    expect(container.textContent).not.toContain("识别链接形态");
    expect(container.textContent).toContain("智能探测");
    expect(container.textContent).toContain("高级添加");
  });

  it("creates a news list page source without asking users for selectors", async () => {
    const api = makeApi();
    await act(async () => {
      root.render(<SourceManager api={api} />);
    });
    await flush();

    click(container.querySelector("button")!);
    await flush();
    click(findButton("添加来源")!);
    await flush();
    click(findButton("高级添加")!);
    await flush();

    input(container.querySelector("input[placeholder='https://example.com/feed.xml']")!, "https://rockylinux.org/news");
    const typeSelect = container.querySelectorAll("select")[1] as HTMLSelectElement;
    act(() => {
      typeSelect.value = "page_monitor";
      typeSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();

    expect(container.textContent).toContain("新闻列表页");
    expect(container.textContent).toContain("系统会自动识别文章链接、标题和发布日期");
    expect(container.textContent).not.toContain("link_selector");
    expect(container.textContent).not.toContain("date_selector");

    click(findButton("确认添加")!);
    await flush();

    expect(api.createSource).toHaveBeenCalledWith(
      expect.objectContaining({
        url: "https://rockylinux.org/news",
        type: "page_monitor",
      }),
    );
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

    // Expand the section to reveal the source list and delete button
    click(container.querySelector("button")!);
    await flush();

    click(findButton("删除")!);
    await flush();

    expect(api.deleteSource).toHaveBeenCalledWith(7);
  });

  it("creates an api source from a successful discovery", async () => {
    const api = makeApi({
      discoverSource: vi.fn().mockResolvedValue({
        root_url: "https://example.com/blog",
        success: true,
        api_url: "https://api.example.com/blog/list",
        method: "GET",
        items_path: "data.records",
        fields: { title: "title", url: "url", published_at: "published_at" },
        name_suggestion: "Example Blog",
        sample_items: [
          {
            title: "Hello World",
            url: "https://example.com/blog/1",
            published_at: "2026-06-20T10:00:00Z",
            content_preview: "Lorem ipsum",
          },
        ],
        real_content_count: 1,
        candidates: [],
        notes: [],
      }),
      createSourceFromProbe: vi.fn().mockResolvedValue({
        id: 10,
        name: "Example Blog",
        url: "https://api.example.com/blog/list",
        type: "api",
        main_category: "OS跟踪来源",
        enabled: true,
      }),
    });
    const onSourcesChanged = vi.fn();
    await act(async () => {
      root.render(<SourceManager api={api} onSourcesChanged={onSourcesChanged} />);
    });
    await flush();

    click(container.querySelector("button")!);
    await flush();
    click(findButton("添加来源")!);
    await flush();
    click(findButton("智能探测")!);
    await flush();
    input(container.querySelector("input[placeholder='https://example.com/feed.xml']")!, "https://example.com/blog");
    click(findButton("开始智能探测")!);
    await flush();

    expect(container.textContent).toContain("发现 API 端点");
    expect(container.textContent).toContain("https://api.example.com/blog/list");

    click(findButton("创建为标准 API 来源")!);
    await flush();

    expect(api.createSourceFromProbe).toHaveBeenCalledWith(
      expect.objectContaining({
        api_url: "https://api.example.com/blog/list",
        method: "GET",
        items_path: "data.records",
        main_category: "OS跟踪来源",
      }),
    );
    expect(onSourcesChanged).toHaveBeenCalledOnce();
  });
});
