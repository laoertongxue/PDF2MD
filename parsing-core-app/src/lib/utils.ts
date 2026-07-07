export function cn(...classes: (string | undefined | false | null)[]) {
  return classes.filter(Boolean).join(" ");
}

export function formatStatus(s: string): string {
  const map: Record<string, string> = {
    PENDING: "排队中", PARSING: "解析中", SECTIONING: "切分中",
    LLM_RUNNING: "AI分析中", MERGING: "合流中", COMPLETED: "已完成",
    FAILED: "失败", PARTIAL: "部分完成", CANCELLED: "已取消",
  };
  return map[s] || s;
}

export function statusColor(s: string): string {
  if (s === "COMPLETED") return "text-green-600";
  if (s === "FAILED") return "text-red-500";
  if (s === "LLM_RUNNING" || s === "PARSING") return "text-blue-500";
  return "text-gray-500";
}
