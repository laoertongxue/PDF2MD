import { Link } from "react-router-dom";
import MermaidEditor from "./MermaidEditor";

const knowledgeGraph = `flowchart TD
  A[本章核心问题] --> B[关键概念]
  B --> C[分析模型]
  C --> D[管理启发]
  D --> E[写作素材]`;

const applicationFlow = `flowchart LR
  A[现实管理情境] --> B[识别问题]
  B --> C[套用模型]
  C --> D[提出判断]
  D --> E[形成行动建议]`;

export default function ChapterWorkbench() {
  return (
    <div className="space-y-6 animate-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">章节精读工作台</h1>
          <p className="mt-1 text-sm text-zinc-500">精读笔记必须包含可直接预览的 Mermaid 图，下面先放最小可编辑预览。</p>
        </div>
        <Link to="/workbench/cards" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:border-zinc-300">
          查看卡片池
        </Link>
      </div>

      <section className="rounded-lg border border-zinc-200 bg-white p-5">
        <MermaidEditor title="知识结构图" initial={knowledgeGraph} />
      </section>

      <section className="rounded-lg border border-zinc-200 bg-white p-5">
        <MermaidEditor title="应用流程图" initial={applicationFlow} />
      </section>
    </div>
  );
}
