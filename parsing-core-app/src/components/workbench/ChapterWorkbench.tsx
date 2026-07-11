import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Loader2, Sparkles } from "lucide-react";
import MermaidEditor from "./MermaidEditor";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import { chapterOptionLabel, createSourceChapterGroups } from "./SourceChapterTree";

export default function ChapterWorkbench() {
  const {
    chapters,
    loadChapterNoteBlocks,
    loadChapters,
    loadCourses,
    loadSources,
    noteBlocksByChapter,
    runHybridChapter,
    selectedCourseId,
    sources,
  } = useWorkbenchStore();
  const [activeChapterId, setActiveChapterId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runningHybrid, setRunningHybrid] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");

  const chapterGroups = useMemo(
    () => createSourceChapterGroups(selectedCourseId ? (sources[selectedCourseId] ?? []) : [], chapters),
    [chapters, selectedCourseId, sources],
  );
  const courseChapters = useMemo(() => chapterGroups.flatMap((group) => group.chapters), [chapterGroups]);
  const activeChapter = courseChapters.find((chapter) => chapter.id === activeChapterId) ?? null;
  const blocks = activeChapterId ? (noteBlocksByChapter[activeChapterId] ?? []) : [];
  const knowledge = blocks.find((block) => block.kind === "knowledge_mermaid");
  const application = blocks.find((block) => block.kind === "application_mermaid");

  useEffect(() => {
    loadCourses().catch((err: unknown) => setError(err instanceof Error ? err.message : "课程加载失败"));
  }, [loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    loadSources(selectedCourseId)
      .then((sources) => Promise.all(sources.map((source) => loadChapters(source.id))))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "章节加载失败"));
  }, [loadChapters, loadSources, selectedCourseId]);

  useEffect(() => {
    let cancelled = false;

    async function chooseChapter() {
      if (courseChapters.length === 0) {
        setActiveChapterId(null);
        return;
      }

      const requested = requestedChapterId ? courseChapters.find((chapter) => chapter.id === requestedChapterId) : null;
      if (requested) {
        await loadChapterNoteBlocks(requested.id);
        if (!cancelled) setActiveChapterId(requested.id);
        return;
      }

      for (const chapter of courseChapters) {
        if ((noteBlocksByChapter[chapter.id] ?? []).length > 0) {
          setActiveChapterId(chapter.id);
          return;
        }
      }

      for (const chapter of courseChapters) {
        const blocks = await loadChapterNoteBlocks(chapter.id);
        if (cancelled) return;
        if (blocks.length > 0) {
          setActiveChapterId(chapter.id);
          return;
        }
      }

      const fallback = courseChapters.find((chapter) => chapter.status === "CONFIRMED" || chapter.status === "COMPLETED");
      if (!fallback) {
        setActiveChapterId(null);
        return;
      }
      await loadChapterNoteBlocks(fallback.id);
      if (!cancelled) setActiveChapterId(fallback.id);
    }

    chooseChapter().catch((err: unknown) => setError(err instanceof Error ? err.message : "精读结果加载失败"));
    return () => {
      cancelled = true;
    };
  }, [courseChapters, loadChapterNoteBlocks, requestedChapterId]);

  const chooseChapter = (chapterId: string) => {
    setActiveChapterId(chapterId);
    setSearchParams({ chapterId });
    loadChapterNoteBlocks(chapterId).catch((err: unknown) => setError(err instanceof Error ? err.message : "精读结果加载失败"));
  };

  const canRunHybrid = activeChapter?.status === "CONFIRMED" || activeChapter?.status === "FAILED";

  const runHybrid = async () => {
    if (!activeChapterId) return;
    setRunningHybrid(true);
    setError(null);
    try {
      await runHybridChapter(activeChapterId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "混合精读启动失败");
    } finally {
      setRunningHybrid(false);
    }
  };

  return (
    <div className="space-y-6 animate-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">章节精读工作台</h1>
          <p className="mt-1 text-sm text-zinc-500">{activeChapter ? activeChapter.title : "查看已生成的章节精读结果"}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={runHybrid}
            disabled={!activeChapterId || !canRunHybrid || runningHybrid}
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
          >
            {runningHybrid ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
            混合精读
          </button>
          <Link to="/workbench/cards" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:border-zinc-300">
            查看卡片池
          </Link>
        </div>
      </div>

      {error && <p className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>}

      {courseChapters.length > 0 && (
        <label className="block max-w-xl">
          <span className="text-xs text-zinc-500">选择章节</span>
          <select
            value={activeChapterId ?? ""}
            onChange={(event) => chooseChapter(event.target.value)}
            className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          >
            <option value="" disabled>
              选择一个章节
            </option>
            {chapterGroups.map(({ source, chapters: sourceChapters }) => (
              <optgroup key={source.id} label={`《${source.title}》`}>
                {sourceChapters.map((chapter) => <option key={chapter.id} value={chapter.id}>{chapterOptionLabel(source, chapter)}（{chapter.status}）</option>)}
              </optgroup>
            ))}
          </select>
          <span className="mt-1 block text-xs text-zinc-400">仅 CONFIRMED 或 FAILED 章节可执行混合精读。</span>
        </label>
      )}

      {!knowledge && !application ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-white px-8 py-12 text-center">
          <p className="text-sm font-medium text-zinc-700">还没有精读结果</p>
          <p className="mt-1 text-xs text-zinc-400">先确认章节并运行精读。</p>
          <Link to="/workbench/chapters" className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white">
            去章节确认
          </Link>
        </div>
      ) : (
        <>
          {knowledge && (
            <section className="rounded-lg border border-zinc-200 bg-white p-5">
              <MermaidEditor title={knowledge.title} initial={knowledge.body} />
            </section>
          )}

          {application && (
            <section className="rounded-lg border border-zinc-200 bg-white p-5">
              <MermaidEditor title={application.title} initial={application.body} />
            </section>
          )}
        </>
      )}
    </div>
  );
}
