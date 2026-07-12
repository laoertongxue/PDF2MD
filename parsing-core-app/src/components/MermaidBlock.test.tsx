import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const initialize = vi.fn();
const renderMermaid = vi.fn().mockResolvedValue({
  svg: '<svg onclick="alert(1)"><script>alert(1)</script><foreignObject>bad</foreignObject><a href="javascript:alert(1)">x</a><text>safe</text></svg>',
});
vi.mock("mermaid", () => ({ default: { initialize, render: renderMermaid } }));

import MermaidBlock from "./MermaidBlock";

describe("MermaidBlock", () => {
  afterEach(cleanup);

  it("initializes Mermaid once in strict mode and strips active SVG content", async () => {
    const view = render(<MermaidBlock code={'flowchart LR\nA["<img src=x onerror=alert(1)>"]-->B'} />);
    await screen.findByText("safe");
    view.rerender(<MermaidBlock code="flowchart LR\nA-->C" />);
    await waitFor(() => expect(renderMermaid).toHaveBeenCalledTimes(2));
    expect(initialize).toHaveBeenCalledTimes(1);
    expect(initialize).toHaveBeenCalledWith({ securityLevel: "strict", startOnLoad: false });
    const html = view.container.innerHTML;
    expect(html).not.toMatch(/script|foreignObject|onclick|javascript:/i);
  });

  it("serializes concurrent Mermaid renders", async () => {
    let active = 0;
    let maxActive = 0;
    renderMermaid.mockImplementation(async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 10));
      active -= 1;
      return { svg: "<svg><text>rendered</text></svg>" };
    });

    render(<><MermaidBlock code="graph TD\nA-->B" /><MermaidBlock code="graph TD\nC-->D" /></>);

    await screen.findAllByText("rendered");
    expect(maxActive).toBe(1);
  });

  it("removes Mermaid temporary error nodes after a failed render", async () => {
    renderMermaid.mockImplementationOnce(async (id: string) => {
      const temporary = document.createElement("div");
      temporary.id = `d${id}`;
      document.body.appendChild(temporary);
      throw new Error("invalid diagram");
    });

    render(<MermaidBlock code="invalid" />);

    await screen.findByText("invalid");
    expect(document.querySelector('[id^="dmm-"]')).toBeNull();
  });
});
