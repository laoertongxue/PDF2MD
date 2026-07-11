import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, RefreshCw, RotateCcw, Save, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "../MermaidBlock";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import type { CourseTopic, TopicNoteBlock } from "../../api/workbenchTypes";

const SECTIONS = [
  ["overview", "主题概要"], ["linked_sources", "关联教材与章节"], ["core_concepts", "核心概念"],
  ["viewpoint_comparison", "教材观点对照"], ["consensus_disagreements", "共识与分歧"], ["complementary_views", "互补视角"],
  ["plain_explanation", "通俗、有趣、生活化的解释"], ["textbook_cases", "教材案例解读"], ["real_world_problem_solving", "现实案例与问题解决"],
  ["integrated_framework", "综合分析框架"], ["application_methods", "实际应用方法"], ["further_thinking", "延伸思考"],
  ["knowledge_mermaid", "Mermaid 知识结构图"], ["application_mermaid", "Mermaid 应用流程图"], ["cards", "写作卡片"],
] as const;
const STATUS: Record<string, string> = { DRAFT: "草稿", NOT_READY: "未就绪", READY: "可生成", RUNNING: "生成中", COMPLETED: "已完成", STALE: "需要更新", FAILED: "失败" };
const SOURCE_RE = /(\[《[^\]\n]+》·第\s*\d+\s*章\])/g;

export default function TopicFusion({ courseId, topicId }: { courseId: string; topicId: string }) {
  const store = useWorkbenchStore();
  const topics = store.topicsByCourse[courseId] ?? [];
  const topic = topics.find((item) => item.id === topicId) ?? null;
  const blocks = store.topicBlocksById[topicId] ?? [];
  const cards = store.topicCardsById[topicId] ?? [];
  const runs = store.topicRunsById[topicId] ?? [];
  const [error, setError] = useState<string | null>(null);
  const [recovering, setRecovering] = useState(false);
  const requestVersion = useRef(0);
  const pageKey = `${courseId}:${topicId}`;
  const pageKeyRef = useRef(pageKey);
  if (pageKeyRef.current !== pageKey) {
    pageKeyRef.current = pageKey;
    requestVersion.current += 1;
  }

  useEffect(() => {
    const version = ++requestVersion.current;
    setError(null); setRecovering(false);
    Promise.all([
      store.loadTopics(courseId), store.loadTopicBlocks(topicId), store.loadTopicCards(topicId), store.loadTopicRuns(topicId),
      store.loadSources(courseId).then((sources) => Promise.all(sources.map((source) => store.loadChapters(source.id)))),
    ]).catch((reason: unknown) => {
      if (requestVersion.current === version) setError(reason instanceof Error ? reason.message : "融合精读加载失败");
    });
  }, [courseId, topicId, store.loadChapters, store.loadSources, store.loadTopicBlocks, store.loadTopicCards, store.loadTopicRuns, store.loadTopics]);

  const sourceTargets = useMemo(() => {
    const result = new Map<string, string>();
    const sources = store.sources[courseId] ?? [];
    const displayTitles = allocateSourceDisplayTitles(sources);
    for (const source of sources) {
      for (const chapter of store.chapters[source.id] ?? []) result.set(`[《${displayTitles.get(source.id)}》·第 ${chapter.seq + 1} 章]`, chapter.id);
    }
    return result;
  }, [courseId, store.chapters, store.sources]);
  const chapterById = useMemo(() => new Map(Object.values(store.chapters).flat().map((chapter) => [chapter.id, chapter])), [store.chapters]);
  const latestSuccess = [...runs].filter((run) => run.status === "COMPLETED" && run.finished_at).sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0];
  const latestFailure = [...runs].filter((run) => run.status === "FAILED").sort((a, b) => b.started_at - a.started_at)[0];
  const busy = topic?.status === "RUNNING" || !!store.topicActions[`runTopicHybrid:${topicId}`]?.loading;
  const canRun = topic?.status === "READY" || topic?.status === "STALE" || topic?.status === "FAILED" || topic?.status === "COMPLETED";
  const isFirstRun = topic?.status === "READY" || topic?.status === "NOT_READY";

  const run = async () => {
    if (!canRun || busy) return;
    const version = requestVersion.current;
    setError(null);
    try {
      await store.runTopicHybrid(topicId);
      await Promise.all([store.loadTopicBlocks(topicId), store.loadTopicCards(topicId), store.loadTopicRuns(topicId)]);
    } catch (reason) {
      if (requestVersion.current === version) setError(reason instanceof Error ? reason.message : "融合精读运行失败");
    }
  };

  const recover = async () => {
    if (recovering) return;
    const version = requestVersion.current;
    setRecovering(true); setError(null);
    try { await store.recoverTopic(topicId); }
    catch (reason) {
      if (requestVersion.current === version) setError(reason instanceof Error ? reason.message : "恢复检查失败");
    } finally {
      if (requestVersion.current === version) setRecovering(false);
    }
  };

  const saveBlock = async (kind: string, content: string, expectedContent: string) => {
    const version = requestVersion.current;
    setError(null);
    try {
      await store.saveTopicBlock(topicId, kind, content, expectedContent);
      return requestVersion.current === version;
    } catch (reason) {
      if (requestVersion.current !== version) return false;
      const message = reason instanceof Error ? reason.message : "保存失败，请稍后重试";
      if (message === "编辑已保存到数据库，Markdown同步失败，可重试") {
        await Promise.all([store.loadTopicBlocks(topicId), store.loadTopics(courseId)]).catch(() => undefined);
      }
      if (requestVersion.current === version) setError(message);
      return false;
    }
  };

  if (!topic) return <div className="py-16 text-center text-sm text-zinc-500">正在加载主题…</div>;
  return (
    <div className="grid min-w-0 border-y border-zinc-200 md:grid-cols-[220px_minmax(0,1fr)]">
      <aside className="border-b border-zinc-200 bg-zinc-50 md:border-b-0 md:border-r" aria-label="课程主题列表">
        <div className="border-b border-zinc-200 px-4 py-3 text-sm font-semibold">课程主题</div>
        {topics.map((item) => <Link key={item.id} to={`/workbench/courses/${courseId}/topics/${item.id}`} className={`block border-b border-zinc-100 px-4 py-3 ${item.id === topicId ? "bg-emerald-50" : "hover:bg-white"}`}><span className="block break-words text-sm font-medium">{item.title}</span><span className="mt-1 block text-xs text-zinc-500">{STATUS[item.status] ?? item.status}</span></Link>)}
      </aside>
      <main className="min-w-0 px-4 py-5 sm:px-6 lg:px-10">
        <header className="border-b border-zinc-200 pb-5">
          <div className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0"><h1 className="break-words text-2xl font-semibold">{topic.title}</h1><div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500"><span>{STATUS[topic.status] ?? topic.status}</span><span>最后成功：{latestSuccess?.finished_at ? new Date(latestSuccess.finished_at * 1000).toLocaleString() : "暂无"}</span></div></div><div className="flex flex-wrap gap-2"><button type="button" onClick={run} disabled={!canRun || busy} aria-label={isFirstRun ? "运行融合精读" : "重新生成"} className="inline-flex items-center gap-2 bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">{busy ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />}{isFirstRun ? "运行融合精读" : "重新生成"}</button>{topic.status === "RUNNING" && <button type="button" onClick={recover} disabled={recovering} aria-label="检查并恢复" title="检查运行状态并恢复过期任务" className="inline-flex items-center gap-2 border border-zinc-200 px-3 py-2 text-sm disabled:opacity-40">{recovering ? <Loader2 size={16} className="animate-spin" /> : <RotateCcw size={16} />}检查并恢复</button>}</div></div>
          {topic.stale_reason && <p className="mt-3 text-sm text-amber-700">过期原因：{topic.stale_reason}</p>}
          {topic.blocking_chapter_ids.length > 0 && <div className="mt-3 border-l-2 border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-900">阻塞章节：{topic.blocking_chapter_ids.map((id) => chapterById.get(id)?.title ?? id).join("、")}</div>}
          {topic.sync_status === "FAILED" && <div className="mt-3 flex items-center gap-3 text-sm text-red-700"><span>Markdown 同步失败，已保留数据库版本</span><button type="button" onClick={() => store.retryTopicSync(topicId)} className="inline-flex items-center gap-1 underline"><RefreshCw size={14} />重试同步</button></div>}
          {(error || latestFailure?.error) && <p role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-sm text-red-800">{error ?? latestFailure?.error}</p>}
        </header>
        <div className="mx-auto max-w-3xl divide-y divide-zinc-200">
          {SECTIONS.map(([kind, title], index) => <section key={kind} className="py-7"><h2 className="text-lg font-semibold">{index + 1}. {title}</h2><div className="mt-4">{kind === "cards" ? <CardSection cards={cards} sourceTargets={sourceTargets} /> : <BlockSection block={blocks.find((item) => item.kind === kind)} sourceTargets={sourceTargets} onSave={(content, expected) => saveBlock(kind, content, expected)} />}</div></section>)}
        </div>
      </main>
    </div>
  );
}

function allocateSourceDisplayTitles(sources: Array<{ id: string; title: string }>) {
  const normalized = (title: string) => title.normalize("NFKC").toLocaleLowerCase().trim().replace(/\s+/g, " ");
  const reserved = new Set(sources.map((source) => normalized(source.title)));
  const assigned = new Set<string>();
  const result = new Map<string, string>();
  for (const source of sources) {
    let display = source.title;
    if (assigned.has(normalized(display))) {
      for (let suffix = 2; suffix <= 10_000; suffix += 1) {
        const candidate = `${source.title}（${suffix}）`;
        const key = normalized(candidate);
        if (!reserved.has(key) && !assigned.has(key)) { display = candidate; break; }
      }
    }
    assigned.add(normalized(display)); result.set(source.id, display);
  }
  return result;
}

function RichText({ text, sourceTargets }: { text: string; sourceTargets: Map<string, string> }) {
  return <div className="space-y-3 text-sm leading-7 text-zinc-700">{text.split(SOURCE_RE).map((part, index) => { const chapterId = sourceTargets.get(part); if (chapterId) return <Link key={`${part}-${index}`} to={`/workbench/chapter?chapterId=${chapterId}`} className="text-emerald-700 underline">{part}</Link>; if (/^\[《[^\]\n]+》·第\s*\d+\s*章\]$/.test(part)) return <span key={`${part}-${index}`} className="text-zinc-500">{part}</span>; return <ReactMarkdown key={index}>{part}</ReactMarkdown>; })}</div>;
}

function BlockSection({ block, sourceTargets, onSave }: { block?: TopicNoteBlock; sourceTargets: Map<string, string>; onSave: (content: string, expectedContent: string) => Promise<boolean> }) {
  const [code, setCode] = useState(block?.content ?? ""); const [saved, setSaved] = useState(false); const [saving, setSaving] = useState(false);
  const saveGeneration = useRef(0);
  useEffect(() => { saveGeneration.current += 1; setCode(block?.content ?? ""); setSaved(false); setSaving(false); }, [block?.id, block?.content]);
  if (!block) return <p className="text-sm text-zinc-400">暂无内容</p>;
  if (!block.kind.endsWith("_mermaid")) return <RichText text={block.content} sourceTargets={sourceTargets} />;
  return <div className="space-y-3"><MermaidBlock code={code} /><label className="block text-xs font-medium text-zinc-500">Mermaid 源码<textarea aria-label={`${block.kind} Mermaid 源码`} value={code} onChange={(event) => { setCode(event.target.value); setSaved(false); }} spellCheck={false} className="mt-2 h-40 w-full resize-y border border-zinc-200 p-3 font-mono text-xs leading-5" /></label><button type="button" onClick={async () => { if (saving) return; const generation = ++saveGeneration.current; setSaving(true); const ok = await onSave(code, block.content); if (saveGeneration.current === generation) { setSaved(ok); setSaving(false); } }} disabled={saving || !code.trim() || code === block.content} className="inline-flex items-center gap-2 border border-zinc-200 px-3 py-2 text-sm disabled:opacity-40">{saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}保存 Mermaid</button>{saved && <span className="ml-3 text-xs text-emerald-700">已保存并同步 Markdown</span>}</div>;
}

function CardSection({ cards, sourceTargets }: { cards: Array<{ id: string; card_type: string; title: string; content: string; source_refs: string[] }>; sourceTargets: Map<string, string> }) {
  return <div className="grid gap-4 sm:grid-cols-2">{cards.map((card) => <article key={card.id} className="border-l-2 border-emerald-500 pl-4"><p className="text-xs text-zinc-500">{card.card_type}</p><h3 className="mt-1 text-sm font-semibold">{card.title}</h3><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-zinc-700">{card.content}</p><div className="mt-3 flex flex-wrap gap-2">{card.source_refs.map((ref) => sourceTargets.has(ref) ? <Link key={ref} to={`/workbench/chapter?chapterId=${sourceTargets.get(ref)}`} className="text-xs text-emerald-700 underline">{ref}</Link> : <span key={ref} className="text-xs text-zinc-500">{ref}</span>)}</div></article>)}</div>;
}
