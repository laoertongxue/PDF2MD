import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import TaskCard from "./TaskCard";
import { formatStatus, statusColor } from "../lib/utils";
import { PlusCircle, Clock, CheckCircle2, AlertCircle, Loader2, BarChart3 } from "lucide-react";

export default function Dashboard() {
  const { batches, loadBatches } = useStore();
  const navigate = useNavigate();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    loadBatches();
    const timer = setInterval(() => loadBatches(), 3000);
    return () => clearInterval(timer);
  }, [loadBatches]);

  const stats = {
    total: batches.length,
    running: batches.filter((b) => !["COMPLETED", "FAILED", "CANCELLED"].includes(b.status)).length,
    completed: batches.filter((b) => b.status === "COMPLETED").length,
    failed: batches.filter((b) => b.status === "FAILED").length,
  };

  return (
    <div className={`space-y-8 transition-opacity duration-500 ${mounted ? "opacity-100" : "opacity-0"}`}>
      {/* Hero */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">仪表盘</h2>
          <p className="text-sm text-muted mt-1">管理所有文档解析批次</p>
        </div>
        <button
          onClick={() => navigate("/submit")}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-primary-dark transition-colors"
        >
          <PlusCircle size={16} />
          新建批次
        </button>
      </div>

      {/* Stats */}
      {batches.length > 0 && (
        <div className="grid grid-cols-4 gap-4">
          {[
            { label: "全部批次", value: stats.total, icon: BarChart3, color: "text-blue-600 bg-blue-50" },
            { label: "进行中", value: stats.running, icon: Loader2, color: "text-amber-600 bg-amber-50" },
            { label: "已完成", value: stats.completed, icon: CheckCircle2, color: "text-green-600 bg-green-50" },
            { label: "失败", value: stats.failed, icon: AlertCircle, color: "text-red-600 bg-red-50" },
          ].map((s) => {
            const Icon = s.icon;
            return (
              <div key={s.label} className="bg-white rounded-xl border border-gray-200 p-4 flex items-center gap-3">
                <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${s.color}`}>
                  <Icon size={20} />
                </div>
                <div>
                  <p className="text-2xl font-semibold">{s.value}</p>
                  <p className="text-xs text-muted">{s.label}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Empty */}
      {batches.length === 0 && (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-gray-100 mb-4">
            <BarChart3 size={28} className="text-gray-400" />
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-1">暂无批次</h3>
          <p className="text-sm text-muted mb-6 max-w-sm">
            后端服务已就绪，创建第一个解析批次开始处理文档
          </p>
          <button
            onClick={() => navigate("/submit")}
            className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-medium text-white hover:bg-primary-dark transition-colors"
          >
            <PlusCircle size={16} />
            创建第一个批次
          </button>
        </div>
      )}

      {/* Batch List */}
      <div className="space-y-3">
        {batches.map((b) => (
          <div
            key={b.batch_id}
            className="group bg-white rounded-xl border border-gray-200 p-5 hover:border-gray-300 hover:shadow-sm transition-all"
          >
            {/* Top row */}
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <span className="font-mono text-xs text-gray-400 bg-gray-50 px-2 py-0.5 rounded border border-gray-100">
                  {b.batch_id.slice(0, 8)}
                </span>
                <span className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${
                  b.status === "COMPLETED" ? "bg-green-50 text-green-700" :
                  b.status === "FAILED" ? "bg-red-50 text-red-700" :
                  b.status === "CANCELLED" ? "bg-gray-100 text-gray-600" :
                  "bg-blue-50 text-blue-700"
                }`}>
                  {b.status === "LLM_RUNNING" && <Loader2 size={11} className="animate-spin" />}
                  {formatStatus(b.status)}
                </span>
              </div>
              <div className="flex items-center gap-1 text-xs text-muted">
                <Clock size={12} />
                <span>{b.total_tasks} 个文件</span>
              </div>
            </div>

            {/* Progress */}
            <div className="mb-3">
              <div className="flex justify-between text-xs text-muted mb-1.5">
                <span>{b.completed_tasks}/{b.total_tasks} 完成</span>
                <span>{Math.round((b.total_tasks > 0 ? b.completed_tasks / b.total_tasks : 0) * 100)}%</span>
              </div>
              <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-700 ${
                    b.status === "COMPLETED" ? "bg-green-500" :
                    b.status === "FAILED" ? "bg-red-400" :
                    "bg-primary"
                  }`}
                  style={{ width: `${b.total_tasks > 0 ? (b.completed_tasks / b.total_tasks) * 100 : 0}%` }}
                />
              </div>
            </div>

            {/* Task chips */}
            <div className="flex flex-wrap gap-1.5">
              {(b.tasks || []).map((t) => (
                <TaskCard key={t.task_id} item={t} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
