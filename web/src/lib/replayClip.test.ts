import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "../api/types";
import { STOPPED_ID, SESSIONS } from "../test/fakeServer";
import { CAUSAL, RICH_TIMELINE, plainReport, richReport } from "../test/fixtures";
import { FORBIDDEN, LEAKS, hostileReport } from "../test/hostile";
import { installMediaMocks, recordingContext, type MediaMocks } from "../test/media";
import clipDrawSource from "./clipDraw.ts?raw";
import clipPlanSource from "./clipPlan.ts?raw";
import clipRecorderSource from "./clipRecorder.ts?raw";
import { drawClipFrame } from "./clipDraw";
import {
  CLIP_FPS,
  CLIP_HEIGHT,
  CLIP_WIDTH,
  MAX_CLIP_FRAMES,
  clipTexts,
  planReplayClip,
  sceneAt,
  type ReplayClipScene,
  type ReplayClipTimeline,
} from "./clipPlan";
import {
  ClipExportError,
  ENCODE_TIMEOUT_MS,
  UNSUPPORTED_MESSAGE,
  WEBM_MIME_PREFERENCE,
  clipErrorMessage,
  detectClipSupport,
  pickWebmMimeType,
  recordReplayClip,
  type ClipProgress,
} from "./clipRecorder";
import { WEBM_FILENAME } from "./download";
import { buildShareModel, type ShareCardModel } from "./shareModel";

const SCREEN_WORDING = /screen ?record|screen ?capture|desktop capture|footage|recorded video|video of the agent|camera feed|filmed/i;
const CAUSAL_CAPS = /ROOT CAUSE|FIXED BY|CAUSED BY|CULPRIT|BUG FIXED/i;

const rich = (): ShareCardModel => buildShareModel(richReport(STOPPED_ID));

function plain(): ShareCardModel {
  const summary = SESSIONS.find(
    (s): s is Extract<SessionSummary, { readable: true }> => s.readable && s.recording_state === "stopped",
  );
  if (summary === undefined) throw new Error("fixture has a stopped session");
  return buildShareModel(plainReport(summary, []));
}

function unresolved(): ShareCardModel {
  const r = richReport(STOPPED_ID);
  r.recoveries = { items: [], total: 0, truncated: false };
  return buildShareModel(r);
}

function live(): ShareCardModel {
  const r = richReport(STOPPED_ID);
  r.recording_state = "recording";
  r.session_status = "recording";
  r.analysis_complete = false;
  return buildShareModel(r);
}

/** Every string drawn across every frame of the clip. */
function drawAll(tl: ReplayClipTimeline): { texts: string[]; fonts: Set<string> } {
  const { ctx, drawn, fonts } = recordingContext();
  for (let f = 0; f < tl.totalFrames; f += 1) drawClipFrame(ctx, tl, f);
  return { texts: drawn.map((d) => d.text), fonts };
}

function scene<K extends ReplayClipScene["kind"]>(tl: ReplayClipTimeline, kind: K, eyebrow?: string) {
  const found = tl.scenes.find(
    (s): s is Extract<ReplayClipScene, { kind: K }> =>
      s.kind === kind && (eyebrow === undefined || ("eyebrow" in s && s.eyebrow === eyebrow)),
  );
  if (found === undefined) throw new Error(`no ${kind} scene`);
  return found;
}

describe("replay clip scene plan", () => {
  it("is deterministic", () => {
    expect(planReplayClip(rich())).toEqual(planReplayClip(rich()));
    expect(drawAll(planReplayClip(rich())).texts).toEqual(drawAll(planReplayClip(rich())).texts);
  });

  it("renders 1280×720 at 30 fps", () => {
    const tl = planReplayClip(rich());
    expect([tl.width, tl.height, tl.fps]).toEqual([1280, 720, 30]);
    expect([CLIP_WIDTH, CLIP_HEIGHT, CLIP_FPS]).toEqual([1280, 720, 30]);
    expect(tl.durationMs).toBe((tl.totalFrames / 30) * 1000);
  });

  it("stays between 8 and 15 seconds whatever the session holds", () => {
    const huge = rich();
    huge.totalEvents = 999_999;
    huge.observableFailures = 999_999;
    for (const model of [rich(), plain(), unresolved(), live(), huge]) {
      const tl = planReplayClip(model);
      expect(tl.totalFrames).toBeLessThanOrEqual(MAX_CLIP_FRAMES);
      expect(tl.durationMs).toBeGreaterThanOrEqual(8_000);
      expect(tl.durationMs).toBeLessThanOrEqual(15_000);
      const last = tl.scenes.at(-1);
      expect((last?.startFrame ?? 0) + (last?.frames ?? 0)).toBe(tl.totalFrames);
    }
    expect(planReplayClip(rich()).durationMs).toBe(12_000);
    expect(planReplayClip(plain()).durationMs).toBe(8_000);
  });

  it("tiles scenes back to back and maps every frame to one", () => {
    const tl = planReplayClip(rich());
    let next = 0;
    for (const s of tl.scenes) {
      expect(s.startFrame).toBe(next);
      next += s.frames;
    }
    expect(sceneAt(tl, 0).scene.kind).toBe("intro");
    expect(sceneAt(tl, tl.totalFrames - 1).scene.kind).toBe("outro");
    expect(sceneAt(tl, 10_000).scene.kind).toBe("outro");
    expect(sceneAt(tl, -5).local).toBe(0);
  });

  it("skips failure and recovery scenes when there are none", () => {
    const tl = planReplayClip(plain());
    expect(tl.scenes.map((s) => s.kind)).toEqual(["intro", "stats", "final", "outro"]);
    const texts = clipTexts(tl);
    expect(texts).not.toContain("FIRST OBSERVABLE FAILURE");
    expect(texts).not.toContain("OBSERVED RECOVERY");
    expect(scene(tl, "final").detail).toBe("No check results recorded");
  });

  it("shows the first observable failure with its event number and safe label", () => {
    const tl = planReplayClip(rich());
    expect(tl.scenes.map((s) => s.title)).toEqual([
      "Intro",
      "Session stats",
      "First observable failure",
      "Observed recovery",
      "Final recorded state",
      "Outro",
    ]);
    const failure = scene(tl, "moment", "FIRST OBSERVABLE FAILURE");
    expect(failure).toMatchObject({ big: "#5", label: "Test failed: pytest", tone: "red" });
    expect(failure.markers).toEqual([{ sequence: 5, tone: "red", animate: true }]);
    expect(scene(tl, "stats").stats).toEqual([
      { value: RICH_TIMELINE.length, label: "recorded events", tone: "text" },
      { value: 3, label: "files changed", tone: "text" },
      { value: 3, label: "observable failures", tone: "red" },
      { value: 1, label: "observed recovery", tone: "green" },
    ]);
  });

  it("shows the observed recovery after the failure", () => {
    const tl = planReplayClip(rich());
    const recovery = scene(tl, "moment", "OBSERVED RECOVERY");
    expect(recovery).toMatchObject({ big: "#14", label: "Test passed: pytest", tone: "green" });
    expect(recovery.markers).toEqual([
      { sequence: 5, tone: "red", animate: false },
      { sequence: 14, tone: "green", animate: true },
    ]);
    const final = scene(tl, "final");
    expect(final).toMatchObject({
      eyebrow: "FINAL RECORDED STATE",
      tag: null,
      verdict: "Checks: FAIL",
      detail: "1 passing · 1 failing",
      note: "1 unresolved in this recording",
    });
    expect(final.checks).toEqual([
      { name: "pytest", passed: true },
      { name: "ruff", passed: false },
    ]);
  });

  it("marks failures with no observed recovery as unresolved in this recording", () => {
    const tl = planReplayClip(unresolved());
    expect(tl.scenes.map((s) => s.kind)).toEqual(["intro", "stats", "moment", "moment", "final", "outro"]);
    expect(scene(tl, "moment", "UNRESOLVED IN THIS RECORDING")).toMatchObject({
      big: "1",
      label: "observable failure with no observed recovery",
    });
    expect(clipTexts(tl)).not.toContain("OBSERVED RECOVERY");
  });

  it("words a live session as a snapshot so far, never as complete", () => {
    const tl = planReplayClip(live());
    const texts = clipTexts(tl);
    expect(tl.live).toBe(true);
    expect(texts).toContain("LATEST RECORDED STATE");
    expect(texts).toContain("LIVE SNAPSHOT");
    expect(texts).toContain("LIVE SNAPSHOT · SO FAR");
    expect(texts).toContain("LIVE · SO FAR");
    expect(texts).toContain("Recorded evidence replay · live snapshot");
    expect(texts).not.toContain("FINAL RECORDED STATE");
    expect(texts.join("\n")).not.toMatch(/complete|finished|STOPPED/i);
  });

  it("draws the intro, outro and the plan's own strings only", () => {
    const tl = planReplayClip(rich());
    const { texts, fonts } = drawAll(tl);
    for (const t of ["Agent", "Bodycam", "YOUR AI CODING AGENT HAS A BODYCAM", "Recorded evidence replay"]) {
      expect(texts).toContain(t);
    }
    expect(texts).toContain("Recorded with AgentBodycam");
    expect(texts).toContain("Local-first · No source code uploaded");
    const allowed = new Set([...clipTexts(tl), "Agent", "Bodycam", "RECORDED EVIDENCE REPLAY", "PASS", "FAIL", "#1"]);
    for (const t of texts) {
      if (allowed.has(t)) continue;
      // Animated counters, the event axis end and the scene counter.
      expect(t, t).toMatch(/^(#?[\d,]+\+?|\d{2} \/ \d{2})$/);
    }
    for (const font of fonts) expect(font).not.toMatch(/url\(|@import|https?:/i);
  });

  it("bounds every drawn line", () => {
    for (const model of [rich(), plain(), unresolved(), live(), buildShareModel(hostileReport())]) {
      for (const t of drawAll(planReplayClip(model)).texts) expect(Array.from(t).length, t).toBeLessThanOrEqual(48);
    }
  });
});

describe("replay clip privacy", () => {
  const hostile = planReplayClip(buildShareModel(hostileReport()));
  const drawn = drawAll(hostile).texts.join("\n");
  const planned = JSON.stringify(hostile);

  it("is planned from the safe share model only, never the report", () => {
    for (const source of [clipPlanSource, clipDrawSource, clipRecorderSource]) {
      expect(source).not.toContain("ForensicReport");
      expect(source).not.toMatch(/\bfetch\(|XMLHttpRequest|sendBeacon|WebSocket|getDisplayMedia|getUserMedia|html2canvas/);
      const imports = Array.from(source.matchAll(/^import (type )?[^;]*? from "([^"]+)";$/gms), (m) => `${m[1] ?? ""}${m[2]}`);
      for (const spec of imports) {
        expect(["type ../api/types", "./shareCard", "./shareModel", "./clipPlan", "./clipDraw", "type ./clipPlan"]).toContain(spec);
      }
    }
    const tainted = { ...rich(), summary: "C:\\Users\\alice\\secret.py", argv: ["pytest", "-q"], session_id: STOPPED_ID };
    const tl = planReplayClip(tainted);
    expect(JSON.stringify(tl)).not.toMatch(/alice|secret\.py|-q|argv/);
    expect(JSON.stringify(tl)).not.toContain(STOPPED_ID);
    expect(tl).toEqual(planReplayClip(rich()));
  });

  it.each(Object.entries(LEAKS))("never plans or draws %s", (_category, probes) => {
    for (const probe of probes) {
      expect(planned, probe).not.toContain(probe);
      expect(drawn, probe).not.toContain(probe);
    }
  });

  it("falls back to generic labels for unsafe names", () => {
    expect(scene(hostile, "moment", "FIRST OBSERVABLE FAILURE").label).toBe("Test failed");
    expect(scene(hostile, "moment", "OBSERVED RECOVERY").label).toBe("Test passed");
    expect(scene(hostile, "final").checks.map((c) => c.name)).toEqual(["pytest", "lint"]);
    for (const probe of FORBIDDEN) expect(drawn).not.toContain(probe);
  });

  it("uses no causal wording", () => {
    for (const model of [rich(), unresolved(), live(), plain(), buildShareModel(hostileReport())]) {
      const all = [...clipTexts(planReplayClip(model)), ...drawAll(planReplayClip(model)).texts].join("\n");
      expect(all).not.toMatch(CAUSAL);
      expect(all).not.toMatch(CAUSAL_CAPS);
    }
  });

  it("never claims to be a screen recording", () => {
    for (const model of [rich(), live()]) {
      expect(drawAll(planReplayClip(model)).texts.join("\n")).not.toMatch(SCREEN_WORDING);
    }
  });
});

describe("WebM codec selection", () => {
  it("prefers VP9, then VP8, then plain WebM", () => {
    expect(WEBM_MIME_PREFERENCE).toEqual(["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"]);
    const only = (...types: string[]) => (t: string) => types.includes(t);
    expect(pickWebmMimeType(only(...WEBM_MIME_PREFERENCE))).toBe("video/webm;codecs=vp9");
    expect(pickWebmMimeType(only("video/webm;codecs=vp8", "video/webm"))).toBe("video/webm;codecs=vp8");
    expect(pickWebmMimeType(only("video/webm"))).toBe("video/webm");
    expect(pickWebmMimeType(only("video/mp4"))).toBeNull();
    expect(
      pickWebmMimeType((t) => {
        if (t.includes("vp9")) throw new Error("probe failed");
        return true;
      }),
    ).toBe("video/webm;codecs=vp8");
  });

  describe("browser support", () => {
    let mocks: MediaMocks | undefined;
    afterEach(() => mocks?.restore());

    it("is supported with MediaRecorder, captureStream and a WebM type", () => {
      mocks = installMediaMocks({ supported: ["video/webm;codecs=vp8", "video/webm"] });
      expect(detectClipSupport()).toEqual({ supported: true, mimeType: "video/webm;codecs=vp8", codec: "VP8" });
    });

    it("is unsupported without MediaRecorder", () => {
      mocks = installMediaMocks({ mediaRecorder: false });
      expect(detectClipSupport()).toEqual({ supported: false, reason: UNSUPPORTED_MESSAGE });
      expect(UNSUPPORTED_MESSAGE).toBe("Replay clip export is not supported in this browser.");
    });

    it("is unsupported without canvas.captureStream", () => {
      mocks = installMediaMocks({ captureStream: false });
      expect(detectClipSupport()).toEqual({ supported: false, reason: UNSUPPORTED_MESSAGE });
    });

    it("is unsupported when no WebM type can be recorded", () => {
      mocks = installMediaMocks({ supported: ["video/mp4"] });
      expect(detectClipSupport().supported).toBe(false);
    });
  });
});

describe("recordReplayClip", () => {
  let mocks: MediaMocks;
  const tl = planReplayClip(rich());

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  });

  afterEach(() => {
    mocks.restore();
    vi.useRealTimers();
  });

  function start(signal = new AbortController().signal) {
    const progress: ClipProgress[] = [];
    let outcome: { blob: Blob } | { error: unknown } | undefined;
    recordReplayClip(tl, { mimeType: "video/webm;codecs=vp9", signal, onProgress: (p) => progress.push(p) }).then(
      (blob) => (outcome = { blob }),
      (error: unknown) => (outcome = { error }),
    );
    return { progress, outcome: () => outcome };
  }

  /** Everything the export held is released and none of its timers survive. */
  async function released(): Promise<void> {
    for (const s of mocks.streams) for (const t of s.tracks) expect(t.stopped).toBe(true);
    for (const r of mocks.recorders) expect(r.state).toBe("inactive");
    // Let the fake recorder deliver its own delayed `stop` event first.
    await vi.advanceTimersByTimeAsync(100);
    expect(vi.getTimerCount()).toBe(0);
  }

  it("renders every frame in real time, encodes and returns a WebM blob", async () => {
    mocks = installMediaMocks();
    const run = start();
    const recorder = mocks.recorders[0];
    expect(recorder?.options).toEqual({ mimeType: "video/webm;codecs=vp9", videoBitsPerSecond: 6_000_000 });
    expect(recorder?.timeslice).toBe(1000);
    expect(recorder?.state).toBe("recording");
    expect(mocks.streams[0]?.fps).toBe(30);
    expect(run.progress.slice(0, 2)).toEqual([{ phase: "preparing" }, { phase: "rendering", percent: 0 }]);

    await vi.advanceTimersByTimeAsync(tl.durationMs / 2);
    expect(run.progress.at(-1)).toEqual({ phase: "rendering", percent: 50 });
    expect(run.outcome()).toBeUndefined();

    await vi.advanceTimersByTimeAsync(tl.durationMs / 2);
    expect(run.progress.at(-1)).toEqual({ phase: "encoding" });
    expect(run.progress).toContainEqual({ phase: "rendering", percent: 100 });
    await vi.advanceTimersByTimeAsync(100);
    const outcome = run.outcome();
    if (outcome === undefined || !("blob" in outcome)) throw new Error("export did not finish");
    expect(outcome.blob.type).toBe("video/webm");
    expect(outcome.blob.size).toBe(4096);
    const texts = mocks.contexts[0]?.drawn.map((d) => d.text) ?? [];
    expect(texts).toContain("Recorded with AgentBodycam");
    expect(texts).toContain("FIRST OBSERVABLE FAILURE");
    await released();
  });

  it("stops the timer, the recorder and the stream tracks when cancelled", async () => {
    mocks = installMediaMocks();
    const controller = new AbortController();
    const run = start(controller.signal);
    await vi.advanceTimersByTimeAsync(3000);
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    const outcome = run.outcome();
    expect(outcome !== undefined && "error" in outcome && outcome.error).toBeInstanceOf(ClipExportError);
    expect(clipErrorMessage(outcome !== undefined && "error" in outcome ? outcome.error : null)).toBeNull();
    expect(mocks.recorders[0]?.stopCalls).toBe(1);
    expect(mocks.recorders[0]?.onstop).toBeNull();
    await released();
  });

  it("does nothing when already cancelled", async () => {
    mocks = installMediaMocks();
    const controller = new AbortController();
    controller.abort();
    const run = start(controller.signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.recorders).toHaveLength(0);
    expect(run.progress).toEqual([]);
    expect((run.outcome() as { error: ClipExportError }).error.code).toBe("cancelled");
  });

  it("fails cleanly on a recorder error", async () => {
    mocks = installMediaMocks();
    const run = start();
    await vi.advanceTimersByTimeAsync(1000);
    mocks.recorders[0]?.fail();
    await vi.advanceTimersByTimeAsync(0);
    const error = (run.outcome() as { error: ClipExportError }).error;
    expect(error.code).toBe("recorder");
    expect(clipErrorMessage(error)).toBe("The browser's recorder reported an error. No file was saved.");
    await released();
  });

  it("rejects a zero-byte result", async () => {
    mocks = installMediaMocks({ chunkBytes: 0 });
    const run = start();
    await vi.advanceTimersByTimeAsync(tl.durationMs + 100);
    const error = (run.outcome() as { error: ClipExportError }).error;
    expect(error.code).toBe("empty");
    expect(clipErrorMessage(error)).toBe("The browser produced an empty clip. No file was saved.");
    await released();
  });

  it("gives up on a recorder that never finishes", async () => {
    mocks = installMediaMocks({ neverStop: true });
    const run = start();
    await vi.advanceTimersByTimeAsync(tl.durationMs + ENCODE_TIMEOUT_MS - 100);
    expect(run.outcome()).toBeUndefined();
    await vi.advanceTimersByTimeAsync(200);
    expect((run.outcome() as { error: ClipExportError }).error.code).toBe("recorder");
    await released();
  });

  it("reports an unsupported browser when no 2D context is available", async () => {
    mocks = installMediaMocks({ context: false });
    const run = start();
    await vi.advanceTimersByTimeAsync(0);
    const error = (run.outcome() as { error: ClipExportError }).error;
    expect(error.code).toBe("unsupported");
    expect(error.message).toBe(UNSUPPORTED_MESSAGE);
    expect(mocks.recorders).toHaveLength(0);
  });

  it("uses a fixed generic file name", () => {
    expect(WEBM_FILENAME).toBe("agentbodycam-replay.webm");
  });
});
