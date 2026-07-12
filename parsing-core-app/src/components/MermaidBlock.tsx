import { useEffect, useState } from "react";
import { Loader2, AlertCircle } from "lucide-react";

let mermaidModule: Promise<typeof import("mermaid")> | null = null;
let renderQueue: Promise<void> = Promise.resolve();

function loadMermaid() {
  if (!mermaidModule) {
    mermaidModule = import("mermaid").then((module) => {
      module.default.initialize({ securityLevel: "strict", startOnLoad: false });
      return module;
    });
  }
  return mermaidModule;
}

function sanitizeSvg(svg: string) {
  const documentNode = new DOMParser().parseFromString(svg, "image/svg+xml");
  documentNode.querySelectorAll("script, foreignObject").forEach((node) => node.remove());
  documentNode.querySelectorAll("*").forEach((node) => {
    for (const attribute of [...node.attributes]) {
      const value = attribute.value.trim().toLowerCase();
      if (attribute.name.toLowerCase().startsWith("on") || value.startsWith("javascript:")) {
        node.removeAttribute(attribute.name);
      }
    }
  });
  return documentNode.documentElement.outerHTML;
}

function renderMermaid(module: typeof import("mermaid"), id: string, code: string) {
  const render = renderQueue.then(() => module.default.render(id, code));
  renderQueue = render.then(() => undefined, () => undefined);
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
            setSvg(sanitizeSvg(svg));
          }
        })
        .catch(() => {
          if (!c) {
            setSvg("");
            setError(true);
          }
        })
        .finally(() => document.getElementById(`d${renderId}`)?.remove());
    });
    return () => { c = true; };
  }, [code]);

  if (error) {
    return (
      <div className="flex items-start gap-2.5 rounded-xl bg-amber-50 border border-amber-200 p-3.5">
        <AlertCircle size={16} className="text-amber-500 shrink-0 mt-0.5" />
        <pre className="text-[11px] text-amber-800 overflow-auto whitespace-pre-wrap">{code}</pre>
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
    <div className="flex justify-center overflow-x-auto" dangerouslySetInnerHTML={{ __html: svg }} />
  );
}
