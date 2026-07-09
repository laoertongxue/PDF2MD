import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import MermaidEditor from "./MermaidEditor";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function ChapterWorkbench() {
  const {
    chapters,
    loadChapterNoteBlocks,
    loadChapters,
    loadCourses,
    loadSources,
    noteBlocksByChapter,
    selectedCourseId,
  } = useWorkbenchStore();
  const [activeChapterId, setActiveChapterId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");

  const courseChapters = useMemo(
    () => Object.values(chapters).flat().filter((chapter) => !selectedCourseId || chapter.course_id === selectedCourseId),
    [chapters, selectedCourseId],
  );
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

  return (
    <div className="space-y-6 animate-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">章节精读工作台</h1>
          <p className="mt-1 text-sm text-zinc-500">{activeChapter ? activeChapter.title : "查看已生成的章节精读结果"}</p>
        </div>
        <Link to="/workbench/cards" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:border-zinc-300">
          查看卡片池
        </Link>
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
            {courseChapters.map((chapter) => (
              <option key={chapter.id} value={chapter.id}>
                {chapter.seq + 1}. {chapter.title}（{chapter.status}）
              </option>
            ))}
          </select>
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
