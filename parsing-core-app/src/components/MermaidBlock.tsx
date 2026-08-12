import { useEffect, useState } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import { sanitizeMermaidSvg } from "./mermaidSanitizer";

let mermaidModule: Promise<typeof import("mermaid")> | null = null;
let renderQueue: Promise<void> = Promise.resolve();

function loadMermaid() {
  if (!mermaidModule) {
    mermaidModule = import("mermaid").then((module) => {
      module.default.initialize({
        securityLevel: "strict",
        startOnLoad: false,
        htmlLabels: false,
        flowchart: { htmlLabels: false, useMaxWidth: true },
      });
      return module;
    });
  }
  return mermaidModule;
}

function removeTemporaryNode(id: string) {
  document.getElementById(`d${id}`)?.remove();
}

function renderMermaid(module: typeof import("mermaid"), id: string, code: string) {
  const render = renderQueue.then(async () => {
    try {
      return await module.default.render(id, code);
    } finally {
      removeTemporaryNode(id);
    }
  });
  renderQueue = render.then(
    () => undefined,
    () => undefined,
  );
  return render;
}

export default function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState(false);

  useEffect(() => {
    let c = false;
    setError(false);
    setSvg("");
    loadMermaid().then((m) => {
      const renderId = `mm-${Math.random().toString(36).slice(2, 8)}`;
      renderMermaid(m, renderId, code)
        .then(({ svg }) => {
          if (!c) {
            setError(false);
            setSvg(sanitizeMermaidSvg(svg));
          }
        })
        .catch(() => {
          if (!c) {
            setSvg("");
            setError(true);
          }
        })
        .finally(() => removeTemporaryNode(renderId));
    });
    return () => {
      c = true;
    };
  }, [code]);

  if (error) {
    return (
      <div
        role="alert"
        className="flex min-w-0 max-w-full items-start gap-2.5 overflow-hidden rounded-lg border border-amber-200 bg-amber-50 p-3.5"
      >
        <AlertCircle size={16} className="text-amber-500 shrink-0 mt-0.5" />
        <pre className="min-w-0 max-w-full overflow-auto whitespace-pre-wrap break-words text-[11px] text-amber-800">
          {code}
        </pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="flex items-center justify-center h-32 rounded-xl bg-gray-50 border border-dashed border-gray-200">
        <Loader2 size={20} className="animate-spin text-gray-300" />
      </div>
    );
  }

  return (
    <div
      className="mermaid-preview flex min-w-0 max-w-full justify-center overflow-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
