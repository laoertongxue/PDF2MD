import React from "react";
import { createRoot } from "react-dom/client";
import "../index.css";

const sourceChapters = [
  { source: "战略管理", id: "chapter-strategy-1", chapters: ["第一章 管理导论", "第二章 竞争战略"] },
  { source: "组织行为学", id: "chapter-org-1", chapters: ["第一章 管理导论", "第二章 组织动机"] },
];

function Shell({ scenario, title, children }: React.PropsWithChildren<{ scenario: string; title: string }>) {
  return <main data-scenario={scenario} className="mx-auto min-h-screen max-w-6xl bg-white px-8 py-7 text-zinc-900">
    <header className="mb-6 border-b border-zinc-200 pb-5">
      <p className="text-xs font-semibold text-zinc-500">MBA 课程精读工作台 / TASK 12</p>
      <h1 className="mt-2 text-2xl font-semibold">{title}</h1>
      <p className="mt-1 text-sm text-zinc-500">企业战略与组织协同</p>
    </header>
    {children}
  </main>;
}

const Badge = ({ children, tone = "zinc" }: React.PropsWithChildren<{ tone?: "zinc" | "blue" | "red" | "green" }>) => {
  const colors = { zinc: "bg-zinc-100 text-zinc-700", blue: "bg-blue-50 text-blue-700", red: "bg-red-50 text-red-700", green: "bg-emerald-50 text-emerald-700" };
  return <span className={`px-2 py-1 text-xs font-medium ${colors[tone]}`}>{children}</span>;
};

function ImportQueue() {
  return <Shell scenario="import-queue" title="导入教材">
    <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(260px,0.8fr)] gap-6 max-[800px]:grid-cols-1">
      <section className="border border-zinc-200 p-5"><div className="flex items-center justify-between"><h2 className="font-semibold">本次导入队列</h2><Badge tone="blue">2 个文件</Badge></div>
        <ul className="mt-4 divide-y divide-zinc-200 border-y border-zinc-200">
          {[{ name: "战略管理（第 5 版）.pdf", size: "18.4 MB", status: "章节识别中" }, { name: "组织行为学（第 3 版）.docx", size: "6.8 MB", status: "等待导入" }].map((file) => <li data-testid="import-row" key={file.name} className="flex items-center gap-4 py-4"><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{file.name}</p><p className="mt-1 text-xs text-zinc-500">{file.size} · 独立导入与章节识别</p></div><Badge tone={file.status === "等待导入" ? "zinc" : "blue"}>{file.status}</Badge></li>)}
        </ul><button className="mt-5 bg-zinc-900 px-4 py-2 text-sm text-white">导入全部</button>
      </section>
      <aside className="border border-zinc-200 p-5"><h2 className="font-semibold">已导入教材</h2><div className="mt-4 space-y-3">{["市场营销原理", "财务管理基础"].map((name) => <div data-testid="existing-source" key={name} className="border-l-2 border-emerald-500 bg-zinc-50 p-3"><p className="text-sm font-medium">{name}</p><p className="mt-1 text-xs text-zinc-500">章节已确认</p></div>)}</div></aside>
    </div>
  </Shell>;
}

function TopicMap() {
  return <Shell scenario="topic-map" title="课程主题与章节映射">
    <div className="grid grid-cols-[300px_minmax(0,1fr)] gap-6 max-[800px]:grid-cols-1">
      <aside className="border border-zinc-200 p-4"><h2 className="text-sm font-semibold">课程主题</h2>{["战略选择", "组织协同"].map((topic, index) => <div key={topic} className={`mt-3 border p-3 ${index === 0 ? "border-zinc-900 bg-zinc-50" : "border-zinc-200"}`}><p className="font-medium">{topic}</p><p className="mt-1 text-xs text-zinc-500">{index === 0 ? "3 个章节" : "1 个章节"}</p></div>)}</aside>
      <section className="border border-zinc-200 p-5"><div className="flex items-start justify-between gap-4"><div><h2 className="font-semibold">战略选择</h2><p className="mt-1 text-sm text-zinc-500">按教材分组，保留同名章节的来源边界</p></div><button className="bg-zinc-900 px-4 py-2 text-sm text-white">保存章节映射</button></div>
        <div className="mt-5 grid grid-cols-2 gap-4 max-[700px]:grid-cols-1">{sourceChapters.map((group, groupIndex) => <fieldset data-testid="source-chapter-group" key={group.source} className="border border-zinc-200 p-4"><legend className="px-2 text-sm font-semibold">{group.source}</legend>{group.chapters.map((chapter, chapterIndex) => <label key={chapter} className="mt-3 flex items-center gap-3 text-sm"><input type="checkbox" defaultChecked={groupIndex === 0 || chapterIndex === 0} /><span>{chapter}</span></label>)}</fieldset>)}</div>
      </section>
    </div>
  </Shell>;
}

function FusionSources() {
  return <Shell scenario="fusion-sources" title="主题融合精读">
    <article className="border border-zinc-200 p-6"><div className="flex items-center justify-between"><div><Badge tone="green">已完成</Badge><h2 className="mt-3 text-xl font-semibold">战略选择：从竞争定位到组织执行</h2></div><button className="border border-zinc-300 px-3 py-2 text-sm">编辑主题</button></div>
      <section className="mt-6 border-t border-zinc-200 pt-5"><h3 className="font-semibold">关联教材与章节</h3><div className="mt-3 grid grid-cols-2 gap-3 max-[700px]:grid-cols-1">{sourceChapters.map((group) => <a data-testid="source-link" key={group.id} href={`/workbench/chapter?chapterId=${group.id}`} className="border border-zinc-200 p-4 text-sm text-blue-700 underline underline-offset-4">《{group.source}》·{group.chapters[0]}</a>)}</div></section>
      <section className="mt-6 border-t border-zinc-200 pt-5"><h3 className="font-semibold">核心观点</h3><p className="mt-3 max-w-3xl text-sm leading-7 text-zinc-700">战略的价值不仅在于选择竞争位置，还在于让组织结构、激励机制与执行节奏形成一致的行动系统。</p></section>
    </article>
  </Shell>;
}

function CardFilter() {
  const cards = ["竞争优势不是单点能力", "组织结构必须服务战略", "复盘让战略持续校准"];
  return <Shell scenario="card-filter" title="写作卡片库">
    <div className="flex flex-wrap items-center gap-2 border-b border-zinc-200 pb-4"><span className="mr-2 text-sm text-zinc-500">来源筛选</span><button aria-pressed="false" className="border border-zinc-200 px-3 py-2 text-sm">全部</button><button aria-pressed="false" className="border border-zinc-200 px-3 py-2 text-sm">教材章节</button><button aria-pressed="true" className="bg-zinc-900 px-3 py-2 text-sm text-white">融合精读</button></div>
    <div className="mt-5 grid grid-cols-3 gap-4 max-[800px]:grid-cols-2">{cards.map((title, index) => <article data-testid="writing-card" data-origin="topic" key={title} className="border border-zinc-200 p-4"><div className="flex items-center justify-between"><Badge tone="blue">融合精读</Badge><span className="text-xs text-zinc-400">0{index + 1}</span></div><h2 className="mt-4 font-semibold">{title}</h2><p className="mt-2 text-sm leading-6 text-zinc-600">来自“战略选择”主题，可直接用于课程复盘与文章提纲。</p></article>)}</div>
  </Shell>;
}

function ErrorStop() {
  return <Shell scenario="error-stop" title="主题融合运行记录"><div className="grid grid-cols-[minmax(0,1fr)_280px] gap-6 max-[800px]:grid-cols-1"><section className="border border-red-200 bg-red-50 p-5"><div className="flex items-center justify-between"><Badge tone="red">运行失败</Badge><span data-testid="round-status" data-status="FAILED" className="text-xs text-red-700">ROUND 3 / FAILED</span></div><div role="alert" className="mt-4 text-sm font-medium text-red-800">主题融合失败：模型服务暂时不可用</div><p className="mt-2 text-sm text-red-700">生成流程已停止，未继续写入后续章节和卡片。</p><button className="mt-5 border border-red-300 bg-white px-4 py-2 text-sm text-red-800">检查并恢复</button></section><aside className="border border-zinc-200 p-5"><h2 className="font-semibold">运行状态</h2><dl className="mt-4 space-y-3 text-sm"><div><dt className="text-zinc-500">主题</dt><dd className="mt-1">战略选择</dd></div><div><dt className="text-zinc-500">停止位置</dt><dd className="mt-1">观点对照</dd></div><div><dt className="text-zinc-500">已有结果</dt><dd className="mt-1">保留</dd></div></dl></aside></div></Shell>;
}

function RecoveryComplete() {
  return <Shell scenario="recovery-complete" title="主题融合运行记录"><section className="border border-zinc-200 p-6"><div className="flex items-start justify-between gap-4"><div><Badge tone="green">恢复完成</Badge><h2 className="mt-3 text-xl font-semibold">战略选择</h2><p className="mt-2 text-sm text-zinc-600">已结束过期任务，可重新生成</p></div><span data-testid="topic-status" data-status="FAILED"><Badge tone="red">失败</Badge></span></div><div className="mt-6 border-t border-zinc-200 pt-5"><p className="text-sm text-zinc-600">旧任务租约已清理，已生成内容保持不变。重新生成将从当前主题重新开始。</p><button className="mt-5 bg-zinc-900 px-4 py-2 text-sm text-white">重新生成</button></div></section></Shell>;
}

const scenario = new URLSearchParams(location.search).get("scenario") || "import-queue";
const fixtures: Record<string, React.ReactNode> = { "import-queue": <ImportQueue />, "topic-map": <TopicMap />, "fusion-sources": <FusionSources />, "card-filter": <CardFilter />, "error-stop": <ErrorStop />, "recovery-complete": <RecoveryComplete /> };
createRoot(document.getElementById("root")!).render(fixtures[scenario] ?? <Shell scenario="unknown" title="未知验收场景"><p>Unsupported scenario</p></Shell>);
