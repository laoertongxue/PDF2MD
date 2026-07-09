import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

export default function CardPool() {
  const { cardsByCourse, loadCourseCards, loadCourses, selectedCourseId } = useWorkbenchStore();
  const [error, setError] = useState<string | null>(null);
  const cards = selectedCourseId ? (cardsByCourse[selectedCourseId] ?? []) : [];

  useEffect(() => {
    loadCourses().catch((err: unknown) => setError(err instanceof Error ? err.message : "课程加载失败"));
  }, [loadCourses]);

  useEffect(() => {
    if (!selectedCourseId) return;
    loadCourseCards(selectedCourseId).catch((err: unknown) => setError(err instanceof Error ? err.message : "卡片加载失败"));
  }, [loadCourseCards, selectedCourseId]);

  return (
    <div className="space-y-5 animate-in">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">课程卡片池</h1>
        <p className="mt-1 text-sm text-zinc-500">章节精读沉淀的卡片会汇总到这里。</p>
      </div>

      {error && <p className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>}

      {cards.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-white px-8 py-12 text-center">
          <p className="text-sm font-medium text-zinc-700">暂无卡片</p>
          <p className="mt-1 text-xs text-zinc-400">先运行章节精读生成选题卡。</p>
          <Link to="/workbench/chapter" className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white">
            返回章节精读
          </Link>
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {cards.map((card) => (
            <article key={card.id} className="rounded-lg border border-zinc-200 bg-white p-4">
              <div className="mb-2 flex items-center justify-between gap-3">
                <h2 className="truncate text-sm font-medium text-zinc-900">{card.title}</h2>
                <span className="rounded bg-zinc-100 px-2 py-0.5 text-xs text-zinc-500">{card.kind}</span>
              </div>
              <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-600">{card.body}</p>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
