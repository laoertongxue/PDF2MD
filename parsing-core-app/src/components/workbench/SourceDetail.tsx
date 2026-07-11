import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";
import ImportTextbooks from "./ImportTextbooks";
import { importSources } from "../../api/workbench";

export default function SourceDetail() {
  const { courses, detectChapters, loadCourses, loadSources, selectedCourseId, sources } = useWorkbenchStore();
  const [error, setError] = useState<string | null>(null);
  const course = selectedCourseId ? courses.find((item) => item.id === selectedCourseId) : null;
  const existingSources = selectedCourseId ? (sources[selectedCourseId] ?? []) : [];

  useEffect(() => {
    if (courses.length === 0) loadCourses().catch((err: unknown) => setError(err instanceof Error ? err.message : "课程加载失败"));
  }, [courses.length, loadCourses]);

  useEffect(() => {
    if (selectedCourseId) loadSources(selectedCourseId).catch((err: unknown) => setError(err instanceof Error ? err.message : "资料加载失败"));
  }, [loadSources, selectedCourseId]);

  if (!course) {
    return (
      <div className="max-w-xl space-y-5 animate-in">
        <div><h1 className="text-xl font-semibold text-zinc-900">导入课程资料</h1><p className="mt-1 text-sm text-zinc-500">当前没有选中的课程，请先回到课程列表选择一个课程。</p></div>
        <Link to="/workbench" className="inline-flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:border-zinc-300"><ArrowLeft size={15} />返回课程列表</Link>
      </div>
    );
  }

  return (
    <div className="max-w-2xl space-y-6 animate-in">
      <div>
        <p className="text-xs font-medium text-zinc-400">当前课程：{course.title}</p>
        <h1 className="mt-1 text-xl font-semibold text-zinc-900">导入课程资料</h1>
        <p className="mt-1 text-sm text-zinc-500">选择或拖放多本教材，系统将逐本导入并识别章节。</p>
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <ImportTextbooks key={course.id} courseId={course.id} currentSources={existingSources} importSources={importSources} detectChapters={detectChapters} loadSources={loadSources} />
      {existingSources.length > 0 && (
        <section aria-labelledby="existing-sources-title" className="border-t border-zinc-200 pt-4">
          <h2 id="existing-sources-title" className="mb-2 text-xs font-medium text-zinc-500">已有教材 · {existingSources.length}</h2>
          <div className="divide-y divide-zinc-100">
            {existingSources.map((source) => <div key={source.id} className="flex min-h-12 items-center justify-between gap-3 py-2"><p className="min-w-0 truncate text-sm font-medium text-zinc-900" title={source.title}>{source.title}</p><span className="shrink-0 text-xs text-zinc-400">{source.status}</span></div>)}
          </div>
        </section>
      )}
    </div>
  );
}
