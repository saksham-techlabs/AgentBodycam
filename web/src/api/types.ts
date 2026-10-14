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
