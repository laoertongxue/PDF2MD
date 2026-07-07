import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import TaskCard from "./TaskCard";
import { statusColor, formatStatus } from "../lib/utils";

export default function Dashboard() {
  const { batches, loadBatches } = useStore();
  const navigate = useNavigate();

  useEffect(() => {
    loadBatches();
    const timer = setInterval(() => loadBatches(), 3000);
    return () => clearInterval(timer);
  }, [loadBatches]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-semibold">批次列表</h2>
        <button
          onClick={() => navigate("/submit")}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
        >
          + 新建批次
        </button>
      </div>
      {batches.length === 0 && (
        <p className="text-gray-400">暂无批次，请先启动 parsing-core serve 并新建批次</p>
      )}
      <div className="space-y-3">
        {batches.map((b) => (
          <div key={b.batch_id} className="bg-white rounded-lg border p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm text-gray-500 font-mono">{b.batch_id.slice(0, 8)}...</span>
              <span className={`text-sm font-medium ${statusColor(b.status)}`}>
                {formatStatus(b.status)}
              </span>
            </div>
            <div className="w-full bg-gray-100 rounded-full h-2 mb-2">
              <div
                className="bg-blue-500 h-2 rounded-full transition-all"
                style={{ width: `${b.total_tasks > 0 ? (b.completed_tasks / b.total_tasks) * 100 : 0}%` }}
              />
            </div>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span>{b.completed_tasks}/{b.total_tasks} 完成</span>
              <div className="flex flex-wrap gap-1">
                {(b.tasks || []).map((t) => (
                  <TaskCard key={t.task_id} item={t} />
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
