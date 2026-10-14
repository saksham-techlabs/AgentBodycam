/**
 * Minimal runtime checks that a response really has the declared shape, so a malformed or
 * unexpected body becomes a clean `invalid_response` error instead of a render crash.
 */

import {
  EVENT_CATEGORIES,
  EVIDENCE_KINDS,
  FAILURE_TYPES,
  FILE_CHANGES,
  RECORDING_STATES,
  SESSION_STATUSES,
  type Bounded,
  type ChangeWindow,
  type CheckState,
  type CommandSummary,
  type ErrorObserved,
  type EventCategory,
  type EvidenceKind,
  type FailureFinding,
  type FailureType,
  type FileChange,
  type FileChangeRef,
  type FileChangeSummary,
  type ForensicReport,
  type ForensicWarning,
  type GitStateSummary,
  type HealthResponse,
  type IncompleteCommand,
  type PathObservation,
  type ProjectSummary,
  type RecordingState,
  type Recovery,
  type SessionDetail,
  type SessionStatus,
  type SessionSummary,
  type SessionsResponse,
  type TimelineItem,
  type TimelinePage,
  type UiBootstrap,
  type UnresolvedFailure,
} from "./types";

/** Exactly the backend's session-ID pattern (`models/session.py`). */
export const SESSION_ID_PATTERN = /^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

/** Exactly the backend's event-ID pattern (`models/events.py`). */
export const EVENT_ID_PATTERN = /^[0-9a-f]{32}$/;

const EVENT_TYPE_PATTERN = /^[a-z]+\.[a-z_]+$/;

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

// -- forensic report and timeline ----------------------------------------------------------

function nullableCount(value: unknown): number | null {
  return value === null ? null : count(value);
}

function nullableInt(value: unknown): number | null {
  if (value === null) return null;
  return typeof value === "number" && Number.isInteger(value) ? value : fail();
}

function nullableBool(value: unknown): boolean | null {
  return value === null ? null : bool(value);
}

function sequence(value: unknown): number {
  const n = count(value);
  return n >= 1 ? n : fail();
}

function nullableSequence(value: unknown): number | null {
  return value === null ? null : sequence(value);
}

function eventId(value: unknown): string {
  const id = text(value);
  return EVENT_ID_PATTERN.test(id) ? id : fail();
}

function nullableEventId(value: unknown): string | null {
  return value === null ? null : eventId(value);
}

function oneOf<T extends string>(allowed: readonly T[], value: unknown): T {
  return allowed.includes(value as T) ? (value as T) : fail();
}

function list<T>(value: unknown, parse: (item: unknown) => T): T[] {
  return Array.isArray(value) ? value.map(parse) : fail();
}

function eventIds(value: unknown): string[] {
  return list(value, eventId);
}

function evidence(value: unknown): EvidenceKind {
  return oneOf(EVIDENCE_KINDS, value);
}

function fileChange(value: unknown): FileChange {
  return oneOf(FILE_CHANGES, value);
}

function bounded<T>(value: unknown, parse: (item: unknown) => T): Bounded<T> {
  const data = object(value);
  const items = list(data.items, parse);
  const total = count(data.total);
  const truncated = bool(data.truncated);
  if (total < items.length || truncated !== total > items.length) fail();
  return { items, total, truncated };
}

export function parseTimelineItem(value: unknown): TimelineItem {
  const data = object(value);
  const type = text(data.type);
  if (!EVENT_TYPE_PATTERN.test(type)) fail();
  return {
    sequence: sequence(data.sequence),
    event_id: eventId(data.event_id),
    timestamp: text(data.timestamp),
    type,
    category: oneOf<EventCategory>(EVENT_CATEGORIES, data.category),
    source: text(data.source),
    summary: text(data.summary),
    path: nullableText(data.path),
    command_id: nullableText(data.command_id),
    evidence: evidence(data.evidence),
  };
}

/** Items in strictly increasing sequence with no repeated event ID, else invalid. */
function assertOrdered(items: readonly TimelineItem[], after: number): void {
  let previous = after;
  const ids = new Set<string>();
  for (const item of items) {
    if (item.sequence <= previous || ids.has(item.event_id)) fail();
    previous = item.sequence;
    ids.add(item.event_id);
  }
}

/**
 * A timeline page, checked for internal consistency: the cursor fields must describe exactly
 * the items returned, which must be strictly ordered after `after_sequence`.
 */
export function parseTimelinePage(value: unknown): TimelinePage {
  const data = object(value);
  const afterSequence = count(data.after_sequence);
  const limit = count(data.limit);
  const items = list(data.items, parseTimelineItem);
  const next = count(data.next_after_sequence);
  const hasMore = bool(data.has_more);
  const total = count(data.total);
  if (limit < 1 || items.length > limit) fail();
  assertOrdered(items, afterSequence);
  const last = items.at(-1);
  if (next !== (last === undefined ? afterSequence : last.sequence)) fail();
  if (next > total || hasMore !== next < total) fail();
  return {
    api_version: count(data.api_version),
    session_id: sessionId(data.session_id),
    after_sequence: afterSequence,
    limit,
    items,
    next_after_sequence: next,
    has_more: hasMore,
    total,
  };
}

function fileChangeRef(value: unknown): FileChangeRef {
  const data = object(value);
  return {
    sequence: sequence(data.sequence),
    event_id: eventId(data.event_id),
    change: fileChange(data.change),
    path: nullableText(data.path),
  };
}

function changeWindow(value: unknown): ChangeWindow {
  const data = object(value);
  const window: ChangeWindow = {
    after_sequence: nullableSequence(data.after_sequence),
    before_sequence: nullableSequence(data.before_sequence),
    description: text(data.description),
    file_change_count: count(data.file_change_count),
    created: count(data.created),
    modified: count(data.modified),
    deleted: count(data.deleted),
    file_changes: list(data.file_changes, fileChangeRef),
    file_changes_truncated: bool(data.file_changes_truncated),
  };
  const shown = window.file_changes.length;
  if (
    window.created + window.modified + window.deleted !== window.file_change_count ||
    shown > window.file_change_count ||
    window.file_changes_truncated !== window.file_change_count > shown
  ) {
    fail();
  }
  return window;
}

function nullableChangeWindow(value: unknown): ChangeWindow | null {
  return value === null ? null : changeWindow(value);
}

function failureFinding(value: unknown): FailureFinding {
  const data = object(value);
  return {
    event_id: eventId(data.event_id),
    sequence: sequence(data.sequence),
    failure_type: oneOf<FailureType>(FAILURE_TYPES, data.failure_type),
    summary: text(data.summary),
    command_id: nullableText(data.command_id),
    check_kind: nullableText(data.check_kind),
    check_name: nullableText(data.check_name),
    check_signature: nullableText(data.check_signature),
    exit_code: nullableInt(data.exit_code),
    reason: nullableText(data.reason),
    evidence_event_ids: eventIds(data.evidence_event_ids),
    finding_evidence: evidence(data.finding_evidence),
  };
}

/** Fields shared by recoveries and unresolved failures (one check signature's failing period). */
function failingPeriod(data: Json) {
  return {
    check_signature: text(data.check_signature),
    kind: text(data.kind),
    name: text(data.name),
    display_name: text(data.display_name),
    first_failed_event_id: eventId(data.first_failed_event_id),
    first_failed_sequence: sequence(data.first_failed_sequence),
    last_failed_event_id: eventId(data.last_failed_event_id),
    last_failed_sequence: sequence(data.last_failed_sequence),
    failed_attempts: count(data.failed_attempts),
    retry_count: count(data.retry_count),
    previous_pass_event_id: nullableEventId(data.previous_pass_event_id),
    previous_pass_sequence: nullableSequence(data.previous_pass_sequence),
    previous_pass_note: nullableText(data.previous_pass_note),
    changes_before_failure: nullableChangeWindow(data.changes_before_failure),
    changes_after_last_failure: changeWindow(data.changes_after_last_failure),
    summary: text(data.summary),
    evidence_event_ids: eventIds(data.evidence_event_ids),
    evidence_event_ids_truncated: bool(data.evidence_event_ids_truncated),
    finding_evidence: evidence(data.finding_evidence),
  };
}

function recovery(value: unknown): Recovery {
  const data = object(value);
  const period = failingPeriod(data);
  const result: Recovery = {
    ...period,
    recovered_event_id: eventId(data.recovered_event_id),
    recovered_sequence: sequence(data.recovered_sequence),
    changes_between_failure_and_pass: changeWindow(data.changes_between_failure_and_pass),
  };
  if (
    result.first_failed_sequence > result.last_failed_sequence ||
    result.last_failed_sequence >= result.recovered_sequence
  ) {
    fail();
  }
  return result;
}

function unresolvedFailure(value: unknown): UnresolvedFailure {
  const result = failingPeriod(object(value));
  if (result.first_failed_sequence > result.last_failed_sequence) fail();
  return result;
}

function checkState(value: unknown): CheckState {
  const data = object(value);
  return {
    check_signature: nullableText(data.check_signature),
    identity_established: bool(data.identity_established),
    kind: text(data.kind),
    name: text(data.name),
    display_name: text(data.display_name),
    working_directory: nullableText(data.working_directory),
    latest_result: oneOf(["passed", "failed"] as const, data.latest_result),
    latest_event_id: eventId(data.latest_event_id),
    latest_sequence: sequence(data.latest_sequence),
    attempts: count(data.attempts),
    passed: count(data.passed),
    failed: count(data.failed),
    summary: text(data.summary),
    finding_evidence: evidence(data.finding_evidence),
  };
}

function incompleteCommand(value: unknown): IncompleteCommand {
  const data = object(value);
  return {
    command_id: text(data.command_id),
    started_event_id: eventId(data.started_event_id),
    started_sequence: sequence(data.started_sequence),
    executable: nullableText(data.executable),
    working_directory: nullableText(data.working_directory),
    command_summary: text(data.command_summary),
    summary: text(data.summary),
    evidence_event_ids: eventIds(data.evidence_event_ids),
    finding_evidence: evidence(data.finding_evidence),
  };
}

function pathObservation(value: unknown): PathObservation {
  const data = object(value);
  return {
    path: text(data.path),
    change_events: count(data.change_events),
    first_change: fileChange(data.first_change),
    last_change: fileChange(data.last_change),
    last_sequence: sequence(data.last_sequence),
    last_event_id: eventId(data.last_event_id),
    present_at_last_observation: bool(data.present_at_last_observation),
  };
}

function fileChangeSummary(value: unknown): FileChangeSummary {
  const data = object(value);
  return {
    description: text(data.description),
    created_events: count(data.created_events),
    modified_events: count(data.modified_events),
    deleted_events: count(data.deleted_events),
    total_events: count(data.total_events),
    unique_paths: count(data.unique_paths),
    paths: bounded(data.paths, pathObservation),
  };
}

function gitState(value: unknown): GitStateSummary | null {
  if (value === null) return null;
  const data = object(value);
  return {
    description: text(data.description),
    event_id: eventId(data.event_id),
    sequence: sequence(data.sequence),
    snapshot_count: count(data.snapshot_count),
    repository: bool(data.repository),
    unavailable_reason: nullableText(data.unavailable_reason),
    branch: nullableText(data.branch),
    detached: nullableBool(data.detached),
    unborn: nullableBool(data.unborn),
    head: nullableText(data.head),
    state: data.state === null ? null : oneOf(["clean", "dirty"] as const, data.state),
    staged: nullableCount(data.staged),
    unstaged: nullableCount(data.unstaged),
    untracked: nullableCount(data.untracked),
    conflicted: nullableCount(data.conflicted),
    upstream: nullableText(data.upstream),
    ahead: nullableCount(data.ahead),
    behind: nullableCount(data.behind),
    summary: text(data.summary),
  };
}

function errorObserved(value: unknown): ErrorObserved {
  const data = object(value);
  return {
    sequence: sequence(data.sequence),
    event_id: eventId(data.event_id),
    source: text(data.source),
    operation: nullableText(data.operation),
    error: nullableText(data.error),
    path: nullableText(data.path),
    summary: text(data.summary),
  };
}

function forensicWarning(value: unknown): ForensicWarning {
  const data = object(value);
  return {
    code: text(data.code),
    severity: oneOf(["info", "warning"] as const, data.severity),
    message: text(data.message),
    event_ids: eventIds(data.event_ids),
  };
}

export function parseForensicReport(value: unknown): ForensicReport {
  const data = object(value);
  const timeline = bounded(data.timeline, parseTimelineItem);
  assertOrdered(timeline.items, 0);
  return {
    schema_version: count(data.schema_version),
    session_id: sessionId(data.session_id),
    session_status: sessionStatus(data.session_status),
    recording_state: recordingState(data.recording_state),
    analysis_complete: bool(data.analysis_complete),
    started_at: text(data.started_at),
    ended_at: nullableText(data.ended_at),
    event_count: count(data.event_count),
    event_counts: parseEventCounts(data.event_counts),
    finding_evidence: evidence(data.finding_evidence),
    timeline,
    first_observable_failure:
      data.first_observable_failure === null ? null : failureFinding(data.first_observable_failure),
    observable_failures: bounded(data.observable_failures, failureFinding),
    recoveries: bounded(data.recoveries, recovery),
    unresolved_failures: bounded(data.unresolved_failures, unresolvedFailure),
    final_check_states: bounded(data.final_check_states, checkState),
    incomplete_commands: bounded(data.incomplete_commands, incompleteCommand),
    command_summary: parseCommandSummary(data.command_summary),
    file_change_summary: fileChangeSummary(data.file_change_summary),
    latest_git_state: gitState(data.latest_git_state),
    errors_observed: bounded(data.errors_observed, errorObserved),
    warnings: bounded(data.warnings, forensicWarning),
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
