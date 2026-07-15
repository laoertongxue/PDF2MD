import { cleanup, render, screen, waitFor } from "@testing-library/react";
import mermaid from "mermaid";
import { afterEach, describe, expect, it } from "vitest";
import MermaidBlock from "./MermaidBlock";

if (!("CSSStyleSheet" in globalThis)) {
  class TestCSSStyleSheet {
    cssRules: string[] = [];

    insertRule(rule: string, index: number) {
      this.cssRules.splice(index, 0, rule);
      return index;
    }
  }

  Object.defineProperty(globalThis, "CSSStyleSheet", {
    configurable: true,
    value: TestCSSStyleSheet,
  });
}

if (!("getComputedTextLength" in SVGElement.prototype)) {
  Object.defineProperty(SVGElement.prototype, "getComputedTextLength", {
    configurable: true,
    value() {
      return (this.textContent ?? "").length * 8;
    },
  });
}

if (!("getBBox" in SVGElement.prototype)) {
  Object.defineProperty(SVGElement.prototype, "getBBox", {
    configurable: true,
    value() {
      return { height: 20, width: (this.textContent ?? "").length * 8, x: 0, y: 0 };
    },
  });
}

const GENERATED_TOPIC_DIAGRAMS = [
  "graph TD\n  A[核心概念] --> B[融合框架]",
  "flowchart LR\n  A[识别问题] --> B[应用框架]",
  `flowchart LR
%% 合法注释
subgraph S[战略分析]
  A[识别问题] --> B(比较方案)
  B -->|形成选择| C{执行?}
end
classDef focus fill:#fff,stroke:#333
class A,B focus
style C fill:#eee
linkStyle 0 stroke:#333`,
];

describe("Mermaid 11 runtime contract", () => {
  afterEach(() => {
    cleanup();
    document.body.querySelectorAll('[id^="dmm-"]').forEach((node) => node.remove());
  });

  it.each(GENERATED_TOPIC_DIAGRAMS)("parses generated topic Mermaid with the real parser", async (code) => {
    await expect(mermaid.parse(code)).resolves.toBeTruthy();
  });

  it("renders a generated diagram without leaked Mermaid error output", async () => {
    const view = render(<MermaidBlock code={GENERATED_TOPIC_DIAGRAMS[0]} />);

    await waitFor(() => {
      const labels = Array.from(view.container.querySelectorAll("svg text")).map(
        (node) => node.textContent,
      );
      expect(labels).toEqual(expect.arrayContaining(["核心概念", "融合框架"]));
    });
    expect(view.container.textContent).not.toContain("Syntax error in text");
    expect(document.body.textContent).not.toContain("Syntax error in text");
    expect(document.body.querySelectorAll('[id^="dmm-"]')).toHaveLength(0);
    expect(view.container.firstElementChild).toHaveClass("min-w-0", "max-w-full");
  });

  it("contains an invalid diagram error inside the block without leaking or overflowing the page", async () => {
    const view = render(<MermaidBlock code={"graph TD\n  end\n  A --> B"} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(document.body.querySelectorAll('[id^="dmm-"]')).toHaveLength(0);
    expect(view.container.firstElementChild).toHaveClass("min-w-0", "max-w-full", "overflow-hidden");
  });
});
