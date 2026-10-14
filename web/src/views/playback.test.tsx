import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "../api/client";
import type { ForensicReport, SessionSummary } from "../api/types";
import { App } from "../app/App";
import { JUMP_PAGE_CAP } from "../hooks/useTimeline";
import { LIVE_POLL_INTERVAL_MS } from "../hooks/usePolling";
import { CAUSAL, item, longTimeline } from "../test/fixtures";
import { CRASHED_ID, CREATED_ID, FakeServer, LIVE_ID, STOPPED_ID, UNKNOWN_ID } from "../test/fakeServer";

let server: FakeServer;
let hidden: boolean;
const realSetTimeout = globalThis.setTimeout;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  server = new FakeServer();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * Advance fake time in 100 ms steps, each in its own `act`, so React commits and runs the
 * effect that schedules the next playback step (every interval is a multiple of 100 ms).
 */
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
  const view = render(<App client={new ReplayApiClient(server.fetch)} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: sessionId })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
  await waitFor(() => expect(screen.getByTestId("playback-counter")).not.toHaveTextContent("/ 0"));
  return view;
}

const counter = () => screen.getByTestId("playback-counter").textContent;
const status = () => screen.getByTestId("playback-status").textContent;
const playButton = () => screen.getByTestId("play-button");
const card = () => screen.getByTestId("current-event");
const slider = () => screen.getByRole("slider", { name: "Replay position by event sequence" });
const timelineRequests = () => server.pathsRequested().filter((p) => p.includes("/timeline"));

function row(sequence: number): HTMLElement {
  const label = `#${sequence.toLocaleString("en-US")}`;
  const found = screen
    .queryAllByTestId("timeline-row")
    .find((r) => r.querySelector(".tl-seq")?.textContent === label);
  if (found === undefined) throw new Error(`row #${sequence} not rendered`);
  return found;
}

function speed(label: string): void {
  fireEvent.click(screen.getByRole("button", { name: `Playback speed ${label}` }));
}

function key(k: string, options: { shiftKey?: boolean; target?: Element } = {}): void {
  fireEvent.keyDown(options.target ?? document.body, { key: k, shiftKey: options.shiftKey ?? false });
}

async function scrubTo(sequence: number): Promise<void> {
  fireEvent.change(slider(), { target: { value: String(sequence) } });
  fireEvent.pointerUp(slider());
  await tick(0);
}

function withReport(sessionId: string, mutate: (report: ForensicReport) => void): void {
  const report = server.reportFor(sessionId)!;
  mutate(report);
  server.reports.set(sessionId, report);
}

function setHidden(value: boolean): void {
  hidden = value;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

// -- play / pause / timing -------------------------------------------------------------------

describe("play and pause", () => {
  it("starts playback on the first event", async () => {
    await openTimeline();
    expect(counter()).toBe("— / 21");
    expect(playButton()).toHaveTextContent("Play");
    fireEvent.click(playButton());
    expect(counter()).toBe("#1 / 21");
    expect(status()).toBe("PLAYING");
    expect(playButton()).toHaveTextContent("Pause");
    expect(playButton()).toHaveAccessibleName("Pause replay");
  });

  it("stops advancing when paused", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800);
    expect(counter()).toBe("#2 / 21");
    fireEvent.click(playButton());
    expect(status()).toBe("PAUSED");
    await tick(10_000);
    expect(counter()).toBe("#2 / 21");
  });

  it("advances one event per interval", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(799);
    expect(counter()).toBe("#1 / 21");
    await tick(1);
    expect(counter()).toBe("#2 / 21");
    await tick(800);
    expect(counter()).toBe("#3 / 21");
  });

  it.each([
    ["0.5x", 1600],
    ["1x", 800],
    ["2x", 400],
    ["4x", 200],
  ])("plays at %s (%i ms per event)", async (label, interval) => {
    await openTimeline();
    expect(screen.getByRole("button", { name: "Playback speed 1x" })).toHaveAttribute("aria-pressed", "true");
    speed(label);
    expect(screen.getByRole("button", { name: `Playback speed ${label}` })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(playButton());
    await tick(interval - 1);
    expect(counter()).toBe("#1 / 21");
    await tick(1);
    expect(counter()).toBe("#2 / 21");
    await tick(interval * 3);
    expect(counter()).toBe("#5 / 21");
  });

  it("stops at the end of the recording and offers a replay", async () => {
    await openTimeline();
    speed("4x");
    fireEvent.click(playButton());
    await tick(200 * 30);
    expect(counter()).toBe("#21 / 21");
    expect(status()).toBe("END OF RECORDING");
    expect(playButton()).toHaveTextContent("Replay");
    await tick(5000);
    expect(counter()).toBe("#21 / 21");

    fireEvent.click(playButton());
    expect(counter()).toBe("#1 / 21");
    expect(status()).toBe("PLAYING");
    await tick(200);
    expect(counter()).toBe("#2 / 21");
  });
});

// -- pages -----------------------------------------------------------------------------------

describe("playback across pages", () => {
  it("loads the next page at the end of the loaded events and continues", async () => {
    await openTimeline(CRASHED_ID);
    expect(screen.getByTestId("scrub-loaded-label")).toHaveTextContent("100 / 250 loaded");
    await scrubTo(100);
    expect(counter()).toBe("#100 / 250");
    speed("4x");
    fireEvent.click(playButton());
    await tick(200);
    await waitFor(() => expect(counter()).toBe("#101 / 250"));
    expect(timelineRequests()).toEqual([
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=0&limit=100`,
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=100&limit=100`,
    ]);
    expect(screen.getByTestId("scrub-loaded-label")).toHaveTextContent("200 / 250 loaded");
    expect(status()).toBe("PLAYING");
  });

  it("pauses safely and keeps its position when the next page fails", async () => {
    await openTimeline(CRASHED_ID);
    server.override = (path) => (path.includes("after_sequence=100") ? "network-error" : undefined);
    await scrubTo(100);
    speed("4x");
    fireEvent.click(playButton());
    await tick(200);
    await waitFor(() => expect(status()).toBe("PAUSED · PAGE NOT LOADED"));
    expect(counter()).toBe("#100 / 250");
    expect(playButton()).toHaveTextContent("Play");
    expect(screen.getByText(/the next timeline page could not be loaded/)).toHaveAttribute("role", "alert");
    expect(screen.getByText(/the next timeline page could not be loaded/)).toHaveTextContent(
      "Replay server unavailable",
    );
    await tick(5000);
    expect(counter()).toBe("#100 / 250");
  });

  it("treats an invalid next page as a stall, not as data", async () => {
    await openTimeline(CRASHED_ID);
    server.override = (path) =>
      path.includes("after_sequence=100") ? { status: 200, body: { api_version: 1 } } : undefined;
    await scrubTo(100);
    fireEvent.click(playButton());
    await tick(800);
    await waitFor(() => expect(status()).toBe("PAUSED · PAGE NOT LOADED"));
    expect(screen.getByText(/the next timeline page could not be loaded/)).toHaveTextContent(
      "Unexpected response",
    );
    expect(counter()).toBe("#100 / 250");
  });
});

// -- scrubber --------------------------------------------------------------------------------

describe("scrubber", () => {
  it("moves the current event", async () => {
    await openTimeline();
    fireEvent.change(slider(), { target: { value: "9" } });
    expect(counter()).toBe("#9 / 21");
    expect(card()).toHaveTextContent("#9");
    expect(row(9)).toHaveAttribute("data-current", "true");
    expect(slider()).toHaveAttribute("aria-valuetext", "Event 9 of 21; 21 loaded");
  });

  it("loads pages to reach an unloaded event, only when released", async () => {
    await openTimeline(CRASHED_ID);
    fireEvent.change(slider(), { target: { value: "230" } });
    expect(screen.getByTestId("scrub-loaded-label")).toHaveTextContent("release to load up to #230");
    expect(timelineRequests()).toHaveLength(1);
    fireEvent.pointerUp(slider());
    await waitFor(() => expect(counter()).toBe("#230 / 250"));
    expect(timelineRequests()).toEqual([
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=0&limit=100`,
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=100&limit=500`,
    ]);
    expect(screen.getByTestId("event-detail")).toHaveTextContent("#230");
  });

  it("stops seeking at the page cap", async () => {
    server.timelines.set(CRASHED_ID, longTimeline(26_000));
    await openTimeline(CRASHED_ID);
    await scrubTo(26_000);
    await waitFor(() => expect(screen.getByText(/lies beyond the 50 pages/)).toBeInTheDocument(), 2000);
    expect(timelineRequests()).toHaveLength(1 + JUMP_PAGE_CAP);
    expect(counter()).toBe("— / 26,000");
    expect(screen.getByTestId("scrub-loaded-label")).toHaveTextContent("25,100 / 26,000 loaded");
  });

  it("places markers by sequence and shows how much is loaded", async () => {
    await openTimeline();
    const marks = screen.getAllByTestId("scrubber-marker");
    expect(marks.map((m) => [m.dataset.sequence, m.dataset.kind, m.style.left])).toEqual([
      ["5", "first_failure", "20%"],
      ["9", "check_failed", "40%"],
      ["14", "recovered", "65%"],
      ["17", "unresolved", "80%"],
    ]);
    expect(screen.getByTestId("scrub-loaded")).toHaveStyle({ width: "100%" });
  });

  it("does not show unloaded events as loaded", async () => {
    await openTimeline(CRASHED_ID);
    const width = parseFloat(screen.getByTestId("scrub-loaded").style.width);
    expect(width).toBeCloseTo((99 / 249) * 100, 5);
    expect(screen.getByTestId("scrub-loaded-label")).toHaveTextContent("100 / 250 loaded");
  });
});

// -- jumps -----------------------------------------------------------------------------------

describe("jumps", () => {
  it("jumps to the first failure and pauses", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800);
    fireEvent.click(screen.getByRole("button", { name: "Jump to first failure" }));
    await waitFor(() => expect(counter()).toBe("#5 / 21"));
    expect(status()).toBe("PAUSED");
    expect(row(5)).toHaveAttribute("data-current", "true");
    await waitFor(() => expect(row(5)).toHaveFocus());
    expect((slider() as HTMLInputElement).value).toBe("5");
    await tick(5000);
    expect(counter()).toBe("#5 / 21");
  });

  it("jumps to an observed recovery from its marker", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    const marker = screen.getByRole("button", { name: "OBSERVED RECOVERY, event #14" });
    fireEvent.click(marker);
    await waitFor(() => expect(counter()).toBe("#14 / 21"));
    expect(status()).toBe("PAUSED");
    expect(card()).toHaveTextContent("OBSERVED RECOVERY");
    expect(card()).toHaveTextContent("pytest passed");
    expect(screen.getByTestId("event-detail")).toHaveTextContent("#14");
    expect(row(14)).toHaveAttribute("data-current", "true");
  });

  it("steps through important events in both directions", async () => {
    await openTimeline();
    const next = screen.getByRole("button", { name: "Next important event" });
    const prev = screen.getByRole("button", { name: "Previous important event" });
    expect(prev).toBeDisabled();
    const seen: string[] = [];
    for (let i = 0; i < 6; i += 1) {
      fireEvent.click(next);
      await tick(0);
      seen.push(counter() ?? "");
    }
    expect(seen).toEqual(["#1 / 21", "#5 / 21", "#9 / 21", "#14 / 21", "#17 / 21", "#21 / 21"]);
    expect(next).toBeDisabled();
    fireEvent.click(prev);
    await tick(0);
    expect(counter()).toBe("#17 / 21");
    fireEvent.click(prev);
    await tick(0);
    expect(counter()).toBe("#14 / 21");
  });

  it("reaches an important event that is not loaded yet", async () => {
    withReport(CRASHED_ID, (r) => {
      r.recoveries = {
        items: [{ ...server.reportFor(STOPPED_ID)!.recoveries.items[0]!, recovered_sequence: 240, recovered_event_id: longTimeline(250)[239]!.event_id }],
        total: 1,
        truncated: false,
      };
    });
    await openTimeline(CRASHED_ID);
    fireEvent.click(screen.getByRole("button", { name: "Next important event" }));
    await tick(0);
    expect(counter()).toBe("#1 / 250");
    fireEvent.click(screen.getByRole("button", { name: "Next important event" }));
    await waitFor(() => expect(counter()).toBe("#240 / 250"));
  });
});

// -- current event ---------------------------------------------------------------------------

describe("current event", () => {
  it("is highlighted in the timeline", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800);
    expect(row(2)).toHaveClass("is-current");
    expect(row(2)).toHaveTextContent("Current replay event.");
    expect(row(1)).not.toHaveClass("is-current");
    expect(document.querySelectorAll(".tl-row.is-current")).toHaveLength(1);
  });

  it("fills the event detail panel and the current-event card", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800 * 5);
    expect(counter()).toBe("#6 / 21");
    expect(screen.getByTestId("event-detail")).toHaveTextContent("#6");
    expect(screen.getByTestId("event-detail")).toHaveTextContent("src/app.py");
    const now = card();
    expect(now).toHaveTextContent("#6");
    expect(now).toHaveTextContent("02:15:06.412 UTC");
    expect(now).toHaveTextContent("FILE MODIFIED");
    expect(now).toHaveTextContent("file.modified");
    expect(now).toHaveTextContent("Modified src/app.py");
    expect(now).toHaveTextContent("src/app.py");
    expect(now).toHaveTextContent("OBSERVED");
  });

  it("leaves a closed detail panel closed during automatic playback", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800);
    fireEvent.click(screen.getByRole("button", { name: "Close event details" }));
    expect(screen.queryByTestId("event-detail")).toBeNull();
    expect(status()).toBe("PLAYING");
    await tick(800 * 2);
    expect(counter()).toBe("#4 / 21");
    expect(screen.queryByTestId("event-detail")).toBeNull();
  });

  it("follows a row picked in the timeline", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    fireEvent.click(row(11));
    expect(counter()).toBe("#11 / 21");
    expect(status()).toBe("PAUSED");
    fireEvent.click(playButton());
    await tick(800);
    expect(counter()).toBe("#12 / 21");
  });
});

// -- keyboard --------------------------------------------------------------------------------

describe("keyboard shortcuts", () => {
  it("toggle playback with Space", async () => {
    await openTimeline();
    key(" ");
    expect(status()).toBe("PLAYING");
    key(" ");
    expect(status()).toBe("PAUSED");
    expect(counter()).toBe("#1 / 21");
  });

  it("step with Left/Right and jump between important events with Shift", async () => {
    await openTimeline();
    key("ArrowRight");
    expect(counter()).toBe("#1 / 21");
    key("ArrowRight");
    expect(counter()).toBe("#2 / 21");
    key("ArrowLeft");
    expect(counter()).toBe("#1 / 21");
    key("ArrowRight", { shiftKey: true });
    await tick(0);
    expect(counter()).toBe("#5 / 21");
    key("ArrowRight", { shiftKey: true });
    await tick(0);
    expect(counter()).toBe("#9 / 21");
    key("ArrowLeft", { shiftKey: true });
    await tick(0);
    expect(counter()).toBe("#5 / 21");
  });

  it("are ignored while typing or when a control owns the key", async () => {
    await openTimeline();
    const search = screen.getByRole("searchbox", { name: "Search loaded events" });
    search.focus();
    key(" ", { target: search });
    key("ArrowRight", { target: search });
    key("ArrowRight", { target: search, shiftKey: true });
    expect(counter()).toBe("— / 21");
    expect(status()).toBe("PAUSED");

    const filter = screen.getByRole("button", { name: "Checks" });
    key(" ", { target: filter });
    expect(status()).toBe("PAUSED");

    // The tab list uses Left/Right itself, so playback does not also step.
    const tab = screen.getByRole("tab", { name: /^Timeline/ });
    key("ArrowRight", { target: tab });
    expect(screen.getByRole("tab", { name: /^Findings/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    expect(counter()).toBe("— / 21");
  });

  it("leave Space and arrows to the focused scrubber", async () => {
    await openTimeline();
    slider().focus();
    expect(slider()).toHaveFocus();
    key(" ", { target: slider() });
    expect(status()).toBe("PAUSED");
    expect(counter()).toBe("— / 21");
    key("ArrowRight", { target: slider() });
    expect(counter()).toBe("— / 21");

    await scrubTo(4);
    expect(counter()).toBe("#4 / 21");
    slider().focus();
    key(" ", { target: slider() });
    expect(status()).toBe("PAUSED");
    expect(counter()).toBe("#4 / 21");

    // Space elsewhere still toggles playback.
    key(" ");
    expect(status()).toBe("PLAYING");
  });

  it("move within the marker rail without moving playback", async () => {
    await openTimeline();
    const rail = screen.getByRole("toolbar", { name: "Important event markers" });
    const marks = within(rail).getAllByRole("button");
    expect(marks.filter((m) => m.tabIndex === 0)).toHaveLength(1);
    marks[0]!.focus();
    key("ArrowRight", { target: marks[0]! });
    expect(marks[1]).toHaveFocus();
    expect(counter()).toBe("— / 21");
  });
});

// -- visibility and motion -------------------------------------------------------------------

describe("visibility and motion", () => {
  it("pauses when the tab is hidden and stays paused when visible again", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    await tick(800);
    expect(counter()).toBe("#2 / 21");
    setHidden(true);
    expect(status()).toBe("PAUSED");
    await tick(10_000);
    expect(counter()).toBe("#2 / 21");
    setHidden(false);
    await tick(10_000);
    expect(status()).toBe("PAUSED");
    expect(counter()).toBe("#2 / 21");
  });

  function stubMotion(reduce: boolean): ReturnType<typeof vi.fn> {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: reduce && query.includes("reduce"),
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const scrollTo = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { value: scrollTo, configurable: true });
    return scrollTo;
  }

  afterEach(() => {
    delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo;
  });

  it("scrolls smoothly to follow playback by default", async () => {
    const scrollTo = stubMotion(false);
    await openTimeline(CRASHED_ID);
    speed("4x");
    fireEvent.click(playButton());
    await tick(200 * 20);
    expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: "smooth" }));
  });

  it("never scrolls smoothly when reduced motion is requested", async () => {
    const scrollTo = stubMotion(true);
    await openTimeline(CRASHED_ID);
    speed("4x");
    fireEvent.click(playButton());
    await tick(200 * 20);
    expect(counter()).toBe("#21 / 250");
    expect(scrollTo).not.toHaveBeenCalled();
    expect(screen.getByTestId("timeline-scroll").scrollTop).toBeGreaterThan(0);
    expect(row(21)).toHaveClass("is-current");
  });
});

// -- edge cases ------------------------------------------------------------------------------

describe("edge cases", () => {
  it("has nothing to play without events", async () => {
    window.history.replaceState(null, "", `/session/${CREATED_ID}`);
    render(<App client={new ReplayApiClient(server.fetch)} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: CREATED_ID })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    await waitFor(() => expect(screen.getByText("No events recorded")).toBeInTheDocument());
    expect(playButton()).toBeDisabled();
    expect(counter()).toBe("— / 0");
    expect(slider()).toBeDisabled();
    expect(card()).toHaveTextContent("No events to replay.");
    key(" ");
    expect(status()).toBe("PAUSED");
  });

  it("says it is loading before the first page, not that there is nothing to replay", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const fetch = server.fetch;
    const client = new ReplayApiClient(async (input, init) => {
      if (input.includes("/timeline")) await gate;
      return fetch(input, init);
    });
    window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
    render(<App client={client} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: STOPPED_ID })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    expect(card()).toHaveTextContent("Loading recorded events…");
    expect(card()).not.toHaveTextContent("No events to replay");
    expect(screen.getByTestId("playback-announcement")).toHaveTextContent("Loading recorded events.");
    expect(playButton()).toBeDisabled();
    release!();
    await waitFor(() => expect(counter()).toBe("— / 21"));
    expect(card()).toHaveTextContent("Press Play to step through the 21 recorded events");
  });

  it("says so when the first page cannot be loaded", async () => {
    server.override = (path) => (path.includes("/timeline") ? "network-error" : undefined);
    window.history.replaceState(null, "", `/session/${STOPPED_ID}`);
    render(<App client={new ReplayApiClient(server.fetch)} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: STOPPED_ID })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    await waitFor(() => expect(card()).toHaveTextContent("Recorded events could not be loaded."));
    expect(playButton()).toBeDisabled();
  });

  it("plays a single event and ends", async () => {
    server.timelines.set(UNKNOWN_ID, [item(1, "session.started", "Recording started", { salt: "d" })]);
    server.sessions = server.sessions.map((s): SessionSummary =>
      s.session_id === UNKNOWN_ID && s.readable ? { ...s, event_count: 1 } : s,
    );
    await openTimeline(UNKNOWN_ID);
    expect(counter()).toBe("— / 1");
    fireEvent.click(playButton());
    expect(counter()).toBe("#1 / 1");
    await tick(800);
    expect(status()).toBe("END OF RECORDING");
    expect(playButton()).toHaveTextContent("Replay");
  });

  it("resets when another session is opened", async () => {
    await openTimeline();
    speed("4x");
    fireEvent.click(playButton());
    await tick(200 * 3);
    expect(counter()).toBe("#4 / 21");
    act(() => {
      window.history.pushState(null, "", `/session/${UNKNOWN_ID}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(screen.getByRole("heading", { name: UNKNOWN_ID })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    await waitFor(() => expect(counter()).toBe("— / 2"));
    expect(status()).toBe("PAUSED");
    expect(screen.getByRole("button", { name: "Playback speed 1x" })).toHaveAttribute("aria-pressed", "true");
    await tick(5000);
    expect(counter()).toBe("— / 2");
  });

  it("pauses when another tab is opened", async () => {
    await openTimeline();
    fireEvent.click(playButton());
    fireEvent.click(screen.getByRole("tab", { name: /^Findings/ }));
    await tick(5000);
    fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
    expect(counter()).toBe("#1 / 21");
    expect(status()).toBe("PAUSED");
  });
});

// -- live ------------------------------------------------------------------------------------

describe("live recordings", () => {
  const BASE = "2026-10-14T03:15:00Z";

  async function playToLiveEdge(): Promise<void> {
    await openTimeline(LIVE_ID);
    fireEvent.click(playButton());
    await tick(800 * 3);
    expect(counter()).toBe("#3 / 3");
  }

  it("stops at the live edge and plays only recorded events", async () => {
    await playToLiveEdge();
    expect(status()).toBe("LIVE EDGE");
    // A recording state, not failure evidence: never the danger tone.
    const chip = screen.getByTestId("playback-status");
    expect(chip).toHaveClass("tone-info");
    expect(chip).not.toHaveClass("tone-danger");
    expect(playButton()).toBeDisabled();
    expect(screen.getByTestId("playback-announcement")).toHaveTextContent("At the live edge, event 3 of 3.");
  });

  it("does not follow new events by default", async () => {
    await playToLiveEdge();
    const follow = screen.getByRole("checkbox", { name: "Follow live" });
    expect(follow).not.toBeChecked();
    server.timelines.get(LIVE_ID)!.push(item(4, "file.created", "Created notes.md", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(counter()).toBe("#3 / 4"));
    expect(status()).toBe("PAUSED");
    expect(playButton()).toBeEnabled();
  });

  it("advances to new events at the live edge when following", async () => {
    await playToLiveEdge();
    fireEvent.click(screen.getByRole("checkbox", { name: "Follow live" }));
    server.timelines.get(LIVE_ID)!.push(item(4, "file.created", "Created notes.md", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(counter()).toBe("#4 / 4"));
    expect(status()).toBe("LIVE EDGE");
    expect(card()).toHaveTextContent("Created notes.md");
  });

  it("does not follow when the cursor is behind the live edge", async () => {
    await openTimeline(LIVE_ID);
    fireEvent.click(screen.getByRole("checkbox", { name: "Follow live" }));
    key("ArrowRight");
    expect(counter()).toBe("#1 / 3");
    server.timelines.get(LIVE_ID)!.push(item(4, "file.created", "Created notes.md", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(counter()).toBe("#1 / 4"));
  });
});

// -- wording, safety and accessibility -------------------------------------------------------

describe("wording and safety", () => {
  it("uses no causal wording and never claims video", async () => {
    await openTimeline();
    const texts: string[] = [screen.getByTestId("playback").textContent ?? ""];
    for (let i = 0; i < 21; i += 1) {
      key("ArrowRight");
      texts.push(screen.getByTestId("playback").textContent ?? "");
    }
    const all = texts.join("\n");
    expect(all).not.toMatch(CAUSAL);
    expect(all).not.toMatch(/solved by|cause removed|FIXED/i);
    expect(all).not.toMatch(/\bframe\b|recorded video|screen recording|footage/i);
    expect(all).toContain("OBSERVED RECOVERY");
  });

  it("never shows raw payload fields", async () => {
    const leaky = server.timelines.get(STOPPED_ID)!.map((i) => ({
      ...i,
      payload: { stdout: "SECRET-STDOUT", argv: ["--token", "SECRET-ARG"], sha256: "f".repeat(64) },
    }));
    server.timelines.set(STOPPED_ID, leaky);
    await openTimeline();
    for (let i = 0; i < 21; i += 1) {
      key("ArrowRight");
      expect(card().textContent).not.toMatch(/SECRET|payload|stdout|argv|sha256|f{64}/);
    }
    expect(document.body.textContent).not.toMatch(/SECRET/);
  });
});

describe("accessibility", () => {
  it("labels every control and announces the paused position", async () => {
    await openTimeline();
    const bar = screen.getByRole("region", { name: "Replay controls" });
    expect(within(bar).getByRole("button", { name: "Play replay" })).toBeEnabled();
    for (const name of ["Previous event", "Next event", "Previous important event", "Next important event"]) {
      expect(within(bar).getByRole("button", { name })).toBeInTheDocument();
    }
    const speeds = within(within(bar).getByRole("group", { name: "Playback speed" })).getAllByRole("button");
    expect(speeds.map((b) => b.textContent)).toEqual(["0.5x", "1x", "2x", "4x"]);
    expect(speeds.map((b) => b.getAttribute("aria-pressed"))).toEqual(["false", "true", "false", "false"]);

    const range = slider();
    expect(range).toHaveAttribute("min", "1");
    expect(range).toHaveAttribute("max", "21");
    expect(range).toHaveAttribute("aria-valuetext", "No event selected; 21 events recorded, 21 loaded");

    const announcement = screen.getByTestId("playback-announcement");
    expect(announcement).toHaveAttribute("role", "status");
    expect(announcement).toHaveTextContent("Playback stopped. Press Play to start.");
    fireEvent.click(playButton());
    expect(announcement).toHaveTextContent("Playing at 1x.");
    await tick(800 * 3);
    expect(announcement).toHaveTextContent("Playing at 1x.");
    fireEvent.click(playButton());
    expect(announcement).toHaveTextContent("Playback paused at event 4 of 21.");
    expect(screen.getByText("Space")).toBeInTheDocument();
  });
});
