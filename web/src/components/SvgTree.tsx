import { createElement, type ReactNode } from "react";

import { assertSafeAttr, assertSafeTag, type SvgNode } from "../lib/svg";

/** SVG attribute names as React props: `stroke-width` becomes `strokeWidth`. */
function propName(name: string): string {
  return name.replace(/-([a-z])/g, (_, c: string) => c.toUpperCase());
}

function render(node: SvgNode | string, key: number): ReactNode {
  if (typeof node === "string") return node;
  assertSafeTag(node.tag);
  const props: Record<string, string | number> = { key };
  for (const [name, value] of Object.entries(node.attrs)) {
    assertSafeAttr(name, value);
    props[propName(name)] = value;
  }
  return createElement(node.tag, props, ...node.children.map((child, i) => render(child, i)));
}

/**
 * Renders an `SvgNode` tree as React elements: the in-app preview of exactly the tree that
 * is serialized for download. Text stays text; React escapes it.
 */
export function SvgTree({
  root,
  className,
  labelledBy,
}: {
  root: SvgNode;
  className?: string;
  labelledBy?: string;
}) {
  const props: Record<string, string | number> = {};
  for (const [name, value] of Object.entries(root.attrs)) {
    assertSafeAttr(name, value);
    props[propName(name)] = value;
  }
  return (
    <svg
      {...props}
      className={className}
      role="img"
      aria-labelledby={labelledBy}
      focusable="false"
      preserveAspectRatio="xMidYMid meet"
    >
      {root.children.map((child, i) => render(child, i))}
    </svg>
  );
}
