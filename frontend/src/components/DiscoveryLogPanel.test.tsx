// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import type { NewsRunLogEntry } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("DiscoveryLogPanel", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("supports filtering between discovery and crawl-method logs", async () => {
    const logs: NewsRunLogEntry[] = [
      { id: 1, ts: "2026-07-01T00:00:00Z", level: "info", stage: "探查", source: "site-a", message: "站点探查完成" },
      { id: 2, ts: "2026-07-01T00:00:01Z", level: "info", stage: "抓方式", source: "site-a", message: "爬取方式抓取完成", method_id: 9 },
    ];

    await act(async () => {
      root.render(<DiscoveryLogPanel logs={logs} />);
    });

    expect(container.textContent).toContain("站点探查完成");
    expect(container.textContent).toContain("爬取方式抓取完成");

    const crawlMethodButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("抓方式"),
    );
    expect(crawlMethodButton).toBeTruthy();

    await act(async () => {
      crawlMethodButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("爬取方式抓取完成");
    expect(container.textContent).not.toContain("站点探查完成");
  });
});
