/** Fixed, generic export file names: no project, session, user or time in them. */
export const SVG_FILENAME = "agentbodycam-replay.svg";
export const JSON_FILENAME = "agentbodycam-summary.json";
export const WEBM_FILENAME = "agentbodycam-replay.webm";

export const SVG_MIME = "image/svg+xml;charset=utf-8";
export const JSON_MIME = "application/json;charset=utf-8";

/**
 * How long an object URL outlives the download click. Browsers may read the Blob after
 * `click()` returns (a multi-megabyte clip certainly is), and revoking synchronously can
 * abort the download in some browsers. The Blob stays inside this page either way; the
 * delay only postpones releasing it. Each revocation is a one-shot timer that holds
 * nothing but the URL, so it deliberately outlives the dialog that started the download.
 */
export const TEXT_REVOKE_DELAY_MS = 1_000;
export const VIDEO_REVOKE_DELAY_MS = 2_000;

/**
 * Saves `blob` as a local file through a temporary object URL. Nothing leaves the machine:
 * the Blob lives in this page and the URL is always revoked, `revokeDelayMs` after the
 * click, or at once if the click itself failed.
 */
export function downloadBlob(filename: string, blob: Blob, revokeDelayMs = TEXT_REVOKE_DELAY_MS): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.hidden = true;
  document.body.append(link);
  let clicked = false;
  try {
    link.click();
    clicked = true;
  } finally {
    link.remove();
    if (clicked && revokeDelayMs > 0) setTimeout(() => URL.revokeObjectURL(url), revokeDelayMs);
    else URL.revokeObjectURL(url);
  }
}

export function downloadText(filename: string, mime: string, content: string): void {
  downloadBlob(filename, new Blob([content], { type: mime }));
}
