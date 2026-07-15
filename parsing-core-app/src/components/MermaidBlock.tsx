import { useEffect, useState } from "react";
import { Loader2, AlertCircle } from "lucide-react";
import DOMPurify from "dompurify";

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

const SVG_TAGS = [
  "svg", "g", "defs", "desc", "title", "path", "rect", "circle", "ellipse", "line",
  "polyline", "polygon", "text", "tspan", "marker", "clipPath", "mask", "pattern",
  "linearGradient", "radialGradient", "stop", "a",
];

const SVG_ATTRIBUTES = [
  "xmlns", "viewBox", "width", "height", "x", "y", "dx", "dy", "x1", "x2", "y1", "y2", "cx", "cy",
  "r", "rx", "ry", "d", "points", "transform", "id", "class", "role", "aria-label",
  "aria-labelledby", "aria-describedby", "aria-roledescription", "tabindex", "dominant-baseline",
  "text-anchor", "textLength", "lengthAdjust", "font-family", "font-size", "font-weight", "font-style", "fill", "fill-opacity", "stroke",
  "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "stroke-opacity", "opacity",
  "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "markerWidth",
  "markerHeight", "markerUnits", "refX", "refY", "orient", "preserveAspectRatio", "clip-path",
  "mask", "marker-start", "marker-mid", "marker-end",
];

const LOCAL_REFERENCE_ATTRIBUTES = new Set([
  "clip-path", "mask", "marker-start", "marker-mid", "marker-end", "fill", "stroke",
]);

function isSafeSvgAttribute(name: string, value: string) {
  if (!value.toLowerCase().includes("url(")) return true;
  return LOCAL_REFERENCE_ATTRIBUTES.has(name) && /^url\(\s*#[A-Za-z_][\w:.-]*\s*\)$/i.test(value.trim());
}

export function sanitizeMermaidSvg(svg: string) {
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    ALLOWED_TAGS: SVG_TAGS,
    ALLOWED_ATTR: SVG_ATTRIBUTES,
    ADD_URI_SAFE_ATTR: Array.from(LOCAL_REFERENCE_ATTRIBUTES),
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ["foreignObject", "style", "use", "image"],
    FORBID_ATTR: ["style", "href", "xlink:href"],
  });
  const documentNode = new DOMParser().parseFromString(clean, "image/svg+xml");
  documentNode.querySelectorAll("*").forEach((node) => {
    for (const attribute of Array.from(node.attributes)) {
      if (!isSafeSvgAttribute(attribute.name, attribute.value)) node.removeAttribute(attribute.name);
    }
  });
  return documentNode.documentElement.outerHTML;
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
    return () => { c = true; };
  }, [code]);

  if (error) {
    return (
      <div role="alert" className="flex min-w-0 max-w-full items-start gap-2.5 overflow-hidden rounded-lg border border-amber-200 bg-amber-50 p-3.5">
        <AlertCircle size={16} className="text-amber-500 shrink-0 mt-0.5" />
        <pre className="min-w-0 max-w-full overflow-auto whitespace-pre-wrap break-words text-[11px] text-amber-800">{code}</pre>
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
    <div className="mermaid-preview flex min-w-0 max-w-full justify-center overflow-auto" dangerouslySetInnerHTML={{ __html: svg }} />
  );
}
