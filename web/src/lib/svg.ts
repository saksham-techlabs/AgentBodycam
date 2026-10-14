/**
 * A minimal SVG element tree with a string serializer. The same tree is rendered by React
 * for the in-app preview (`SvgTree`) and serialized for download, so both match exactly.
 *
 * Only presentational elements exist in `SvgTag`: no script, foreignObject, image, use,
 * a, style or anything that can load a resource or run code. Attribute names are checked
 * against a pattern that excludes event handlers, href and style; values and text nodes
 * are always escaped, so report text can never become markup.
 */

export type SvgTag =
  | "svg"
  | "title"
  | "desc"
  | "defs"
  | "linearGradient"
  | "radialGradient"
  | "stop"
  | "g"
  | "rect"
  | "circle"
  | "line"
  | "path"
  | "text"
  | "tspan";

export type SvgAttrs = Readonly<Record<string, string | number>>;

export interface SvgNode {
  tag: SvgTag;
  attrs: SvgAttrs;
  children: readonly (SvgNode | string)[];
}

export const SVG_NS = "http://www.w3.org/2000/svg";

// `SvgTag` is only a compile-time type; serialization re-checks every tag at run time.
const SVG_TAGS: ReadonlySet<string> = new Set<SvgTag>([
  "svg",
  "title",
  "desc",
  "defs",
  "linearGradient",
  "radialGradient",
  "stop",
  "g",
  "rect",
  "circle",
  "line",
  "path",
  "text",
  "tspan",
]);
const ATTR_NAME = /^[a-zA-Z][a-zA-Z0-9-]*$/;
const FORBIDDEN_ATTR = /^(on|href$|style$|src$)/i;
// Paint may reference a gradient defined in the same document, nothing else.
const URL_VALUE = /url\(/i;
const LOCAL_URL = /^url\(#[a-z][a-z0-9-]*\)$/;

export function el(tag: SvgTag, attrs: SvgAttrs = {}, ...children: (SvgNode | string | null)[]): SvgNode {
  return { tag, attrs, children: children.filter((c): c is SvgNode | string => c !== null) };
}

export function assertSafeTag(tag: string): void {
  if (!SVG_TAGS.has(tag)) throw new Error("unsafe SVG element");
}

export function assertSafeAttr(name: string, value: string | number): void {
  if (!ATTR_NAME.test(name) || FORBIDDEN_ATTR.test(name)) {
    throw new Error(`unsafe SVG attribute name: ${name}`);
  }
  if (typeof value === "string" && URL_VALUE.test(value) && !LOCAL_URL.test(value)) {
    throw new Error(`unsafe SVG attribute value for ${name}`);
  }
}

export function escapeXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function format(value: string | number): string {
  return typeof value === "number" ? String(Math.round(value * 100) / 100) : value;
}

function serializeNode(node: SvgNode | string): string {
  if (typeof node === "string") return escapeXml(node);
  assertSafeTag(node.tag);
  const attrs = Object.entries(node.attrs)
    .map(([name, value]) => {
      assertSafeAttr(name, value);
      return ` ${name}="${escapeXml(format(value))}"`;
    })
    .join("");
  if (node.children.length === 0) return `<${node.tag}${attrs}/>`;
  return `<${node.tag}${attrs}>${node.children.map(serializeNode).join("")}</${node.tag}>`;
}

/** A standalone SVG document for the root `svg` node. */
export function serializeSvg(root: SvgNode): string {
  if (root.tag !== "svg") throw new Error("root must be an svg element");
  const withNs: SvgNode = { ...root, attrs: { xmlns: SVG_NS, ...root.attrs } };
  return `<?xml version="1.0" encoding="UTF-8"?>\n${serializeNode(withNs)}\n`;
}
