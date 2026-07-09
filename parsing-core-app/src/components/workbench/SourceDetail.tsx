import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, Loader2, Search } from "lucide-react";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function SourceDetail() {
  const { addSource, courses, detectChapters, loadCourses, loadSources, selectedCourseId, sources } = useWorkbenchStore();
  const [title, setTitle] = useState("");
  const [filePath, setFilePath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const course = selectedCourseId ? courses.find((item) => item.id === selectedCourseId) : null;
  const existingSources = selectedCourseId ? (sources[selectedCourseId] ?? []) : [];

  useEffect(() => {
    if (courses.length === 0) {
      loadCourses().catch((err: unknown) => setError(err instanceof Error ? err.message : "课程加载失败"));
    }
  }, [courses.length, loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    loadSources(selectedCourseId).catch((err: unknown) => setError(err instanceof Error ? err.message : "资料加载失败"));
  }, [loadSources, selectedCourseId]);

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!course || !title.trim() || !filePath.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const source = await addSource(course.id, filePath.trim(), title.trim());
      await detectChapters(source.id);
      navigate("/workbench/chapters");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "资料导入失败");
    } finally {
      setSaving(false);
    }
  };

  if (!course) {
    return (
      <div className="max-w-xl space-y-5 animate-in">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">导入课程资料</h1>
          <p className="mt-1 text-sm text-zinc-500">当前没有选中的课程，请先回到课程列表选择一个课程。</p>
        </div>
        <Link
          to="/workbench"
          className="inline-flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-4 py-2 text-sm font-medium text-zinc-700 hover:border-zinc-300"
        >
          <ArrowLeft size={15} />
          返回课程列表
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-2xl space-y-6 animate-in">
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-zinc-400">当前课程：{course.title}</p>
        <h1 className="mt-1 text-xl font-semibold text-zinc-900">导入课程资料</h1>
        <p className="mt-1 text-sm text-zinc-500">录入主资料标题和本地路径后识别章节。</p>
      </div>

      <form onSubmit={submit} className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
        <label className="block">
          <span className="text-xs text-zinc-500">资料标题</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="战略管理教材第 1 部分"
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">本地路径</span>
          <input
            value={filePath}
            onChange={(e) => setFilePath(e.target.value)}
            placeholder="~/.local/share/parsing-core/workbench-courses/战略管理/strategy.pdf"
            className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
        </label>
        {error && <p className="text-sm text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={saving || !title.trim() || !filePath.trim()}
          className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-50"
        >
          {saving ? <Loader2 size={15} className="animate-spin" /> : <Search size={15} />}
          识别章节
        </button>
      </form>

      {existingSources.length > 0 && (
        <section className="rounded-lg border border-zinc-200 bg-white">
          <div className="border-b border-zinc-100 px-4 py-3 text-xs font-medium text-zinc-400">已有资料</div>
          {existingSources.map((source) => (
            <div key={source.id} className="border-b border-zinc-100 px-4 py-3 last:border-b-0">
              <p className="text-sm font-medium text-zinc-900">{source.title}</p>
              <p className="mt-1 truncate font-mono text-xs text-zinc-400">{source.file_path}</p>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
