import { useRef } from "react";
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
    const title = hMatch?.[1] || "(无标题)";
    const aiIdx = p.indexOf("\n### ▸ AI 解读");
    const raw = aiIdx > -1 ? getRaw(p, aiIdx) : p;
    const aiPart = aiIdx > -1 ? p.slice(aiIdx) : "";
    const mermaidMatch = aiPart.match(/```mermaid\n([\s\S]*?)```/);
    const aiText = aiPart.replace(/```mermaid[\s\S]*?```/, "").trim();
    return { title, raw: raw.trim(), ai: aiText || undefined, mermaid: mermaidMatch?.[1] };
  });
}

function getRaw(p: string, aiIdx: number) {
  let section = p.slice(0, aiIdx);
  section = section.replace(/^## 第 \d+ 节.*$/m, "");
  return section.replace(/^>\s*(任务|源文件|生成时间).*$/gm, "").trim();
}

export default function VirtualDoc({ md }: { md: string }) {
  const sections = parseSections(md);
  const parentRef = useRef<HTMLDivElement>(null);

  return (
    <div ref={parentRef} className="h-[calc(100vh-120px)] overflow-y-auto">
      {sections.map((sec, i) => (
        <div key={i} className="border-b py-4 px-1">
          <h3 className="text-lg font-medium mb-2">{sec.title}</h3>
          <div className="prose prose-sm max-w-none mb-3">
            <ReactMarkdown>{sec.raw}</ReactMarkdown>
          </div>
          {sec.ai && (
            <div className="bg-blue-50 rounded-lg p-3 mb-2">
              <div className="prose prose-sm max-w-none">
                <ReactMarkdown>{sec.ai}</ReactMarkdown>
              </div>
            </div>
          )}
          {sec.mermaid && <MermaidBlock code={sec.mermaid} />}
        </div>
      ))}
    </div>
  );
}
