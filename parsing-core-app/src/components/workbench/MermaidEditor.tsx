import { useEffect, useState } from "react";
import MermaidBlock from "../MermaidBlock";

interface MermaidEditorProps {
  title: string;
  initial: string;
}

export default function MermaidEditor({ title, initial }: MermaidEditorProps) {
  const [code, setCode] = useState(initial);

  useEffect(() => {
    setCode(initial);
  }, [initial]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-zinc-800">{title}</h2>
        <span className="text-xs text-zinc-400">Mermaid 直接预览</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <textarea
          value={code}
          onChange={(e) => setCode(e.target.value)}
          spellCheck={false}
          className="h-72 w-full resize-y rounded-md border border-zinc-200 bg-white p-3 font-mono text-xs leading-5 text-zinc-800 focus:outline-none focus:ring-2 focus:ring-zinc-200"
        />
        <div className="min-h-72 rounded-md border border-zinc-200 bg-white p-4">
          <MermaidBlock code={code} />
        </div>
      </div>
    </div>
  );
}
