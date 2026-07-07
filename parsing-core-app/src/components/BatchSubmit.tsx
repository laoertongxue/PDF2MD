import { useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";

export default function BatchSubmit() {
  const [paths, setPaths] = useState<string[]>([]);
  const [concurrency, setConcurrency] = useState(4);
  const [submitting, setSubmitting] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const submitBatch = useStore((s) => s.submitBatch);
  const navigate = useNavigate();

  const addPaths = (newPaths: string[]) => setPaths((p) => [...new Set([...p, ...newPaths])]);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files: string[] = [];
    for (let i = 0; i < e.dataTransfer.files.length; i++) {
      const f = e.dataTransfer.files[i] as File & { path?: string };
      files.push(f.path || f.name);
    }
    addPaths(files);
  }, []);

  const onPaste = useCallback((e: React.ClipboardEvent) => {
    const text = e.clipboardData.getData("text");
    if (text) {
      addPaths(text.split("\n").map((s) => s.trim()).filter(Boolean));
    }
  }, []);

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
      <div
        className={`border-2 border-dashed rounded-lg p-8 mb-4 text-center transition-colors ${dragOver ? "border-blue-400 bg-blue-50" : "border-gray-300"}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onPaste={onPaste}
        tabIndex={0}
        style={{ outline: "none" }}
      >
        <p className="text-gray-500 mb-2">拖拽文件到此处，或粘贴文件路径</p>
        <p className="text-xs text-gray-400">支持 .xlsx .pdf .docx .md .png 等</p>
      </div>

      <div className="mb-4">
        <label className="text-sm text-gray-600 mr-2">并发数</label>
        <input
          type="number" min={1} max={32} value={concurrency}
          onChange={(e) => setConcurrency(Number(e.target.value))}
          className="w-20 border rounded px-2 py-1 text-sm"
        />
      </div>

      {paths.length > 0 && (
        <div className="mb-4">
          <h3 className="text-sm font-medium mb-2">待提交 {paths.length} 个文件</h3>
          <ul className="text-xs text-gray-500 space-y-1 max-h-40 overflow-y-auto">
            {paths.map((p, i) => (
              <li key={i} className="flex items-center justify-between truncate">
                <span className="truncate">{p}</span>
                <button onClick={() => setPaths(paths.filter((_, j) => j !== i))} className="ml-2 text-red-400 hover:text-red-600">×</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        onClick={onSubmit}
        disabled={paths.length === 0 || submitting}
        className="px-6 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
      >
        {submitting ? "提交中..." : "提交批次"}
      </button>
    </div>
  );
}
