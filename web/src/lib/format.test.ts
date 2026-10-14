import { describe, expect, it } from "vitest";

import { formatDuration, formatUtc, parseTimestamp, plural } from "./format";

describe("format", () => {
  it("parses recorded microsecond UTC timestamps", () => {
    expect(parseTimestamp("2026-10-14T00:10:23.639517Z")).toBe(Date.UTC(2026, 9, 14, 0, 10, 23, 639));
    expect(parseTimestamp("2026-10-14T00:10:23Z")).toBe(Date.UTC(2026, 9, 14, 0, 10, 23));
    expect(parseTimestamp("yesterday")).toBeNull();
  });

  it("shows times in UTC", () => {
    expect(formatUtc("2026-10-14T00:10:23.639517Z")).toBe("2026-10-14 00:10:23 UTC");
    expect(formatUtc("not a time")).toBe("not a time");
  });

  it("formats durations", () => {
    const start = "2026-10-14T00:00:00.000000Z";
    expect(formatDuration(start, null)).toBeNull();
    expect(formatDuration(start, "2026-10-14T00:00:00.250000Z")).toBe("250ms");
    expect(formatDuration(start, "2026-10-14T00:00:17.000000Z")).toBe("17s");
    expect(formatDuration(start, "2026-10-14T00:04:05.000000Z")).toBe("4m 05s");
    expect(formatDuration(start, "2026-10-14T01:02:00.000000Z")).toBe("1h 02m");
    expect(formatDuration(start, "2026-10-17T04:00:00.000000Z")).toBe("3d 04h");
    expect(formatDuration("2026-10-14T00:00:05Z", start)).toBeNull();
  });

  it("pluralises counts", () => {
    expect(plural(1, "recorded session")).toBe("1 recorded session");
    expect(plural(12, "recorded session")).toBe("12 recorded sessions");
    expect(plural(1234, "event")).toBe("1,234 events");
  });
});
