import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "./MermaidBlock";

interface Section {
  title: string;
  raw: string;
  ai?: string;
  mermaid?: string;
}

function parse(md: string): Section[] {
  return md.split(/\n---\n/).filter(Boolean).map((p) => {
    const h = p.match(/^## 第 \d+ 节：(.+)$/m);
    const title = h?.[1] || "无标题";
    const aiIdx = p.indexOf("\n### ▸ AI 解读");
    const raw = aiIdx > -1
      ? p.slice(0, aiIdx).replace(/^## 第 \d+ 节.*$/m, "").replace(/^>\s*(任务|源文件|生成时间).*$/gm, "").trim()
      : p.replace(/^>\s*(任务|源文件|生成时间).*$/gm, "").trim();
    const aiPart = aiIdx > -1 ? p.slice(aiIdx) : "";
    const mm = aiPart.match(/```mermaid\n([\s\S]*?)```/);
    const ai = aiPart.replace(/```mermaid[\s\S]*?```/, "").trim();
    return { title, raw: raw || p, ai: ai || undefined, mermaid: mm?.[1] };
  });
}

export default function VirtualDoc({ md }: { md: string }) {
  const sections = parse(md);
  const [active, setActive] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onScroll = () => {
      const top = el.scrollTop + 80;
      for (let i = sections.length - 1; i >= 0; i--) {
        const sec = document.getElementById(`sec-${i}`);
        if (sec && sec.offsetTop <= top) { setActive(i); break; }
      }
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [sections]);

  return (
    <div className="flex gap-6 h-[calc(100vh-130px)]">
      {/* TOC sidebar */}
      <nav className="hidden xl:block w-52 shrink-0">
        <div className="sticky top-20">
          <p className="text-[10px] font-bold text-muted uppercase tracking-widest mb-3 ml-2.5">目录</p>
          <div className="space-y-0.5 max-h-[calc(100vh-200px)] overflow-y-auto pr-1">
            {sections.map((s, i) => (
              <button
                key={i}
                onClick={() => document.getElementById(`sec-${i}`)?.scrollIntoView({ behavior: "smooth" })}
                className={`block w-full text-left text-[12px] px-2.5 py-2 rounded-lg transition-all duration-150 truncate ${
                  i === active
                    ? "bg-accent/10 text-accent font-semibold"
                    : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
                }`}
              >
                <span className="text-[10px] mr-1.5 opacity-50">{i + 1}</span>
                {s.title}
              </button>
            ))}
          </div>
        </div>
      </nav>

      {/* Main content */}
      <div ref={containerRef} className="flex-1 overflow-y-auto pr-1 space-y-12 pb-20" id="doc-scroll">
        {sections.map((sec, i) => (
          <section key={i} id={`sec-${i}`} className="scroll-mt-24">
            {/* Section header */}
            <div className="flex items-center gap-3 mb-5 pb-3 border-b border-border/60">
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent text-white text-[11px] font-bold shadow-sm">
                {i + 1}
              </span>
              <h3 className="text-[15px] font-semibold text-gray-900">{sec.title}</h3>
            </div>

            {/* Raw content */}
            <div className="mb-8">
              <div className="prose prose-sm prose-slate max-w-none
                prose-headings:text-gray-900 prose-headings:font-semibold
                prose-p:text-gray-700 prose-p:leading-relaxed
                prose-table:border prose-table:border-gray-200
                prose-th:bg-gray-50 prose-th:px-3 prose-th:py-2 prose-th:text-[12px] prose-th:font-semibold prose-th:text-gray-600
                prose-td:px-3 prose-td:py-2 prose-td:text-[13px] prose-td:text-gray-700
                prose-code:bg-gray-100 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-[12px] prose-code:font-normal prose-code:before:content-none prose-code:after:content-none
                prose-pre:bg-gray-900 prose-pre:text-gray-100 prose-pre:rounded-xl prose-pre:border-0
                prose-a:text-accent prose-a:no-underline hover:prose-a:underline
                prose-li:text-gray-700 prose-li:text-[13px]">
                <ReactMarkdown>{sec.raw}</ReactMarkdown>
              </div>
            </div>

            {/* AI block */}
            {sec.ai && (
              <div className="relative rounded-2xl border border-accent/20 bg-gradient-to-br from-accent/[0.03] to-accent/[0.06] p-5 mb-8">
                <div className="absolute -top-3 left-5 inline-flex items-center gap-1.5 rounded-full bg-accent text-white text-[11px] font-bold px-3 py-1 shadow-sm shadow-accent/20">
                  ✦ AI 解读
                </div>
                <div className="prose prose-sm prose-slate max-w-none mt-2
                  prose-headings:text-gray-900 prose-headings:font-semibold
                  prose-p:text-gray-700 prose-p:leading-relaxed
                  prose-strong:text-gray-900
                  prose-li:text-gray-700 prose-li:text-[13px]">
                  <ReactMarkdown>{sec.ai}</ReactMarkdown>
                </div>
              </div>
            )}

            {/* Mermaid */}
            {sec.mermaid && (
              <div className="rounded-2xl border border-border bg-white p-4 mb-8">
                <MermaidBlock code={sec.mermaid} />
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
