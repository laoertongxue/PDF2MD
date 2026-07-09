import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, FolderOpen, Loader2, PlusCircle } from "lucide-react";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function CourseList() {
  const { courses, createCourse, loadCourseCards, loadCourses, loadSources, selectCourse, selectedCourseId } = useWorkbenchStore();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [rootDir, setRootDir] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadCourses().catch((e: unknown) => setError(e instanceof Error ? e.message : "加载失败")).finally(() => setLoading(false));
  }, [loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    Promise.all([loadSources(selectedCourseId), loadCourseCards(selectedCourseId)]).catch((e: unknown) =>
      setError(e instanceof Error ? e.message : "课程数据加载失败"),
    );
  }, [loadCourseCards, loadSources, selectedCourseId]);

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!title.trim() || !rootDir.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await createCourse(title.trim(), description.trim(), rootDir.trim());
      setTitle("");
      setDescription("");
      setRootDir("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSaving(false);
    }
  };

  const chooseRootDir = async () => {
    setError(null);
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const dir = await invoke<string | null>("pick_directory");
      if (dir) setRootDir(dir);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "无法打开文件夹选择器");
    }
  };

  return (
    <div className="space-y-6 animate-in max-w-2xl">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">课程精读</h1>
        <p className="text-sm text-zinc-500 mt-0.5">管理课程工作台入口</p>
      </div>

      <div className="flex flex-wrap gap-2">
        <Link
          to="/workbench/source"
          className={`rounded-md px-3 py-2 text-sm font-medium ${
            selectedCourseId ? "bg-zinc-900 text-white hover:bg-zinc-800" : "pointer-events-none bg-zinc-100 text-zinc-400"
          }`}
        >
          导入资料
        </Link>
        <Link to="/workbench/cards" className="rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-700 hover:border-zinc-300">
          卡片池
        </Link>
      </div>

      <form onSubmit={submit} className="rounded-lg border border-zinc-200 bg-white p-5 space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs text-zinc-500">课程名称</span>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="战略管理"
              className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">说明</span>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="MBA 课程资料"
              className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-200"
            />
          </label>
        </div>
        <label className="block">
          <span className="text-xs text-zinc-500">本地目录</span>
          <div className="mt-1 flex gap-2">
            <input
              value={rootDir}
              onChange={(e) => setRootDir(e.target.value)}
              placeholder="选择或粘贴课程资料所在文件夹"
              className="min-w-0 flex-1 rounded-md border border-zinc-200 px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-zinc-200"
            />
            <button
              type="button"
              onClick={chooseRootDir}
              className="inline-flex shrink-0 items-center gap-2 rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50"
            >
              <FolderOpen size={15} />
              选择文件夹
            </button>
          </div>
        </label>
        {error && <p className="text-sm text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={saving || !title.trim() || !rootDir.trim()}
          className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 transition-colors active:scale-[0.98] disabled:opacity-50"
        >
          {saving ? <Loader2 size={15} className="animate-spin" /> : <PlusCircle size={15} />}
          创建课程
        </button>
      </form>

      <div className="space-y-3">
        {loading && (
          <div className="rounded-lg border border-zinc-200 bg-white p-5">
            <div className="h-4 w-24 shimmer rounded mb-3" />
            <div className="h-3 w-48 shimmer rounded" />
          </div>
        )}

        {!loading && courses.length === 0 && (
          <div className="rounded-lg border border-dashed border-zinc-300 bg-white py-12 px-8 text-center">
            <BookOpen size={32} className="text-zinc-300 mx-auto mb-3" strokeWidth={1.5} />
            <p className="text-sm font-medium text-zinc-700">还没有课程</p>
            <p className="text-xs text-zinc-400 mt-1">先创建一个课程工作台</p>
          </div>
        )}

        {courses.map((course) => {
          const selected = course.id === selectedCourseId;
          return (
            <button
              key={course.id}
              type="button"
              onClick={() => selectCourse(course.id)}
              className={`block w-full rounded-lg border p-5 text-left transition-colors ${
                selected ? "border-zinc-900 bg-zinc-50" : "border-zinc-200 bg-white hover:border-zinc-300"
              }`}
            >
              <div className="flex items-start gap-3">
                <div
                  className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${
                    selected ? "bg-zinc-900 text-white" : "bg-zinc-100 text-zinc-600"
                  }`}
                >
                  <BookOpen size={17} strokeWidth={1.5} />
                </div>
                <div className="min-w-0 flex-1">
                  <h2 className="text-sm font-medium text-zinc-900 truncate">{course.title}</h2>
                  {course.description && <p className="text-sm text-zinc-500 mt-1">{course.description}</p>}
                  <p className="text-xs font-mono text-zinc-400 mt-2 truncate">{course.root_dir}</p>
                </div>
                {selected && <span className="shrink-0 rounded-full bg-zinc-900 px-2 py-0.5 text-xs font-medium text-white">已选择</span>}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
