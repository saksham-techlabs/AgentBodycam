/**
 * The privacy-safe export model. Everything a share card or JSON summary shows comes from
 * here, and this module copies only explicitly named fields out of the forensic report:
 * counts, event sequence numbers, recording state, check kinds/names and results.
 *
 * Free-text summaries are never copied: they can contain project-relative paths and
 * command lines. Labels are rebuilt from structured fields instead, and check names are
 * re-validated against a conservative allowlist (the backend already generates them from
 * fixed strings, never from argv).
 */

import type {
  CheckResult,
  FailureFinding,
  ForensicReport,
  RecordingState,
} from "../api/types";

export const MAX_LABEL_CHARS = 34;
export const MAX_CHECK_NAME_CHARS = 24;
export const MAX_FINAL_CHECKS = 12;
const MAX_COUNT = 999_999;

export type ShareVerdict = "pass" | "fail" | "partial" | "none";

export interface ShareMoment {
  sequence: number;
  label: string;
}

export interface ShareCheck {
  kind: string;
  name: string;
  result: CheckResult;
}

export interface ShareCardModel {
  recordingState: RecordingState;
  analysisComplete: boolean;
  totalEvents: number;
  changedFiles: number;
  fileChangeEvents: number;
  failedChecks: number;
  passedChecks: number;
  observableFailures: number;
  recoveries: number;
  unresolved: number;
  firstFailure: ShareMoment | null;
  recovery: ShareMoment | null;
  finalChecks: ShareCheck[];
  finalChecksTotal: number;
  finalPassing: number;
  finalFailing: number;
  verdict: ShareVerdict;
}

const KIND_LABELS: Record<string, string> = {
  test: "Test",
  build: "Build",
  lint: "Lint",
  typecheck: "Typecheck",
};

// Exactly the names the recorder generates for recognised tools (core/checks.py). An explicit
// `--check` of any other program is named after its file, which may be private: generic here.
const KNOWN_CHECK_NAMES: ReadonlySet<string> = new Set([
  "pytest",
  "unittest",
  "build",
  "mypy",
  "pyright",
  "ruff",
  "eslint",
  "tsc",
  ...["npm", "pnpm", "yarn"].flatMap((m) => ["test", "build", "lint", "typecheck"].map((s) => `${m} ${s}`)),
  ...["cargo", "go", "dotnet"].flatMap((t) => [`${t} test`, `${t} build`]),
]);

/** `text` cut to at most `max` characters, ending in an ellipsis when cut. */
export function bound(text: string, max: number): string {
  const chars = Array.from(text);
  return chars.length <= max ? text : `${chars.slice(0, max - 1).join("").trimEnd()}…`;
}

function count(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.min(Math.floor(value), MAX_COUNT) : 0;
}

function sequence(value: number): number {
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

/** The check kind if it is one of the known kinds, else null. */
export function safeKind(kind: string | null): string | null {
  return kind !== null && Object.hasOwn(KIND_LABELS, kind) ? kind : null;
}

/** The check name if it passes the allowlist, else null (shown generically). */
export function safeCheckName(name: string | null): string | null {
  return name !== null && KNOWN_CHECK_NAMES.has(name) ? name : null;
}

function checkLabel(kind: string | null, name: string | null, outcome: "failed" | "passed"): string {
  const k = safeKind(kind);
  const n = safeCheckName(name);
  const head = `${k === null ? "Check" : KIND_LABELS[k]} ${outcome}`;
  return bound(n === null ? head : `${head}: ${n}`, MAX_LABEL_CHARS);
}

function failureLabel(f: FailureFinding): string {
  switch (f.failure_type) {
    case "check_failed":
      return checkLabel(f.check_kind, f.check_name, "failed");
    case "command_spawn_error":
      return "Command could not start";
    case "command_signaled":
      return "Command ended by a signal";
    case "command_interrupted":
      return "Command interrupted";
    default:
      return "Observable failure";
  }
}

/** Builds the export model from named report fields only. */
export function buildShareModel(report: ForensicReport): ShareCardModel {
  const first = report.first_observable_failure;
  const recovered = report.recoveries.items.reduce<ForensicReport["recoveries"]["items"][number] | null>(
    (earliest, r) => (earliest === null || r.recovered_sequence < earliest.recovered_sequence ? r : earliest),
    null,
  );
  const states = report.final_check_states;
  const finalChecks = states.items.slice(0, MAX_FINAL_CHECKS).map((s) => ({
    kind: safeKind(s.kind) ?? "check",
    name: safeCheckName(s.name) ?? "check",
    result: s.latest_result === "passed" ? ("passed" as const) : ("failed" as const),
  }));
  const anyFailed =
    states.items.some((s) => s.latest_result !== "passed") || report.unresolved_failures.total > 0;
  const complete = !states.truncated && states.items.length === states.total;
  const verdict: ShareVerdict =
    states.total === 0 ? "none" : anyFailed ? "fail" : complete ? "pass" : "partial";

  return {
    recordingState: report.recording_state,
    analysisComplete: report.analysis_complete,
    totalEvents: count(report.event_count),
    changedFiles: count(report.file_change_summary.unique_paths),
    fileChangeEvents: count(report.file_change_summary.total_events),
    failedChecks: count(report.command_summary.checks_failed),
    passedChecks: count(report.command_summary.checks_passed),
    observableFailures: count(report.observable_failures.total),
    recoveries: count(report.recoveries.total),
    unresolved: count(report.unresolved_failures.total),
    firstFailure: first === null ? null : { sequence: sequence(first.sequence), label: failureLabel(first) },
    recovery:
      recovered === null
        ? null
        : {
            sequence: sequence(recovered.recovered_sequence),
            label: checkLabel(recovered.kind, recovered.name, "passed"),
          },
    finalChecks,
    finalChecksTotal: count(states.total),
    finalPassing: count(states.items.filter((s) => s.latest_result === "passed").length),
    finalFailing: count(states.items.filter((s) => s.latest_result !== "passed").length),
    verdict,
  };
}

// -- JSON summary ------------------------------------------------------------------------

export const SUMMARY_FORMAT = "agentbodycam-share-summary";
export const SUMMARY_VERSION = 1;

/** Every key the JSON summary may contain, in output order. */
export const SUMMARY_KEYS = [
  "format",
  "version",
  "recording_state",
  "analysis_complete",
  "events",
  "file_changes",
  "file_change_events",
  "failed_checks",
  "passed_checks",
  "observable_failures",
  "observed_recoveries",
  "unresolved_failures",
  "first_observable_failure",
  "first_observed_recovery",
  "final_check_verdict",
  "final_checks",
  "final_checks_total",
  "final_checks_passing",
  "final_checks_failing",
] as const;

export type ShareSummary = {
  format: typeof SUMMARY_FORMAT;
  version: typeof SUMMARY_VERSION;
  recording_state: RecordingState;
  analysis_complete: boolean;
  events: number;
  file_changes: number;
  file_change_events: number;
  failed_checks: number;
  passed_checks: number;
  observable_failures: number;
  observed_recoveries: number;
  unresolved_failures: number;
  first_observable_failure: { event: number; label: string } | null;
  first_observed_recovery: { event: number; label: string } | null;
  final_check_verdict: ShareVerdict;
  final_checks: { kind: string; name: string; result: CheckResult }[];
  final_checks_total: number;
  final_checks_passing: number;
  final_checks_failing: number;
};

const moment = (m: ShareMoment | null) => (m === null ? null : { event: m.sequence, label: m.label });

export function buildShareSummary(model: ShareCardModel): ShareSummary {
  return {
    format: SUMMARY_FORMAT,
    version: SUMMARY_VERSION,
    recording_state: model.recordingState,
    analysis_complete: model.analysisComplete,
    events: model.totalEvents,
    file_changes: model.changedFiles,
    file_change_events: model.fileChangeEvents,
    failed_checks: model.failedChecks,
    passed_checks: model.passedChecks,
    observable_failures: model.observableFailures,
    observed_recoveries: model.recoveries,
    unresolved_failures: model.unresolved,
    first_observable_failure: moment(model.firstFailure),
    first_observed_recovery: moment(model.recovery),
    final_check_verdict: model.verdict,
    final_checks: model.finalChecks.map((c) => ({ kind: c.kind, name: c.name, result: c.result })),
    final_checks_total: model.finalChecksTotal,
    final_checks_passing: model.finalPassing,
    final_checks_failing: model.finalFailing,
  };
}

export function serializeSummary(summary: ShareSummary): string {
  return `${JSON.stringify(summary, null, 2)}\n`;
}
