import React from "react";
import { createRoot } from "react-dom/client";
import MermaidBlock, { sanitizeMermaidSvg } from "../components/MermaidBlock";
import "../index.css";

const maliciousSvg = `<svg xmlns="http://www.w3.org/2000/svg" onload="alert('svg-xss')">
  <style>@import url(https://evil.invalid/style.css)</style>
  <foreignObject><img src="https://evil.invalid/image.png" onerror="alert('image-xss')" /></foreignObject>
  <use href="https://evil.invalid/icons.svg#x" />
  <a href="java&#x73;cript:alert('link-xss')"><text>危险链接</text></a>
  <text x="10" y="24">安全中文标签</text>
</svg>`;

function AcceptanceFixture() {
  const sanitized = sanitizeMermaidSvg(maliciousSvg);
  return (
    <main className="mx-auto min-h-[1100px] max-w-5xl space-y-8 bg-white p-8 text-zinc-900">
      <header>
        <p className="text-xs font-semibold text-zinc-500">TASK 12 / MERMAID 11</p>
        <h1 className="mt-2 text-2xl font-semibold">SVG 安全与中文标签验收</h1>
      </header>
      <section aria-label="知识结构图" className="border border-zinc-200 p-5">
        <h2 className="mb-4 text-base font-semibold">知识结构图</h2>
        <MermaidBlock code={"flowchart TD\nA[核心概念] --> B[融合框架]\nB --> C[组织设计]"} />
      </section>
      <section aria-label="应用流程图" className="border border-zinc-200 p-5">
        <h2 className="mb-4 text-base font-semibold">应用流程图</h2>
        <MermaidBlock code={"flowchart LR\nA[识别问题] --> B[应用框架]\nB --> C[复盘改进]"} />
      </section>
      <section aria-label="对抗 SVG 净化结果" className="border border-zinc-200 p-5">
        <h2 className="mb-4 text-base font-semibold">对抗 SVG 净化结果</h2>
        <div data-testid="adversarial-svg" dangerouslySetInnerHTML={{ __html: sanitized }} />
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<AcceptanceFixture />);
