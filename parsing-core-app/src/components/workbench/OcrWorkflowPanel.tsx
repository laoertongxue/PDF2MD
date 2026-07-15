import { useEffect, useState } from "react";
import { Ban, CheckCircle2, Loader2, Play, RefreshCw, Sparkles, XCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import MermaidBlock from "../MermaidBlock";
import {
  cancelSourceOcr,
  confirmSourceChapter,
  generateSourceNote,
  getSourceOcrStatus,
  recognizeSourceChapters,
  startSourceOcr,
} from "../../api/workbench";
import type { OcrChapter, OcrChapterTree, OcrNoteResult, OcrStatus, Source } from "../../api/workbenchTypes";

export default function OcrWorkflowPanel({ source }: { source: Source }) {
  const [status, setStatus] = useState<OcrStatus | null>(null);
  const [tree, setTree] = useState<OcrChapterTree | null>(null);
  const [note, setNote] = useState<OcrNoteResult | null>(null);
  const [selectedChapter, setSelectedChapter] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try { setStatus(await getSourceOcrStatus(source.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取 OCR 状态"); }
  };

  useEffect(() => { void refresh(); }, [source.id]);
  useEffect(() => {
    if (status?.status !== "running") return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [status?.status, source.id]);

  const run = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await operation(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败，请重试"); }
    finally { setBusy(false); }
  };

  const chapters = tree?.chapters ?? [];
  const start = () => void run(async () => { setTree(null); setNote(null); await startSourceOcr(source.id); });
  const cancel = () => void run(() => cancelSourceOcr(source.id));
  const detect = () => void run(async () => {
    const next = await recognizeSourceChapters(source.id);
    setTree(next); setSelectedChapter(next.chapters[0]?.id ?? null);
  });
  const confirm = () => selectedChapter ? void run(() => confirmSourceChapter(source.id, selectedChapter)) : undefined;
  const generate = () => selectedChapter ? void run(async () => setNote(await generateSourceNote(source.id, selectedChapter))) : undefined;

  return <section aria-label={`无人值守 OCR：${source.title}`} className="border-t border-zinc-200 pt-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-sm font-semibold">无人值守精读链路</h2><p className="mt-1 text-xs text-zinc-500">Apple Vision → Codex 视觉复核 → 百度冲突升级 → Codex 终审</p></div>
      <StatusBadge status={status} />
    </div>
    <div className="mt-4 flex flex-wrap gap-2">
      {status?.status !== "running" && <button type="button" onClick={start} disabled={busy} className="inline-flex items-center gap-1.5 bg-zinc-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"><Play size={14} />启动 OCR</button>}
      {status?.status === "running" && <button type="button" onClick={cancel} disabled={busy} className="inline-flex items-center gap-1.5 border border-red-200 px-3 py-2 text-xs text-red-700 disabled:opacity-40"><Ban size={14} />取消任务</button>}
      {status?.status === "completed" && <button type="button" onClick={detect} disabled={busy} className="inline-flex items-center gap-1.5 border border-zinc-200 px-3 py-2 text-xs text-zinc-700 disabled:opacity-40"><RefreshCw size={14} />识别章节</button>}
      {status && ["failed", "blocked", "cancelled"].includes(status.status) && <button type="button" onClick={start} disabled={busy} className="inline-flex items-center gap-1.5 border border-zinc-200 px-3 py-2 text-xs text-zinc-700 disabled:opacity-40"><RefreshCw size={14} />重试</button>}
    </div>
    {status?.error && <p role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">{status.error}</p>}
    {error && <p role="alert" className="mt-3 border-l-2 border-red-500 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>}
    {status?.status === "completed" && !status.publishable && <p className="mt-3 text-xs text-amber-700">OCR 已结束，但尚未发布完整精读结果，不能标记为完成。</p>}
    {chapters.length > 0 && <div className="mt-4 border border-zinc-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-xs font-semibold">章节候选 · {chapters.length}</h3><div className="flex gap-2"><button type="button" onClick={confirm} disabled={!selectedChapter || busy} className="border border-zinc-200 px-2.5 py-1.5 text-xs disabled:opacity-40">确认章节</button><button type="button" onClick={generate} disabled={!selectedChapter || busy} className="inline-flex items-center gap-1.5 bg-emerald-600 px-2.5 py-1.5 text-xs text-white disabled:opacity-40"><Sparkles size={13} />运行精读</button></div></div>
      <div className="mt-3 space-y-1">{chapters.map((chapter) => <ChapterOption key={chapter.id} chapter={chapter} selected={selectedChapter === chapter.id} onSelect={setSelectedChapter} />)}</div>
    </div>}
    {note?.publishable && <NotePreview markdown={note.markdown} />}
  </section>;
}

function StatusBadge({ status }: { status: OcrStatus | null }) {
  const value = status?.status ?? "idle";
  const label = { idle: "未启动", running: "处理中", completed: "OCR 已完成", blocked: "已阻断", failed: "失败", cancelled: "已取消" }[value];
  const Icon = value === "running" ? Loader2 : value === "completed" ? CheckCircle2 : ["failed", "blocked", "cancelled"].includes(value) ? XCircle : RefreshCw;
  return <span className={`inline-flex items-center gap-1.5 text-xs ${value === "completed" ? "text-emerald-700" : ["failed", "blocked"].includes(value) ? "text-red-700" : "text-zinc-500"}`}><Icon size={14} className={value === "running" ? "animate-spin" : ""} />{label}</span>;
}

function ChapterOption({ chapter, selected, onSelect }: { chapter: OcrChapter; selected: boolean; onSelect: (id: string) => void }) {
  return <label className={`block cursor-pointer border px-3 py-2 text-xs ${selected ? "border-emerald-500 bg-emerald-50" : "border-zinc-100 hover:bg-zinc-50"}`}><input type="radio" name="ocr-chapter" checked={selected} onChange={() => onSelect(chapter.id)} className="mr-2" />{chapter.number} {chapter.title}<span className="ml-2 text-zinc-400">PDF 第 {chapter.page_start ?? "?"}-{chapter.page_end ?? "?"} 页</span>{chapter.needs_confirmation && <span className="ml-2 text-amber-700">需要确认</span>}</label>;
}

function NotePreview({ markdown }: { markdown: string }) {
  const parts = markdown.split(/```mermaid\n([\s\S]*?)```/g);
  return <article className="mt-5 border-t border-zinc-200 pt-5"><h3 className="text-sm font-semibold">精读 Markdown 预览</h3><div className="prose prose-zinc mt-3 max-w-none text-sm leading-7">{parts.map((part, index) => index % 2 === 1 ? <MermaidBlock key={index} code={part.trim()} /> : <ReactMarkdown key={index}>{part}</ReactMarkdown>)}</div></article>;
}
