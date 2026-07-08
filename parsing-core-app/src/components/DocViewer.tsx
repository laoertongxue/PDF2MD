import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import VirtualDoc from "./VirtualDoc";
import { formatStatus } from "../lib/utils";
import { ArrowLeft, Loader2, FileWarning, FileText } from "lucide-react";

export default function DocViewer() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const { tasks, mergedDocs, loadTask, loadMerged } = useStore();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const task = taskId ? tasks[taskId] : undefined;
  const md = taskId ? mergedDocs[taskId] : undefined;

  useEffect(() => {
    if (!taskId) return;
    (async () => {
      setLoading(true);
      setError("");
      try {
        await loadTask(taskId);
        await loadMerged(taskId);
      } catch (e: any) {
        setError(e.message || "加载失败");
      }
      setLoading(false);
    })();
  }, [taskId]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-32 animate-fade-in">
        <Loader2 size={28} className="animate-spin text-accent mb-4" />
        <p className="text-[13px] text-muted">正在加载文档...</p>
      </div>
    );
  }

  if (error || !md) {
    return (
      <div className="flex flex-col items-center justify-center py-32 animate-fade-in">
        <FileWarning size={40} className="text-amber-400 mb-4" strokeWidth={1.5} />
        <h3 className="text-[16px] font-semibold text-gray-900 mb-1">文档不可用</h3>
        <p className="text-[13px] text-muted mb-6">{error || "文档尚未完成解析或已被删除"}</p>
        <button
          onClick={() => navigate(-1)}
          className="inline-flex items-center gap-2 rounded-xl border border-gray-200 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 transition-colors"
        >
          <ArrowLeft size={14} />返回
        </button>
      </div>
    );
  }

  return (
    <div className="animate-fade-in">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate(-1)} className="text-muted hover:text-gray-700 transition-colors">
            <ArrowLeft size={18} />
          </button>
          <div className="h-5 w-px bg-gray-200" />
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/10">
              <FileText size={14} className="text-accent" />
            </div>
            <span className="text-[14px] font-semibold text-gray-900">文档查看器</span>
          </div>
        </div>
        {task && (
          <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-full ${
            task.status === "COMPLETED" ? "bg-emerald-50 text-emerald-700" : "bg-blue-50 text-blue-700"
          }`}>
            {formatStatus(task.status)}
          </span>
        )}
      </div>

      <VirtualDoc md={md} />
    </div>
  );
}
