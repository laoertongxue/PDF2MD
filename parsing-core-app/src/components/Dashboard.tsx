import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import { formatStatus } from "../lib/utils";
import { PlusCircle, Clock, CheckCircle2, AlertCircle, Loader2, FileText, ChevronRight } from "lucide-react";

function Skeleton() {
  return (
    <div className="space-y-3">
      {[1, 2, 3].map((i) => (
        <div key={i} className="rounded-xl border border-border bg-white p-5">
          <div className="flex items-center justify-between mb-4">
            <div className="h-5 w-24 animate-shimmer rounded-md" />
            <div className="h-5 w-16 animate-shimmer rounded-full" />
          </div>
          <div className="h-1.5 w-full animate-shimmer rounded-full mb-3" />
          <div className="flex gap-2">
            <div className="h-6 w-32 animate-shimmer rounded-full" />
            <div className="h-6 w-20 animate-shimmer rounded-full" />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const { batches, loadBatches } = useStore();
  const navigate = useNavigate();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    loadBatches().then(() => setTimeout(() => setReady(true), 200));
    const t = setInterval(() => loadBatches(), 4000);
    return () => clearInterval(t);
  }, [loadBatches]);

  if (!ready) return <Skeleton />;

  const stats = [
    { label: "全部批次", value: batches.length, icon: FileText, color: "text-indigo-600 bg-indigo-50" },
    { label: "进行中", value: batches.filter((b) => !["COMPLETED", "FAILED", "CANCELLED"].includes(b.status)).length, icon: Loader2, color: "text-amber-600 bg-amber-50" },
    { label: "已完成", value: batches.filter((b) => b.status === "COMPLETED").length, icon: CheckCircle2, color: "text-emerald-600 bg-emerald-50" },
    { label: "失败", value: batches.filter((b) => b.status === "FAILED").length, icon: AlertCircle, color: "text-red-600 bg-red-50" },
  ];

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-end justify-between">
        <div>
          <h2 className="text-[22px] font-semibold tracking-tight text-gray-900">仪表盘</h2>
          <p className="text-[13px] text-muted mt-0.5">管理所有文档解析批次</p>
        </div>
        <button
          onClick={() => navigate("/submit")}
          className="inline-flex items-center gap-2 rounded-xl bg-accent px-5 py-2.5 text-[13px] font-semibold text-white shadow-sm shadow-accent/20 hover:bg-accent-dark transition-all duration-200 hover:shadow-md hover:shadow-accent/25 active:scale-[0.98]"
        >
          <PlusCircle size={17} strokeWidth={2} />
          新建批次
        </button>
      </div>

      {/* Stats */}
      {batches.length > 0 && (
        <div className="grid grid-cols-4 gap-3">
          {stats.map(({ label, value, icon: Icon, color }) => (
            <div
              key={label}
              className="rounded-xl border border-border bg-white p-4 animate-fade-in"
              style={{ animationDelay: `${stats.indexOf({ label, value, icon: Icon, color }) * 80}ms` }}
            >
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-[28px] font-bold tracking-tight text-gray-900 leading-none mb-1">{value}</p>
                  <p className="text-[12px] text-muted font-medium">{label}</p>
                </div>
                <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${color}`}>
                  <Icon size={18} strokeWidth={2} />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {batches.length === 0 && (
        <div className="flex flex-col items-center justify-center py-24 text-center animate-fade-in">
          <div className="flex h-20 w-20 items-center justify-center rounded-2xl bg-gray-100 mb-5 animate-pulse-glow">
            <FileText size={34} className="text-gray-300" strokeWidth={1.5} />
          </div>
          <h3 className="text-lg font-semibold text-gray-900 mb-1">欢迎使用 PDF2MD</h3>
          <p className="text-[13px] text-muted mb-8 max-w-xs">
            将 Excel / PDF / Word 批量转换为结构化 Markdown，AI 自动生成解读与图表
          </p>
          <button
            onClick={() => navigate("/submit")}
            className="inline-flex items-center gap-2 rounded-xl bg-accent px-6 py-3 text-[14px] font-semibold text-white shadow-md shadow-accent/20 hover:bg-accent-dark hover:shadow-lg hover:shadow-accent/25 transition-all duration-200 active:scale-[0.98]"
          >
            <PlusCircle size={18} strokeWidth={2} />
            开始解析第一个文档
          </button>
        </div>
      )}

      {/* Batch cards */}
      {batches.map((b, idx) => {
        const pct = b.total_tasks > 0 ? Math.round((b.completed_tasks / b.total_tasks) * 100) : 0;
        const isDone = b.status === "COMPLETED";
        const isFail = b.status === "FAILED";
        const badgeClass = isDone ? "bg-emerald-50 text-emerald-700" : isFail ? "bg-red-50 text-red-700" : b.status === "CANCELLED" ? "bg-gray-100 text-gray-500" : "bg-blue-50 text-blue-700";

        return (
          <div
            key={b.batch_id}
            className="rounded-xl border border-border bg-white p-5 transition-all duration-200 hover:border-gray-300 hover:shadow-sm cursor-pointer animate-slide-up group"
            style={{ animationDelay: `${idx * 60}ms` }}
            onClick={() => {
              if (b.tasks?.some((t) => t.status === "COMPLETED")) {
                const first = b.tasks.find((t) => t.status === "COMPLETED");
                if (first) navigate(`/doc/${first.task_id}`);
              }
            }}
          >
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <span className="font-mono text-[11px] text-muted bg-gray-50 px-2 py-0.5 rounded-md border border-border/60 tracking-tight">
                  {b.batch_id.slice(0, 8)}
                </span>
                <span className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full ${badgeClass}`}>
                  {!isDone && !isFail && b.status !== "CANCELLED" && <Loader2 size={11} className="animate-spin" />}
                  {formatStatus(b.status)}
                </span>
              </div>
              <div className="flex items-center gap-1.5 text-[11px] text-muted">
                <Clock size={12} />
                <span>{b.total_tasks} 个文件</span>
                <ChevronRight size={14} className="opacity-0 group-hover:opacity-100 transition-opacity -mr-1" />
              </div>
            </div>

            {/* Progress bar */}
            <div className="mb-3">
              <div className="flex justify-between text-[11px] text-muted mb-1.5 font-medium">
                <span>{b.completed_tasks}/{b.total_tasks} 完成</span>
                <span>{pct}%</span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-gray-100 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-700 ease-out ${
                    isDone ? "bg-emerald-500" : isFail ? "bg-red-400" : "bg-accent"
                  }`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>

            {/* Task pills */}
            <div className="flex flex-wrap gap-1.5">
              {(b.tasks || []).map((t) => {
                const name = t.file_path.split("/").pop() || t.file_path;
                const done = t.status === "COMPLETED";
                return (
                  <span
                    key={t.task_id}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (done) navigate(`/doc/${t.task_id}`);
                    }}
                    className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full border font-medium transition-colors ${
                      done
                        ? "bg-emerald-50 text-emerald-700 border-emerald-200 hover:bg-emerald-100 cursor-pointer"
                        : t.status === "FAILED"
                        ? "bg-red-50 text-red-500 border-red-100"
                        : "bg-gray-50 text-gray-500 border-gray-100"
                    }`}
                  >
                    <span className="truncate max-w-[100px]">{name}</span>
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
