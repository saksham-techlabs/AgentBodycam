/**
 * Encodes a replay clip to WebM in this browser: frames are drawn onto an offscreen canvas
 * in real time and the canvas's own stream is recorded with `MediaRecorder`. Nothing on the
 * page or screen is captured, and nothing leaves the browser.
 */

import { drawClipFrame } from "./clipDraw";
import type { ReplayClipTimeline } from "./clipPlan";

/** Preferred first: VP9, then VP8, then whatever WebM codec the browser picks. */
export const WEBM_MIME_PREFERENCE = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"] as const;
export const CLIP_MIME = "video/webm";
export const CLIP_BITS_PER_SECOND = 6_000_000;
/** How often the recorder hands over encoded data while rendering. */
const CHUNK_MS = 1_000;
/** A recorder that never reports `stop` must not hold the export (and its tracks) forever. */
export const ENCODE_TIMEOUT_MS = 10_000;

export const UNSUPPORTED_MESSAGE = "Replay clip export is not supported in this browser.";

export type ClipCodec = "VP9" | "VP8" | "WebM";
export type ClipSupport = { supported: true; mimeType: string; codec: ClipCodec } | { supported: false; reason: string };

export type ClipErrorCode = "unsupported" | "recorder" | "empty" | "cancelled";

export class ClipExportError extends Error {
  constructor(
    readonly code: ClipErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ClipExportError";
  }
}

const MESSAGES: Record<ClipErrorCode, string> = {
  unsupported: UNSUPPORTED_MESSAGE,
  recorder: "The browser's recorder reported an error. No file was saved.",
  empty: "The browser produced an empty clip. No file was saved.",
  cancelled: "Export cancelled.",
};

const fail = (code: ClipErrorCode): ClipExportError => new ClipExportError(code, MESSAGES[code]);

/** The first WebM type in `WEBM_MIME_PREFERENCE` the browser can record, or null. */
export function pickWebmMimeType(isTypeSupported: (type: string) => boolean): string | null {
  for (const type of WEBM_MIME_PREFERENCE) {
    try {
      if (isTypeSupported(type)) return type;
    } catch {
      // Treat a throwing probe as "not supported" and keep looking.
    }
  }
  return null;
}

export function detectClipSupport(): ClipSupport {
  const Recorder = (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder;
  const canCapture =
    typeof HTMLCanvasElement !== "undefined" && typeof HTMLCanvasElement.prototype.captureStream === "function";
  if (typeof Recorder !== "function" || typeof Recorder.isTypeSupported !== "function" || !canCapture) {
    return { supported: false, reason: UNSUPPORTED_MESSAGE };
  }
  const mimeType = pickWebmMimeType((type) => Recorder.isTypeSupported(type));
  if (mimeType === null) return { supported: false, reason: UNSUPPORTED_MESSAGE };
  const codec: ClipCodec = mimeType.includes("vp9") ? "VP9" : mimeType.includes("vp8") ? "VP8" : "WebM";
  return { supported: true, mimeType, codec };
}

export type ClipProgress = { phase: "preparing" } | { phase: "rendering"; percent: number } | { phase: "encoding" };

export interface RecordOptions {
  mimeType: string;
  signal: AbortSignal;
  onProgress: (progress: ClipProgress) => void;
}

/**
 * Renders and records `timeline`, resolving with a non-empty WebM Blob. Rejects with a
 * `ClipExportError` on cancellation (`signal`), recorder errors or an empty result. On every
 * outcome the frame timer is cleared, the recorder stopped, every stream track stopped and
 * the canvas released before the promise settles.
 *
 * Frames are paced by elapsed time rather than counted, so the clip keeps its planned
 * length even when the browser throttles timers (for example in a background tab).
 */
export function recordReplayClip(timeline: ReplayClipTimeline, options: RecordOptions): Promise<Blob> {
  const { mimeType, signal, onProgress } = options;
  return new Promise<Blob>((resolve, reject) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let recorder: MediaRecorder | undefined;
    let stream: MediaStream | undefined;
    const chunks: Blob[] = [];
    const canvas = document.createElement("canvas");

    const cleanup = (): void => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      signal.removeEventListener("abort", onAbort);
      if (recorder !== undefined) {
        recorder.ondataavailable = null;
        recorder.onstop = null;
        recorder.onerror = null;
        if (recorder.state !== "inactive") {
          try {
            recorder.stop();
          } catch {
            // Already stopping; the tracks are stopped below either way.
          }
        }
      }
      for (const track of stream?.getTracks() ?? []) track.stop();
      chunks.length = 0;
      canvas.width = 0;
      canvas.height = 0;
    };

    const settle = (outcome: { blob: Blob } | { error: ClipExportError }): void => {
      if (settled) return;
      settled = true;
      cleanup();
      if ("blob" in outcome) resolve(outcome.blob);
      else reject(outcome.error);
    };

    function onAbort(): void {
      settle({ error: fail("cancelled") });
    }

    if (signal.aborted) {
      settle({ error: fail("cancelled") });
      return;
    }
    signal.addEventListener("abort", onAbort);
    onProgress({ phase: "preparing" });

    const setup = (): { draw: CanvasRenderingContext2D; active: MediaRecorder } => {
      canvas.width = timeline.width;
      canvas.height = timeline.height;
      const ctx = canvas.getContext("2d", { alpha: false });
      if (ctx === null || typeof canvas.captureStream !== "function") throw fail("unsupported");
      drawClipFrame(ctx, timeline, 0);
      stream = canvas.captureStream(timeline.fps);
      recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: CLIP_BITS_PER_SECOND });
      return { draw: ctx, active: recorder };
    };
    let ready: ReturnType<typeof setup>;
    try {
      ready = setup();
    } catch {
      // A missing 2D context or capture stream, or a refused recorder configuration, all
      // mean this browser cannot produce the clip.
      settle({ error: fail("unsupported") });
      return;
    }
    const { draw, active } = ready;

    active.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    active.onerror = () => settle({ error: fail("recorder") });
    active.onstop = () => {
      const blob = new Blob(chunks, { type: CLIP_MIME });
      settle(blob.size > 0 ? { blob } : { error: fail("empty") });
    };

    const frameMs = 1000 / timeline.fps;
    let start = 0;
    let drawn = 0;
    let percent = -1;

    const finish = (): void => {
      onProgress({ phase: "encoding" });
      timer = setTimeout(() => settle({ error: fail("recorder") }), ENCODE_TIMEOUT_MS);
      try {
        active.stop();
      } catch {
        settle({ error: fail("recorder") });
      }
    };

    const tick = (): void => {
      timer = undefined;
      const elapsed = performance.now() - start;
      const frame = Math.min(timeline.totalFrames - 1, Math.floor(elapsed / frameMs));
      if (frame !== drawn) {
        drawClipFrame(draw, timeline, frame);
        drawn = frame;
      }
      const next = Math.min(100, Math.floor(((frame + 1) / timeline.totalFrames) * 100));
      if (next !== percent) {
        percent = next;
        onProgress({ phase: "rendering", percent });
      }
      if (elapsed >= timeline.durationMs) {
        finish();
        return;
      }
      timer = setTimeout(tick, Math.max(1, start + (frame + 1) * frameMs - performance.now()));
    };

    try {
      active.start(CHUNK_MS);
    } catch {
      settle({ error: fail("recorder") });
      return;
    }
    start = performance.now();
    tick();
  });
}

/** A user-facing message for a failed export, or null when the user cancelled it. */
export function clipErrorMessage(error: unknown): string | null {
  if (error instanceof ClipExportError) return error.code === "cancelled" ? null : error.message;
  return MESSAGES.recorder;
}
