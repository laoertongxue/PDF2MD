import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import { formatStatus } from "../lib/utils";
import { PlusCircle, FileText, Loader2, CheckCircle2, AlertCircle, Clock, ArrowRight } from "lucide-react";

export default function Dashboard() {
  const { batches, loadBatches } = useStore();
  const navigate = useNavigate();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    loadBatches().then(() => setTimeout(() => setReady(true), 150));
    const t = setInterval(() => loadBatches(), 5000);
    return () => clearInterval(t);
  }, [loadBatches]);

  if (!ready) {
    return (
      <div className="space-y-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="rounded-lg border border-zinc-200 bg-white p-5">
            <div className="flex items-center justify-between mb-3">
              <div className="h-4 w-20 shimmer rounded" />
              <div className="h-4 w-14 shimmer rounded-full" />
            </div>
            <div className="h-2 w-full shimmer rounded-full mb-3" />
            <div className="flex gap-1.5">
              <div className="h-6 w-24 shimmer rounded-full" />
              <div className="h-6 w-16 shimmer rounded-full" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  const stats = [
    { label: "全部", value: batches.length, icon: FileText },
    { label: "进行中", value: batches.filter((b) => !["COMPLETED", "FAILED", "CANCELLED"].includes(b.status)).length, icon: Loader2 },
    { label: "已完成", value: batches.filter((b) => b.status === "COMPLETED").length, icon: CheckCircle2 },
    { label: "失败", value: batches.filter((b) => b.status === "FAILED").length, icon: AlertCircle },
  ];

  return (
    <div className="space-y-6 animate-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-900">仪表盘</h1>
          <p className="text-sm text-zinc-500 mt-0.5">管理所有文档解析批次</p>
        </div>
        <button
          onClick={() => navigate("/submit")}
          className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 transition-colors active:scale-[0.98]"
        >
          <PlusCircle size={16} /> 新建批次
        </button>
      </div>

      {/* Stats */}
      {batches.length > 0 && (
        <div className="grid grid-cols-4 gap-3">
          {stats.map((s, i) => {
            const Icon = s.icon;
            return (
              <div key={s.label} className="rounded-lg border border-zinc-200 bg-white px-4 py-3.5 animate-in" style={{ animationDelay: `${i * 0.05}s` }}>
                <div className="flex items-center gap-2.5">
                  <Icon size={16} className="text-zinc-400" strokeWidth={1.5} />
                  <span className="text-xs text-zinc-500">{s.label}</span>
                </div>
                <p className="text-2xl font-semibold text-zinc-900 mt-1.5">{s.value}</p>
              </div>
            );
          })}
        </div>
      )}

      {/* Empty */}
      {batches.length === 0 && (
        <div className="rounded-lg border border-dashed border-zinc-300 bg-white py-16 px-8 text-center animate-in">
          <FileText size={36} className="text-zinc-300 mx-auto mb-4" strokeWidth={1} />
          <h2 className="text-base font-medium text-zinc-700 mb-1">还没有批次</h2>
          <p className="text-sm text-zinc-400 mb-6">提交第一批文档开始解析</p>
          <button
            onClick={() => navigate("/submit")}
            className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-zinc-800 transition-colors active:scale-[0.98]"
          >
            <PlusCircle size={16} /> 创建第一个批次
          </button>
        </div>
      )}

      {/* Batch cards */}
      {batches.map((b, i) => {
        const pct = b.total_tasks ? Math.round((b.completed_tasks / b.total_tasks) * 100) : 0;
        const done = b.status === "COMPLETED";
        const fail = b.status === "FAILED";
        const badge = done ? "bg-emerald-50 text-emerald-700" : fail ? "bg-red-50 text-red-600" : b.status === "CANCELLED" ? "bg-zinc-100 text-zinc-500" : "bg-blue-50 text-blue-600";

        return (
          <div
            key={b.batch_id}
            className="rounded-lg border border-zinc-200 bg-white p-5 hover:border-zinc-300 transition-colors cursor-pointer animate-in group"
            style={{ animationDelay: `${i * 0.06}s` }}
            onClick={() => {
              const first = b.tasks?.find((t) => t.status === "COMPLETED");
              if (first) navigate(`/doc/${first.task_id}`);
            }}
          >
            {/* Top row */}
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2.5">
                <span className="text-xs font-mono text-zinc-400 bg-zinc-50 px-2 py-0.5 rounded border border-zinc-200">
                  {b.batch_id.slice(0, 8)}
                </span>
                <span className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${badge}`}>
                  {!done && !fail && b.status !== "CANCELLED" && <Loader2 size={10} className="animate-spin" />}
                  {formatStatus(b.status)}
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-xs text-zinc-400">
                <Clock size={12} />
                {b.total_tasks} 文件
                <ArrowRight size={14} className="opacity-0 group-hover:opacity-100 transition-opacity -ml-0.5" />
              </div>
            </div>

            {/* Progress */}
            <div className="mb-3">
              <div className="flex justify-between text-xs text-zinc-400 mb-1.5">
                <span>{b.completed_tasks}/{b.total_tasks} 完成</span>
                <span>{pct}%</span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-zinc-100 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-700 ${done ? "bg-emerald-500" : fail ? "bg-red-400" : "bg-zinc-800"}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>

            {/* Task chips */}
            <div className="flex flex-wrap gap-1.5">
              {b.tasks?.map((t) => {
                const name = t.file_path.split("/").pop() || t.file_path;
                const ok = t.status === "COMPLETED";
                return (
                  <span
                    key={t.task_id}
                    onClick={(e) => { e.stopPropagation(); if (ok) navigate(`/doc/${t.task_id}`); }}
                    className={`inline-flex items-center text-[11px] px-2 py-0.5 rounded-full border transition-colors ${
                      ok ? "bg-zinc-50 text-zinc-600 border-zinc-200 hover:bg-zinc-100 cursor-pointer"
                        : t.status === "FAILED" ? "bg-red-50 text-red-500 border-red-100"
                        : "bg-zinc-50 text-zinc-400 border-zinc-100"
                    }`}
                  >
                    <span className="truncate max-w-[80px]">{name}</span>
                  </span>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
