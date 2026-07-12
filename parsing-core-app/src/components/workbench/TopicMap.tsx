import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Loader2, Merge, Pencil, Plus, RefreshCw, Scissors, Sparkles, Trash2, X } from "lucide-react";
import { Link } from "react-router-dom";
import type { Chapter, CourseTopic, TopicNoteBlock } from "../../api/workbenchTypes";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

type Modal = "create" | "merge" | "split" | null;

const statusLabel: Record<string, string> = {
  DRAFT: "草稿", NOT_READY: "未就绪", READY: "可精读", RUNNING: "生成中", COMPLETED: "已完成",
  STALE: "需要更新", FAILED: "失败",
};

export default function TopicMap({ initialTopicId, oldResult = false }: { initialTopicId?: string; oldResult?: boolean } = {}) {
  return oldResult && initialTopicId ? <TopicOldResult topicId={initialTopicId} /> : <TopicMapEditor initialTopicId={initialTopicId} />;
}

function TopicMapEditor({ initialTopicId }: { initialTopicId?: string }) {
  const store = useWorkbenchStore();
  const courseId = store.selectedCourseId;
  const topics = courseId ? (store.topicsByCourse[courseId] ?? []) : [];
  const sourceList = courseId ? (store.sources[courseId] ?? []) : [];
  const allChapters = useMemo(() => sourceList.flatMap((source) => store.chapters[source.id] ?? []), [sourceList, store.chapters]);
  const sourceById = useMemo(() => new Map(sourceList.map((source) => [source.id, source])), [sourceList]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = topics.find((topic) => topic.id === selectedId) ?? topics[0] ?? null;
  const [mapping, setMapping] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [modal, setModal] = useState<Modal>(null);
  const [modalName, setModalName] = useState("");
  const [modalIds, setModalIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  useEffect(() => {
    const version = ++requestVersion.current;
    setSelectedId(initialTopicId ?? null); setError(null); setBusy(false); closeModal();
    if (!courseId) return;
    store.loadTopics(courseId).catch((reason) => {
      if (requestVersion.current === version) setError(reason instanceof Error ? reason.message : "课程主题加载失败");
    });
  }, [courseId, initialTopicId, store.loadTopics]);

  useEffect(() => {
    setMapping(selected?.chapter_ids ?? []);
    setTitle(selected?.title ?? "");
    setDescription(selected?.description ?? "");
    setEditing(false);
  }, [selected?.id, selected?.chapter_ids, selected?.title, selected?.description]);

  const blockers = allChapters.filter((chapter) => chapter.status !== "COMPLETED");
  const generationBusy = !!(courseId && store.topicActions[`generateTopics:${courseId}`]?.loading);
  const anyBusy = busy || generationBusy;
  const canConfirm = topics.length > 0 && topics.every((topic) => topic.chapter_ids.length > 0) && !anyBusy;
  const mappedCounts = new Map<string, number>();
  topics.forEach((topic) => topic.chapter_ids.forEach((id) => mappedCounts.set(id, (mappedCounts.get(id) ?? 0) + 1)));
  const uncovered = allChapters.filter((chapter) => !topics.some((topic) => topic.chapter_ids.includes(chapter.id)));

  const execute = async <T,>(operation: () => Promise<T>, onSuccess?: (result: T) => void) => {
    if (anyBusy) return;
    const generation = requestVersion.current;
    setBusy(true); setError(null);
    try {
      const result = await operation();
      if (requestVersion.current === generation) onSuccess?.(result);
    }
    catch (reason) {
      if (requestVersion.current === generation) setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
    } finally {
      if (requestVersion.current === generation) setBusy(false);
    }
  };

  const create = () => {
    if (!courseId || !modalName.trim()) return;
    execute(
      () => store.createTopic(courseId, { title: modalName.trim(), description: "" }),
      (created) => { setSelectedId(created.id); closeModal(); },
    );
  };
  const closeModal = () => { setModal(null); setModalName(""); setModalIds([]); };
  const merge = () => {
    if (!courseId || modalIds.length < 2 || !modalName.trim()) return;
    const selectedTopics = topics.filter((topic) => modalIds.includes(topic.id));
    const chapterIds = [...new Set(selectedTopics.flatMap((topic) => topic.chapter_ids))];
    execute(
      () => store.mergeTopics(courseId, { topic_ids: modalIds, title: modalName.trim(), description: selectedTopics.map((topic) => topic.description).filter(Boolean).join("；"), chapter_ids: chapterIds }),
      (created) => { setSelectedId(created.id); closeModal(); },
    );
  };
  const split = () => {
    if (!courseId || !selected || !modalName.trim() || modalIds.length === 0) return;
    execute(
      () => store.splitTopic(selected.id, { title: modalName.trim(), description: selected.description, new_chapter_ids: modalIds }),
      (result) => { setSelectedId(result[1].id); closeModal(); },
    );
  };

  if (!courseId) return <Empty text="请先选择课程" />;
  const courseTitle = store.courses.find((course) => course.id === courseId)?.title ?? "当前课程";
  const loadState = store.topicActions[`loadTopics:${courseId}`];
  if (loadState?.loading && topics.length === 0) return <Empty text="正在加载课程主题…" loading />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-zinc-200 pb-4">
        <div><h2 className="text-xl font-semibold">课程主题目录</h2><p className="mt-1 text-sm text-zinc-500">{courseTitle} · 按主题组织多本教材章节</p></div>
        <div className="flex shrink-0 flex-wrap gap-2 whitespace-nowrap">
          <button type="button" onClick={() => setModal("create")} disabled={anyBusy} className="inline-flex items-center gap-2 border border-zinc-200 px-3 py-2 text-sm"><Plus size={16} />新建主题</button>
          <button type="button" onClick={() => setModal("merge")} disabled={topics.length < 2 || anyBusy} className="inline-flex items-center gap-2 border border-zinc-200 px-3 py-2 text-sm"><Merge size={16} />合并主题</button>
          <button type="button" aria-label="AI 生成课程主题" onClick={() => execute(() => store.generateTopics(courseId, "hybrid"))} disabled={blockers.length > 0 || allChapters.length === 0 || anyBusy} className="inline-flex items-center gap-2 bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">{generationBusy ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />}AI 生成课程主题</button>
        </div>
      </div>
      {blockers.length > 0 && <div className="border-l-2 border-amber-400 bg-amber-50 px-4 py-3 text-sm"><p className="font-medium text-amber-900">以下章节完成后才能生成</p><ul className="mt-1 text-amber-800">{blockers.map((chapter) => <li key={chapter.id}>{sourceById.get(chapter.source_id)?.title} · {chapter.title}</li>)}</ul></div>}
      {error && <div role="alert" className="border-l-2 border-red-500 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div>}
      {allChapters.length === 0 ? <Empty text={`${courseTitle}还没有教材章节`} /> : topics.length === 0 ? <Empty text="尚未生成课程主题" /> : (
        <div className="grid min-w-0 gap-0 border border-zinc-200 xl:grid-cols-[minmax(220px,0.8fr)_minmax(260px,1fr)_minmax(300px,1.2fr)]">
          <section aria-label="主题列表" className="min-w-0 border-b border-zinc-200 xl:border-b-0 xl:border-r">
            <h3 className="border-b border-zinc-100 px-4 py-3 text-sm font-semibold">主题列表</h3>
            {topics.map((topic, index) => <div key={topic.id} className={`border-b border-zinc-100 p-3 ${selected?.id === topic.id ? "bg-emerald-50" : ""}`}>
              <button type="button" onClick={() => setSelectedId(topic.id)} className="w-full text-left"><span className="text-sm font-medium">{topic.title}</span><span className="ml-2 text-xs text-zinc-500">{statusLabel[topic.status] ?? topic.status}</span><p className="mt-1 text-xs text-zinc-500">{topic.description || "暂无说明"}</p><p className="mt-1 text-xs text-zinc-400">{topic.generation_reason || "手动创建"}</p><SyncLabel topic={topic} /></button>
              <div className="mt-2 flex gap-1">
                <IconButton label={`上移 ${topic.title}`} disabled={index === 0 || anyBusy} onClick={() => execute(() => move(topics, index, -1, courseId, store.reorderTopics))}><ArrowUp size={15} /></IconButton>
                <IconButton label={`下移 ${topic.title}`} disabled={index === topics.length - 1 || anyBusy} onClick={() => execute(() => move(topics, index, 1, courseId, store.reorderTopics))}><ArrowDown size={15} /></IconButton>
                <IconButton label={`删除 ${topic.title}`} disabled={anyBusy} onClick={() => execute(() => store.deleteTopic(courseId, topic.id))}><Trash2 size={15} /></IconButton>
                {topic.sync_status === "FAILED" && <IconButton label={`重试同步 ${topic.title}`} disabled={anyBusy} onClick={() => execute(() => store.retryTopicSync(topic.id))}><RefreshCw size={15} /></IconButton>}
              </div>
            </div>)}
          </section>
          <section aria-label="主题详情" className="min-w-0 border-b border-zinc-200 p-4 xl:border-b-0 xl:border-r">
            {selected && <><div className="flex items-start justify-between gap-3"><div className="min-w-0"><h3 className="break-words text-base font-semibold">{selected.title}</h3><p className="mt-1 text-xs text-zinc-500">{statusLabel[selected.status] ?? selected.status}</p></div><IconButton label="编辑主题" onClick={() => setEditing(true)}><Pencil size={16} /></IconButton></div>
              {editing ? <div className="mt-4 space-y-3"><label className="block text-xs font-medium">主题名称<input aria-label="主题名称" value={title} onChange={(e) => setTitle(e.target.value)} className="mt-1 w-full border border-zinc-300 px-3 py-2 text-sm" /></label><label className="block text-xs font-medium">主题说明<textarea aria-label="主题说明" value={description} onChange={(e) => setDescription(e.target.value)} className="mt-1 min-h-24 w-full border border-zinc-300 px-3 py-2 text-sm" /></label><button type="button" disabled={!title.trim() || anyBusy} onClick={() => execute(() => store.patchTopic(selected.id, { title: title.trim(), description: description.trim() }), () => setEditing(false))} className="bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">保存主题</button></div> : <><p className="mt-4 text-sm text-zinc-700">{selected.description || "暂无说明"}</p><dl className="mt-4 space-y-3 text-sm"><div><dt className="text-xs text-zinc-400">建议理由</dt><dd className="mt-1">{selected.generation_reason || "手动创建"}</dd></div><div><dt className="text-xs text-zinc-400">未覆盖章节</dt><dd className="mt-1">{uncovered.length ? `未覆盖章节：${uncovered.map((chapter) => chapter.title).join("、")}` : "全部章节已覆盖"}</dd></div>{selected.sync_status !== "SYNCED" && <div><dt className="text-xs text-zinc-400">Markdown 状态</dt><dd className="mt-1"><SyncLabel topic={selected} detail /></dd></div>}{selected.status === "STALE" && <div><dt className="text-xs text-zinc-400">更新状态</dt><dd className="mt-1 text-amber-700"><span>需要更新</span>{selected.stale_reason && <span className="mt-1 block">{selected.stale_reason}</span>}<Link to={`/workbench/courses/${courseId}/topics/${selected.id}`} className="mt-1 inline-block underline">查看旧结果</Link></dd></div>}</dl></>}
              <button type="button" onClick={() => setModal("split")} disabled={selected.chapter_ids.length === 0 || anyBusy} className="mt-5 inline-flex items-center gap-2 border border-zinc-200 px-3 py-2 text-sm"><Scissors size={16} />拆分当前主题</button></>}
          </section>
          <section aria-label="章节映射" className="min-w-0 p-4"><h3 className="text-sm font-semibold">教材章节映射</h3>{sourceList.map((source) => <fieldset key={source.id} className="mt-4"><legend className="mb-2 text-sm font-semibold">{source.title}</legend><div className="space-y-2">{(store.chapters[source.id] ?? []).map((chapter) => <ChapterCheck key={chapter.id} chapter={chapter} count={mappedCounts.get(chapter.id) ?? 0} checked={mapping.includes(chapter.id)} onChange={() => setMapping(toggle(mapping, chapter.id))} />)}</div></fieldset>)}<button type="button" disabled={!selected || anyBusy} onClick={() => selected && execute(() => store.updateTopicMapping(selected.id, mapping))} className="mt-5 bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">保存章节映射</button></section>
        </div>
      )}
      <div className="flex justify-end"><button type="button" disabled={!canConfirm} onClick={() => execute(() => store.confirmTopics(courseId))} className="bg-emerald-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">确认课程主题目录</button></div>
      {modal && <ModalView type={modal} name={modalName} setName={setModalName} ids={modalIds} setIds={setModalIds} topics={topics} selected={selected} chapters={allChapters} close={closeModal} submit={modal === "create" ? create : modal === "merge" ? merge : split} busy={anyBusy} />}
    </div>
  );
}

function move(topics: CourseTopic[], index: number, delta: number, courseId: string, reorder: (courseId: string, ids: string[]) => Promise<unknown>) { const ids = topics.map((topic) => topic.id); [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]]; return reorder(courseId, ids); }
function toggle(ids: string[], id: string) { return ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id]; }
function IconButton({ label, disabled, onClick, children }: { label: string; disabled?: boolean; onClick: () => void; children: React.ReactNode }) { return <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onClick} className="p-1.5 text-zinc-500 hover:bg-zinc-100 disabled:opacity-30">{children}</button>; }
function ChapterCheck({ chapter, count, checked, onChange }: { chapter: Chapter; count: number; checked: boolean; onChange: () => void }) { return <label className="flex min-w-0 items-start gap-2 text-sm"><input type="checkbox" checked={checked} onChange={onChange} className="mt-0.5" /><span className="min-w-0 break-words">{chapter.title} · {count} 个主题</span></label>; }
function Empty({ text, loading = false }: { text: string; loading?: boolean }) { return <div className="flex min-h-48 items-center justify-center border border-dashed border-zinc-300 text-sm text-zinc-500">{loading && <Loader2 size={16} className="mr-2 animate-spin" />}{text}</div>; }
function SyncLabel({ topic, detail = false }: { topic: CourseTopic; detail?: boolean }) {
  if (topic.sync_status === "SYNCED") return null;
  if (topic.sync_status === "FAILED") return <span className={`${detail ? "block" : "mt-1 block text-xs"} text-red-700`}><span>Markdown 同步失败</span>{topic.sync_error && <span className="mt-1 block">{topic.sync_error}</span>}</span>;
  return <span className={`${detail ? "block" : "mt-1 block text-xs"} text-amber-700`}>待同步</span>;
}

function ModalView({ type, name, setName, ids, setIds, topics, selected, chapters, close, submit, busy }: { type: Exclude<Modal, null>; name: string; setName: (name: string) => void; ids: string[]; setIds: (ids: string[]) => void; topics: CourseTopic[]; selected: CourseTopic | null; chapters: Chapter[]; close: () => void; submit: () => void; busy: boolean }) {
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    const restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const focusable = () => [...(dialog?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])];
    focusable().find((element) => element.tagName === "INPUT")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); return; }
      if (event.key !== "Tab") return;
      const nodes = focusable();
      const first = nodes[0]; const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); restoreFocus?.focus(); };
  }, []);
  const labels = type === "create" ? ["新建主题", "新主题名称", "创建"] : type === "merge" ? ["合并主题", "合并后名称", "确认合并"] : ["拆分主题", "拆分主题名称", "确认拆分"];
  const options = type === "merge" ? topics.map((topic) => ({ id: topic.id, label: topic.title })) : chapters.filter((chapter) => selected?.chapter_ids.includes(chapter.id)).map((chapter) => ({ id: chapter.id, label: `${chapter.title} · ${topics.filter((topic) => topic.chapter_ids.includes(chapter.id)).length} 个主题` }));
  const valid = !!name.trim() && (type === "create" || (type === "merge" ? ids.length >= 2 : ids.length >= 1 && ids.length < options.length));
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"><div role="dialog" aria-modal="true" aria-label={labels[0]} className="w-full max-w-md bg-white p-5 shadow-xl"><div className="flex justify-between"><h2 className="font-semibold">{labels[0]}</h2><IconButton label="关闭" onClick={close}><X size={17} /></IconButton></div><label className="mt-4 block text-sm">{labels[1]}<input aria-label={labels[1]} value={name} onChange={(e) => setName(e.target.value)} className="mt-1 w-full border border-zinc-300 px-3 py-2" /></label>{options.length > 0 && <div className="mt-4 max-h-56 space-y-2 overflow-y-auto">{options.map((option) => <label key={option.id} className="flex gap-2 text-sm"><input type="checkbox" checked={ids.includes(option.id)} onChange={() => setIds(toggle(ids, option.id))} />{option.label}</label>)}</div>}{type === "split" && (ids.length === 0 || ids.length === options.length) && <p className="mt-3 text-sm text-amber-700">原主题必须至少保留一个章节</p>}<div className="mt-5 flex justify-end gap-2"><button type="button" onClick={close} className="border border-zinc-200 px-3 py-2 text-sm">取消</button><button type="button" disabled={!valid || busy} onClick={submit} className="bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">{labels[2]}</button></div></div></div>;
}

const BLOCK_ORDER = ["overview", "linked_sources", "core_concepts", "viewpoint_comparison", "consensus_disagreements", "complementary_views", "plain_explanation", "textbook_cases", "real_world_problem_solving", "integrated_framework", "application_methods", "further_thinking", "knowledge_mermaid", "application_mermaid"];
const BLOCK_TITLES: Record<string, string> = { overview: "主题概要", linked_sources: "关联教材与章节", core_concepts: "核心概念", viewpoint_comparison: "教材观点对照", consensus_disagreements: "共识与分歧", complementary_views: "互补视角", plain_explanation: "通俗解释", textbook_cases: "教材案例", real_world_problem_solving: "现实问题解决", integrated_framework: "综合分析框架", application_methods: "实际应用方法", further_thinking: "延伸思考", knowledge_mermaid: "知识结构图", application_mermaid: "应用流程图" };

function TopicOldResult({ topicId }: { topicId: string }) {
  const store = useWorkbenchStore();
  const courseId = store.selectedCourseId;
  const blocks = store.topicBlocksById[topicId] ?? [];
  const [error, setError] = useState<string | null>(null);
  const version = useRef(0);
  useEffect(() => {
    const current = ++version.current;
    setError(null);
    store.loadTopicBlocks(topicId).catch((reason) => {
      if (version.current === current) setError(reason instanceof Error ? reason.message : "旧结果加载失败，请稍后重试");
    });
  }, [courseId, topicId, store.loadTopicBlocks]);
  const loading = !!store.topicActions[`loadTopicBlocks:${topicId}`]?.loading;
  const ordered = [...blocks].sort((left, right) => blockIndex(left) - blockIndex(right));
  return <div className="space-y-5"><div className="border-b border-zinc-200 pb-4"><p className="text-xs text-zinc-400">历史产物</p><h2 className="mt-1 text-xl font-semibold">主题旧结果</h2><Link to={`/workbench/courses/${courseId ?? ""}/topics`} className="mt-3 inline-block text-sm text-emerald-700 underline">返回主题目录</Link></div>{error ? <div role="alert" className="border-l-2 border-red-500 bg-red-50 px-4 py-3 text-sm text-red-800">{error}</div> : loading && blocks.length === 0 ? <Empty text="正在加载旧结果…" loading /> : ordered.length === 0 ? <Empty text="该主题暂无旧结果" /> : <div className="divide-y divide-zinc-200 border-y border-zinc-200">{ordered.map((block) => <section key={block.id} className="py-5"><h3 className="text-base font-semibold">{BLOCK_TITLES[block.kind] ?? block.kind}</h3><div className="mt-3 whitespace-pre-wrap text-sm leading-7 text-zinc-700">{block.content}</div></section>)}</div>}</div>;
}

function blockIndex(block: TopicNoteBlock) { const index = BLOCK_ORDER.indexOf(block.kind); return index < 0 ? BLOCK_ORDER.length : index; }
