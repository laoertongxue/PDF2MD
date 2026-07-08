import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "./MermaidBlock";

interface S { title: string; raw: string; ai?: string; mermaid?: string }

function parse(md: string): S[] {
  return md.split(/\n---\n/).filter(Boolean).map((p) => {
    const h = p.match(/^## 第 \d+ 节：(.+)$/m);
    const aiIdx = p.indexOf("\n### ▸ AI 解读");
    const raw = (aiIdx > -1 ? p.slice(0, aiIdx) : p)
      .replace(/^## 第 \d+ 节.*$/m, "").replace(/^>\s*(任务|源文件|生成时间).*$/gm, "").trim();
    const aiPart = aiIdx > -1 ? p.slice(aiIdx) : "";
    const mm = aiPart.match(/```mermaid\n([\s\S]*?)```/);
    const ai = aiPart.replace(/```mermaid[\s\S]*?```/, "").trim();
    return { title: h?.[1] || "无标题", raw: raw || p, ai: ai || undefined, mermaid: mm?.[1] };
  });
}

export default function VirtualDoc({ md }: { md: string }) {
  const secs = parse(md);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current; if (!el) return;
    const cb = () => {
      for (let i = secs.length - 1; i >= 0; i--) {
        const s = document.getElementById(`s-${i}`);
        if (s && s.offsetTop <= el.scrollTop + 100) { setActive(i); break; }
      }
    };
    el.addEventListener("scroll", cb, { passive: true });
    return () => el.removeEventListener("scroll", cb);
  }, [secs]);

  return (
    <div className="flex gap-8 h-[calc(100vh-110px)]">
      {/* TOC */}
      <nav className="hidden xl:block w-48 shrink-0">
        <div className="sticky top-6">
          <p className="text-[10px] font-semibold text-zinc-400 uppercase tracking-widest mb-3">目录</p>
          <div className="space-y-px max-h-[calc(100vh-160px)] overflow-y-auto">
            {secs.map((s, i) => (
              <button
                key={i}
                onClick={() => document.getElementById(`s-${i}`)?.scrollIntoView({ behavior: "smooth" })}
                className={`block w-full text-left text-[13px] px-2.5 py-2 rounded-md transition-colors truncate ${
                  i === active ? "bg-zinc-100 text-zinc-900 font-medium" : "text-zinc-500 hover:text-zinc-700 hover:bg-zinc-50"
                }`}
              >{s.title}</button>
            ))}
          </div>
        </div>
      </nav>

      {/* Content */}
      <div ref={ref} className="flex-1 overflow-y-auto space-y-16 pb-24">
        {secs.map((sec, i) => (
          <section key={i} id={`s-${i}`} className="scroll-mt-20">
            {/* Header */}
            <div className="flex items-center gap-2.5 mb-5 pb-3 border-b border-zinc-100">
              <span className="text-[11px] font-bold text-zinc-400 bg-zinc-50 px-2 py-0.5 rounded">§{i + 1}</span>
              <h3 className="text-sm font-semibold text-zinc-900">{sec.title}</h3>
            </div>

            {/* Original */}
            <div className="prose prose-sm prose-zinc max-w-none mb-8
              prose-headings:font-semibold prose-headings:text-zinc-900
              prose-p:text-zinc-700 prose-p:leading-relaxed prose-p:text-[14px]
              prose-table:text-[13px] prose-th:bg-zinc-50 prose-th:font-medium prose-th:text-zinc-600 prose-th:px-3 prose-th:py-2 prose-td:px-3 prose-td:py-2 prose-td:text-zinc-700 prose-table:border-zinc-200
              prose-code:bg-zinc-100 prose-code:rounded prose-code:px-1.5 prose-code:py-0.5 prose-code:text-[12px] prose-code:font-normal prose-code:before:content-none prose-code:after:content-none
              prose-pre:bg-zinc-950 prose-pre:text-zinc-100 prose-pre:rounded-lg prose-pre:border-0 prose-pre:text-[12px]
              prose-a:text-zinc-900 prose-a:underline prose-a:underline-offset-2
              prose-li:text-zinc-700 prose-li:text-[13px]">
              <ReactMarkdown>{sec.raw}</ReactMarkdown>
            </div>

            {/* AI */}
            {sec.ai && (
              <div className="relative rounded-lg border border-blue-100 bg-blue-50/30 p-5 mb-8">
                <span className="absolute -top-2.5 left-4 text-[11px] font-semibold bg-blue-600 text-white px-2.5 py-0.5 rounded-full">AI 解读</span>
                <div className="prose prose-sm prose-zinc max-w-none mt-2
                  prose-p:text-zinc-700 prose-p:leading-relaxed prose-p:text-[13px]
                  prose-strong:text-zinc-900 prose-li:text-zinc-700 prose-li:text-[13px]">
                  <ReactMarkdown>{sec.ai}</ReactMarkdown>
                </div>
              </div>
            )}

            {/* Mermaid */}
            {sec.mermaid && (
              <div className="rounded-lg border border-zinc-200 bg-white p-4 mb-8">
                <MermaidBlock code={sec.mermaid} />
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
