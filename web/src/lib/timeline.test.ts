import { describe, expect, it } from "vitest";

import type { TimelineItem, TimelinePage } from "../api/types";
import { InvalidResponseError } from "../api/validate";
import { CAUSAL, RICH_TIMELINE, eventId, item, richReport } from "../test/fixtures";
import {
  EMPTY_TIMELINE,
  appendPage,
  buildMarkers,
  eventPresentation,
  formatClock,
  markersFor,
  matches,
  normalizeQuery,
  type LoadedTimeline,
} from "./timeline";

const SID = "20261014T021500Z-2222222222222222";

function page(after: number, items: TimelineItem[], total: number): TimelinePage {
  const next = items.at(-1)?.sequence ?? after;
  return {
    api_version: 1,
    session_id: SID,
    after_sequence: after,
    limit: 100,
    items,
    next_after_sequence: next,
    has_more: next < total,
    total,
  };
}

function loaded(...pages: TimelinePage[]): LoadedTimeline {
  return pages.reduce(appendPage, EMPTY_TIMELINE);
}

describe("appendPage", () => {
  it("appends pages in strict sequence order without duplicates", () => {
    const t = loaded(page(0, RICH_TIMELINE.slice(0, 10), 21), page(10, RICH_TIMELINE.slice(10), 21));
    expect(t.items.map((i) => i.sequence)).toEqual(RICH_TIMELINE.map((i) => i.sequence));
    expect(new Set(t.items.map((i) => i.event_id)).size).toBe(21);
    expect(t.cursor).toBe(21);
    expect(t.hasMore).toBe(false);
    expect(t.index.get(eventId(14))).toBe(13);
  });

  it("only ever continues from the server's own cursor", () => {
    const t = loaded(page(0, RICH_TIMELINE.slice(0, 10), 21));
    expect(() => appendPage(t, page(9, RICH_TIMELINE.slice(9, 12), 21))).toThrow(InvalidResponseError);
    expect(() => appendPage(t, page(11, RICH_TIMELINE.slice(11, 12), 21))).toThrow(InvalidResponseError);
  });

  it("rejects a page that repeats a loaded event", () => {
    const t = loaded(page(0, RICH_TIMELINE.slice(0, 10), 21));
    const repeat = { ...RICH_TIMELINE[11]!, event_id: eventId(3) };
    expect(() => appendPage(t, page(10, [repeat], 21))).toThrow(InvalidResponseError);
  });

  it("rejects a total that shrinks below what is loaded", () => {
    const t = loaded(page(0, RICH_TIMELINE.slice(0, 10), 21));
    expect(() => appendPage(t, page(10, RICH_TIMELINE.slice(10, 12), 11))).toThrow(InvalidResponseError);
  });

  it("keeps the loaded items when a live poll finds nothing new", () => {
    const t = loaded(page(0, RICH_TIMELINE, 21));
    const next = appendPage(t, page(21, [], 21));
    expect(next.items).toBe(t.items);
    expect(next.index).toBe(t.index);
    expect(next.cursor).toBe(21);
  });
});

describe("markers", () => {
  const markers = buildMarkers(richReport(SID));
  const at = (sequence: number) => markersFor(markers, RICH_TIMELINE[sequence - 1]!).map((m) => m.label);

  it("marks the first observable failure and the recovery path from report references", () => {
    expect(at(5)).toEqual(["FIRST OBSERVABLE FAILURE", "FIRST FAILED RUN"]);
    expect(at(9)).toEqual(["LAST FAILED RUN"]);
    expect(at(14)).toEqual(["OBSERVED RECOVERY"]);
    expect(at(17)).toEqual(["UNRESOLVED IN THIS RECORDING"]);
    expect(at(6)).toEqual([]);
  });

  it("applies a marker only when event ID and sequence both match", () => {
    const moved = { ...RICH_TIMELINE[13]!, sequence: 99 };
    expect(markersFor(markers, moved)).toEqual([]);
  });

  it("states order, never cause", () => {
    const text = [...markers.byEvent.values()].flat().map((m) => `${m.label} ${m.detail}`).join(" ");
    expect(text).toContain("pytest later passed after 2 failed recorded attempts");
    expect(text).not.toMatch(CAUSAL);
  });

  it("collects every observable failure for the failures filter", () => {
    expect([...markers.failures].sort()).toEqual([eventId(5), eventId(9), eventId(17)].sort());
  });

  it("is empty without a report", () => {
    expect(buildMarkers(null).byEvent.size).toBe(0);
  });
});

describe("filters and search", () => {
  const failures = buildMarkers(richReport(SID)).failures;
  const ids = (filter: Parameters<typeof matches>[1], query = "") =>
    RICH_TIMELINE.filter((i) => matches(i, filter, normalizeQuery(query), failures)).map((i) => i.sequence);

  it("filters by category without reordering", () => {
    expect(ids("all")).toEqual(RICH_TIMELINE.map((i) => i.sequence));
    expect(ids("files")).toEqual([6, 10, 11]);
    expect(ids("git")).toEqual([2, 20]);
    expect(ids("commands")).toEqual([3, 4, 7, 8, 12, 13, 15, 16, 19]);
    expect(ids("checks")).toEqual([5, 9, 14, 17]);
    expect(ids("errors")).toEqual([18]);
    expect(ids("failures")).toEqual([5, 9, 17]);
  });

  it("searches summary, path and type only", () => {
    expect(ids("all", "  HELPERS ")).toEqual([10]);
    expect(ids("all", "check.passed")).toEqual([14]);
    expect(ids("all", "logs/locked")).toEqual([18]);
    expect(ids("checks", "ruff")).toEqual([17]);
    expect(ids("all", eventId(5))).toEqual([]);
    expect(ids("all", "cmd-1")).toEqual([]);
  });
});

describe("presentation", () => {
  it("gives every event type a text label, a tone and a shape", () => {
    const seen = RICH_TIMELINE.map((i) => [i.type, eventPresentation(i)] as const);
    const by = Object.fromEntries(seen);
    expect(by["check.failed"]).toMatchObject({ label: "CHECK FAILED", tone: "danger" });
    expect(by["check.passed"]).toMatchObject({ label: "CHECK PASSED", tone: "success" });
    expect(by["error.observed"]).toMatchObject({ label: "ERROR", tone: "warning" });
    for (const type of ["file.created", "file.modified", "file.deleted"]) {
      expect(by[type]?.tone).toBe("info");
    }
    expect(by["git.snapshot"]?.label).toBe("GIT");
    expect(by["session.started"]?.label).toBe("SESSION START");
    expect(eventPresentation(item(1, "agent.thought", "x")).label).toBe("AGENT.THOUGHT");
  });

  it("formats times as UTC clock times", () => {
    expect(formatClock("2026-10-14T02:15:05.412000Z")).toBe("02:15:05.412");
    expect(formatClock("2026-10-14T02:15:05Z")).toBe("02:15:05.000");
    expect(formatClock("garbage")).toBe("garbage");
  });
});
