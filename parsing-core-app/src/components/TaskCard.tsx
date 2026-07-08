import { useNavigate } from "react-router-dom";
import type { TaskItem } from "../api/types";
import { formatStatus } from "../lib/utils";
import { FileText, Loader2, CheckCircle2, XCircle } from "lucide-react";

export default function TaskCard({ item }: { item: TaskItem }) {
  const navigate = useNavigate();
  const canView = item.status === "COMPLETED";
  const name = item.file_path.split("/").pop() || item.file_path;

  const statusIcon = () => {
    if (item.status === "COMPLETED") return <CheckCircle2 size={11} />;
    if (item.status === "FAILED") return <XCircle size={11} />;
    return <Loader2 size={11} className="animate-spin" />;
  };

  const chipColors = () => {
    if (item.status === "COMPLETED")
      return "bg-green-50 text-green-700 border-green-200 hover:bg-green-100 hover:border-green-300";
    if (item.status === "FAILED")
      return "bg-red-50 text-red-400 border-red-100 cursor-default";
    if (item.status === "CANCELLED")
      return "bg-gray-50 text-gray-400 border-gray-100 cursor-default";
    return "bg-blue-50 text-blue-600 border-blue-100 cursor-default";
  };

  return (
    <button
      onClick={() => canView && navigate(`/doc/${item.task_id}`)}
      disabled={!canView}
      className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition-colors ${chipColors()}`}
    >
      <FileText size={11} />
      <span className="truncate max-w-24">{name}</span>
      <span className="flex items-center gap-0.5 opacity-70">
        {statusIcon()}
        {formatStatus(item.status)}
      </span>
    </button>
  );
}
