import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStore } from "../store/useStore";
import { Upload, Clipboard, X, Loader2, Zap, ArrowRight } from "lucide-react";

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
      const p = await invoke<string[]>("pick_files");
      if (p?.length) setPaths((prev) => [...new Set([...prev, ...p])]);
    } catch { setPasting(true); }
  };

  const submit = async () => {
    if (!paths.length) return;
    setSubmitting(true);
    try { await submitBatch(paths, concurrency); navigate("/"); }
    catch (e: any) { alert("提交失败: " + (e.message || "未知")); setSubmitting(false); }
  };

  const remove = (i: number) => setPaths((prev) => prev.filter((_, j) => j !== i));

  return (
    <div className="space-y-6 animate-in max-w-xl">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">新建批次</h1>
        <p className="text-sm text-zinc-500 mt-0.5">选择文件并提交批量解析</p>
      </div>

      {/* File picker card */}
      <div className="rounded-lg border-2 border-dashed border-zinc-200 bg-white p-10 text-center hover:border-zinc-300 transition-colors">
        <Upload size={28} className="text-zinc-300 mx-auto mb-3" strokeWidth={1.5} />
        <p className="text-sm text-zinc-600 mb-1 font-medium">拖拽文件或点击选择</p>
        <p className="text-xs text-zinc-400 mb-5">Excel · PDF · Word · Markdown · 图片</p>
        <div className="flex items-center justify-center gap-2.5">
          <button
            onClick={pickFiles}
            className="inline-flex items-center gap-2 rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 transition-colors active:scale-[0.98]"
          ><Upload size={15} /> 选择文件</button>
          <button
            onClick={() => setPasting(true)}
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-200 px-4 py-2 text-sm text-zinc-600 hover:bg-zinc-50 transition-colors active:scale-[0.98]"
          ><Clipboard size={15} /> 粘贴路径</button>
        </div>
      </div>

      {/* Paste area */}
      {pasting && (
        <div className="rounded-lg border border-zinc-200 bg-white p-5 animate-in">
          <div className="flex items-center justify-between mb-3">
            <p className="text-sm font-medium text-zinc-700">粘贴文件绝对路径</p>
            <button onClick={() => setPasting(false)} className="text-zinc-400 hover:text-zinc-600"><X size={15} /></button>
          </div>
          <textarea
            autoFocus
            onPaste={(e) => {
              const t = e.clipboardData.getData("text");
              if (t) { e.preventDefault(); setPaths((p) => [...new Set([...p, ...t.split("\n").map((s) => s.trim()).filter(Boolean)])]); setPasting(false); }
            }}
            placeholder="每行一个路径&#10;/Users/xxx/a.xlsx&#10;/Users/xxx/b.pdf&#10;&#10;⌘+V 粘贴"
            className="w-full h-24 text-sm font-mono border border-zinc-200 rounded-md p-3 placeholder:text-zinc-300 resize-none focus:outline-none focus:ring-2 focus:ring-zinc-200"
          />
          <p className="text-xs text-zinc-400 mt-2">Finder 中 ⌥⌘C 复制路径 → 回到这里 ⌘V</p>
        </div>
      )}

      {/* File list */}
      {paths.length > 0 && (
        <div className="rounded-lg border border-zinc-200 bg-white animate-in">
          <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-100">
            <span className="text-sm font-medium text-zinc-700">{paths.length} 个文件</span>
            <button onClick={() => setPaths([])} className="text-xs text-zinc-400 hover:text-red-500">清空</button>
          </div>
          <div className="max-h-56 overflow-y-auto divide-y divide-zinc-50">
            {paths.map((p, i) => (
              <div key={i} className="flex items-center justify-between px-4 py-2.5 text-sm hover:bg-zinc-50">
                <div className="flex items-center gap-2.5 min-w-0">
                  <span className="text-[10px] font-mono text-zinc-400 w-4">{i + 1}</span>
                  <span className="text-sm font-mono text-zinc-600 truncate">{p}</span>
                </div>
                <button onClick={() => remove(i)} className="text-zinc-300 hover:text-red-500 shrink-0"><X size={14} /></button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Concurrency + submit */}
      {paths.length > 0 && (
        <div className="flex items-end gap-4 animate-in">
          <div>
            <label className="text-xs text-zinc-500 block mb-1.5">并发数</label>
            <div className="flex rounded-md border border-zinc-200 overflow-hidden">
              {[1, 2, 4, 8, 16].map((n) => (
                <button key={n} onClick={() => setConcurrency(n)}
                  className={`px-3 py-1.5 text-sm transition-colors ${concurrency === n ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-50"}`}
                >{n}x</button>
              ))}
            </div>
          </div>
          <button onClick={submit} disabled={submitting}
            className="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-5 py-2 text-sm font-medium text-white hover:bg-emerald-700 transition-colors active:scale-[0.98] disabled:opacity-50"
          >
            {submitting ? <><Loader2 size={15} className="animate-spin" /> 提交中...</>
            : <><Zap size={15} /> 开始解析 <ArrowRight size={14} /></>}
          </button>
        </div>
      )}
    </div>
  );
}
