/**
 * Display formatting. Recorded times are UTC and shown as UTC, like the CLI, so a timeline
 * reads the same on every machine.
 */

const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$/;

/** Milliseconds since the epoch for a recorded `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` time. */
export function parseTimestamp(value: string): number | null {
  const match = TIMESTAMP.exec(value);
  if (match === null) return null;
  const [, y, mo, d, h, mi, s, fraction = ""] = match;
  const ms = Number(fraction.padEnd(3, "0").slice(0, 3));
  const time = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s), ms);
  return Number.isNaN(time) ? null : time;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** `2026-10-14 02:45:07 UTC`, or the raw text if it is not a recorded timestamp. */
export function formatUtc(value: string): string {
  const time = parseTimestamp(value);
  if (time === null) return value;
  const d = new Date(time);
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`
  );
}

/** `02:45:07` (UTC) for compact places. */
export function formatUtcTime(value: number): string {
  const d = new Date(value);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** `850ms`, `17s`, `4m 05s`, `1h 02m`, `3d 04h`; null when either end is unknown. */
export function formatDuration(startedAt: string, endedAt: string | null): string | null {
  if (endedAt === null) return null;
  const start = parseTimestamp(startedAt);
  const end = parseTimestamp(endedAt);
  if (start === null || end === null || end < start) return null;
  const ms = end - start;
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${pad(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${pad(minutes % 60)}m`;
  return `${Math.floor(hours / 24)}d ${pad(hours % 24)}h`;
}

const NUMBER = new Intl.NumberFormat("en-US");

export function formatCount(n: number): string {
  return NUMBER.format(n);
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}
