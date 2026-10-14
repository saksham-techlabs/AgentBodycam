import { describe, expect, it } from "vitest";

import { RICH_TIMELINE, item, longTimeline, richReport } from "../test/fixtures";
import {
  BASE_STEP_MS,
  DEFAULT_SPEED,
  MAX_SCRUBBER_MARKERS,
  PLAYBACK_SPEEDS,
  adjacentImportant,
  buildImportant,
  isOwnSpaceControl,
  isPlaybackSpeed,
  isTextEntry,
  lowerBound,
  markerPercent,
  scrubberMarkers,
  stepInterval,
} from "./playback";

describe("playback timing", () => {
  it("offers exactly 0.5x, 1x, 2x and 4x, defaulting to 1x", () => {
    expect(PLAYBACK_SPEEDS).toEqual([0.5, 1, 2, 4]);
    expect(DEFAULT_SPEED).toBe(1);
    expect(isPlaybackSpeed(8)).toBe(false);
    expect(isPlaybackSpeed(0)).toBe(false);
  });

  it("uses one fixed interval per event, scaled by speed", () => {
    expect(BASE_STEP_MS).toBe(800);
    expect(PLAYBACK_SPEEDS.map(stepInterval)).toEqual([1600, 800, 400, 200]);
  });
});

describe("positions", () => {
  it("finds loaded events by sequence", () => {
    const items = longTimeline(10);
    expect(lowerBound(items, 1)).toBe(0);
    expect(lowerBound(items, 7)).toBe(6);
    expect(lowerBound(items, 11)).toBe(10);
    expect(lowerBound([], 3)).toBe(0);
  });

  it("places markers by event position, not time", () => {
    expect(markerPercent(1, 21)).toBe(0);
    expect(markerPercent(21, 21)).toBe(100);
    expect(markerPercent(5, 21)).toBe(20);
    expect(markerPercent(14, 21)).toBe(65);
    expect(markerPercent(1, 1)).toBe(0);
    expect(markerPercent(500, 100)).toBe(100);
  });
});

describe("important events", () => {
  it("come from report references and recorded event types only", () => {
    const important = buildImportant(richReport("s"), RICH_TIMELINE);
    expect(important.map((e) => [e.sequence, e.kinds[0]])).toEqual([
      [1, "session_start"],
      [5, "first_failure"],
      [9, "check_failed"],
      [14, "recovered"],
      [17, "unresolved"],
      [21, "session_stop"],
    ]);
    expect(important.find((e) => e.sequence === 5)!.kinds).toEqual(["first_failure", "check_failed"]);
  });

  it("never parse summary text", () => {
    const items = [
      item(1, "file.modified", "check failed: tests FAILED, recovered, unresolved", { path: "a.py" }),
      item(2, "command.finished", "pytest failed (exit 1)"),
    ];
    expect(buildImportant(null, items)).toEqual([]);
  });

  it("include report references beyond the loaded pages", () => {
    const report = richReport("s");
    const important = buildImportant(report, RICH_TIMELINE.slice(0, 3));
    expect(important.map((e) => e.sequence)).toEqual([1, 5, 9, 14, 17]);
  });

  it("navigate to the nearest one before or after", () => {
    const important = buildImportant(richReport("s"), RICH_TIMELINE);
    expect(adjacentImportant(important, 0, 1)?.sequence).toBe(1);
    expect(adjacentImportant(important, 5, 1)?.sequence).toBe(9);
    expect(adjacentImportant(important, 6, -1)?.sequence).toBe(5);
    expect(adjacentImportant(important, 21, 1)).toBeNull();
    expect(adjacentImportant(important, 1, -1)).toBeNull();
  });
});

describe("scrubber markers", () => {
  it("mark failures, recoveries and unresolved failures with honest labels", () => {
    const marks = scrubberMarkers(buildImportant(richReport("s"), RICH_TIMELINE), 21);
    expect(marks.map((m) => [m.sequence, m.label, m.percent])).toEqual([
      [5, "FIRST OBSERVABLE FAILURE", 20],
      [9, "CHECK FAILED", 40],
      [14, "OBSERVED RECOVERY", 65],
      [17, "UNRESOLVED IN THIS RECORDING", 80],
    ]);
    expect(marks.map((m) => m.label).join(" ")).not.toMatch(/\bfixed\b|\bsolved\b|\bcause/i);
  });

  it("are bounded however many failures were recorded", () => {
    const items = Array.from({ length: 5000 }, (_, i) => item(i + 1, "check.failed", "x"));
    const marks = scrubberMarkers(buildImportant(null, items), 5000);
    expect(marks.length).toBeLessThanOrEqual(MAX_SCRUBBER_MARKERS);
    expect(marks.length).toBeGreaterThan(100);
  });
});

describe("shortcut targets", () => {
  it("treat text-entry controls as typing", () => {
    const make = (html: string) => {
      const host = document.createElement("div");
      host.innerHTML = html;
      return host.firstElementChild!;
    };
    expect(isTextEntry(make('<input type="search">'))).toBe(true);
    expect(isTextEntry(make("<input>"))).toBe(true);
    expect(isTextEntry(make("<textarea></textarea>"))).toBe(true);
    expect(isTextEntry(make("<select></select>"))).toBe(true);
    expect(isTextEntry(make('<input type="range">'))).toBe(false);
    expect(isTextEntry(make("<button></button>"))).toBe(false);
    expect(isTextEntry(document.body)).toBe(false);
    expect(isOwnSpaceControl(make("<button></button>"))).toBe(true);
    expect(isOwnSpaceControl(make('<input type="checkbox">'))).toBe(true);
    expect(isOwnSpaceControl(make('<input type="range">'))).toBe(false);
    expect(isOwnSpaceControl(document.body)).toBe(false);
  });
});
