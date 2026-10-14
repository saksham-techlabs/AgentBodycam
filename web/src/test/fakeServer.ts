/**
 * An in-memory stand-in for the Replay server's HTTP interface, used as `fetch`. It applies
 * the same bearer-token rule as the real server and records every request for assertions.
 */

import { vi } from "vitest";

import type { ForensicReport, SessionDetail, SessionSummary, TimelineItem } from "../api/types";
import { RICH_TIMELINE, item, longTimeline, plainReport, richReport } from "./fixtures";

export const TOKEN = "Zq3vX8_t-Y2kLm9Pw4Rb7Nc1Hs6Df0Gj5Ua2Ke8Vo3";

export const LIVE_ID = "20261014T031500Z-1111111111111111";
export const STOPPED_ID = "20261014T021500Z-2222222222222222";
export const CRASHED_ID = "20261014T011500Z-3333333333333333";
export const UNKNOWN_ID = "20261014T004500Z-4444444444444444";
export const CREATED_ID = "20261014T003000Z-5555555555555555";
export const UNREADABLE_ID = "20261014T001500Z-6666666666666666";

export interface RecordedRequest {
  path: string;
  method: string;
  headers: Record<string, string>;
}

export type Reply = { status: number; body: unknown } | "network-error";
type Handler = (path: string) => Reply | undefined;

function readable(
  session_id: string,
  recording_state: SessionDetail["recording_state"],
  overrides: Partial<SessionSummary> = {},
): SessionSummary {
  const stopped = recording_state === "stopped";
  return {
    session_id,
    readable: true,
    status: stopped ? "stopped" : recording_state === "not_started" ? "created" : "recording",
    recording_state,
    analysis_complete: stopped || recording_state === "ended_without_stop",
    started_at: `${session_id.slice(0, 4)}-${session_id.slice(4, 6)}-${session_id.slice(6, 8)}T${session_id.slice(9, 11)}:${session_id.slice(11, 13)}:${session_id.slice(13, 15)}.250000Z`,
    ended_at: stopped ? "2026-10-14T02:19:05.750000Z" : null,
    event_count: 14,
    ...overrides,
  } as SessionSummary;
}

export const SESSIONS: SessionSummary[] = [
  readable(LIVE_ID, "recording", { event_count: 3 }),
  readable(STOPPED_ID, "stopped", { event_count: RICH_TIMELINE.length }),
  readable(CRASHED_ID, "ended_without_stop", { event_count: 250 }),
  readable(UNKNOWN_ID, "unknown", { event_count: 2 }),
  readable(CREATED_ID, "not_started", { event_count: 0 }),
  {
    session_id: UNREADABLE_ID,
    readable: false,
    status: null,
    recording_state: null,
    analysis_complete: null,
    started_at: null,
    ended_at: null,
    event_count: null,
  },
];

export function detailFor(summary: SessionSummary): SessionDetail {
  if (!summary.readable) throw new Error("unreadable sessions have no detail");
  return {
    api_version: 1,
    session_id: summary.session_id,
    status: summary.status,
    recording_state: summary.recording_state,
    analysis_complete: summary.analysis_complete,
    started_at: summary.started_at,
    ended_at: summary.ended_at,
    event_count: summary.event_count,
    event_counts: { "check.failed": 1, "check.passed": 2, "file.modified": 2 },
    command_summary: {
      commands_started: 3,
      commands_finished: 3,
      incomplete_commands: 0,
      checks_passed: 2,
      checks_failed: 1,
      checks_ungrouped: 0,
      check_signatures: 1,
    },
    observable_failure_count: 1,
    recovery_count: 1,
    unresolved_failure_count: 0,
    warning_count: 2,
  };
}

function error(status: number, code: string): Reply {
  return { status, body: { error: { code, message: `backend text for ${code} at C:\\secret` } } };
}

export function defaultTimelines(): Map<string, TimelineItem[]> {
  return new Map([
    [
      LIVE_ID,
      [
        item(1, "session.started", "Recording started", { salt: "c", base: "2026-10-14T03:15:00Z" }),
        item(2, "git.snapshot", "Git: main @ 9f8e7d6, clean", { salt: "c", base: "2026-10-14T03:15:00Z" }),
        item(3, "file.modified", "Modified README.md", { path: "README.md", salt: "c", base: "2026-10-14T03:15:00Z" }),
      ],
    ],
    [STOPPED_ID, [...RICH_TIMELINE]],
    [CRASHED_ID, longTimeline(250)],
    [UNKNOWN_ID, longTimeline(2, "d")],
    [CREATED_ID, []],
  ]);
}

export class FakeServer {
  readonly requests: RecordedRequest[] = [];
  sessions: SessionSummary[] = [...SESSIONS];
  timelines = defaultTimelines();
  /** Per-session report replacements; otherwise a report is derived from the fixtures. */
  reports = new Map<string, ForensicReport>();
  bootstrap: Reply = { status: 200, body: { api_version: 1, token: TOKEN } };
  token = TOKEN;
  /** Checked before the built-in routes; return undefined to fall through. */
  override: Handler = () => undefined;

  readonly fetch = vi.fn(async (input: string, init: RequestInit): Promise<Response> => {
    const headers = Object.fromEntries(new Headers(init.headers).entries());
    this.requests.push({ path: input, method: init.method ?? "GET", headers });
    if (init.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const reply = this.override(input) ?? this.#route(input, headers);
    if (reply === "network-error") throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify(reply.body), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  });

  reportFor(sessionId: string): ForensicReport | null {
    const replaced = this.reports.get(sessionId);
    if (replaced !== undefined) return replaced;
    const session = this.sessions.find((s) => s.session_id === sessionId);
    if (session === undefined || !session.readable) return null;
    if (sessionId === STOPPED_ID) return richReport(sessionId);
    return plainReport(session, this.timelines.get(sessionId) ?? []);
  }

  #route(path: string, headers: Record<string, string>): Reply {
    if (path === "/ui/bootstrap") return this.bootstrap;
    if (path === "/api/v1/health") {
      return { status: 200, body: { status: "ok", api_version: 1, product: "AgentBodycam" } };
    }
    if (headers.authorization !== `Bearer ${this.token}`) return error(401, "unauthorized");
    if (path === "/api/v1/project") {
      return { status: 200, body: { api_version: 1, session_count: this.sessions.length } };
    }
    if (path === "/api/v1/sessions") {
      return {
        status: 200,
        body: { api_version: 1, sessions: this.sessions, total: this.sessions.length },
      };
    }
    const match = /^\/api\/v1\/sessions\/([^/?]+)(?:\/(report|timeline))?(?:\?(.*))?$/.exec(path);
    if (match === null) return error(404, "not_found");
    const [, sessionId = "", sub, query] = match;
    const session = this.sessions.find((s) => s.session_id === sessionId);
    if (session === undefined) return error(404, "session_not_found");
    if (!session.readable) return error(500, "session_unreadable");
    if (sub === undefined) return { status: 200, body: detailFor(session) };
    if (sub === "report") return { status: 200, body: this.reportFor(sessionId) };
    const params = new URLSearchParams(query ?? "");
    const after = Number(params.get("after_sequence") ?? "0");
    const limit = Number(params.get("limit") ?? "100");
    const all = this.timelines.get(sessionId) ?? [];
    const items = all.filter((i) => i.sequence > after).slice(0, limit);
    const next = items.at(-1)?.sequence ?? after;
    return {
      status: 200,
      body: {
        api_version: 1,
        session_id: sessionId,
        after_sequence: after,
        limit,
        items,
        next_after_sequence: next,
        has_more: next < all.length,
        total: all.length,
      },
    };
  }

  pathsRequested(): string[] {
    return this.requests.map((r) => r.path);
  }
}
