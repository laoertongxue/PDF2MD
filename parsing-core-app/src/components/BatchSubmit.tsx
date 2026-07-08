import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import { Upload, Clipboard, X, Loader2, Zap, ChevronRight } from "lucide-react";

export default function BatchSubmit() {
  const [paths, setPaths] = useState<string[]>([]);
  const [concurrency, setConcurrency] = useState(4);
  const [submitting, setSubmitting] = useState(false);
  const [pasting, setPasting] = useState(false);
  const submitBatch = useStore((s) => s.submitBatch);
  const navigate = useNavigate();

  const pickFiles = async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string[]>("pick_files");
      if (picked?.length) setPaths((p) => [...new Set([...p, ...picked])]);
    } catch {
      setPasting(true);
    }
  };

  const onSubmit = async () => {
    if (!paths.length) return;
    setSubmitting(true);
    try {
      await submitBatch(paths, concurrency);
      navigate("/");
    } catch (e: any) {
      alert("提交失败: " + (e.message || "未知错误"));
      setSubmitting(false);
    }
  };

  const remove = (i: number) => setPaths((p) => p.filter((_, j) => j !== i));

  return (
    <div className="max-w-2xl space-y-8 animate-fade-in">
      <div>
        <h2 className="text-[22px] font-semibold tracking-tight text-gray-900">新建批次</h2>
        <p className="text-[13px] text-muted mt-0.5">选择文档文件，批量提交解析</p>
      </div>

      {/* File picker */}
      <div className="rounded-2xl border-2 border-dashed border-gray-200 bg-white p-10 text-center hover:border-accent/40 hover:bg-accent/[0.01] transition-all duration-200">
        <div className="flex justify-center mb-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-accent/10">
            <Upload size={26} className="text-accent" strokeWidth={1.5} />
          </div>
        </div>
        <h3 className="text-[15px] font-semibold text-gray-900 mb-1">选择文件或粘贴路径</h3>
        <p className="text-[13px] text-muted mb-5">支持 Excel · PDF · Word · Markdown · 图片</p>
        <div className="flex items-center justify-center gap-3">
          <button
            onClick={pickFiles}
            className="inline-flex items-center gap-2 rounded-xl bg-accent px-5 py-2.5 text-[13px] font-semibold text-white shadow-sm shadow-accent/20 hover:bg-accent-dark hover:shadow-md hover:shadow-accent/25 transition-all duration-200 active:scale-[0.98]"
          >
            <Upload size={16} strokeWidth={2} />
            选择文件
          </button>
          <button
            onClick={() => setPasting(true)}
            className="inline-flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-5 py-2.5 text-[13px] font-medium text-gray-700 hover:bg-gray-50 hover:border-gray-300 transition-all duration-200 active:scale-[0.98]"
          >
            <Clipboard size={16} strokeWidth={2} />
            粘贴路径
          </button>
        </div>
      </div>

      {/* Paste modal */}
      {pasting && (
        <div className="rounded-2xl border border-border bg-white p-6 animate-slide-up">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[14px] font-semibold text-gray-900">粘贴文件绝对路径</h3>
            <button onClick={() => setPasting(false)} className="text-muted hover:text-gray-700"><X size={16} /></button>
          </div>
          <textarea
            autoFocus
            onPaste={(e) => {
              const text = e.clipboardData.getData("text");
              if (text) {
                e.preventDefault();
                const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
                setPaths((p) => [...new Set([...p, ...lines])]);
                setPasting(false);
              }
            }}
            placeholder="每行一个完整路径，例如：&#10;/Users/xxx/report.xlsx&#10;/Users/xxx/data.pdf&#10;&#10;直接 ⌘+V 粘贴即可"
            className="w-full h-28 rounded-xl border border-gray-200 p-3.5 text-[13px] font-mono text-gray-700 placeholder:text-gray-300 resize-none focus:outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent"
          />
          <p className="text-[11px] text-muted mt-2">在 Finder 中选中文件 → ⌥⌘C 复制路径 → 回到这里 ⌘V 粘贴</p>
        </div>
      )}

      {/* File list */}
      {paths.length > 0 && (
        <div className="rounded-2xl border border-border bg-white animate-slide-up">
          <div className="flex items-center justify-between px-5 py-3.5 border-b border-border/60">
            <h3 className="text-[13px] font-semibold text-gray-900">
              已选择 <span className="text-accent font-bold">{paths.length}</span> 个文件
            </h3>
            <button onClick={() => setPaths([])} className="text-[12px] text-muted hover:text-red-500 transition-colors">清空全部</button>
          </div>
          <div className="max-h-64 overflow-y-auto divide-y divide-border/40">
            {paths.map((p, i) => (
              <div key={i} className="flex items-center justify-between px-5 py-2.5 hover:bg-gray-50/50 transition-colors">
                <div className="flex items-center gap-2.5 min-w-0">
                  <span className="text-[10px] font-mono text-muted bg-gray-100 px-1.5 py-0.5 rounded">{i + 1}</span>
                  <span className="text-[12px] font-mono text-gray-700 truncate">{p}</span>
                </div>
                <button onClick={() => remove(i)} className="text-muted hover:text-red-500 transition-colors shrink-0 ml-3"><X size={14} /></button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Settings + submit */}
      {paths.length > 0 && (
        <div className="flex items-end gap-4 animate-slide-up">
          <div>
            <label className="text-[12px] font-medium text-muted block mb-1.5">并发数</label>
            <div className="flex rounded-xl border border-border bg-white overflow-hidden">
              {[1, 2, 4, 8, 16].map((n) => (
                <button
                  key={n}
                  onClick={() => setConcurrency(n)}
                  className={`px-3.5 py-2 text-[13px] font-medium transition-colors ${
                    concurrency === n
                      ? "bg-accent text-white"
                      : "text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  {n}x
                </button>
              ))}
            </div>
          </div>
          <button
            onClick={onSubmit}
            disabled={submitting}
            className="inline-flex items-center gap-2 rounded-xl bg-emerald-600 px-7 py-2.5 text-[13px] font-semibold text-white shadow-sm shadow-emerald-200 hover:bg-emerald-700 hover:shadow-md hover:shadow-emerald-200 transition-all duration-200 active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? (
              <><Loader2 size={16} className="animate-spin" />提交中...</>
            ) : (
              <><Zap size={16} strokeWidth={2} />开始解析 {paths.length} 个文件 <ChevronRight size={15} /></>
            )}
          </button>
        </div>
      )}

      {/* Hint when no files */}
      {paths.length === 0 && (
        <div className="text-center animate-fade-in">
          <p className="text-[13px] text-muted">点击上方的「选择文件」按钮或「粘贴路径」来添加待解析的文档</p>
        </div>
      )}
    </div>
  );
}
