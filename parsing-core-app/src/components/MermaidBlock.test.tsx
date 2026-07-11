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
});
