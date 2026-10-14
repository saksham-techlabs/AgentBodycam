/** Fixed, generic export file names: no project, session, user or time in them. */
export const SVG_FILENAME = "agentbodycam-replay.svg";
export const JSON_FILENAME = "agentbodycam-summary.json";

export const SVG_MIME = "image/svg+xml;charset=utf-8";
export const JSON_MIME = "application/json;charset=utf-8";

/**
 * Saves `content` as a local file through a temporary object URL. Nothing leaves the
 * machine: the Blob lives in this page and the URL is revoked right after the click.
 */
export function downloadText(filename: string, mime: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.hidden = true;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    URL.revokeObjectURL(url);
  }
}
