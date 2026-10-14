/**
 * Pure replay-playback logic: the step interval per speed, important events and where
 * their markers sit on the scrubber.
 *
 * Playback is a visualization of the recorded event log, one event per step, in sequence
 * order. It does not reproduce the real time between events (timestamps are display
 * metadata only), and there is no screen video: AgentBodycam records evidence events.
 *
 * Important events come only from structured evidence: an event's recorded `type`
 * (`session.started`, `session.stopped`, `check.failed`) and the forensic report's event
 * references (first observable failure, observable check failures, observed recoveries,
 * unresolved failures). Summary text is never parsed.
 */

import type { ForensicReport, TimelineItem } from "../api/types";
import type { Tone } from "./sessionState";

// -- timing ----------------------------------------------------------------------------------

export const PLAYBACK_SPEEDS = [0.5, 1, 2, 4] as const;
export type PlaybackSpeed = (typeof PLAYBACK_SPEEDS)[number];
export const DEFAULT_SPEED: PlaybackSpeed = 1;

/** Milliseconds one event stays current at 1x. */
export const BASE_STEP_MS = 800;

/** Fixed time per event: 1600 ms at 0.5x, 800 ms at 1x, 400 ms at 2x, 200 ms at 4x. */
export function stepInterval(speed: PlaybackSpeed): number {
  return Math.round(BASE_STEP_MS / speed);
}

export function isPlaybackSpeed(value: number): value is PlaybackSpeed {
  return (PLAYBACK_SPEEDS as readonly number[]).includes(value);
}

export function formatSpeed(speed: PlaybackSpeed): string {
  return `${speed}x`;
}

// -- positions -------------------------------------------------------------------------------

/** Index of the first loaded item whose sequence is >= `sequence` (items.length if none). */
export function lowerBound(items: readonly TimelineItem[], sequence: number): number {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (items[mid]!.sequence < sequence) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Left offset (0-100 %) of event `sequence` on a scrubber spanning events 1..`scaleMax`.
 * The scale is event position, never elapsed time.
 */
export function markerPercent(sequence: number, scaleMax: number): number {
  if (scaleMax <= 1) return 0;
  const pct = ((sequence - 1) / (scaleMax - 1)) * 100;
  return Math.min(100, Math.max(0, pct));
}

// -- important events ------------------------------------------------------------------------

export type ImportantKind =
  | "session_start"
  | "session_stop"
  | "first_failure"
  | "check_failed"
  | "recovered"
  | "unresolved";

export interface ImportantEvent {
  sequence: number;
  /** Known for report references and loaded events. */
  eventId: string;
  /** Every structured reason this event is important, highest priority first. */
  kinds: readonly ImportantKind[];
}

export const IMPORTANT_LABELS: Record<ImportantKind, string> = {
  first_failure: "FIRST OBSERVABLE FAILURE",
  unresolved: "UNRESOLVED IN THIS RECORDING",
  recovered: "OBSERVED RECOVERY",
  check_failed: "CHECK FAILED",
  session_start: "SESSION START",
  session_stop: "SESSION STOP",
};

export const IMPORTANT_TONES: Record<ImportantKind, Tone> = {
  first_failure: "danger",
  unresolved: "danger",
  recovered: "success",
  check_failed: "danger",
  session_start: "neutral",
  session_stop: "neutral",
};

const PRIORITY: readonly ImportantKind[] = [
  "first_failure",
  "unresolved",
  "recovered",
  "check_failed",
  "session_start",
  "session_stop",
];

/** Kinds drawn on the scrubber; session start and stop are its two ends anyway. */
const MARKED: ReadonlySet<ImportantKind> = new Set([
  "first_failure",
  "unresolved",
  "recovered",
  "check_failed",
]);

const BY_TYPE: Partial<Record<string, ImportantKind>> = {
  "session.started": "session_start",
  "session.stopped": "session_stop",
  "check.failed": "check_failed",
};

/**
 * Important events in sequence order: the report's references (which may point past the
 * loaded pages) merged with loaded events whose recorded type is important.
 */
export function buildImportant(
  report: ForensicReport | null,
  items: readonly TimelineItem[],
): readonly ImportantEvent[] {
  const found = new Map<number, { eventId: string; kinds: Set<ImportantKind> }>();
  const add = (sequence: number, eventId: string, kind: ImportantKind): void => {
    const entry = found.get(sequence);
    if (entry === undefined) found.set(sequence, { eventId, kinds: new Set([kind]) });
    else if (entry.eventId === eventId) entry.kinds.add(kind);
  };

  if (report !== null) {
    const first = report.first_observable_failure;
    if (first !== null) add(first.sequence, first.event_id, "first_failure");
    for (const f of report.observable_failures.items) {
      if (f.failure_type === "check_failed") add(f.sequence, f.event_id, "check_failed");
    }
    for (const r of report.recoveries.items) add(r.recovered_sequence, r.recovered_event_id, "recovered");
    for (const u of report.unresolved_failures.items) {
      add(u.last_failed_sequence, u.last_failed_event_id, "unresolved");
    }
  }
  for (const item of items) {
    const kind = BY_TYPE[item.type];
    if (kind !== undefined) add(item.sequence, item.event_id, kind);
  }

  return [...found.entries()]
    .sort(([a], [b]) => a - b)
    .map(([sequence, { eventId, kinds }]) => ({
      sequence,
      eventId,
      kinds: PRIORITY.filter((k) => kinds.has(k)),
    }));
}

/** The nearest important event strictly after (dir 1) or before (dir -1) `sequence`. */
export function adjacentImportant(
  events: readonly ImportantEvent[],
  sequence: number,
  dir: 1 | -1,
): ImportantEvent | null {
  if (dir === 1) return events.find((e) => e.sequence > sequence) ?? null;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i]!.sequence < sequence) return events[i]!;
  }
  return null;
}

export interface ScrubberMarker {
  sequence: number;
  eventId: string;
  kind: ImportantKind;
  label: string;
  tone: Tone;
  /** Left offset on the scrubber, 0-100 %. */
  percent: number;
}

/** At most this many markers are drawn; nearer ones share a slot (highest priority wins). */
export const MAX_SCRUBBER_MARKERS = 200;

export function scrubberMarkers(
  events: readonly ImportantEvent[],
  scaleMax: number,
): readonly ScrubberMarker[] {
  const slots = new Map<number, ScrubberMarker>();
  for (const event of events) {
    const kind = event.kinds.find((k) => MARKED.has(k));
    if (kind === undefined) continue;
    const percent = markerPercent(event.sequence, scaleMax);
    const slot = Math.round((percent / 100) * (MAX_SCRUBBER_MARKERS - 1));
    const held = slots.get(slot);
    if (held !== undefined && PRIORITY.indexOf(held.kind) <= PRIORITY.indexOf(kind)) continue;
    slots.set(slot, {
      sequence: event.sequence,
      eventId: event.eventId,
      kind,
      label: IMPORTANT_LABELS[kind],
      tone: IMPORTANT_TONES[kind],
      percent,
    });
  }
  return [...slots.values()].sort((a, b) => a.sequence - b.sequence);
}

// -- keyboard --------------------------------------------------------------------------------

const TEXT_INPUT_TYPES = new Set([
  "text",
  "search",
  "email",
  "number",
  "password",
  "tel",
  "url",
  "date",
  "datetime-local",
  "month",
  "time",
  "week",
]);

/** Elements where typing must never trigger a playback shortcut. */
export function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  return target instanceof HTMLInputElement && TEXT_INPUT_TYPES.has(target.type);
}

/** Controls whose own Space activation must win over the play/pause shortcut. */
export function isOwnSpaceControl(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target instanceof HTMLInputElement) return target.type !== "range";
  return target.closest("button, a[href], summary, [role='button'], [role='tab'], select") !== null;
}
