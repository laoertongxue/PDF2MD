import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";

export default function BatchSubmit() {
  const [paths, setPaths] = useState<string[]>([]);
  const [concurrency, setConcurrency] = useState(4);
  const [submitting, setSubmitting] = useState(false);
  const submitBatch = useStore((s) => s.submitBatch);
  const navigate = useNavigate();

  const pickFiles = async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string[]>("pick_files");
      if (picked && picked.length > 0) {
        setPaths((p) => [...new Set([...p, ...picked])]);
      }
    } catch {
      // fallback: running in browser without Tauri, use prompt
      const input = prompt("输入文件路径（每行一个）:");
      if (input) {
        setPaths((p) => [...p, ...input.split("\n").map((s) => s.trim()).filter(Boolean)]);
      }
    }
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const text = e.clipboardData.getData("text");
    if (text) {
      const newPaths = text.split("\n").map((s) => s.trim()).filter((s) => s.length > 0);
      setPaths((p) => [...new Set([...p, ...newPaths])]);
    }
  };

  const onSubmit = async () => {
    if (paths.length === 0) return;
    setSubmitting(true);
    try {
      await submitBatch(paths, concurrency);
      navigate("/");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <h2 className="text-xl font-semibold mb-4">新建批次</h2>

      <div className="space-y-4">
        <div>
          <p className="text-sm text-gray-600 mb-2">选择要解析的文件</p>
          <button
            onClick={pickFiles}
            className="px-6 py-3 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 flex items-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            选择文件
          </button>
          <p className="text-xs text-gray-400 mt-1">支持 .xlsx .pdf .docx .md .png 等格式</p>
        </div>

        <div>
          <p className="text-sm text-gray-600 mb-1">
            或粘贴文件绝对路径（每行一个，⌘+V）
          </p>
          <textarea
            onPaste={onPaste}
            placeholder={`/Users/xxx/report.xlsx\n/Users/xxx/data.pdf`}
            className="w-full h-24 border rounded-lg p-3 text-sm font-mono resize-y"
            onKeyDown={(e) => {
              if (e.key === "Enter" && e.metaKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
          />
        </div>

        <div>
          <label className="text-sm text-gray-600 mr-2">并发数</label>
          <select
            value={concurrency}
            onChange={(e) => setConcurrency(Number(e.target.value))}
            className="border rounded px-3 py-1.5 text-sm"
          >
            {[1, 2, 4, 8, 16].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </div>

        {paths.length > 0 && (
          <div>
            <h3 className="text-sm font-medium mb-2">
              待提交 <span className="text-blue-600">{paths.length}</span> 个文件
              <button onClick={() => setPaths([])} className="ml-3 text-xs text-gray-400 hover:text-red-500">
                清空
              </button>
            </h3>
            <ul className="text-xs text-gray-600 space-y-1 max-h-48 overflow-y-auto bg-gray-50 rounded-lg p-3">
              {paths.map((p, i) => (
                <li key={i} className="flex items-center justify-between hover:bg-gray-100 rounded px-1 py-0.5">
                  <span className="truncate font-mono">{p}</span>
                  <button
                    onClick={() => setPaths(paths.filter((_, j) => j !== i))}
                    className="ml-2 text-gray-400 hover:text-red-500 shrink-0"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <button
          onClick={onSubmit}
          disabled={paths.length === 0 || submitting}
          className={`px-8 py-2.5 rounded-lg text-sm font-medium text-white ${
            paths.length === 0 || submitting
              ? "bg-gray-300 cursor-not-allowed"
              : "bg-green-600 hover:bg-green-700"
          }`}
        >
          {submitting ? "提交中..." : `提交批次 (${paths.length} 个文件)`}
        </button>
      </div>
    </div>
  );
}
