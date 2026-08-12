import DOMPurify from "dompurify";

const SVG_TAGS = [
  "svg",
  "g",
  "defs",
  "desc",
  "title",
  "path",
  "rect",
  "circle",
  "ellipse",
  "line",
  "polyline",
  "polygon",
  "text",
  "tspan",
  "marker",
  "clipPath",
  "mask",
  "pattern",
  "linearGradient",
  "radialGradient",
  "stop",
  "a",
];

const SVG_ATTRIBUTES = [
  "xmlns",
  "viewBox",
  "width",
  "height",
  "x",
  "y",
  "dx",
  "dy",
  "x1",
  "x2",
  "y1",
  "y2",
  "cx",
  "cy",
  "r",
  "rx",
  "ry",
  "d",
  "points",
  "transform",
  "id",
  "class",
  "role",
  "aria-label",
  "aria-labelledby",
  "aria-describedby",
  "aria-roledescription",
  "tabindex",
  "dominant-baseline",
  "text-anchor",
  "textLength",
  "lengthAdjust",
  "font-family",
  "font-size",
  "font-weight",
  "font-style",
  "fill",
  "fill-opacity",
  "stroke",
  "stroke-width",
  "stroke-dasharray",
  "stroke-linecap",
  "stroke-linejoin",
  "stroke-opacity",
  "opacity",
  "offset",
  "stop-color",
  "stop-opacity",
  "gradientUnits",
  "gradientTransform",
  "markerWidth",
  "markerHeight",
  "markerUnits",
  "refX",
  "refY",
  "orient",
  "preserveAspectRatio",
  "clip-path",
  "mask",
  "marker-start",
  "marker-mid",
  "marker-end",
];

const LOCAL_REFERENCE_ATTRIBUTES = new Set([
  "clip-path",
  "mask",
  "marker-start",
  "marker-mid",
  "marker-end",
  "fill",
  "stroke",
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
