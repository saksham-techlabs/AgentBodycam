/**
 * Response models of the local Replay API (`/api/v1`), mirroring
 * `src/agentbodycam/models/replay.py` field for field. Nothing here is invented: if the
 * backend does not return a field, it is not declared.
 */

export type RecordingState =
  | "stopped"
  | "recording"
  | "ended_without_stop"
  | "unknown"
  | "not_started";

export const RECORDING_STATES: readonly RecordingState[] = [
  "stopped",
  "recording",
  "ended_without_stop",
  "unknown",
  "not_started",
];

/** `session.json` status: `created`, `recording` or `stopped`. */
export type SessionStatus = "created" | "recording" | "stopped";

export const SESSION_STATUSES: readonly SessionStatus[] = ["created", "recording", "stopped"];

/** `GET /api/v1/health` (the only unauthenticated route). */
export interface HealthResponse {
  status: "ok";
  api_version: number;
  product: string;
}

/** `GET /api/v1/project`. The absolute project path is deliberately never returned. */
export interface ProjectSummary {
  api_version: number;
  session_count: number;
}

export interface ReadableSessionSummary {
  session_id: string;
  readable: true;
  status: SessionStatus;
  recording_state: RecordingState;
  analysis_complete: boolean;
  started_at: string;
  ended_at: string | null;
  event_count: number;
}

/** A session whose stored evidence could not be read: every other field is null. */
export interface UnreadableSessionSummary {
  session_id: string;
  readable: false;
  status: null;
  recording_state: null;
  analysis_complete: null;
  started_at: null;
  ended_at: null;
  event_count: null;
}

export type SessionSummary = ReadableSessionSummary | UnreadableSessionSummary;

/** `GET /api/v1/sessions`: newest first. */
export interface SessionsResponse {
  api_version: number;
  sessions: SessionSummary[];
  total: number;
}

export interface CommandSummary {
  commands_started: number;
  commands_finished: number;
  incomplete_commands: number;
  checks_passed: number;
  checks_failed: number;
  checks_ungrouped: number;
  check_signatures: number;
}

/** `GET /api/v1/sessions/<id>`: header data taken from the session's forensic report. */
export interface SessionDetail {
  api_version: number;
  session_id: string;
  status: SessionStatus;
  recording_state: RecordingState;
  analysis_complete: boolean;
  started_at: string;
  ended_at: string | null;
  event_count: number;
  event_counts: Record<string, number>;
  command_summary: CommandSummary;
  observable_failure_count: number;
  recovery_count: number;
  unresolved_failure_count: number;
  warning_count: number;
}

/** Body of every API error response. `message` is fixed backend text; the UI never shows it. */
export interface ApiErrorBody {
  error: { code: string; message: string };
}

/** `GET /ui/bootstrap`: served only to the Replay UI page itself (see `auth`). */
export interface UiBootstrap {
  api_version: number;
  token: string;
}

// -- forensic report and timeline (`src/agentbodycam/models/forensics.py`) ---------------

/** `EvidenceKind` of an event. Every forensic finding is currently `observed`. */
export type EvidenceKind = "observed" | "inferred";

export const EVIDENCE_KINDS: readonly EvidenceKind[] = ["observed", "inferred"];

/** `EventCategory`: the part of an event type before the dot. */
export type EventCategory = "session" | "file" | "git" | "command" | "check" | "error";

export const EVENT_CATEGORIES: readonly EventCategory[] = [
  "session",
  "file",
  "git",
  "command",
  "check",
  "error",
];

/** A list capped for size; `total` always counts every item, shown or not. */
export interface Bounded<T> {
  items: T[];
  total: number;
  truncated: boolean;
}

/** The safe, replay-facing view of one event: no payload, no file content. */
export interface TimelineItem {
  sequence: number;
  event_id: string;
  /** Display only; `sequence` is the authoritative order. */
  timestamp: string;
  type: string;
  category: EventCategory;
  source: string;
  summary: string;
  /** Project-relative, `/`-separated. */
  path: string | null;
  command_id: string | null;
  evidence: EvidenceKind;
}

/** `GET /api/v1/sessions/<id>/timeline`: events with `after_sequence < sequence <= next_after_sequence`. */
export interface TimelinePage {
  api_version: number;
  session_id: string;
  after_sequence: number;
  limit: number;
  items: TimelineItem[];
  next_after_sequence: number;
  has_more: boolean;
  /** Events stored when the page was read. */
  total: number;
}

export type FileChange = "created" | "modified" | "deleted";

export const FILE_CHANGES: readonly FileChange[] = ["created", "modified", "deleted"];

export interface FileChangeRef {
  sequence: number;
  event_id: string;
  change: FileChange;
  path: string | null;
}

/** File events strictly between two sequence numbers (`null`: open-ended). */
export interface ChangeWindow {
  after_sequence: number | null;
  before_sequence: number | null;
  description: string;
  file_change_count: number;
  created: number;
  modified: number;
  deleted: number;
  file_changes: FileChangeRef[];
  file_changes_truncated: boolean;
}

export type FailureType =
  | "check_failed"
  | "command_spawn_error"
  | "command_signaled"
  | "command_interrupted";

export const FAILURE_TYPES: readonly FailureType[] = [
  "check_failed",
  "command_spawn_error",
  "command_signaled",
  "command_interrupted",
];

export interface FailureFinding {
  event_id: string;
  sequence: number;
  failure_type: FailureType;
  summary: string;
  command_id: string | null;
  check_kind: string | null;
  check_name: string | null;
  check_signature: string | null;
  exit_code: number | null;
  reason: string | null;
  evidence_event_ids: string[];
  finding_evidence: EvidenceKind;
}

/** A check signature failed, then a later run of the same signature passed. */
export interface Recovery {
  check_signature: string;
  kind: string;
  name: string;
  display_name: string;
  first_failed_event_id: string;
  first_failed_sequence: number;
  last_failed_event_id: string;
  last_failed_sequence: number;
  recovered_event_id: string;
  recovered_sequence: number;
  failed_attempts: number;
  retry_count: number;
  previous_pass_event_id: string | null;
  previous_pass_sequence: number | null;
  previous_pass_note: string | null;
  changes_before_failure: ChangeWindow | null;
  changes_between_failure_and_pass: ChangeWindow;
  changes_after_last_failure: ChangeWindow;
  summary: string;
  evidence_event_ids: string[];
  evidence_event_ids_truncated: boolean;
  finding_evidence: EvidenceKind;
}

/** The latest recorded result of a check signature is a failure. */
export interface UnresolvedFailure {
  check_signature: string;
  kind: string;
  name: string;
  display_name: string;
  first_failed_event_id: string;
  first_failed_sequence: number;
  last_failed_event_id: string;
  last_failed_sequence: number;
  failed_attempts: number;
  retry_count: number;
  previous_pass_event_id: string | null;
  previous_pass_sequence: number | null;
  previous_pass_note: string | null;
  changes_before_failure: ChangeWindow | null;
  changes_after_last_failure: ChangeWindow;
  summary: string;
  evidence_event_ids: string[];
  evidence_event_ids_truncated: boolean;
  finding_evidence: EvidenceKind;
}

export type CheckResult = "passed" | "failed";

/** The last observed result of one check signature (or one ungrouped check run). */
export interface CheckState {
  check_signature: string | null;
  identity_established: boolean;
  kind: string;
  name: string;
  display_name: string;
  working_directory: string | null;
  latest_result: CheckResult;
  latest_event_id: string;
  latest_sequence: number;
  attempts: number;
  passed: number;
  failed: number;
  summary: string;
  finding_evidence: EvidenceKind;
}

export interface IncompleteCommand {
  command_id: string;
  started_event_id: string;
  started_sequence: number;
  executable: string | null;
  working_directory: string | null;
  command_summary: string;
  summary: string;
  evidence_event_ids: string[];
  finding_evidence: EvidenceKind;
}

export interface PathObservation {
  path: string;
  change_events: number;
  first_change: FileChange;
  last_change: FileChange;
  last_sequence: number;
  last_event_id: string;
  present_at_last_observation: boolean;
}

export interface FileChangeSummary {
  description: string;
  created_events: number;
  modified_events: number;
  deleted_events: number;
  total_events: number;
  unique_paths: number;
  paths: Bounded<PathObservation>;
}

/** The last `git.snapshot` of the recording: historical evidence, not the current state. */
export interface GitStateSummary {
  description: string;
  event_id: string;
  sequence: number;
  snapshot_count: number;
  repository: boolean;
  unavailable_reason: string | null;
  branch: string | null;
  detached: boolean | null;
  unborn: boolean | null;
  /** Abbreviated SHA. */
  head: string | null;
  state: "clean" | "dirty" | null;
  staged: number | null;
  unstaged: number | null;
  untracked: number | null;
  conflicted: number | null;
  upstream: string | null;
  ahead: number | null;
  behind: number | null;
  summary: string;
}

export interface ErrorObserved {
  sequence: number;
  event_id: string;
  source: string;
  operation: string | null;
  error: string | null;
  path: string | null;
  summary: string;
}

export type WarningSeverity = "info" | "warning";

export interface ForensicWarning {
  code: string;
  severity: WarningSeverity;
  message: string;
  event_ids: string[];
}

/** `GET /api/v1/sessions/<id>/report`: exactly `agentbodycam analyze --json`. */
export interface ForensicReport {
  schema_version: number;
  session_id: string;
  session_status: SessionStatus;
  recording_state: RecordingState;
  analysis_complete: boolean;
  started_at: string;
  ended_at: string | null;
  event_count: number;
  event_counts: Record<string, number>;
  finding_evidence: EvidenceKind;
  timeline: Bounded<TimelineItem>;
  first_observable_failure: FailureFinding | null;
  observable_failures: Bounded<FailureFinding>;
  recoveries: Bounded<Recovery>;
  unresolved_failures: Bounded<UnresolvedFailure>;
  final_check_states: Bounded<CheckState>;
  incomplete_commands: Bounded<IncompleteCommand>;
  command_summary: CommandSummary;
  file_change_summary: FileChangeSummary;
  latest_git_state: GitStateSummary | null;
  errors_observed: Bounded<ErrorObserved>;
  warnings: Bounded<ForensicWarning>;
}
