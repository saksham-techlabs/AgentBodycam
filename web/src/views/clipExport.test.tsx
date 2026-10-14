import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "../api/client";
import { App } from "../app/App";
import { FakeServer, STOPPED_ID } from "../test/fakeServer";
import { CAUSAL } from "../test/fixtures";
import { installMediaMocks, type MediaMockOptions, type MediaMocks } from "../test/media";

const CLIP_MS = 12_000;

let server: FakeServer;
let mocks: MediaMocks | undefined;
let blobs: Blob[];
let revoked: string[];
let clicks: { download: string; href: string }[];

beforeEach(() => {
  server = new FakeServer();
  blobs = [];
  revoked = [];
  clicks = [];
  let n = 0;
  URL.createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob);
    n += 1;
    return `blob:local/${n}`;
  });
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicks.push({ download: this.download, href: this.getAttribute("href") ?? "" });
  });
});

afterEach(() => {
  vi.useRealTimers();
  mocks?.restore();
  mocks = undefined;
});

async function openClip(options: MediaMockOptions = {}): Promise<HTMLElement> {
  mocks = installMediaMocks(options);
  window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
  render(<App client={new ReplayApiClient(server.fetch)} />);
  await screen.findByRole("heading", { name: STOPPED_ID });
  const button = await screen.findByRole("button", { name: "Export" });
  await vi.waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  const dialog = screen.getByRole("dialog", { name: "Export replay summary" });
  fireEvent.click(within(dialog).getByRole("button", { name: /Replay clip/ }));
  // The session is loaded; from here on only the export's own timers run, under test control.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  return dialog;
}

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
const exportButton = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: "Export WebM" });
const status = (dialog: HTMLElement) => within(dialog).getByTestId("clip-status");

function media(): MediaMocks {
  if (mocks === undefined) throw new Error("media mocks not installed");
  return mocks;
}

function tracksStopped(): boolean {
  const streams = media().streams;
  return streams.length > 0 && streams.every((s) => s.tracks.every((t) => t.stopped));
}

describe("replay clip export", () => {
  it("offers Share card, Replay clip and JSON summary, with the clip's format, length and support", async () => {
    const dialog = await openClip();
    const formats = within(within(dialog).getByRole("group", { name: "Export format" })).getAllByRole("button");
    expect(formats.map((b) => b.textContent)).toEqual([
      "Share cardSVG · 1200×630",
      "Replay clipWebM · 1280×720",
      "JSON summaryCounts and check results",
    ]);
    expect(within(dialog).getByRole("button", { name: /Replay clip/ })).toHaveAttribute("aria-pressed", "true");
    const panel = within(dialog).getByTestId("clip-panel");
    expect(panel).toHaveTextContent("Generated replay clip from recorded evidence.");
    expect(panel).toHaveTextContent("WebM · 1280×720 · 30 fps");
    expect(panel).toHaveTextContent("About 12 seconds");
    expect(within(dialog).getByTestId("clip-support")).toHaveTextContent("Supported · VP9");
    expect(within(dialog).getByTestId("export-privacy-note")).toHaveTextContent(
      "Source code, paths, command arguments and identifiers are excluded.",
    );
    const scenes = within(panel).getByRole("list", { name: "Clip scenes" });
    expect(within(scenes).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Intro",
      "Session stats",
      "First observable failure",
      "Observed recovery",
      "Final recorded state",
      "Outro",
    ]);
    expect(exportButton(dialog)).toBeEnabled();
    const text = dialog.textContent ?? "";
    expect(text).not.toMatch(CAUSAL);
    expect(text).not.toMatch(/screen ?record|screen ?capture|desktop capture|footage|recorded video|video of the agent/i);
    expect(text).not.toContain(STOPPED_ID);
  });

  it("says so, and cannot export, without MediaRecorder", async () => {
    const dialog = await openClip({ mediaRecorder: false });
    expect(within(dialog).getByTestId("clip-support")).toHaveTextContent(
      "Replay clip export is not supported in this browser.",
    );
    expect(exportButton(dialog)).toBeDisabled();
  });

  it("says so, and cannot export, without canvas.captureStream", async () => {
    const dialog = await openClip({ captureStream: false });
    expect(within(dialog).getByTestId("clip-support")).toHaveTextContent(
      "Replay clip export is not supported in this browser.",
    );
    expect(exportButton(dialog)).toBeDisabled();
  });

  it("shows progress through rendering and encoding, then downloads agentbodycam-replay.webm", async () => {
    const dialog = await openClip();
    fireEvent.click(exportButton(dialog));
    expect(status(dialog)).toHaveTextContent("Rendering 0%");
    expect(exportButton(dialog)).toHaveAttribute("aria-disabled", "true");

    await advance(CLIP_MS / 2);
    expect(status(dialog)).toHaveTextContent("Rendering 50%");
    expect(within(dialog).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");

    await advance(CLIP_MS / 2);
    expect(status(dialog)).toHaveTextContent("Encoding…");
    expect(clicks).toEqual([]);

    await advance(60);
    expect(clicks).toEqual([{ download: "agentbodycam-replay.webm", href: "blob:local/1" }]);
    expect(blobs[0]?.type).toBe("video/webm");
    expect(blobs[0]?.size).toBe(4096);
    expect(status(dialog)).toHaveTextContent("Done · saved agentbodycam-replay.webm (4 KB)");
    expect(document.querySelector("a[download]")).toBeNull();
    expect(tracksStopped()).toBe(true);
    expect(exportButton(dialog)).toHaveAttribute("aria-disabled", "false");

    expect(revoked).toEqual([]);
    await advance(2_000);
    expect(revoked).toEqual(["blob:local/1"]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("starts a single export however often Export WebM is clicked", async () => {
    const dialog = await openClip();
    fireEvent.click(exportButton(dialog));
    fireEvent.click(exportButton(dialog));
    await advance(1000);
    fireEvent.click(exportButton(dialog));
    expect(media().recorders).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: /Share card/ })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: /JSON summary/ })).toBeDisabled();
    await advance(CLIP_MS + 100);
    expect(clicks).toHaveLength(1);
  });

  it("cancels: stops rendering, the recorder and the tracks, and returns to ready", async () => {
    const dialog = await openClip();
    fireEvent.click(exportButton(dialog));
    await advance(4000);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(tracksStopped()).toBe(true);
    expect(media().recorders[0]?.state).toBe("inactive");
    expect(status(dialog)).toHaveTextContent(/^$/);
    expect(within(dialog).queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(exportButton(dialog)).toHaveAttribute("aria-disabled", "false");
    await advance(CLIP_MS);
    expect(clicks).toEqual([]);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);

    fireEvent.click(exportButton(dialog));
    expect(media().recorders).toHaveLength(2);
  });

  it("reports a recorder error and saves nothing", async () => {
    const dialog = await openClip();
    fireEvent.click(exportButton(dialog));
    await advance(2000);
    act(() => media().recorders[0]?.fail());
    await advance(0);
    expect(status(dialog)).toHaveTextContent("The browser's recorder reported an error. No file was saved.");
    expect(tracksStopped()).toBe(true);
    expect(clicks).toEqual([]);
    expect(exportButton(dialog)).toHaveAttribute("aria-disabled", "false");
  });

  it("rejects a zero-byte clip", async () => {
    const dialog = await openClip({ chunkBytes: 0 });
    fireEvent.click(exportButton(dialog));
    await advance(CLIP_MS + 100);
    expect(status(dialog)).toHaveTextContent("The browser produced an empty clip. No file was saved.");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(clicks).toEqual([]);
  });

  it("stops everything when the dialog closes mid-export", async () => {
    const dialog = await openClip();
    fireEvent.click(exportButton(dialog));
    await advance(3000);
    fireEvent.keyDown(exportButton(dialog), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(tracksStopped()).toBe(true);
    expect(media().recorders[0]?.state).toBe("inactive");
    await advance(CLIP_MS);
    expect(clicks).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("makes no network request while planning, rendering, encoding or downloading", async () => {
    const dialog = await openClip();
    const before = server.requests.length;
    const fetchSpy = vi.fn(() => Promise.reject(new Error("no network")));
    vi.stubGlobal("fetch", fetchSpy);
    const xhr = vi.spyOn(XMLHttpRequest.prototype, "open");
    const beacon = vi.fn(() => false);
    Object.defineProperty(navigator, "sendBeacon", { value: beacon, configurable: true });
    try {
      fireEvent.click(exportButton(dialog));
      await advance(CLIP_MS + 100);
      await advance(2_000);
      expect(clicks).toHaveLength(1);
      expect(server.requests.length).toBe(before);
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(xhr).not.toHaveBeenCalled();
      expect(beacon).not.toHaveBeenCalled();
      expect(clicks[0]?.href.startsWith("blob:")).toBe(true);
    } finally {
      Reflect.deleteProperty(navigator, "sendBeacon");
    }
  });
});
