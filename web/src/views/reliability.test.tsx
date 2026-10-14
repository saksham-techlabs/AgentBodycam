import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient, type FetchLike } from "../api/client";
import type { SessionSummary } from "../api/types";
import { App } from "../app/App";
import { LIVE_POLL_INTERVAL_MS } from "../hooks/usePolling";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { item, longTimeline } from "../test/fixtures";
import { CRASHED_ID, FakeServer, LIVE_ID, STOPPED_ID } from "../test/fakeServer";
import { THEATER_HEIGHT_VAR } from "./PlaybackBar";

let server: FakeServer;
let fetchImpl: FetchLike;
const realSetTimeout = globalThis.setTimeout;
const BASE = "2026-10-14T03:15:00Z";
const NEW_TOKEN = "Rs8wY1_u-Z3mNn0Qx5Sc8Od2It7Eg1Hk6Vb3Lf9Wp4";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  server = new FakeServer();
  fetchImpl = server.fetch;
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function tick(ms: number): Promise<void> {
  let left = ms;
  do {
    const step = Math.min(left, 100);
    await act(() => vi.advanceTimersByTimeAsync(step));
    left -= step;
  } while (left > 0);
}

/** Retries `check`, yielding to the real event loop; fake time does not advance. */
async function waitFor(check: () => void, tries = 400): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      check();
      return;
    } catch (error) {
      if (attempt >= tries) throw error;
    }
    await act(async () => {
      await new Promise((resolve) => realSetTimeout(resolve, 0));
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function openTimeline(sessionId = STOPPED_ID) {
  window.history.replaceState(null, "", `/session/${sessionId}`);
  const view = render(<App client={new ReplayApiClient((input, init) => fetchImpl(input, init))} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: sessionId })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
  await waitFor(() => expect(screen.getByTestId("playback-counter")).not.toHaveTextContent("/ 0"));
  return view;
}

const counter = () => screen.getByTestId("playback-counter").textContent;
const status = () => screen.getByTestId("playback-status").textContent;
const playButton = () => screen.getByTestId("play-button");
const rows = () => screen.queryAllByTestId("timeline-row");
const page = () => document.querySelector<HTMLElement>(".page")!;
const sequences = () => rows().map((r) => r.querySelector(".tl-seq")?.textContent ?? "");
const timelineRequests = () => server.pathsRequested().filter((p) => p.includes("/timeline"));
const sessionRequests = (id: string) => server.pathsRequested().filter((p) => p.startsWith(`/api/v1/sessions/${id}`));

async function scrubTo(sequence: number): Promise<void> {
  const slider = screen.getByRole("slider", { name: "Replay position by event sequence" });
  fireEvent.change(slider, { target: { value: String(sequence) } });
  fireEvent.pointerUp(slider);
  await tick(0);
}

function stopLiveSession(): void {
  server.sessions = server.sessions.map((s): SessionSummary =>
    s.session_id === LIVE_ID && s.readable
      ? { ...s, status: "stopped", recording_state: "stopped", analysis_complete: true, ended_at: "2026-10-14T03:20:00.000000Z", event_count: 4 }
      : s,
  );
  server.timelines.get(LIVE_ID)!.push(item(4, "session.stopped", "Recording stopped", { salt: "c", base: BASE }));
}

// -- large sessions --------------------------------------------------------------------------

describe("large sessions", () => {
  it("reaches a late event of a 10,000-event session with a bounded DOM", async () => {
    server.timelines.set(CRASHED_ID, longTimeline(10_000));
    await openTimeline(CRASHED_ID);
    await scrubTo(9_999);
    await waitFor(() => expect(counter()).toBe("#9,999 / 10,000"), 2000);
    expect(timelineRequests().length).toBeLessThanOrEqual(21);
    expect(rows().length).toBeLessThan(60);
    expect(screen.getByTestId("event-detail")).toHaveTextContent("#9,999");
    const ids = rows().map((r) => r.getAttribute("data-event-id"));
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("cancels a long jump in a 30,000-event session without further page requests", async () => {
    server.timelines.set(CRASHED_ID, longTimeline(30_000));
    let held: (() => void) | null = null;
    fetchImpl = (input, init) => {
      if (input.includes("/timeline") && timelineRequests().length === 5 && held === null) {
        return new Promise((resolve, reject) => {
          held = () => void server.fetch(input, init).then(resolve, reject);
        });
      }
      return server.fetch(input, init);
    };
    await openTimeline(CRASHED_ID);
    await scrubTo(29_619);
    await waitFor(() => expect(held).not.toBeNull(), 2000);
    const notice = screen.getByText(/Loading timeline pages up to event #29,619/);
    fireEvent.click(within(notice).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/Loading timeline pages up to/)).toBeNull();
    (held as unknown as () => void)();
    await tick(5_000);
    // The first page, four jump pages and the one in flight at the cancel; nothing after it.
    expect(timelineRequests()).toHaveLength(6);
    expect(counter()).toBe("— / 30,000");
    expect(rows().length).toBeLessThan(60);
  });
});

// -- live edge cases -------------------------------------------------------------------------

describe("live sessions that stop", () => {
  it("keep playing through the stop, end on the final event and stop polling", async () => {
    await openTimeline(LIVE_ID);
    fireEvent.click(screen.getByRole("button", { name: "Playback speed 0.5x" }));
    fireEvent.click(playButton());
    expect(counter()).toBe("#1 / 3");
    stopLiveSession();
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(screen.queryByText("Auto-refresh")).toBeNull());
    expect(status()).toBe("PLAYING");
    await tick(1_600 * 4);
    await waitFor(() => expect(status()).toBe("END OF RECORDING"));
    expect(counter()).toBe("#4 / 4");
    expect(screen.queryByRole("checkbox", { name: "Follow live" })).toBeNull();
    const settled = sessionRequests(LIVE_ID).length;
    await tick(LIVE_POLL_INTERVAL_MS * 6);
    expect(sessionRequests(LIVE_ID)).toHaveLength(settled);
  });

  it("follow live onto the final event when the recording stops", async () => {
    await openTimeline(LIVE_ID);
    fireEvent.click(playButton());
    await tick(800 * 2);
    expect(counter()).toBe("#3 / 3");
    fireEvent.click(screen.getByRole("checkbox", { name: "Follow live" }));
    stopLiveSession();
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(counter()).toBe("#4 / 4"));
    await waitFor(() => expect(status()).toBe("END OF RECORDING"));
    expect(screen.getByTestId("current-event")).toHaveTextContent("Recording stopped");
  });
});

// -- API failures ----------------------------------------------------------------------------

describe("API failures", () => {
  it("keeps polling through a network outage and recovers without a reload", async () => {
    await openTimeline(LIVE_ID);
    server.override = () => "network-error";
    const before = sessionRequests(LIVE_ID).length;
    await tick(LIVE_POLL_INTERVAL_MS * 3);
    const connection = screen.getByTestId("connection-status");
    await waitFor(() => expect(connection).toHaveTextContent("Server unreachable"));
    expect(sequences()).toEqual(["#1", "#2", "#3"]);
    const during = sessionRequests(LIVE_ID).length;
    expect(during).toBeGreaterThan(before);

    server.override = () => undefined;
    server.timelines.get(LIVE_ID)!.push(item(4, "file.created", "Created notes.md", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(sequences()).toEqual(["#1", "#2", "#3", "#4"]));
    expect(connection).toHaveTextContent("Connected");
    expect(screen.getByText("Auto-refresh")).toBeInTheDocument();
  });

  it("bootstraps again after a server restart and keeps the session", async () => {
    await openTimeline(LIVE_ID);
    server.token = NEW_TOKEN;
    server.bootstrap = { status: 200, body: { api_version: 1, token: NEW_TOKEN } };
    server.timelines.get(LIVE_ID)!.push(item(4, "file.created", "Created notes.md", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(sequences()).toContain("#4"));
    expect(server.pathsRequested().filter((p) => p === "/ui/bootstrap").length).toBeGreaterThan(1);
    expect(screen.queryByText("Access expired")).toBeNull();
    expect(screen.getByText("Auto-refresh")).toBeInTheDocument();
  });

  it("clears the replay and stops everything on a second 401", async () => {
    await openTimeline(LIVE_ID);
    fireEvent.click(screen.getByRole("button", { name: "Playback speed 0.5x" }));
    fireEvent.click(playButton());
    server.token = NEW_TOKEN;
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(page()).toHaveTextContent("Access expired"));
    expect(screen.queryByTestId("playback")).toBeNull();
    expect(rows()).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Export" })).toBeNull();
    expect(screen.queryByText("Auto-refresh")).toBeNull();
    const settled = server.requests.length;
    await tick(LIVE_POLL_INTERVAL_MS * 6);
    expect(server.requests).toHaveLength(settled);
  });

  it("closes the export dialog when the session disappears", async () => {
    await openTimeline(LIVE_ID);
    const exportButton = screen.getByRole("button", { name: "Export" });
    await waitFor(() => expect(exportButton).toBeEnabled());
    fireEvent.click(exportButton);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    server.sessions = server.sessions.filter((s) => s.session_id !== LIVE_ID);
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(screen.getByText("Session not found")).toBeInTheDocument());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("playback")).toBeNull();
  });

  it("shows a refused request as fatal, with no stale replay behind it", async () => {
    await openTimeline(LIVE_ID);
    server.override = (path) =>
      path.startsWith("/api/v1/sessions/")
        ? { status: 403, body: { error: { code: "forbidden", message: "nope" } } }
        : undefined;
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(page()).toHaveTextContent("Access refused"));
    expect(rows()).toHaveLength(0);
    expect(screen.queryByTestId("playback")).toBeNull();
  });
});

// -- layout and accessibility ----------------------------------------------------------------

describe("sticky theater layout", () => {
  it("marks the theater while a playback alert is shown, and clears it on recovery", async () => {
    await openTimeline(CRASHED_ID);
    server.override = (path) => (path.includes("after_sequence=100") ? "network-error" : undefined);
    await scrubTo(100);
    expect(counter()).toBe("#100 / 250");
    fireEvent.click(screen.getByRole("button", { name: "Next event" }));
    await waitFor(() => expect(screen.getByTestId("playback")).toHaveClass("has-alert"));
    expect(screen.getByText(/Playback paused: the next timeline page could not be loaded/)).toBeInTheDocument();
    server.override = () => undefined;
    fireEvent.click(screen.getByRole("button", { name: "Next event" }));
    await waitFor(() => expect(counter()).toBe("#101 / 250"));
    expect(screen.getByTestId("playback")).not.toHaveClass("has-alert");
  });

  it("publishes the measured theater height on the replay container and removes it on unmount", async () => {
    const observers: { callback: ResizeObserverCallback; disconnected: boolean }[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        entry: { callback: ResizeObserverCallback; disconnected: boolean };
        constructor(callback: ResizeObserverCallback) {
          this.entry = { callback, disconnected: false };
          observers.push(this.entry);
        }
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {
          this.entry.disconnected = true;
        }
      },
    );
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const height = this.classList.contains("replay-theater") ? 351.4 : 0;
      return { height, width: 900, top: 0, left: 0, bottom: height, right: 900, x: 0, y: 0, toJSON: () => ({}) };
    });
    await openTimeline();
    const replay = document.querySelector<HTMLElement>(".replay");
    expect(observers).toHaveLength(1);
    act(() => observers[0]!.callback([], {} as ResizeObserver));
    expect(replay?.style.getPropertyValue(THEATER_HEIGHT_VAR)).toBe("352px");
    fireEvent.click(screen.getByRole("tab", { name: /^Findings/ }));
    expect(observers[0]!.disconnected).toBe(true);
  });

  it("sizes the timeline from the measured theater, with a fallback, and wraps controls by container width", async () => {
    // Vitest stubs CSS imports, so the stylesheet is read from disk.
    const fs = (await import("node:fs" as string)) as { readFileSync: (path: string, encoding: "utf8") => string };
    const css = fs.readFileSync("src/styles/app.css", "utf8").replace(/\s+/g, " ");
    const root = /:root \{[^}]*\}/.exec(css)?.[0] ?? "";
    expect(root).not.toContain("--replay-timeline-height");
    const replay = /\.replay \{[^}]*\}/.exec(css)?.[0] ?? "";
    expect(replay).toContain("var(--replay-theater-height, 304px)");
    expect(replay).toContain("--replay-timeline-height: clamp(224px,");
    expect(css).toContain(".replay-theater { container-type: inline-size; }");
    expect(css).toMatch(/@container \(min-width: 600px\) and \(max-width: 999px\) \{ \.playback-buttons \{ flex: 1 0 100%; \} \}/);
  });
});

describe("keyboard and motion", () => {
  it("ignores playback shortcuts while the export dialog is open", async () => {
    await openTimeline();
    const exportButton = screen.getByRole("button", { name: "Export" });
    await waitFor(() => expect(exportButton).toBeEnabled());
    fireEvent.click(exportButton);
    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(document.body, { key: " " });
    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(status()).toBe("PAUSED");
    expect(counter()).toBe("— / 21");
    fireEvent.keyDown(document.body, { key: "ArrowRight" });
    expect(counter()).toBe("#1 / 21");
  });

  it("follows reduced motion on engines with only the legacy listener API, and cleans up", () => {
    const listeners = new Set<() => void>();
    const list = {
      matches: false,
      addListener: vi.fn((l: () => void) => listeners.add(l)),
      removeListener: vi.fn((l: () => void) => listeners.delete(l)),
    };
    vi.stubGlobal("matchMedia", vi.fn(() => list));
    const { result, unmount } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
    list.matches = true;
    act(() => listeners.forEach((l) => l()));
    expect(result.current).toBe(true);
    unmount();
    expect(list.removeListener).toHaveBeenCalledTimes(1);
    expect(listeners.size).toBe(0);
  });
});
