// @vitest-environment jsdom

import { act, useState, type Dispatch, type SetStateAction } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { RunLimitCard } from "./RunLimitCard";
import type { NewsRunFormState } from "./NewsRunControl";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const initialState: NewsRunFormState = {
  timeMode: "relative",
  relativeRange: "7d",
  startDate: "",
  endDate: "",
  targetCount: "12",
};

function Wrapper(props: { onStateChange?: Dispatch<SetStateAction<NewsRunFormState>> }) {
  const [formState, setFormState] = useState(initialState);

  function handleChange(value: SetStateAction<NewsRunFormState>) {
    setFormState(value);
    props.onStateChange?.(value);
  }

  return <RunLimitCard formState={formState} onChange={handleChange} />;
}

describe("RunLimitCard", () => {
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

  it("switches between relative and absolute time modes", async () => {
    await act(async () => {
      root.render(<Wrapper />);
    });

    expect(container.textContent).toContain("24h");
    expect(container.textContent).toContain("7d");
    expect(container.textContent).not.toContain("开始日期");

    const absoluteButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("绝对范围"),
    );
    expect(absoluteButton).toBeTruthy();

    await act(async () => {
      absoluteButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("开始日期");
    expect(container.textContent).toContain("结束日期");
    expect(container.textContent).not.toContain("24h");

    const relativeButton = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("相对范围"),
    );
    expect(relativeButton).toBeTruthy();

    await act(async () => {
      relativeButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("24h");
    expect(container.textContent).toContain("7d");
    expect(container.textContent).not.toContain("开始日期");
  });
});
