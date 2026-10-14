/**
 * Report and timeline fixtures shaped exactly like the backend's `ForensicReport.to_dict()`
 * and `TimelinePage.to_dict()`.
 */

import type {
  ChangeWindow,
  ForensicReport,
  SessionSummary,
  TimelineItem,
} from "../api/types";

/** Wording the Replay UI must never generate: order is observed, cause is not. */
export const CAUSAL =
  /caused|causes|root cause|culprit|responsible|broke|fixed|likely|probabl|suspect|confidence|blame|because of/i;

export function eventId(sequence: number, salt = "a"): string {
  return `${salt.repeat(16)}${sequence.toString(16).padStart(16, "0")}`;
}

function timestamp(base: string, sequence: number): string {
  const start = Date.parse(base);
  const t = new Date(start + sequence * 1000 + 412);
  return `${t.toISOString().slice(0, 19)}.412000Z`;
}

export function item(
  sequence: number,
  type: string,
  summary: string,
  extra: { path?: string; command_id?: string; base?: string; salt?: string } = {},
): TimelineItem {
  const category = type.split(".")[0] as TimelineItem["category"];
  const source =
    category === "file" ? "filesystem" : category === "git" ? "git" : category === "session" ? "agentbodycam" : "command";
  return {
    sequence,
    event_id: eventId(sequence, extra.salt),
    timestamp: timestamp(extra.base ?? "2026-10-14T02:15:00Z", sequence),
    type,
    category,
    source,
    summary,
    path: extra.path ?? null,
    command_id: extra.command_id ?? null,
    evidence: "observed",
  };
}

/** A stopped session with two pytest failures, a later pytest pass, a ruff failure that
 * never passes again, an error, an incomplete command and two Git snapshots. */
export const RICH_TIMELINE: TimelineItem[] = [
  item(1, "session.started", "Recording started"),
  item(2, "git.snapshot", "Git: main @ 1a2b3c4, clean"),
  item(3, "command.started", "Command started: pytest -q", { command_id: "cmd-1" }),
  item(4, "command.finished", "Command finished: pytest -q (exit 1)", { command_id: "cmd-1" }),
  item(5, "check.failed", "pytest failed (exit 1)", { command_id: "cmd-1" }),
  item(6, "file.modified", "Modified src/app.py", { path: "src/app.py" }),
  item(7, "command.started", "Command started: pytest -q", { command_id: "cmd-2" }),
  item(8, "command.finished", "Command finished: pytest -q (exit 1)", { command_id: "cmd-2" }),
  item(9, "check.failed", "pytest failed (exit 1)", { command_id: "cmd-2" }),
  item(10, "file.created", "Created src/helpers.py", { path: "src/helpers.py" }),
  item(11, "file.deleted", "Deleted src/legacy.py", { path: "src/legacy.py" }),
  item(12, "command.started", "Command started: pytest -q", { command_id: "cmd-3" }),
  item(13, "command.finished", "Command finished: pytest -q (exit 0)", { command_id: "cmd-3" }),
  item(14, "check.passed", "pytest passed", { command_id: "cmd-3" }),
  item(15, "command.started", "Command started: ruff check .", { command_id: "cmd-4" }),
  item(16, "command.finished", "Command finished: ruff check . (exit 1)", { command_id: "cmd-4" }),
  item(17, "check.failed", "ruff failed (exit 1)", { command_id: "cmd-4" }),
  item(18, "error.observed", "Scanner could not read logs/locked.txt", { path: "logs/locked.txt" }),
  item(19, "command.started", "Command started: npm run dev", { command_id: "cmd-5" }),
  item(20, "git.snapshot", "Git: main @ 1a2b3c4, dirty (2 unstaged, 1 untracked)"),
  item(21, "session.stopped", "Recording stopped"),
];

const id = (n: number) => eventId(n);

function window(
  after: number | null,
  before: number | null,
  description: string,
  changes: [number, "created" | "modified" | "deleted", string][],
): ChangeWindow {
  return {
    after_sequence: after,
    before_sequence: before,
    description: `${description}: ${changes.length}.`,
    file_change_count: changes.length,
    created: changes.filter((c) => c[1] === "created").length,
    modified: changes.filter((c) => c[1] === "modified").length,
    deleted: changes.filter((c) => c[1] === "deleted").length,
    file_changes: changes.map(([sequence, change, path]) => ({
      sequence,
      event_id: id(sequence),
      change,
      path,
    })),
    file_changes_truncated: false,
  };
}

function counts(items: TimelineItem[]): Record<string, number> {
  const result: Record<string, number> = {};
  for (const i of items) result[i.type] = (result[i.type] ?? 0) + 1;
  return result;
}

const NO_EARLIER = "No earlier passing result for this exact check was recorded.";

export function richReport(sessionId: string): ForensicReport {
  return {
    schema_version: 1,
    session_id: sessionId,
    session_status: "stopped",
    recording_state: "stopped",
    analysis_complete: true,
    started_at: "2026-10-14T02:15:00.250000Z",
    ended_at: "2026-10-14T02:19:05.750000Z",
    event_count: RICH_TIMELINE.length,
    event_counts: counts(RICH_TIMELINE),
    finding_evidence: "observed",
    timeline: { items: RICH_TIMELINE, total: RICH_TIMELINE.length, truncated: false },
    first_observable_failure: {
      event_id: id(5),
      sequence: 5,
      failure_type: "check_failed",
      summary: "pytest failed (exit 1)",
      command_id: "cmd-1",
      check_kind: "test",
      check_name: "pytest",
      check_signature: "f".repeat(64),
      exit_code: 1,
      reason: null,
      evidence_event_ids: [id(3), id(4), id(5)],
      finding_evidence: "observed",
    },
    observable_failures: {
      items: [5, 9, 17].map((n) => ({
        event_id: id(n),
        sequence: n,
        failure_type: "check_failed" as const,
        summary: n === 17 ? "ruff failed (exit 1)" : "pytest failed (exit 1)",
        command_id: `cmd-${n === 5 ? 1 : n === 9 ? 2 : 4}`,
        check_kind: n === 17 ? "lint" : "test",
        check_name: n === 17 ? "ruff" : "pytest",
        check_signature: (n === 17 ? "e" : "f").repeat(64),
        exit_code: 1,
        reason: null,
        evidence_event_ids: [id(n - 2), id(n - 1), id(n)],
        finding_evidence: "observed" as const,
      })),
      total: 3,
      truncated: false,
    },
    recoveries: {
      items: [
        {
          check_signature: "f".repeat(64),
          kind: "test",
          name: "pytest",
          display_name: "pytest",
          first_failed_event_id: id(5),
          first_failed_sequence: 5,
          last_failed_event_id: id(9),
          last_failed_sequence: 9,
          recovered_event_id: id(14),
          recovered_sequence: 14,
          failed_attempts: 2,
          retry_count: 1,
          previous_pass_event_id: null,
          previous_pass_sequence: null,
          previous_pass_note: NO_EARLIER,
          changes_before_failure: null,
          changes_between_failure_and_pass: window(
            5,
            14,
            "File changes observed between the failed check (#5) and the later passing check (#14)",
            [
              [6, "modified", "src/app.py"],
              [10, "created", "src/helpers.py"],
              [11, "deleted", "src/legacy.py"],
            ],
          ),
          changes_after_last_failure: window(
            9,
            14,
            "File changes observed between the last recorded failing check (#9) and the later passing check (#14)",
            [
              [10, "created", "src/helpers.py"],
              [11, "deleted", "src/legacy.py"],
            ],
          ),
          summary:
            "pytest failed at event #5 and later passed at event #14 after 2 additional recorded attempts.",
          evidence_event_ids: [3, 4, 5, 7, 8, 9, 12, 13, 14].map(id),
          evidence_event_ids_truncated: false,
          finding_evidence: "observed",
        },
      ],
      total: 1,
      truncated: false,
    },
    unresolved_failures: {
      items: [
        {
          check_signature: "e".repeat(64),
          kind: "lint",
          name: "ruff",
          display_name: "ruff",
          first_failed_event_id: id(17),
          first_failed_sequence: 17,
          last_failed_event_id: id(17),
          last_failed_sequence: 17,
          failed_attempts: 1,
          retry_count: 0,
          previous_pass_event_id: null,
          previous_pass_sequence: null,
          previous_pass_note: NO_EARLIER,
          changes_before_failure: null,
          changes_after_last_failure: window(
            17,
            null,
            "File changes observed after the latest failing check (#17) until the end of the recorded evidence",
            [],
          ),
          summary: "Latest recorded ruff result: FAILED (unresolved in this recording).",
          evidence_event_ids: [15, 16, 17].map(id),
          evidence_event_ids_truncated: false,
          finding_evidence: "observed",
        },
      ],
      total: 1,
      truncated: false,
    },
    final_check_states: {
      items: [
        {
          check_signature: "f".repeat(64),
          identity_established: true,
          kind: "test",
          name: "pytest",
          display_name: "pytest",
          working_directory: ".",
          latest_result: "passed",
          latest_event_id: id(14),
          latest_sequence: 14,
          attempts: 3,
          passed: 1,
          failed: 2,
          summary: "Latest recorded pytest result: PASSED (event #14).",
          finding_evidence: "observed",
        },
        {
          check_signature: "e".repeat(64),
          identity_established: true,
          kind: "lint",
          name: "ruff",
          display_name: "ruff",
          working_directory: ".",
          latest_result: "failed",
          latest_event_id: id(17),
          latest_sequence: 17,
          attempts: 1,
          passed: 0,
          failed: 1,
          summary: "Latest recorded ruff result: FAILED (event #17).",
          finding_evidence: "observed",
        },
      ],
      total: 2,
      truncated: false,
    },
    incomplete_commands: {
      items: [
        {
          command_id: "cmd-5",
          started_event_id: id(19),
          started_sequence: 19,
          executable: "npm",
          working_directory: ".",
          command_summary: "Command started: npm run dev",
          summary: "Command started but no finish event was recorded.",
          evidence_event_ids: [id(19)],
          finding_evidence: "observed",
        },
      ],
      total: 1,
      truncated: false,
    },
    command_summary: {
      commands_started: 5,
      commands_finished: 4,
      incomplete_commands: 1,
      checks_passed: 1,
      checks_failed: 3,
      checks_ungrouped: 0,
      check_signatures: 2,
    },
    file_change_summary: {
      description:
        "File events observed during this recording (not necessarily the current file system).",
      created_events: 1,
      modified_events: 1,
      deleted_events: 1,
      total_events: 3,
      unique_paths: 3,
      paths: {
        items: [
          {
            path: "src/app.py",
            change_events: 1,
            first_change: "modified",
            last_change: "modified",
            last_sequence: 6,
            last_event_id: id(6),
            present_at_last_observation: true,
          },
          {
            path: "src/helpers.py",
            change_events: 1,
            first_change: "created",
            last_change: "created",
            last_sequence: 10,
            last_event_id: id(10),
            present_at_last_observation: true,
          },
          {
            path: "src/legacy.py",
            change_events: 1,
            first_change: "deleted",
            last_change: "deleted",
            last_sequence: 11,
            last_event_id: id(11),
            present_at_last_observation: false,
          },
        ],
        total: 3,
        truncated: false,
      },
    },
    latest_git_state: {
      description: "Last recorded Git state (not the current Git state).",
      event_id: id(20),
      sequence: 20,
      snapshot_count: 2,
      repository: true,
      unavailable_reason: null,
      branch: "main",
      detached: false,
      unborn: false,
      head: "1a2b3c4",
      state: "dirty",
      staged: 0,
      unstaged: 2,
      untracked: 1,
      conflicted: 0,
      upstream: "origin/main",
      ahead: 1,
      behind: 0,
      summary: "Git: main @ 1a2b3c4, dirty (2 unstaged, 1 untracked)",
    },
    errors_observed: {
      items: [
        {
          sequence: 18,
          event_id: id(18),
          source: "filesystem",
          operation: "read",
          error: "PermissionError",
          path: "logs/locked.txt",
          summary: "Scanner could not read logs/locked.txt",
        },
      ],
      total: 1,
      truncated: false,
    },
    warnings: {
      items: [
        {
          code: "check_contradicts_command_finished",
          severity: "warning",
          message: "check.failed (#9) contradicts command.finished (#8); command cmd-x is excluded from findings",
          event_ids: [id(8), id(9)],
        },
        {
          code: "no_session_stopped_event",
          severity: "info",
          message: "The session metadata says 'stopped' but no session.stopped event was recorded.",
          event_ids: [],
        },
      ],
      total: 2,
      truncated: false,
    },
  };
}

/** A report with no findings for a session whose timeline is `items`. */
export function plainReport(summary: SessionSummary, items: TimelineItem[]): ForensicReport {
  if (!summary.readable) throw new Error("unreadable sessions have no report");
  return {
    schema_version: 1,
    session_id: summary.session_id,
    session_status: summary.status,
    recording_state: summary.recording_state,
    analysis_complete: summary.analysis_complete,
    started_at: summary.started_at,
    ended_at: summary.ended_at,
    event_count: items.length,
    event_counts: counts(items),
    finding_evidence: "observed",
    timeline: { items, total: items.length, truncated: false },
    first_observable_failure: null,
    observable_failures: { items: [], total: 0, truncated: false },
    recoveries: { items: [], total: 0, truncated: false },
    unresolved_failures: { items: [], total: 0, truncated: false },
    final_check_states: { items: [], total: 0, truncated: false },
    incomplete_commands: { items: [], total: 0, truncated: false },
    command_summary: {
      commands_started: 0,
      commands_finished: 0,
      incomplete_commands: 0,
      checks_passed: 0,
      checks_failed: 0,
      checks_ungrouped: 0,
      check_signatures: 0,
    },
    file_change_summary: {
      description:
        "File events observed during this recording (not necessarily the current file system).",
      created_events: 0,
      modified_events: 0,
      deleted_events: 0,
      total_events: 0,
      unique_paths: 0,
      paths: { items: [], total: 0, truncated: false },
    },
    latest_git_state: null,
    errors_observed: { items: [], total: 0, truncated: false },
    warnings: { items: [], total: 0, truncated: false },
  };
}

/** `n` file events after a session start, for pagination and windowing. */
export function longTimeline(n: number, salt = "b"): TimelineItem[] {
  const items = [item(1, "session.started", "Recording started", { salt })];
  for (let s = 2; s <= n; s += 1) {
    items.push(item(s, "file.modified", `Modified src/module_${s}.py`, { path: `src/module_${s}.py`, salt }));
  }
  return items;
}
