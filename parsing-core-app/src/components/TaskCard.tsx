import { useNavigate } from "react-router-dom";
import type { TaskItem } from "../api/types";
import { formatStatus, statusColor } from "../lib/utils";

export default function TaskCard({ item }: { item: TaskItem }) {
  const navigate = useNavigate();
  const canView = item.status === "COMPLETED";
  const name = item.file_path.split("/").pop() || item.file_path;

  return (
    <button
      onClick={() => canView && navigate(`/doc/${item.task_id}`)}
      disabled={!canView}
      className={`text-xs px-2 py-1 rounded border ${canView ? "border-blue-200 hover:border-blue-400 cursor-pointer" : "border-gray-100 text-gray-400 cursor-default"}`}
    >
      {name}
      <span className={`ml-1 ${statusColor(item.status)}`}>·{formatStatus(item.status)}</span>
    </button>
  );
}
