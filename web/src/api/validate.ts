/**
 * Minimal runtime checks that a response really has the declared shape, so a malformed or
 * unexpected body becomes a clean `invalid_response` error instead of a render crash.
 */

import {
  RECORDING_STATES,
  SESSION_STATUSES,
  type CommandSummary,
  type HealthResponse,
  type ProjectSummary,
  type RecordingState,
  type SessionDetail,
  type SessionStatus,
  type SessionSummary,
  type SessionsResponse,
  type UiBootstrap,
} from "./types";

/** Exactly the backend's session-ID pattern (`models/session.py`). */
export const SESSION_ID_PATTERN = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

export class InvalidResponseError extends Error {
  constructor() {
    super("The server returned an unexpected response.");
    this.name = "InvalidResponseError";
  }
}

type Json = Record<string, unknown>;

function fail(): never {
  throw new InvalidResponseError();
}

function object(value: unknown): Json {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Json)
    : fail();
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : fail();
}

function text(value: unknown): string {
  return typeof value === "string" ? value : fail();
}

function bool(value: unknown): boolean {
  return typeof value === "boolean" ? value : fail();
}

function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}

function sessionId(value: unknown): string {
  const id = text(value);
  return SESSION_ID_PATTERN.test(id) ? id : fail();
}

function recordingState(value: unknown): RecordingState {
  return RECORDING_STATES.includes(value as RecordingState) ? (value as RecordingState) : fail();
}

function sessionStatus(value: unknown): SessionStatus {
  return SESSION_STATUSES.includes(value as SessionStatus) ? (value as SessionStatus) : fail();
}

export function isSessionId(value: string): boolean {
  return SESSION_ID_PATTERN.test(value);
}

export function parseHealth(value: unknown): HealthResponse {
  const data = object(value);
  if (data.status !== "ok") fail();
  return { status: "ok", api_version: count(data.api_version), product: text(data.product) };
}

export function parseBootstrap(value: unknown): UiBootstrap {
  const data = object(value);
  const token = text(data.token);
  if (!TOKEN_PATTERN.test(token)) fail();
  return { api_version: count(data.api_version), token };
}

export function parseProject(value: unknown): ProjectSummary {
  const data = object(value);
  return { api_version: count(data.api_version), session_count: count(data.session_count) };
}

function parseSessionSummary(value: unknown): SessionSummary {
  const data = object(value);
  const id = sessionId(data.session_id);
  if (data.readable === false) {
    return {
      session_id: id,
      readable: false,
      status: null,
      recording_state: null,
      analysis_complete: null,
      started_at: null,
      ended_at: null,
      event_count: null,
    };
  }
  if (data.readable !== true) fail();
  return {
    session_id: id,
    readable: true,
    status: sessionStatus(data.status),
    recording_state: recordingState(data.recording_state),
    analysis_complete: bool(data.analysis_complete),
    started_at: text(data.started_at),
    ended_at: nullableText(data.ended_at),
    event_count: count(data.event_count),
  };
}

export function parseSessions(value: unknown): SessionsResponse {
  const data = object(value);
  const sessions = Array.isArray(data.sessions) ? data.sessions.map(parseSessionSummary) : fail();
  return { api_version: count(data.api_version), sessions, total: count(data.total) };
}

function parseCommandSummary(value: unknown): CommandSummary {
  const data = object(value);
  return {
    commands_started: count(data.commands_started),
    commands_finished: count(data.commands_finished),
    incomplete_commands: count(data.incomplete_commands),
    checks_passed: count(data.checks_passed),
    checks_failed: count(data.checks_failed),
    checks_ungrouped: count(data.checks_ungrouped),
    check_signatures: count(data.check_signatures),
  };
}

function parseEventCounts(value: unknown): Record<string, number> {
  const data = object(value);
  const counts: Record<string, number> = {};
  for (const [name, n] of Object.entries(data)) counts[name] = count(n);
  return counts;
}

export function parseSessionDetail(value: unknown): SessionDetail {
  const data = object(value);
  return {
    api_version: count(data.api_version),
    session_id: sessionId(data.session_id),
    status: sessionStatus(data.status),
    recording_state: recordingState(data.recording_state),
    analysis_complete: bool(data.analysis_complete),
    started_at: text(data.started_at),
    ended_at: nullableText(data.ended_at),
    event_count: count(data.event_count),
    event_counts: parseEventCounts(data.event_counts),
    command_summary: parseCommandSummary(data.command_summary),
    observable_failure_count: count(data.observable_failure_count),
    recovery_count: count(data.recovery_count),
    unresolved_failure_count: count(data.unresolved_failure_count),
    warning_count: count(data.warning_count),
  };
}

/** The error code of an API error body, or null if the body is not one. */
export function errorCode(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const error = (value as Json).error;
  if (typeof error !== "object" || error === null) return null;
  const code = (error as Json).code;
  return typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? code : null;
}
