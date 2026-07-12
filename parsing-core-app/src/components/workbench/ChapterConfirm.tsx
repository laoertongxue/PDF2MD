import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, CheckCircle2, Loader2, Merge, Save, Scissors } from "lucide-react";
import { Link } from "react-router-dom";
import * as api from "../../api/workbench";
import type { ChapterDraft, ChapterDraftSpec, ChapterDraftState, Source } from "../../api/workbenchTypes";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

interface DraftEditorState extends ChapterDraftState { dirty: boolean; loading: boolean; saving: boolean; error: string | null }

const emptyState = (): DraftEditorState => ({ chapters: [], fingerprint: "", dirty: false, loading: true, saving: false, error: null });
const isLocked = (state: DraftEditorState) => state.chapters.length > 0 && state.chapters.every((chapter) => chapter.status !== "DRAFT");
const specs = (chapters: ChapterDraft[]): ChapterDraftSpec[] => chapters.map(({ id, title, start, end }) => ({ id: id.startsWith("local:") ? undefined : id, title: title.trim(), start, end }));

export default function ChapterConfirm() {
  const { loadCourses, loadSources, selectedCourseId } = useWorkbenchStore();
  const [sourceList, setSourceList] = useState<Source[]>([]);
  const [states, setStates] = useState<Record<string, DraftEditorState>>({});
  const [pageError, setPageError] = useState<string | null>(null);

  useEffect(() => { loadCourses().catch((error: unknown) => setPageError(message(error, "课程加载失败"))); }, [loadCourses]);
  useEffect(() => {
    if (!selectedCourseId) { setSourceList([]); setStates({}); return; }
    let active = true;
    loadSources(selectedCourseId).then(async (sources) => {
      if (!active) return;
      setSourceList(sources);
      setStates(Object.fromEntries(sources.map((source) => [source.id, emptyState()])));
      await Promise.all(sources.map(async (source) => {
        try { const state = await api.getChapterDrafts(source.id); if (active) setStates((current) => ({ ...current, [source.id]: { ...state, dirty: false, loading: false, saving: false, error: null } })); }
        catch (error) { if (active) setStates((current) => ({ ...current, [source.id]: { ...emptyState(), loading: false, error: message(error, "章节草稿加载失败") } })); }
      }));
    }).catch((error: unknown) => setPageError(message(error, "教材加载失败")));
    return () => { active = false; };
  }, [loadSources, selectedCourseId]);

  const update = (sourceId: string, transform: (chapters: ChapterDraft[]) => ChapterDraft[]) => setStates((current) => ({ ...current, [sourceId]: { ...current[sourceId], chapters: transform(current[sourceId].chapters), dirty: true, error: null } }));
  const save = async (sourceId: string) => {
    const current = states[sourceId];
    if (!valid(current.chapters) || current.saving || isLocked(current)) return;
    setStates((all) => ({ ...all, [sourceId]: { ...all[sourceId], saving: true, error: null } }));
    try { const next = await api.replaceChapterDrafts(sourceId, current.fingerprint, specs(current.chapters)); setStates((all) => ({ ...all, [sourceId]: { ...next, dirty: false, loading: false, saving: false, error: null } })); }
    catch (error) { setStates((all) => ({ ...all, [sourceId]: { ...all[sourceId], saving: false, error: message(error, "章节草稿保存失败，请刷新后重试") } })); }
  };
  const confirm = async (sourceId: string) => {
    const current = states[sourceId];
    if (current.dirty || current.saving || isLocked(current) || !valid(current.chapters)) return;
    setStates((all) => ({ ...all, [sourceId]: { ...all[sourceId], saving: true, error: null } }));
    try { const next = await api.confirmChapterDrafts(sourceId, current.fingerprint); setStates((all) => ({ ...all, [sourceId]: { ...next, dirty: false, loading: false, saving: false, error: null } })); }
    catch (error) { setStates((all) => ({ ...all, [sourceId]: { ...all[sourceId], saving: false, error: message(error, "章节目录确认失败，请刷新后重试") } })); }
  };

  return <div className="space-y-5 animate-in">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-xl font-semibold">章节确认</h1><p className="mt-1 text-sm text-zinc-500">按教材校正章节名称、顺序与内容边界，保存后确认锁定。</p></div><Link to="/workbench/chapter" className="border border-zinc-200 px-3 py-2 text-sm">打开精读工作台</Link></div>
    {pageError && <p role="alert" className="border-l-2 border-red-500 bg-red-50 px-3 py-2 text-sm text-red-700">{pageError}</p>}
    {sourceList.length === 0 && !pageError ? <div className="border border-dashed border-zinc-300 px-8 py-12 text-center text-sm text-zinc-500">还没有可确认的教材章节</div> : sourceList.map((source) => <SourceDraftEditor key={source.id} source={source} state={states[source.id] ?? emptyState()} update={(transform) => update(source.id, transform)} save={() => void save(source.id)} confirm={() => void confirm(source.id)} />)}
  </div>;
}

function SourceDraftEditor({ source, state, update, save, confirm }: { source: Source; state: DraftEditorState; update: (transform: (chapters: ChapterDraft[]) => ChapterDraft[]) => void; save: () => void; confirm: () => void }) {
  const locked = isLocked(state);
  const invalid = !valid(state.chapters);
  const move = (index: number, delta: number) => update((items) => { const next = [...items]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; return next.map((item, seq) => ({ ...item, seq })); });
  const split = (index: number) => update((items) => { const item = items[index]; const boundary = Math.floor((item.start + item.end) / 2); if (boundary <= item.start || boundary >= item.end) return items; const next = [...items]; next.splice(index, 1, { ...item, end: boundary }, { ...item, id: `local:${crypto.randomUUID()}`, title: `${item.title}（下）`, start: boundary, seq: item.seq + 1 }); return next.map((chapter, seq) => ({ ...chapter, seq })); });
  const merge = (index: number) => update((items) => items.filter((_, itemIndex) => itemIndex !== index + 1).map((item, seq) => itemIndexPatch(item, seq, index, items)));
  return <section aria-label={source.title} className="border border-zinc-200 bg-white">
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-200 bg-zinc-50 px-4 py-3"><div><h2 className="text-sm font-semibold">{source.title}</h2><p className="text-xs text-zinc-500">{state.chapters.length} 个章节</p></div><div className="flex gap-2">{locked ? <span className="inline-flex items-center gap-1.5 text-sm text-emerald-700"><CheckCircle2 size={16} />章节目录已确认并锁定</span> : <><button type="button" onClick={save} disabled={!state.dirty || invalid || state.saving} className="inline-flex items-center gap-1.5 border border-zinc-200 px-3 py-2 text-sm disabled:opacity-40"><Save size={15} />保存章节草稿</button><button type="button" onClick={confirm} disabled={state.dirty || invalid || state.saving || state.chapters.length === 0} className="bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-40">确认章节目录</button></>}</div></header>
    {state.loading ? <div className="flex min-h-28 items-center justify-center text-sm text-zinc-500"><Loader2 className="mr-2 animate-spin" size={16} />读取章节草稿</div> : <div className="divide-y divide-zinc-100">{state.chapters.map((chapter, index) => <div key={chapter.id} className="grid gap-3 px-4 py-3 lg:grid-cols-[36px_minmax(180px,1fr)_110px_110px_180px] lg:items-end"><span className="pb-2 text-sm tabular-nums text-zinc-400">{index + 1}</span><label className="text-xs text-zinc-500">章节名称<input aria-label="章节名称" disabled={locked} value={chapter.title} onChange={(event) => update((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, title: event.target.value } : item))} className="mt-1 h-9 w-full border border-zinc-200 px-2 text-sm disabled:bg-zinc-50" /></label><Boundary label="起始边界" value={chapter.start} disabled={locked} onChange={(value) => update((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, start: value } : item))} /><Boundary label="结束边界" value={chapter.end} disabled={locked} onChange={(value) => update((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, end: value } : item))} /><div className="flex justify-end gap-1 pb-0.5"><Icon label={`上移 ${chapter.title}`} disabled={locked || index === 0} onClick={() => move(index, -1)}><ArrowUp size={15} /></Icon><Icon label={`下移 ${chapter.title}`} disabled={locked || index === state.chapters.length - 1} onClick={() => move(index, 1)}><ArrowDown size={15} /></Icon><Icon label={`拆分 ${chapter.title}`} disabled={locked || chapter.end - chapter.start < 2} onClick={() => split(index)}><Scissors size={15} /></Icon><Icon label={`合并下一章 ${chapter.title}`} disabled={locked || index === state.chapters.length - 1} onClick={() => merge(index)}><Merge size={15} /></Icon></div></div>)}{invalid && <p role="alert" className="px-4 py-3 text-sm text-red-700">章节名称不能为空，且结束边界必须大于起始边界。</p>}{state.error && <p role="alert" className="px-4 py-3 text-sm text-red-700">{state.error}</p>}</div>}
  </section>;
}

function Boundary({ label, value, disabled, onChange }: { label: string; value: number; disabled: boolean; onChange: (value: number) => void }) { return <label className="text-xs text-zinc-500">{label}<input type="number" min={0} aria-label={label} disabled={disabled} value={value} onChange={(event) => onChange(Number(event.target.value))} className="mt-1 h-9 w-full border border-zinc-200 px-2 text-sm tabular-nums disabled:bg-zinc-50" /></label>; }
function Icon({ label, disabled, onClick, children }: { label: string; disabled: boolean; onClick: () => void; children: React.ReactNode }) { return <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onClick} className="flex h-9 w-9 items-center justify-center text-zinc-500 hover:bg-zinc-100 disabled:opacity-30">{children}</button>; }
function valid(chapters: ChapterDraft[]) { return chapters.length > 0 && chapters.every((chapter) => chapter.title.trim() && Number.isInteger(chapter.start) && Number.isInteger(chapter.end) && chapter.start >= 0 && chapter.end > chapter.start); }
function itemIndexPatch(item: ChapterDraft, seq: number, mergeIndex: number, original: ChapterDraft[]) { return seq === mergeIndex ? { ...item, end: original[mergeIndex + 1].end, title: `${item.title} / ${original[mergeIndex + 1].title}`, seq } : { ...item, seq }; }
function message(error: unknown, fallback: string) { return error instanceof Error ? error.message : fallback; }
