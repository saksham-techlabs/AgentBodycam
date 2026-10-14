/**
 * Pure timeline logic: how an event looks, which report findings point at it, filtering,
 * search and the strict rules for appending timeline pages.
 *
 * Markers come only from the forensic report's structured references (event ID *and*
 * sequence must both match); nothing is inferred from summary text. Wording states order,
 * never cause.
 */

import type { ForensicReport, TimelineItem, TimelinePage } from "../api/types";
import { InvalidResponseError } from "../api/validate";
import { formatCount, plural } from "./format";
import type { Tone } from "./sessionState";

// -- event presentation ----------------------------------------------------------------------

/** Marker shapes on the rail, so an event's kind is never told by colour alone. */
export type Glyph = "ring" | "circle" | "square" | "diamond" | "triangle" | "bar";

export interface EventPresentation {
  label: string;
  tone: Tone;
  glyph: Glyph;
}

const BY_TYPE: Record<string, EventPresentation> = {
  "session.started": { label: "SESSION START", tone: "neutral", glyph: "ring" },
  "session.stopped": { label: "SESSION STOP", tone: "neutral", glyph: "ring" },
  "file.created": { label: "FILE CREATED", tone: "info", glyph: "circle" },
  "file.modified": { label: "FILE MODIFIED", tone: "info", glyph: "circle" },
  "file.deleted": { label: "FILE DELETED", tone: "info", glyph: "circle" },
  "git.snapshot": { label: "GIT", tone: "neutral", glyph: "diamond" },
  "command.started": { label: "COMMAND STARTED", tone: "neutral", glyph: "square" },
  "command.finished": { label: "COMMAND FINISHED", tone: "neutral", glyph: "square" },
  "check.passed": { label: "CHECK PASSED", tone: "success", glyph: "circle" },
  "check.failed": { label: "CHECK FAILED", tone: "danger", glyph: "bar" },
  "error.observed": { label: "ERROR", tone: "warning", glyph: "triangle" },
};

export function eventPresentation(item: Pick<TimelineItem, "type">): EventPresentation {
  return (
    BY_TYPE[item.type] ?? { label: item.type.toUpperCase(), tone: "neutral", glyph: "circle" }
  );
}

/** `02:15:03.412` (UTC) from a recorded `...THH:MM:SS.ffffffZ` timestamp, else the raw text. */
export function formatClock(timestamp: string): string {
  const match = /T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(timestamp);
  if (match === null) return timestamp;
  return `${match[1]}.${(match[2] ?? "").padEnd(3, "0").slice(0, 3)}`;
}

// -- report markers --------------------------------------------------------------------------

export type MarkerKind = "first_failure" | "failed_run" | "recovered" | "unresolved";

export interface Marker {
  kind: MarkerKind;
  eventId: string;
  sequence: number;
  /** Short uppercase chip text. */
  label: string;
  tone: Tone;
  /** One factual sentence for the detail panel. */
  detail: string;
}

export interface MarkerIndex {
  /** Markers per event ID; only applied to an item whose sequence matches too. */
  byEvent: ReadonlyMap<string, readonly Marker[]>;
  /** Event IDs of every observable failure the report lists. */
  failures: ReadonlySet<string>;
}

export const EMPTY_MARKERS: MarkerIndex = { byEvent: new Map(), failures: new Set() };

export function recoveryLine(displayName: string, failedAttempts: number): string {
  return `${displayName} later passed after ${plural(failedAttempts, "failed recorded attempt")}`;
}

export function buildMarkers(report: ForensicReport | null): MarkerIndex {
  if (report === null) return EMPTY_MARKERS;
  const byEvent = new Map<string, Marker[]>();
  const add = (marker: Marker): void => {
    const list = byEvent.get(marker.eventId);
    if (list === undefined) byEvent.set(marker.eventId, [marker]);
    else list.push(marker);
  };

  const first = report.first_observable_failure;
  if (first !== null) {
    add({
      kind: "first_failure",
      eventId: first.event_id,
      sequence: first.sequence,
      label: "FIRST OBSERVABLE FAILURE",
      tone: "danger",
      detail: `First observable failure of this recording: ${first.summary}`,
    });
  }

  for (const r of report.recoveries.items) {
    const sameRun = r.first_failed_event_id === r.last_failed_event_id;
    add({
      kind: "failed_run",
      eventId: r.first_failed_event_id,
      sequence: r.first_failed_sequence,
      label: sameRun ? "FAILED · PASSED LATER" : "FIRST FAILED RUN",
      tone: "danger",
      detail:
        `${r.display_name} failed here (#${r.first_failed_sequence}); a later run of the ` +
        `same check passed at #${r.recovered_sequence}.`,
    });
    if (!sameRun) {
      add({
        kind: "failed_run",
        eventId: r.last_failed_event_id,
        sequence: r.last_failed_sequence,
        label: "LAST FAILED RUN",
        tone: "danger",
        detail:
          `Last recorded failing run of ${r.display_name} (#${r.last_failed_sequence}) ` +
          `before the passing run at #${r.recovered_sequence}.`,
      });
    }
    add({
      kind: "recovered",
      eventId: r.recovered_event_id,
      sequence: r.recovered_sequence,
      label: "OBSERVED RECOVERY",
      tone: "success",
      detail:
        `${recoveryLine(r.display_name, r.failed_attempts)} ` +
        `(first failure #${r.first_failed_sequence}` +
        `${sameRun ? "" : `, last failure #${r.last_failed_sequence}`}).`,
    });
  }

  for (const u of report.unresolved_failures.items) {
    if (u.first_failed_event_id !== u.last_failed_event_id) {
      add({
        kind: "failed_run",
        eventId: u.first_failed_event_id,
        sequence: u.first_failed_sequence,
        label: "FIRST FAILED RUN",
        tone: "danger",
        detail:
          `First of ${formatCount(u.failed_attempts)} recorded failing runs of ` +
          `${u.display_name}; no later passing run was recorded.`,
      });
    }
    add({
      kind: "unresolved",
      eventId: u.last_failed_event_id,
      sequence: u.last_failed_sequence,
      label: "UNRESOLVED IN THIS RECORDING",
      tone: "danger",
      detail:
        `Latest recorded ${u.display_name} result is a failure (#${u.last_failed_sequence}); ` +
        "no later passing run was recorded before the evidence ends.",
    });
  }

  const failures = new Set<string>(report.observable_failures.items.map((f) => f.event_id));
  if (first !== null) failures.add(first.event_id);
  return { byEvent, failures };
}

export function markersFor(index: MarkerIndex, item: TimelineItem): readonly Marker[] {
  const markers = index.byEvent.get(item.event_id);
  if (markers === undefined) return [];
  return markers.filter((m) => m.sequence === item.sequence);
}

// -- filters and search ----------------------------------------------------------------------

export type TimelineFilter = "all" | "files" | "git" | "commands" | "checks" | "errors" | "failures";

export const FILTERS: readonly { id: TimelineFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "files", label: "Files" },
  { id: "git", label: "Git" },
  { id: "commands", label: "Commands" },
  { id: "checks", label: "Checks" },
  { id: "errors", label: "Errors" },
  { id: "failures", label: "Failures only" },
];

const FILTER_CATEGORY: Partial<Record<TimelineFilter, TimelineItem["category"]>> = {
  files: "file",
  git: "git",
  commands: "command",
  checks: "check",
  errors: "error",
};

export function normalizeQuery(query: string): string {
  return query.trim().toLowerCase();
}

/**
 * Does `item` pass the filter and the (normalized) search query? Search covers only the
 * safe fields a timeline item already shows: summary, project-relative path and type.
 */
export function matches(
  item: TimelineItem,
  filter: TimelineFilter,
  query: string,
  failures: ReadonlySet<string>,
): boolean {
  if (filter === "failures") {
    if (item.type !== "check.failed" && !failures.has(item.event_id)) return false;
  } else if (filter !== "all" && item.category !== FILTER_CATEGORY[filter]) {
    return false;
  }
  if (query === "") return true;
  return (
    item.summary.toLowerCase().includes(query) ||
    item.type.toLowerCase().includes(query) ||
    (item.path !== null && item.path.toLowerCase().includes(query))
  );
}

// -- loaded pages ----------------------------------------------------------------------------

/** The timeline pages loaded so far, in strict sequence order. */
export interface LoadedTimeline {
  items: readonly TimelineItem[];
  /** Event ID -> position in `items`. */
  index: ReadonlyMap<string, number>;
  /** `next_after_sequence` of the latest page: the only cursor ever sent. */
  cursor: number;
  hasMore: boolean;
  /** Events stored when the latest page was read. */
  total: number;
  pages: number;
}

export const EMPTY_TIMELINE: LoadedTimeline = {
  items: [],
  index: new Map(),
  cursor: 0,
  hasMore: true,
  total: 0,
  pages: 0,
};

/**
 * Append the page that continues `loaded`, or throw `InvalidResponseError`. The page must
 * start exactly at the current cursor and add no event that is already loaded; the event
 * log is append-only, so its total can never shrink below what is loaded.
 */
export function appendPage(loaded: LoadedTimeline, page: TimelinePage): LoadedTimeline {
  if (page.after_sequence !== loaded.cursor) throw new InvalidResponseError();
  const last = loaded.items.at(-1);
  if (last !== undefined && page.items.length > 0 && page.items[0]!.sequence <= last.sequence) {
    throw new InvalidResponseError();
  }
  if (page.total < loaded.items.length + page.items.length) throw new InvalidResponseError();
  // A live poll that finds nothing new must not copy an index of thousands of events.
  let index = loaded.index;
  if (page.items.length > 0) {
    const next = new Map(loaded.index);
    page.items.forEach((item, offset) => {
      if (next.has(item.event_id)) throw new InvalidResponseError();
      next.set(item.event_id, loaded.items.length + offset);
    });
    index = next;
  }
  return {
    items: page.items.length === 0 ? loaded.items : [...loaded.items, ...page.items],
    index,
    cursor: page.next_after_sequence,
    hasMore: page.has_more,
    total: page.total,
    pages: loaded.pages + 1,
  };
}

export function lastSequence(loaded: LoadedTimeline): number {
  return loaded.items.at(-1)?.sequence ?? 0;
}
