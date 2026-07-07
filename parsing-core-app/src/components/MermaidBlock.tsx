import { useEffect, useState } from "react";

export default function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    import("mermaid").then((mermaid) => {
      mermaid.default
        .render(`mermaid-${Math.random().toString(36).slice(2)}`, code)
        .then(({ svg }) => { if (!cancelled) setSvg(svg); })
        .catch(() => { if (!cancelled) setError(true); });
    });
    return () => { cancelled = true; };
  }, [code]);

  if (error) return <pre className="text-xs text-red-500 bg-red-50 p-2 rounded overflow-auto">{code}</pre>;
  if (!svg) return <div className="h-20 bg-gray-50 rounded animate-pulse" />;
  return <div className="flex justify-center my-4" dangerouslySetInnerHTML={{ __html: svg }} />;
}
