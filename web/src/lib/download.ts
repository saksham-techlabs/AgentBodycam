/** Fixed, generic export file names: no project, session, user or time in them. */
export const SVG_FILENAME = "agentbodycam-replay.svg";
export const JSON_FILENAME = "agentbodycam-summary.json";
export const WEBM_FILENAME = "agentbodycam-replay.webm";

export const SVG_MIME = "image/svg+xml;charset=utf-8";
export const JSON_MIME = "application/json;charset=utf-8";

/**
 * How long a video's object URL outlives the download click. Small text files are read
 * before `click()` returns, but a multi-megabyte clip may still be read by the browser's
 * download manager afterwards, and some browsers abort the download if the URL is revoked
 * synchronously. The Blob stays inside this page either way; the delay only postpones
 * releasing it.
 */
export const VIDEO_REVOKE_DELAY_MS = 2_000;

/**
 * Saves `blob` as a local file through a temporary object URL. Nothing leaves the machine:
 * the Blob lives in this page, and the URL is revoked right after the click, or after
 * `revokeDelayMs` when given.
 */
export function downloadBlob(filename: string, blob: Blob, revokeDelayMs = 0): void {
  const url = URL.createObjectURL(blob);
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
    if (revokeDelayMs > 0) setTimeout(() => URL.revokeObjectURL(url), revokeDelayMs);
    else URL.revokeObjectURL(url);
  }
}

export function downloadText(filename: string, mime: string, content: string): void {
  downloadBlob(filename, new Blob([content], { type: mime }));
}
