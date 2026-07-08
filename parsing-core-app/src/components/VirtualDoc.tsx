import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "./MermaidBlock";

interface Section {
  title: string;
  raw: string;
  ai?: string;
  mermaid?: string;
}

function parseSections(md: string): Section[] {
  const parts = md.split(/\n---\n/).filter(Boolean);
  return parts.map((p) => {
    const hMatch = p.match(/^## 第 \d+ 节：(.+)$/m);
    const title = hMatch?.[1] || "无标题";
    const aiIdx = p.indexOf("\n### ▸ AI 解读");
    const raw = aiIdx > -1 ? p.slice(0, aiIdx).replace(/^## 第 \d+ 节.*$/m, "").replace(/^>\s*(任务|源文件|生成时间).*$/gm, "").trim() : p;
    const aiPart = aiIdx > -1 ? p.slice(aiIdx) : "";
    const mermaidMatch = aiPart.match(/```mermaid\n([\s\S]*?)```/);
    const aiText = aiPart.replace(/```mermaid[\s\S]*?```/, "").trim();
    return { title, raw: raw || p.trim(), ai: aiText || undefined, mermaid: mermaidMatch?.[1] };
  });
}

export default function VirtualDoc({ md }: { md: string }) {
  const sections = parseSections(md);
  const containerRef = useRef<HTMLDivElement>(null);
  const [activeSection, setActiveSection] = useState(0);

  return (
    <div className="flex gap-6 h-[calc(100vh-140px)]">
      {/* Table of Contents */}
      <div className="w-56 shrink-0 hidden lg:block">
        <div className="sticky top-20 space-y-0.5">
          <p className="text-xs font-medium text-muted uppercase tracking-wider mb-3">目录</p>
          {sections.map((sec, i) => (
            <button
              key={i}
              onClick={() => {
                setActiveSection(i);
                document.getElementById(`section-${i}`)?.scrollIntoView({ behavior: "smooth" });
              }}
              className={`block w-full text-left text-sm px-2.5 py-1.5 rounded-md transition-colors truncate ${
                i === activeSection
                  ? "bg-gray-100 text-gray-900 font-medium"
                  : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
              }`}
            >
              {sec.title}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div ref={containerRef} className="flex-1 overflow-y-auto pr-2 space-y-6" id="doc-scroll">
        {sections.map((sec, i) => (
          <div key={i} id={`section-${i}`} className="scroll-mt-20">
            {/* Section title */}
            <div className="flex items-center gap-3 mb-4">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary/10 text-primary text-xs font-medium">
                {i + 1}
              </span>
              <h3 className="text-base font-semibold text-gray-900">{sec.title}</h3>
            </div>

            {/* Original content */}
            <div className="prose prose-sm prose-gray max-w-none mb-6">
              <ReactMarkdown>{sec.raw}</ReactMarkdown>
            </div>

            {/* AI Interpretation */}
            {sec.ai && (
              <div className="relative bg-gradient-to-r from-blue-50 to-blue-50/50 rounded-xl border border-blue-100 p-5 mb-6">
                <div className="absolute -top-2.5 left-4 inline-flex items-center gap-1.5 rounded-full bg-blue-600 text-white text-xs font-medium px-3 py-0.5">
                  AI 解读
                </div>
                <div className="prose prose-sm prose-gray max-w-none mt-1">
                  <ReactMarkdown>{sec.ai}</ReactMarkdown>
                </div>
              </div>
            )}

            {/* Mermaid */}
            {sec.mermaid && (
              <div className="bg-white rounded-xl border border-gray-200 p-4 mb-6">
                <MermaidBlock code={sec.mermaid} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
