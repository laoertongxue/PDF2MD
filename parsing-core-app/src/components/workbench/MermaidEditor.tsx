import { useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw, Save } from "lucide-react";
import MermaidBlock from "../MermaidBlock";

interface MermaidEditorProps {
  title: string;
  initial: string;
  onSave: (code: string, expected: string) => Promise<boolean>;
  onDirtyChange?: (dirty: boolean) => void;
}

export default function MermaidEditor({ title, initial, onSave, onDirtyChange }: MermaidEditorProps) {
  const [code, setCode] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const dirty = code !== initial;

  useEffect(() => {
    generation.current += 1;
    setCode(initial);
    setSaving(false); setSaved(false); setError("");
  }, [initial]);

  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  const save = async () => {
    if (saving || !dirty || !code.trim()) return;
    const current = ++generation.current;
    setSaving(true); setSaved(false); setError("");
    try {
      const ok = await onSave(code, initial);
      if (generation.current === current) setSaved(ok);
    } catch (reason) {
      if (generation.current === current) setError(reason instanceof Error ? reason.message : "保存失败，请稍后重试");
    } finally {
      if (generation.current === current) setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-zinc-800">{title}</h2>
        <span className="text-xs text-zinc-400">实时预览</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <textarea
          aria-label={`${title} Mermaid 源码`}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          spellCheck={false}
          className="h-72 w-full resize-y rounded-md border border-zinc-200 bg-white p-3 font-mono text-xs leading-5 text-zinc-800 focus:outline-none focus:ring-2 focus:ring-zinc-200"
        />
        <div className="min-h-72 rounded-md border border-zinc-200 bg-white p-4">
          <MermaidBlock code={code} />
        </div>
      </div>
      <div className="flex min-h-9 flex-wrap items-center gap-3">
        <button type="button" onClick={save} disabled={saving || !dirty || !code.trim()} className="inline-flex items-center gap-2 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium disabled:opacity-40">
          {saving ? <Loader2 size={15} className="animate-spin" /> : error ? <RefreshCw size={15} /> : <Save size={15} />}
          {error ? "重试保存" : "保存 Mermaid"}
        </button>
        {saving && <span className="text-xs text-zinc-500">正在保存…</span>}
        {saved && <span className="text-xs text-emerald-700">已保存并同步 Markdown</span>}
        {error && <span role="alert" className="text-xs text-red-700">{error}</span>}
      </div>
    </div>
  );
}
