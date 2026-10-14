import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { planReplayClip } from "../lib/clipPlan";
import type { ClipProgress, RecordOptions } from "../lib/clipRecorder";
import { buildShareModel } from "../lib/shareModel";
import { STOPPED_ID } from "../test/fakeServer";
import { richReport } from "../test/fixtures";
import { useClipExport } from "./useClipExport";

interface Run {
  options: RecordOptions;
  resolve: (blob: Blob) => void;
  reject: (error: unknown) => void;
}

const runs: Run[] = [];

vi.mock("../lib/clipRecorder", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/clipRecorder")>();
  return {
    ...actual,
    detectClipSupport: () => ({ supported: true, mimeType: "video/webm;codecs=vp9", codec: "VP9" }),
    recordReplayClip: (_timeline: unknown, options: RecordOptions) =>
      new Promise<Blob>((resolve, reject) => {
        runs.push({ options, resolve, reject });
        options.onProgress({ phase: "preparing" });
      }),
  };
});

const timeline = planReplayClip(buildShareModel(richReport(STOPPED_ID)));
let created: string[];
let revoked: string[];

beforeEach(() => {
  runs.length = 0;
  created = [];
  revoked = [];
  URL.createObjectURL = vi.fn(() => {
    created.push(`blob:local/${created.length + 1}`);
    return created.at(-1)!;
  });
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});

afterEach(() => {
  vi.useRealTimers();
});

const webm = (bytes = 2048) => new Blob([new Uint8Array(bytes)], { type: "video/webm" });

function run(i: number): Run {
  const found = runs[i];
  if (found === undefined) throw new Error(`run ${i} was not started`);
  return found;
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("replay clip export state machine", () => {
  it("never lets a cancelled run's late progress, result or error touch the newer run", async () => {
    const { result } = renderHook(() => useClipExport(timeline));
    act(() => result.current.start());
    act(() => run(0).options.onProgress({ phase: "rendering", percent: 40 }));
    act(() => result.current.cancel());
    expect(result.current.status).toEqual({ phase: "idle" });
    expect(run(0).options.signal.aborted).toBe(true);

    act(() => result.current.start());
    act(() => run(1).options.onProgress({ phase: "rendering", percent: 10 }));

    // The first run's callbacks arrive late, in every possible way.
    act(() => run(0).options.onProgress({ phase: "rendering", percent: 90 } satisfies ClipProgress));
    run(0).resolve(webm());
    await settle();
    expect(result.current.status).toEqual({ phase: "rendering", percent: 10 });
    expect(created).toEqual([]);
    run(0).reject(new Error("late failure"));
    await settle();
    expect(result.current.status).toEqual({ phase: "rendering", percent: 10 });

    run(1).resolve(webm(4096));
    await settle();
    expect(result.current.status).toEqual({ phase: "done", bytes: 4096 });
    expect(created).toEqual(["blob:local/1"]);
  });

  it("starts a second export after a successful one, and both URLs are revoked", async () => {
    const { result } = renderHook(() => useClipExport(timeline));
    act(() => result.current.start());
    run(0).resolve(webm());
    await settle();
    expect(result.current.status.phase).toBe("done");

    act(() => result.current.start());
    expect(runs).toHaveLength(2);
    expect(result.current.status).toEqual({ phase: "preparing" });
    run(1).resolve(webm(1000));
    await settle();
    expect(result.current.status).toEqual({ phase: "done", bytes: 1000 });
    expect(created).toEqual(["blob:local/1", "blob:local/2"]);
    act(() => vi.runAllTimers());
    expect(revoked.sort()).toEqual(["blob:local/1", "blob:local/2"]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("exports normally after a cancellation", async () => {
    const { result } = renderHook(() => useClipExport(timeline));
    act(() => result.current.start());
    act(() => result.current.cancel());
    act(() => result.current.start());
    expect(runs).toHaveLength(2);
    run(1).resolve(webm());
    await settle();
    expect(result.current.status.phase).toBe("done");
    expect(created).toHaveLength(1);
  });

  it("ignores Start while a run is in flight, and Cancel while idle", () => {
    const { result } = renderHook(() => useClipExport(timeline));
    act(() => result.current.cancel());
    expect(result.current.status).toEqual({ phase: "idle" });
    act(() => result.current.start());
    act(() => result.current.start());
    expect(runs).toHaveLength(1);
  });

  it("aborts on unmount, and a result arriving afterwards saves nothing", async () => {
    const { result, unmount } = renderHook(() => useClipExport(timeline));
    act(() => result.current.start());
    unmount();
    expect(run(0).options.signal.aborted).toBe(true);
    run(0).resolve(webm());
    await settle();
    expect(created).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("reports a failed save instead of claiming the clip was saved", async () => {
    URL.createObjectURL = vi.fn(() => {
      throw new Error("quota");
    });
    const { result } = renderHook(() => useClipExport(timeline));
    act(() => result.current.start());
    run(0).resolve(webm());
    await settle();
    expect(result.current.status).toEqual({
      phase: "error",
      message: "The clip was created but could not be saved. No file was saved.",
    });
    act(() => result.current.start());
    expect(runs).toHaveLength(2);
  });
});
