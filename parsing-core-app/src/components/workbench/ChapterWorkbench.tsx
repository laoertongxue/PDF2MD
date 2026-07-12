import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Circle, Loader2, Sparkles, XCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import MermaidEditor from "./MermaidEditor";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import { chapterOptionLabel, createSourceChapterGroups } from "./SourceChapterTree";
import type { ChapterRun, NoteBlock } from "../../api/workbenchTypes";

const CONTENT_KINDS = ["summary", "concepts", "plain_explain", "application", "reflection"] as const;
const ROUND_LABELS: Record<string, string> = { structure: "章节结构", concepts: "核心概念", plain_explain: "通俗解释", application: "实际应用", mermaid: "图示生成", cards: "卡片提炼", review: "审核" };
const SOURCE_RE = /\[《([^\]\n]+)》·第\s*(\d+)\s*章\]/g;
const leaveMessage = "当前 Mermaid 有未保存修改，确定离开吗？";

export default function ChapterWorkbench() {
  const store = useWorkbenchStore();
  const [activeChapterId, setActiveChapterId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runningHybrid, setRunningHybrid] = useState(false);
  const [dirtyKinds, setDirtyKinds] = useState<Record<string, boolean>>({});
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");
  const dirty = Object.values(dirtyKinds).some(Boolean);
  const chapterGroups = useMemo(() => createSourceChapterGroups(store.selectedCourseId ? (store.sources[store.selectedCourseId] ?? []) : [], store.chapters), [store.chapters, store.selectedCourseId, store.sources]);
  const courseChapters = useMemo(() => chapterGroups.flatMap((group) => group.chapters), [chapterGroups]);
  const activeChapter = courseChapters.find((chapter) => chapter.id === activeChapterId) ?? null;
  const blocks = activeChapterId ? (store.noteBlocksByChapter[activeChapterId] ?? []) : [];
  const runs = activeChapterId ? (store.chapterRunsById[activeChapterId] ?? []) : [];

  useEffect(() => { store.loadCourses().catch((reason: unknown) => setError(message(reason, "课程加载失败"))); }, [store.loadCourses]);
  useEffect(() => { if (!store.selectedCourseId) return; store.loadSources(store.selectedCourseId).then((items) => Promise.all(items.map((source) => store.loadChapters(source.id)))).catch((reason: unknown) => setError(message(reason, "章节加载失败"))); }, [store.loadChapters, store.loadSources, store.selectedCourseId]);
  useEffect(() => {
    let cancelled = false;
    async function chooseInitial() {
      const next = (requestedChapterId && courseChapters.find((chapter) => chapter.id === requestedChapterId)) || courseChapters.find((chapter) => (store.noteBlocksByChapter[chapter.id] ?? []).length) || courseChapters.find((chapter) => ["CONFIRMED", "COMPLETED", "FAILED"].includes(chapter.status));
      if (!next) { setActiveChapterId(null); return; }
      await Promise.all([store.loadChapterNoteBlocks(next.id), store.loadChapterRuns(next.id)]);
      if (!cancelled) setActiveChapterId(next.id);
    }
    chooseInitial().catch((reason: unknown) => setError(message(reason, "精读结果加载失败")));
    return () => { cancelled = true; };
  }, [courseChapters, requestedChapterId, store.loadChapterNoteBlocks, store.loadChapterRuns]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const confirmLeave = () => !dirty || window.confirm(leaveMessage);
  const chooseChapter = (chapterId: string) => {
    if (!confirmLeave()) return;
    setDirtyKinds({}); setActiveChapterId(chapterId); setSearchParams({ chapterId });
    Promise.all([store.loadChapterNoteBlocks(chapterId), store.loadChapterRuns(chapterId)]).catch((reason: unknown) => setError(message(reason, "精读结果加载失败")));
  };
  const runHybrid = async () => {
    if (!activeChapterId || !confirmLeave()) return;
    setRunningHybrid(true); setError(null);
    try { await store.runHybridChapter(activeChapterId); await store.loadChapterRuns(activeChapterId); }
    catch (reason) { setError(message(reason, "混合精读启动失败")); }
    finally { setRunningHybrid(false); }
  };
  const saveBlock = async (block: NoteBlock, code: string, expected: string) => {
    if (!activeChapterId) return false;
    await store.saveChapterBlock(activeChapterId, block.kind, code, expected);
    setDirtyKinds((current) => ({ ...current, [block.kind]: false }));
    return true;
  };
  const trackDirty = useCallback((kind: string, value: boolean) => setDirtyKinds((current) => current[kind] === value ? current : { ...current, [kind]: value }), []);
  const sourceRefs = useMemo(() => collectSources(blocks), [blocks]);
  const review = runs.find((run) => run.round_key === "review");
  const latestRun = [...runs].sort((a, b) => b.updated_at - a.updated_at)[0];
  const hasContent = blocks.length > 0;

  return <div className="animate-in space-y-5">
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-zinc-200 pb-5">
      <div className="min-w-0"><h1 className="text-xl font-semibold text-zinc-900">章节精读工作台</h1><p className="mt-1 break-words text-sm text-zinc-500">{activeChapter?.title ?? "查看已生成的章节精读结果"}</p></div>
      <div className="flex shrink-0 flex-wrap gap-2"><button type="button" onClick={runHybrid} disabled={!activeChapterId || !["CONFIRMED", "FAILED"].includes(activeChapter?.status ?? "") || runningHybrid} className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40">{runningHybrid ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}混合精读</button><Link onClick={(event) => { if (!confirmLeave()) event.preventDefault(); }} to="/workbench/cards" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700">查看卡片池</Link></div>
    </header>
    {error && <p role="alert" className="border-l-2 border-red-500 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
    {courseChapters.length > 0 && <label className="block max-w-2xl text-xs text-zinc-500">选择章节<select aria-label="选择章节" value={activeChapterId ?? ""} onChange={(event) => chooseChapter(event.target.value)} className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 focus:outline-none focus:ring-2 focus:ring-zinc-200"><option value="" disabled>选择一个章节</option>{chapterGroups.map(({ source, chapters }) => <optgroup key={source.id} label={`《${source.title}》`}>{chapters.map((chapter) => <option key={chapter.id} value={chapter.id}>{chapterOptionLabel(source, chapter)}（{chapter.status}）</option>)}</optgroup>)}</select></label>}
    {!hasContent ? <EmptyState /> : <div className="grid min-w-0 gap-6 min-[1100px]:grid-cols-[minmax(0,1fr)_260px]">
      <main className="min-w-0 divide-y divide-zinc-200">{CONTENT_KINDS.map((kind) => { const block = blocks.find((item) => item.kind === kind); return <ContentSection key={kind} block={block} />; })}
        <section className="py-6"><h2 className="text-base font-semibold">来源</h2><div className="mt-3 flex flex-wrap gap-2">{sourceRefs.length ? sourceRefs.map((source) => <span key={source} className="border-l-2 border-emerald-500 bg-emerald-50 px-3 py-1.5 text-sm text-emerald-900">{source}</span>) : <span className="text-sm text-zinc-400">未标注来源</span>}</div></section>
        <section className="py-6"><h2 className="text-base font-semibold">审核结果</h2><ReviewResult run={review} /></section>
        {(["knowledge_mermaid", "application_mermaid"] as const).map((kind) => { const block = blocks.find((item) => item.kind === kind); return block ? <section key={kind} className="py-6"><MermaidEditor title={block.title} initial={block.body} onSave={(code, expected) => saveBlock(block, code, expected)} onDirtyChange={(value) => trackDirty(kind, value)} /></section> : null; })}
      </main><RunHistory runs={runs} latestRun={latestRun} />
    </div>}
  </div>;
}

function message(reason: unknown, fallback: string) { return reason instanceof Error ? reason.message : fallback; }
function ContentSection({ block }: { block?: NoteBlock }) { return <section className="py-6"><h2 className="text-base font-semibold">{block?.title ?? "未生成内容"}</h2><div className="prose prose-zinc mt-3 max-w-none break-words text-sm leading-7 text-zinc-700">{block ? <ReactMarkdown>{block.body}</ReactMarkdown> : <p className="text-zinc-400">暂无内容</p>}</div></section>; }
function collectSources(blocks: NoteBlock[]) { const result = new Set<string>(); for (const block of blocks) for (const match of block.body.matchAll(SOURCE_RE)) result.add(`《${match[1]}》·第 ${match[2]} 章`); return [...result]; }
function ReviewResult({ run }: { run?: ChapterRun }) { if (!run) return <p className="mt-3 text-sm text-zinc-400">暂无审核记录</p>; const failed = run.status === "FAILED"; return <div role={failed ? "alert" : undefined} className={`mt-3 border-l-2 px-3 py-2 text-sm ${failed ? "border-red-500 bg-red-50 text-red-800" : "border-emerald-500 bg-emerald-50 text-emerald-800"}`}>{failed ? run.error || "审核未通过" : run.output || "审核通过"}</div>; }
function RunHistory({ runs, latestRun }: { runs: ChapterRun[]; latestRun?: ChapterRun }) { return <aside aria-label="精读轮次历史" className="self-start border-t border-zinc-200 min-[1100px]:sticky min-[1100px]:top-5 min-[1100px]:border-l min-[1100px]:border-t-0 min-[1100px]:pl-5"><h2 className="py-4 text-sm font-semibold">精读轮次历史</h2><ol className="space-y-1 pb-5">{runs.length ? runs.map((run) => <li key={run.id} className="flex min-w-0 items-start gap-2 border-b border-zinc-100 py-3"><RunIcon run={run} /><div className="min-w-0 flex-1"><div className="flex flex-wrap justify-between gap-2"><span className="text-sm font-medium">{ROUND_LABELS[run.round_key] ?? run.round_key}</span>{latestRun?.id === run.id && <span className="text-xs text-blue-700">当前轮</span>}</div><p className={`mt-1 text-xs ${run.status === "FAILED" ? "text-red-700" : run.stale ? "text-amber-700" : "text-zinc-500"}`}>{run.status === "FAILED" ? "失败" : run.status === "RUNNING" ? "进行中" : run.stale ? "结果已过期" : "已完成"}</p>{run.status === "FAILED" && run.error && <p className="mt-1 break-words text-xs text-red-700">{run.error}</p>}</div></li>) : <li className="text-sm text-zinc-400">暂无运行记录</li>}</ol></aside>; }
function RunIcon({ run }: { run: ChapterRun }) { if (run.status === "FAILED") return <XCircle size={16} className="mt-0.5 shrink-0 text-red-500" />; if (run.status === "RUNNING") return <Loader2 size={16} className="mt-0.5 shrink-0 animate-spin text-blue-500" />; if (run.stale) return <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />; if (run.status === "COMPLETED") return <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />; return <Circle size={16} className="mt-0.5 shrink-0 text-zinc-400" />; }
function EmptyState() { return <div className="border border-dashed border-zinc-300 bg-white px-8 py-12 text-center"><p className="text-sm font-medium text-zinc-700">还没有精读结果</p><p className="mt-1 text-xs text-zinc-400">先确认章节并运行精读。</p><Link to="/workbench/chapters" className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white">去章节确认</Link></div>; }
