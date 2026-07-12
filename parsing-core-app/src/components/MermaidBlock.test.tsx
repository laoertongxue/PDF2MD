import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const initialize = vi.fn();
const parseMermaid = vi.fn().mockResolvedValue(true);
const renderMermaid = vi.fn().mockResolvedValue({
  svg: `<svg viewBox="0 0 200 100" onclick="alert(1)">
    <style>@import url(https://evil.example/x.css)</style>
    <script>alert(1)</script>
    <foreignObject><div>bad</div></foreignObject>
    <use href="https://evil.example/sprite.svg#x" />
    <image href="data:image/svg+xml,&lt;svg onload=alert(1)&gt;" />
    <a href="java&#x73;cript:alert(1)"><text>bad link</text></a>
    <g style="background:url(https://evil.example/x)" data-danger="1">
      <text x="10" y="20">核心概念</text>
    </g>
  </svg>`,
});
vi.mock("mermaid", () => ({ default: { initialize, parse: parseMermaid, render: renderMermaid } }));

import MermaidBlock from "./MermaidBlock";

describe("MermaidBlock", () => {
  afterEach(cleanup);

  it("initializes Mermaid once in strict mode and keeps Chinese labels as SVG text", async () => {
    const view = render(<MermaidBlock code={'flowchart LR\nA["<img src=x onerror=alert(1)>"]-->B'} />);
    await screen.findByText("核心概念");
    view.rerender(<MermaidBlock code="flowchart LR\nA-->C" />);
    await waitFor(() => expect(renderMermaid).toHaveBeenCalledTimes(2));
    expect(initialize).toHaveBeenCalledTimes(1);
    expect(initialize).toHaveBeenCalledWith({
      securityLevel: "strict",
      startOnLoad: false,
      htmlLabels: false,
      flowchart: { htmlLabels: false, useMaxWidth: true },
    });
    const html = view.container.innerHTML;
    expect(html).toContain("核心概念");
    expect(view.container.querySelector("text")).not.toBeNull();
    expect(html).not.toMatch(/script|onclick|javascript:|foreignobject|<style|<use|<image|data-danger|evil\.example/i);
  });

  it("removes external, dangerous, encoded, and protocol-relative SVG URLs", async () => {
    renderMermaid.mockResolvedValueOnce({
      svg: `<svg xmlns="http://www.w3.org/2000/svg">
        <defs><marker id="arrow"><path d="M0 0L10 5L0 10Z" /></marker></defs>
        <path id="safe" d="M0 0L20 20" marker-end="url(#arrow)" />
        <a href=" https://evil.example/a"><text>https</text></a>
        <a href="//evil.example/a"><text>relative</text></a>
        <a href="jAvAsCrIp%3Aalert(1)"><text>encoded</text></a>
        <a href="java&#x0A;script:alert(1)"><text>controlled</text></a>
        <a href="data:text/html,boom"><text>data</text></a>
      </svg>`,
    });

    const view = render(<MermaidBlock code="flowchart LR\nA[安全]-->B[呈现]" />);
    await screen.findByText("https");

    const html = view.container.innerHTML;
    expect(html).not.toMatch(/evil\.example|javascript|%3a|data:text|href=/i);
    expect(view.container.querySelector("path#safe")?.getAttribute("marker-end")).toBe("url(#arrow)");
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
