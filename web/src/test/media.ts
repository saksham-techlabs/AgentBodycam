/**
 * Test doubles for the browser APIs a replay clip export uses: a recording 2D context, a
 * canvas capture stream with stoppable tracks and a scriptable `MediaRecorder`. CI never
 * needs real codec support.
 */

import { vi } from "vitest";

import type { ClipCanvasContext } from "../lib/clipDraw";

export interface DrawnText {
  text: string;
  font: string;
}

export function recordingContext(): { ctx: ClipCanvasContext & CanvasRenderingContext2D; drawn: DrawnText[]; fonts: Set<string> } {
  const drawn: DrawnText[] = [];
  const fonts = new Set<string>();
  const gradient = { addColorStop: () => undefined };
  const noop = (): void => undefined;
  let font = "";
  const ctx = {
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    textAlign: "left",
    textBaseline: "alphabetic",
    globalAlpha: 1,
    get font() {
      return font;
    },
    set font(value: string) {
      font = value;
      fonts.add(value);
    },
    save: noop,
    restore: noop,
    fillRect: noop,
    beginPath: noop,
    moveTo: noop,
    lineTo: noop,
    arc: noop,
    arcTo: noop,
    closePath: noop,
    fill: noop,
    stroke: noop,
    fillText: (text: string) => {
      drawn.push({ text, font });
    },
    createLinearGradient: () => gradient,
    createRadialGradient: () => gradient,
    measureText: (text: string) => ({ width: Array.from(text).length * 10 }),
  };
  return { ctx: ctx as unknown as ClipCanvasContext & CanvasRenderingContext2D, drawn, fonts };
}

export class FakeTrack {
  stopped = false;
  stop(): void {
    this.stopped = true;
  }
}

export class FakeStream {
  readonly tracks = [new FakeTrack()];
  constructor(readonly fps: number) {}
  getTracks(): FakeTrack[] {
    return this.tracks;
  }
}

export interface MediaMockOptions {
  /** Types `MediaRecorder.isTypeSupported` accepts. */
  supported?: readonly string[];
  /** Bytes in the recorder's final chunk; 0 produces an empty clip. */
  chunkBytes?: number;
  /** Delay between `stop()` and the final data and `stop` events. */
  stopDelayMs?: number;
  /** Never deliver `stop` after `stop()`. */
  neverStop?: boolean;
  mediaRecorder?: boolean;
  captureStream?: boolean;
  context?: boolean;
}

export interface FakeRecorder {
  stream: FakeStream;
  options: MediaRecorderOptions;
  state: RecordingState;
  timeslice: number | undefined;
  stopCalls: number;
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  fail(): void;
}

export interface MediaMocks {
  recorders: FakeRecorder[];
  streams: FakeStream[];
  contexts: ReturnType<typeof recordingContext>[];
  restore(): void;
}

export const ALL_WEBM = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"] as const;

export function installMediaMocks(options: MediaMockOptions = {}): MediaMocks {
  const supported = options.supported ?? ALL_WEBM;
  const chunkBytes = options.chunkBytes ?? 4096;
  const stopDelayMs = options.stopDelayMs ?? 50;
  const recorders: FakeRecorder[] = [];
  const streams: FakeStream[] = [];
  const contexts: ReturnType<typeof recordingContext>[] = [];

  class Recorder implements FakeRecorder {
    static isTypeSupported(type: string): boolean {
      return supported.includes(type);
    }
    state: RecordingState = "inactive";
    timeslice: number | undefined;
    stopCalls = 0;
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: ((event: Event) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    constructor(
      readonly stream: FakeStream,
      readonly options: MediaRecorderOptions,
    ) {
      recorders.push(this);
    }
    start(timeslice?: number): void {
      this.state = "recording";
      this.timeslice = timeslice;
    }
    stop(): void {
      if (this.state === "inactive") throw new Error("InvalidStateError");
      this.state = "inactive";
      this.stopCalls += 1;
      if (options.neverStop === true) return;
      setTimeout(() => {
        this.ondataavailable?.({ data: new Blob([new Uint8Array(chunkBytes)]) });
        this.onstop?.(new Event("stop"));
      }, stopDelayMs);
    }
    fail(): void {
      this.onerror?.(new Event("error"));
    }
  }

  if (options.mediaRecorder !== false) vi.stubGlobal("MediaRecorder", Recorder);
  else vi.stubGlobal("MediaRecorder", undefined);
  const proto = HTMLCanvasElement.prototype as { captureStream?: (fps?: number) => unknown };
  const hadCapture = Object.hasOwn(proto, "captureStream");
  const originalCapture = proto.captureStream;
  if (options.captureStream !== false) {
    proto.captureStream = (fps?: number) => {
      const stream = new FakeStream(fps ?? 0);
      streams.push(stream);
      return stream;
    };
  } else {
    delete proto.captureStream;
  }
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => {
    if (options.context === false) return null;
    const c = recordingContext();
    contexts.push(c);
    return c.ctx;
  }) as unknown as HTMLCanvasElement["getContext"]);

  return {
    recorders,
    streams,
    contexts,
    restore() {
      if (hadCapture && originalCapture !== undefined) proto.captureStream = originalCapture;
      else delete proto.captureStream;
    },
  };
}
