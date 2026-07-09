import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Loader2, PlayCircle, Sparkles } from "lucide-react";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import type { Chapter } from "../../api/workbenchTypes";

type ChapterWithMeta = Chapter & {
  page?: string | number;
  pages?: string | number;
  page_range?: string;
  confidence?: number;
};

function pageLabel(chapter: ChapterWithMeta) {
  return chapter.page_range ?? chapter.pages ?? chapter.page ?? "-";
}

function confidenceLabel(chapter: ChapterWithMeta) {
  return typeof chapter.confidence === "number" ? `${Math.round(chapter.confidence * 100)}%` : "-";
}

export default function ChapterConfirm() {
  const { chapters, confirmChapter, loadChapters, loadCourses, loadSources, runChapter, runHybridChapter, selectedCourseId, sources } =
    useWorkbenchStore();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const visibleChapters = useMemo(
    () => Object.values(chapters).flat().filter((chapter) => !selectedCourseId || chapter.course_id === selectedCourseId),
    [chapters, selectedCourseId],
  );

  useEffect(() => {
    loadCourses().catch((err: unknown) => setError(err instanceof Error ? err.message : "课程加载失败"));
  }, [loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    loadSources(selectedCourseId)
      .then((items) => Promise.all(items.map((source) => loadChapters(source.id))))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "章节加载失败"));
  }, [loadChapters, loadSources, selectedCourseId]);

  useEffect(() => {
    if (!selectedCourseId) return;
    const items = sources[selectedCourseId] ?? [];
    Promise.all(items.map((source) => loadChapters(source.id))).catch((err: unknown) =>
      setError(err instanceof Error ? err.message : "章节加载失败"),
    );
  }, [loadChapters, selectedCourseId, sources]);

  const confirm = async (chapterId: string) => {
    setBusyId(chapterId);
    setError(null);
    try {
      await confirmChapter(chapterId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "章节确认失败");
    } finally {
      setBusyId(null);
    }
  };

  const run = async (chapterId: string) => {
    setBusyId(chapterId);
    setError(null);
    try {
      await runChapter(chapterId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "精读任务启动失败");
    } finally {
      setBusyId(null);
    }
  };

  const runHybrid = async (chapterId: string) => {
    setBusyId(chapterId);
    setError(null);
    try {
      await runHybridChapter(chapterId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "混合精读启动失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-5 animate-in">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">章节确认</h1>
          <p className="mt-1 text-sm text-zinc-500">确认教材章节后，可以启动章节精读。</p>
        </div>
        <Link to="/workbench/chapter" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:border-zinc-300">
          打开精读工作台
        </Link>
      </div>

      {error && <p className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>}

      {visibleChapters.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-white px-8 py-12 text-center">
          <p className="text-sm font-medium text-zinc-700">还没有可确认的章节</p>
          <p className="mt-1 text-xs text-zinc-400">先导入课程资料并识别章节。</p>
          <Link to="/workbench/source" className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white">
            导入资料
          </Link>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-200 bg-white">
          <div className="min-w-[760px]">
            <div className="grid grid-cols-[minmax(0,1fr)_80px_80px_100px_320px] gap-3 border-b border-zinc-100 px-4 py-3 text-xs font-medium text-zinc-400">
              <span>章节</span>
              <span>页码</span>
              <span>置信度</span>
              <span>状态</span>
              <span className="text-right">操作</span>
            </div>
            {visibleChapters.map((chapter) => {
              const meta = chapter as ChapterWithMeta;
              const busy = busyId === chapter.id;
              const canRun = chapter.status === "CONFIRMED" || chapter.status === "COMPLETED";
              const canRunHybrid = chapter.status === "CONFIRMED" || chapter.status === "FAILED";
              return (
                <div
                  key={chapter.id}
                  className="grid grid-cols-[minmax(0,1fr)_80px_80px_100px_320px] items-center gap-3 border-b border-zinc-100 px-4 py-3 last:border-b-0"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-zinc-900">
                      {chapter.seq + 1}. {chapter.title}
                    </p>
                  </div>
                  <span className="text-xs text-zinc-500">{pageLabel(meta)}</span>
                  <span className="text-xs text-zinc-500">{confidenceLabel(meta)}</span>
                  <span className="text-xs text-zinc-500">{chapter.status}</span>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Link
                      to={`/workbench/chapter?chapterId=${chapter.id}`}
                      className="inline-flex items-center rounded-md border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 hover:border-zinc-300"
                    >
                      查看
                    </Link>
                    <button
                      type="button"
                      onClick={() => confirm(chapter.id)}
                      disabled={busy}
                      className="inline-flex items-center gap-1.5 rounded-md border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 hover:border-zinc-300 disabled:opacity-50"
                    >
                      {busy ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                      确认
                    </button>
                    <button
                      type="button"
                      onClick={() => run(chapter.id)}
                      disabled={busy || !canRun}
                      title={canRun ? undefined : "请先确认章节"}
                      className="inline-flex items-center gap-1.5 rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-800 disabled:opacity-50"
                    >
                      <PlayCircle size={14} />
                      运行精读
                    </button>
                    <button
                      type="button"
                      onClick={() => runHybrid(chapter.id)}
                      disabled={busy || !canRunHybrid}
                      title={canRunHybrid ? undefined : "仅支持已确认或失败章节"}
                      className="inline-flex items-center gap-1.5 rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-200 disabled:opacity-50"
                    >
                      {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                      混合精读
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
