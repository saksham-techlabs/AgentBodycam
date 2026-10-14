import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient, type FetchLike } from "../api/client";
import type { SessionSummary } from "../api/types";
import { App } from "../app/App";
import { LIVE_POLL_INTERVAL_MS } from "../hooks/usePolling";
import { item } from "../test/fixtures";
import { FakeServer, LIVE_ID } from "../test/fakeServer";

let server: FakeServer;
let hidden: boolean;
let gated: boolean;
let held: (() => void)[];
let calls: string[];

const BASE = "2026-10-14T03:15:00Z";
const realSetTimeout = globalThis.setTimeout;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  server = new FakeServer();
  hidden = false;
  gated = false;
  held = [];
  calls = [];
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});

afterEach(() => {
  vi.useRealTimers();
});

const fetchImpl: FetchLike = (input, init) => {
  calls.push(input);
  if (!gated) return server.fetch(input, init);
  return new Promise((resolve, reject) => {
    held.push(() => server.fetch(input, init).then(resolve, reject));
  });
};

function liveCalls(): string[] {
  return calls.filter((c) => c.startsWith(`/api/v1/sessions/${LIVE_ID}`));
}

async function tick(ms: number): Promise<void> {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

/**
 * Testing Library's `waitFor` waits on `setTimeout`, which is faked here; this retries
 * `check` while yielding to the real event loop instead, without advancing fake time.
 */
async function waitFor(check: () => void, tries = 300): Promise<void> {
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

async function openLiveTimeline() {
  window.history.replaceState(null, "", `/session/${LIVE_ID}`);
  const view = render(<App client={new ReplayApiClient(fetchImpl)} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: LIVE_ID })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
  await waitFor(() => expect(screen.getAllByTestId("timeline-row")).toHaveLength(3));
  return view;
}

function sequences(): string[] {
  return screen.getAllByTestId("timeline-row").map((r) => r.querySelector(".tl-seq")?.textContent ?? "");
}

function setHidden(value: boolean): void {
  hidden = value;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("live session", () => {
  it("appends newly recorded events from the server's cursor", async () => {
    await openLiveTimeline();
    screen.getAllByTestId("timeline-row")[1]!.click();
    server.timelines.get(LIVE_ID)!.push(
      item(4, "file.created", "Created notes.md", { path: "notes.md", salt: "c", base: BASE }),
    );
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(sequences()).toEqual(["#1", "#2", "#3", "#4"]));
    expect(liveCalls()).toContain(`/api/v1/sessions/${LIVE_ID}/timeline?after_sequence=3&limit=100`);
    expect(liveCalls()).toContain(`/api/v1/sessions/${LIVE_ID}/report`);
    expect(screen.getByTestId("event-detail")).toHaveTextContent("#2");
    expect(screen.getByText(/new events appear as they are recorded/)).toBeInTheDocument();
  });

  it("never overlaps polls", async () => {
    await openLiveTimeline();
    gated = true;
    await tick(LIVE_POLL_INTERVAL_MS);
    const firstRound = liveCalls().length;
    expect(held.length).toBeGreaterThan(0);
    await tick(LIVE_POLL_INTERVAL_MS * 8);
    expect(liveCalls()).toHaveLength(firstRound);

    gated = false;
    for (const release of held.splice(0)) release();
    await tick(0);
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(liveCalls().length).toBeGreaterThan(firstRound));
  });

  it("pauses while the page is hidden", async () => {
    await openLiveTimeline();
    const before = liveCalls().length;
    setHidden(true);
    await tick(LIVE_POLL_INTERVAL_MS * 6);
    expect(liveCalls()).toHaveLength(before);
    setHidden(false);
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(liveCalls().length).toBeGreaterThan(before));
  });

  it("stops once the recording stops, after a final read", async () => {
    await openLiveTimeline();
    expect(screen.getByText("Auto-refresh")).toBeInTheDocument();
    server.sessions = server.sessions.map((s): SessionSummary =>
      s.session_id === LIVE_ID && s.readable
        ? { ...s, status: "stopped", recording_state: "stopped", analysis_complete: true, ended_at: "2026-10-14T03:20:00.000000Z" }
        : s,
    );
    server.timelines.get(LIVE_ID)!.push(item(4, "session.stopped", "Recording stopped", { salt: "c", base: BASE }));
    await tick(LIVE_POLL_INTERVAL_MS);
    await waitFor(() => expect(screen.queryByText("Auto-refresh")).toBeNull());
    await waitFor(() => expect(sequences()).toContain("#4"));
    expect(screen.getAllByText("STOPPED").length).toBeGreaterThan(0);
    await tick(0);

    const settled = liveCalls().length;
    await tick(LIVE_POLL_INTERVAL_MS * 6);
    expect(liveCalls()).toHaveLength(settled);
    expect(screen.getByText("End of the recorded timeline.")).toBeInTheDocument();
  });

  it("cleans up on unmount", async () => {
    const view = await openLiveTimeline();
    const before = calls.length;
    view.unmount();
    await tick(LIVE_POLL_INTERVAL_MS * 6);
    expect(calls).toHaveLength(before);
  });
});
