import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import VirtualDoc from "./VirtualDoc";
import { formatStatus, statusColor } from "../lib/utils";

export default function DocViewer() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const { tasks, mergedDocs, loadTask, loadMerged } = useStore();
  const [loading, setLoading] = useState(true);
  const task = taskId ? tasks[taskId] : undefined;
  const md = taskId ? mergedDocs[taskId] : undefined;

  useEffect(() => {
    if (!taskId) return;
    (async () => {
      try {
        await loadTask(taskId);
        await loadMerged(taskId);
      } catch (e) {
        console.error("Failed to load doc:", e);
      }
      setLoading(false);
    })();
  }, [taskId, loadTask, loadMerged]);

  if (loading) return <div className="text-center py-20 text-gray-400">加载中...</div>;
  if (!md) return <div className="text-center py-20 text-gray-400">文档不存在或尚未完成解析</div>;

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => navigate(-1)} className="text-sm text-gray-500 hover:text-gray-700">← 返回</button>
        {task && (
          <span className={`text-sm font-medium ${statusColor(task.status)}`}>
            {formatStatus(task.status)}
          </span>
        )}
      </div>
      <VirtualDoc md={md} />
    </div>
  );
}
