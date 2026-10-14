/// <reference types="node" />
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "../api/client";
import { App } from "../app/App";
import { CAUSAL } from "../test/fixtures";
import { FakeServer, LIVE_ID, STOPPED_ID } from "../test/fakeServer";

// Vitest stubs CSS imports, so the stylesheet is read from disk (tests run from `web/`).
const css = readFileSync(resolve(process.cwd(), "src/styles/app.css"), "utf8");

let server: FakeServer;
const realSetTimeout = globalThis.setTimeout;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  server = new FakeServer();
});

afterEach(() => {
  vi.useRealTimers();
});

async function tick(ms: number): Promise<void> {
  let left = ms;
  do {
    const step = Math.min(left, 100);
    await act(() => vi.advanceTimersByTimeAsync(step));
    left -= step;
  } while (left > 0);
}

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

function stubMotion(reduce: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: reduce && query.includes("reduce"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

async function openTimeline(sessionId = STOPPED_ID): Promise<void> {
  window.history.replaceState(null, "", `/session/${sessionId}`);
  render(<App client={new ReplayApiClient(server.fetch)} />);
  await waitFor(() => expect(screen.getByRole("heading", { name: sessionId })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /^Timeline/ }));
  await waitFor(() => expect(screen.getByTestId("playback-counter")).not.toHaveTextContent("/ 0"));
}

const theater = () => screen.getByTestId("playback");
const hero = () => screen.getByTestId("current-event");
const status = () => screen.getByTestId("playback-status");
const counter = () => screen.getByTestId("playback-counter").textContent;

function row(sequence: number): HTMLElement {
  const found = screen
    .queryAllByTestId("timeline-row")
    .find((r) => r.querySelector(".tl-seq")?.textContent === `#${sequence}`);
  if (found === undefined) throw new Error(`row #${sequence} not rendered`);
  return found;
}

async function stepTo(sequence: number): Promise<void> {
  for (let i = 0; i < sequence; i += 1) fireEvent.keyDown(document.body, { key: "ArrowRight" });
  await tick(0);
  expect(counter()).toBe(`#${sequence} / 21`);
}

/** The body of every top-level CSS rule whose selector list contains `selector`. */
function rulesFor(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found = css.match(new RegExp(`(^|[},\\s])${escaped}\\s*[,{][^}]*}`, "g"));
  return (found ?? []).join("\n");
}

/** Every `@media <query>` block, concatenated. */
function mediaBlock(query: string): string {
  const blocks: string[] = [];
  for (let start = css.indexOf(`@media ${query}`); start >= 0; start = css.indexOf(`@media ${query}`, start + 1)) {
    let depth = 0;
    for (let i = css.indexOf("{", start); i < css.length; i += 1) {
      if (css[i] === "{") depth += 1;
      else if (css[i] === "}" && --depth === 0) {
        blocks.push(css.slice(start, i + 1));
        break;
      }
    }
  }
  return blocks.join("\n");
}

// -- structure -------------------------------------------------------------------------------

describe("replay theater", () => {
  it("groups controls, status, scrubber, markers and the current event in one region", async () => {
    await openTimeline();
    const region = screen.getByRole("region", { name: "Replay controls" });
    expect(region).toHaveClass("replay-theater");
    expect(within(region).getByTestId("play-button")).toBeInTheDocument();
    expect(within(region).getByTestId("playback-counter")).toBeInTheDocument();
    expect(within(region).getByTestId("playback-status")).toBeInTheDocument();
    expect(within(region).getByRole("slider")).toBeInTheDocument();
    expect(within(region).getByRole("toolbar", { name: "Important event markers" })).toBeInTheDocument();
    expect(within(region).getByTestId("current-event")).toBeInTheDocument();
    // The timeline list lies outside the sticky block, so it is never covered by it.
    expect(region).not.toContainElement(screen.getByTestId("timeline-scroll"));
  });

  it("is sticky below the top bar only where it fits, under the top bar's layer", () => {
    const sticky = mediaBlock("(min-width: 900px) and (min-height: 720px)");
    expect(sticky).toMatch(/\.replay-theater\s*{[^}]*position:\s*sticky/);
    expect(sticky).toMatch(/top:\s*var\(--topbar-height\)/);
    expect(sticky).toMatch(/\.replay \.timeline-scroll\s*{[^}]*--replay-timeline-height/);
    const z = (sel: string) => Number(/z-index:\s*(\d+)/.exec(rulesFor(sel))?.[1]);
    expect(z(".replay-theater")).toBeLessThan(z(".topbar"));
    expect(rulesFor(".topbar")).toMatch(/min-height:\s*var\(--topbar-height\)/);
    // The top bar keeps one line wherever the theater is sticky, so its height stays known.
    expect(rulesFor(".topbar-meta")).toMatch(/flex-wrap:\s*nowrap/);
  });

  it("keeps the detail panel from sliding under the sticky theater", () => {
    const wide = mediaBlock("(min-width: 1101px) and (min-height: 720px)");
    expect(wide).toMatch(/\.replay \.detail\s*{[^}]*position:\s*static/);
  });

  it("has rules for the major breakpoints", () => {
    for (const query of ["(max-width: 1279px)", "(max-width: 899px)", "(max-width: 699px)"]) {
      expect(mediaBlock(query)).not.toBe("");
    }
    expect(mediaBlock("(max-width: 699px)")).toMatch(/\.now-card/);
  });
});

// -- hero ------------------------------------------------------------------------------------

describe("current-event hero", () => {
  it("uses a neutral blue treatment for an ordinary event", async () => {
    await openTimeline();
    await stepTo(6);
    expect(hero()).toHaveAttribute("data-moment", "event");
    expect(hero()).toHaveClass("tone-info");
    expect(hero()).toHaveTextContent("Modified src/app.py");
    expect(theater()).toHaveAttribute("data-moment", "event");
  });

  it("gives the first observable failure the strongest danger treatment", async () => {
    await openTimeline();
    fireEvent.click(screen.getByRole("button", { name: "Jump to first failure" }));
    await waitFor(() => expect(counter()).toBe("#5 / 21"));
    expect(hero()).toHaveAttribute("data-moment", "first-failure");
    expect(hero()).toHaveClass("moment-first-failure", "tone-danger");
    expect(hero()).toHaveTextContent("FIRST OBSERVABLE FAILURE");
    const marker = screen.getByRole("button", { name: "FIRST OBSERVABLE FAILURE, event #5" });
    expect(marker).toHaveClass("is-current");
    expect(row(5)).toHaveClass("is-current", "has-first-failure");
  });

  it("marks an observed recovery in green", async () => {
    await openTimeline();
    fireEvent.click(screen.getByRole("button", { name: "OBSERVED RECOVERY, event #14" }));
    await waitFor(() => expect(counter()).toBe("#14 / 21"));
    expect(hero()).toHaveAttribute("data-moment", "recovered");
    expect(hero()).toHaveClass("tone-success");
    expect(hero()).toHaveTextContent("OBSERVED RECOVERY");
    expect(screen.getByRole("button", { name: "OBSERVED RECOVERY, event #14" })).toHaveClass("is-current");
  });

  it("keeps an unresolved failure red and factual", async () => {
    await openTimeline();
    await stepTo(17);
    expect(hero()).toHaveAttribute("data-moment", "unresolved");
    expect(hero()).toHaveClass("tone-danger");
    expect(hero()).toHaveTextContent("UNRESOLVED IN THIS RECORDING");
  });

  it("uses amber for an observed error", async () => {
    await openTimeline();
    await stepTo(18);
    expect(hero()).toHaveAttribute("data-moment", "warning");
    expect(hero()).toHaveClass("tone-warning");
  });

  it("re-enters the hero body per event, without motion when reduced motion is requested", async () => {
    stubMotion(false);
    await openTimeline();
    await stepTo(2);
    expect(screen.getByTestId("current-event-body")).toHaveClass("is-entering");
    expect(theater()).toHaveAttribute("data-motion", "full");
  });
});

// -- timeline rows ---------------------------------------------------------------------------

describe("current and selected rows", () => {
  it("shows both CURRENT and DETAIL when the cursor event is open in the detail panel", async () => {
    await openTimeline();
    await stepTo(3);
    expect(row(3)).toHaveClass("is-current", "is-selected");
    expect(row(3).querySelector(".tag-current")).toHaveTextContent("CURRENT");
    expect(row(3).querySelector(".tag-selected")).toHaveTextContent("DETAIL");
    expect(row(3)).toHaveAttribute("aria-current", "true");
    expect(row(3)).toHaveTextContent("Current replay event.");
  });

  it("keeps the playback cursor distinct once the detail panel is closed", async () => {
    await openTimeline();
    fireEvent.click(screen.getByTestId("play-button"));
    await tick(800);
    fireEvent.click(screen.getByRole("button", { name: "Close event details" }));
    await tick(800);
    expect(counter()).toBe("#3 / 21");
    expect(row(3)).toHaveClass("is-current");
    expect(row(3)).not.toHaveClass("is-selected");
    expect(row(3).querySelector(".tag-current")).not.toBeNull();
    expect(row(3).querySelector(".tag-selected")).toBeNull();
    expect(document.querySelectorAll(".tl-row.is-selected")).toHaveLength(0);
  });

  it("draws the replayed part of the rail up to the cursor", async () => {
    await openTimeline();
    await stepTo(4);
    expect(row(1)).toHaveClass("is-played");
    expect(row(3)).toHaveClass("is-played");
    expect(row(4)).not.toHaveClass("is-played");
    expect(row(5)).not.toHaveClass("is-played");
  });

  it("styles current and selected with different visual properties", () => {
    expect(rulesFor(".tl-row.is-current")).toMatch(/--bar:/);
    expect(rulesFor(".tl-row.is-selected")).toMatch(/--ring:/);
    expect(rulesFor(".tl-row.is-selected")).not.toMatch(/--bar:/);
  });
});

// -- status ----------------------------------------------------------------------------------

describe("playback status", () => {
  it("keeps PLAYING as readable text with a decorative activity dot", async () => {
    stubMotion(false);
    await openTimeline();
    fireEvent.click(screen.getByTestId("play-button"));
    expect(status().textContent).toBe("PLAYING");
    expect(status()).toHaveAttribute("data-state", "playing");
    const dot = within(status()).getByTestId("status-dot");
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(dot).toHaveClass("pulse");
    expect(theater()).toHaveClass("is-playing");
    expect(screen.getByTestId("playback-announcement")).toHaveTextContent("Playing at 1x.");
  });

  it("shows LIVE EDGE as information, never danger", async () => {
    await openTimeline(LIVE_ID);
    fireEvent.click(screen.getByTestId("play-button"));
    await tick(800 * 3);
    expect(status().textContent).toBe("LIVE EDGE");
    expect(theater()).toHaveAttribute("data-state", "live-edge");
    expect(status()).toHaveClass("tone-info");
    expect(status()).not.toHaveClass("tone-danger");
    expect(status()).not.toHaveClass("tone-warning");
  });
});

// -- reduced motion --------------------------------------------------------------------------

describe("reduced motion", () => {
  it("drops the pulse and the hero transition while keeping every state", async () => {
    stubMotion(true);
    await openTimeline();
    expect(theater()).toHaveAttribute("data-motion", "reduced");
    fireEvent.click(screen.getByTestId("play-button"));
    expect(status().textContent).toBe("PLAYING");
    expect(within(status()).getByTestId("status-dot")).not.toHaveClass("pulse");
    await tick(800);
    expect(counter()).toBe("#2 / 21");
    expect(screen.getByTestId("current-event-body")).not.toHaveClass("is-entering");
  });

  it("turns off transitions and animations in CSS too", () => {
    const all = mediaBlock("(prefers-reduced-motion: reduce)");
    for (const selector of [".hero-body", ".status-dot", ".scrub-played", ".tl-row", ".scrub-marker", ".pulse"]) {
      expect(all).toContain(selector);
    }
    expect(all).toMatch(/animation:\s*none/);
    expect(all).toMatch(/transition:\s*none/);
  });
});

// -- accessibility ---------------------------------------------------------------------------

describe("accessibility", () => {
  it("keeps labelled controls with a primary / secondary / tertiary hierarchy", async () => {
    await openTimeline();
    const region = screen.getByRole("region", { name: "Replay controls" });
    expect(within(region).getByRole("button", { name: "Play replay" })).toHaveClass("play-button");
    for (const name of ["Previous event", "Next event"]) {
      expect(within(region).getByRole("button", { name })).toHaveClass("button-step");
    }
    for (const name of ["Previous important event", "Next important event"]) {
      expect(within(region).getByRole("button", { name })).toHaveClass("button-ghost");
    }
    expect(within(region).getByRole("group", { name: "Playback speed" })).toBeInTheDocument();
    expect(screen.getByText("Space")).toBeInTheDocument();
  });

  it("keeps the native, labelled scrubber", async () => {
    await openTimeline();
    const range = screen.getByRole("slider", { name: "Replay position by event sequence" });
    expect(range.tagName).toBe("INPUT");
    expect(range).toHaveAttribute("type", "range");
    expect(range).toHaveAttribute("aria-valuetext", "No event selected; 21 events recorded, 21 loaded");
    await stepTo(4);
    expect(range).toHaveAttribute("aria-valuetext", "Event 4 of 21; 21 loaded");
  });

  it("keeps the marker rail one tab stop with arrow-key navigation", async () => {
    await openTimeline();
    const rail = screen.getByRole("toolbar", { name: "Important event markers" });
    const markers = within(rail).getAllByRole("button");
    expect(markers.filter((m) => m.tabIndex === 0)).toHaveLength(1);
    markers[0]!.focus();
    fireEvent.keyDown(markers[0]!, { key: "ArrowRight" });
    expect(markers[1]).toHaveFocus();
    fireEvent.keyDown(markers[1]!, { key: "End" });
    expect(markers.at(-1)).toHaveFocus();
  });
});

// -- wording and safety ----------------------------------------------------------------------

describe("wording and safety", () => {
  it("adds no causal or video wording anywhere in the replay", async () => {
    await openTimeline();
    const texts: string[] = [];
    for (let i = 0; i < 21; i += 1) {
      fireEvent.keyDown(document.body, { key: "ArrowRight" });
      await tick(0);
      texts.push(theater().textContent ?? "", screen.getByTestId("timeline-scroll").textContent ?? "");
    }
    const all = texts.join("\n");
    expect(all).not.toMatch(CAUSAL);
    expect(all).not.toMatch(/\bsolved\b|FIXED BY|BROKE HERE|CULPRIT/i);
    expect(all).not.toMatch(/\bframes?\b|footage|filmed|recorded video|video recording|screen recording|camera/i);
    expect(all).toContain("FIRST OBSERVABLE FAILURE");
    expect(all).toContain("OBSERVED RECOVERY");
    expect(all).toContain("UNRESOLVED IN THIS RECORDING");
  });

  it("never exposes raw payload fields, not even in tooltips", async () => {
    const leaky = server.timelines.get(STOPPED_ID)!.map((i) => ({
      ...i,
      payload: { stdout: "SECRET-STDOUT", argv: ["--token", "SECRET-ARG"], sha256: "f".repeat(64) },
    }));
    server.timelines.set(STOPPED_ID, leaky);
    await openTimeline();
    for (let i = 0; i < 21; i += 1) {
      fireEvent.keyDown(document.body, { key: "ArrowRight" });
      await tick(0);
      expect(theater().innerHTML).not.toMatch(/SECRET|stdout|argv|sha256|f{64}/);
    }
    expect(document.body.innerHTML).not.toMatch(/SECRET/);
  });
});
