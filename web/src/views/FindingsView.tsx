import type { ReactNode } from "react";

import type {
  ErrorObserved,
  FailureFinding,
  FailureType,
  ForensicReport,
  ForensicWarning,
  IncompleteCommand,
  Recovery,
  UnresolvedFailure,
} from "../api/types";
import {
  Chip,
  JumpButton,
  ObservedBadge,
  SectionHead,
  SeqButton,
  TruncationNote,
  type JumpHandler,
} from "../components/Evidence";
import { formatCount, plural } from "../lib/format";
import { recoveryLine } from "../lib/timeline";
import { ChangeWindowView } from "./ChangeWindowView";

const FAILURE_LABEL: Record<FailureType, string> = {
  check_failed: "CHECK FAILED",
  command_spawn_error: "COMMAND COULD NOT START",
  command_signaled: "COMMAND SIGNALED",
  command_interrupted: "COMMAND INTERRUPTED",
};

/**
 * The report's findings, grouped as the evidence model defines them. Every finding is an
 * observed statement about recorded events and their order; references open the timeline.
 */
export function FindingsView({
  report,
  onJump,
  live,
}: {
  report: ForensicReport;
  onJump: JumpHandler;
  live: boolean;
}) {
  return (
    <div className="findings">
      {live ? (
        <div className="banner tone-info" role="status">
          Live recording: findings cover the evidence recorded so far and refresh automatically.
        </div>
      ) : null}
      <FirstFailure report={report} onJump={onJump} />
      <Recoveries report={report} onJump={onJump} />
      <Unresolved report={report} onJump={onJump} />
      <Incomplete report={report} onJump={onJump} />
      <Errors report={report} onJump={onJump} />
      <Warnings report={report} onJump={onJump} />
      <p className="footnote">
        Findings state what was recorded and in which order. They do not say why anything
        happened.
      </p>
    </div>
  );
}

function FirstFailure({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const first = report.first_observable_failure;
  const others = report.observable_failures;
  return (
    <section className="finding-section" aria-labelledby="ff-title">
      <SectionHead id="ff-title" title="First observable failure" />
      {first === null ? (
        <p className="finding-empty">No observable check failure was recorded.</p>
      ) : (
        <article className="finding tone-danger" data-testid="first-failure">
          <FindingHead tone="danger" label="FIRST OBSERVABLE FAILURE">
            <SeqButton sequence={first.sequence} eventId={first.event_id} onJump={onJump} />
          </FindingHead>
          <p className="finding-summary">{first.summary}</p>
          <FailureFacts failure={first} />
          <div className="finding-actions">
            <JumpButton eventRef={{ eventId: first.event_id, sequence: first.sequence }} onJump={onJump} />
          </div>
        </article>
      )}
      {others.total > 1 ? (
        <details className="failure-list">
          <summary>All observable failures ({formatCount(others.total)})</summary>
          <ul>
            {others.items.map((failure) => (
              <li key={failure.event_id}>
                <SeqButton sequence={failure.sequence} eventId={failure.event_id} onJump={onJump} />
                <Chip tone="danger">{FAILURE_LABEL[failure.failure_type]}</Chip>
                <span className="clip">{failure.summary}</span>
              </li>
            ))}
          </ul>
          <TruncationNote shown={others.items.length} total={others.total} noun="failures" />
        </details>
      ) : null}
    </section>
  );
}

function FailureFacts({ failure }: { failure: FailureFinding }) {
  return (
    <dl className="finding-facts">
      <Fact label="Type" value={FAILURE_LABEL[failure.failure_type]} />
      {failure.check_name === null ? null : <Fact label="Check" value={failure.check_name} mono />}
      {failure.check_kind === null ? null : <Fact label="Kind" value={failure.check_kind} mono />}
      {failure.exit_code === null ? null : (
        <Fact label="Exit code" value={String(failure.exit_code)} mono />
      )}
      {failure.reason === null ? null : <Fact label="Reason" value={failure.reason} mono />}
      <Fact label="Evidence events" value={formatCount(failure.evidence_event_ids.length)} mono />
    </dl>
  );
}

function Recoveries({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.recoveries;
  return (
    <section className="finding-section" aria-labelledby="rec-title">
      <SectionHead id="rec-title" title="Observed recoveries" count={total} />
      {items.length === 0 ? (
        <p className="finding-empty">
          No check was recorded failing and then passing on a later run of the same check.
        </p>
      ) : (
        items.map((r) => <RecoveryCard key={r.recovered_event_id} recovery={r} onJump={onJump} />)
      )}
      <TruncationNote shown={items.length} total={total} noun="recoveries" />
    </section>
  );
}

function RecoveryCard({ recovery: r, onJump }: { recovery: Recovery; onJump: JumpHandler }) {
  const sameRun = r.first_failed_sequence === r.last_failed_sequence;
  return (
    <article className="finding tone-success" data-testid="recovery">
      <FindingHead tone="success" label="OBSERVED RECOVERY">
        <span className="finding-name mono">{r.display_name}</span>
      </FindingHead>
      <p className="finding-summary">{recoveryLine(r.display_name, r.failed_attempts)}.</p>
      <p className="finding-backend">{r.summary}</p>
      <ol className="sequence-path" aria-label="Recorded order">
        <PathStep label="First failed" sequence={r.first_failed_sequence} eventId={r.first_failed_event_id} tone="danger" onJump={onJump} />
        {sameRun ? null : (
          <PathStep label="Last failed" sequence={r.last_failed_sequence} eventId={r.last_failed_event_id} tone="danger" onJump={onJump} />
        )}
        <PathStep label="Later passed" sequence={r.recovered_sequence} eventId={r.recovered_event_id} tone="success" onJump={onJump} />
      </ol>
      <PreviousPass period={r} onJump={onJump} />
      <div className="windows">
        {r.changes_before_failure === null ? null : (
          <ChangeWindowView title="Before the first failure" window={r.changes_before_failure} onJump={onJump} />
        )}
        <ChangeWindowView
          title="Observed between failure and later pass"
          window={r.changes_between_failure_and_pass}
          onJump={onJump}
        />
        {sameRun ? null : (
          <ChangeWindowView
            title="Observed between last failure and later pass"
            window={r.changes_after_last_failure}
            onJump={onJump}
          />
        )}
      </div>
    </article>
  );
}

function Unresolved({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.unresolved_failures;
  return (
    <section className="finding-section" aria-labelledby="unr-title">
      <SectionHead id="unr-title" title="Unresolved in this recording" count={total} />
      {items.length === 0 ? (
        <p className="finding-empty">
          No check's latest recorded result was a failure.
        </p>
      ) : (
        items.map((u) => <UnresolvedCard key={u.last_failed_event_id} failure={u} onJump={onJump} />)
      )}
      <TruncationNote shown={items.length} total={total} noun="unresolved failures" />
    </section>
  );
}

function UnresolvedCard({ failure: u, onJump }: { failure: UnresolvedFailure; onJump: JumpHandler }) {
  const sameRun = u.first_failed_sequence === u.last_failed_sequence;
  return (
    <article className="finding tone-danger" data-testid="unresolved">
      <FindingHead tone="danger" label="UNRESOLVED IN THIS RECORDING">
        <span className="finding-name mono">{u.display_name}</span>
      </FindingHead>
      <p className="finding-summary">{u.summary}</p>
      <p className="finding-backend">
        Recorded-at-end: no later passing run of this check was recorded before the evidence
        ends. This describes the recording, not the project as it is now.
      </p>
      <ol className="sequence-path" aria-label="Recorded order">
        {sameRun ? null : (
          <PathStep label="First failed" sequence={u.first_failed_sequence} eventId={u.first_failed_event_id} tone="danger" onJump={onJump} />
        )}
        <PathStep label="Latest failed" sequence={u.last_failed_sequence} eventId={u.last_failed_event_id} tone="danger" onJump={onJump} />
        <li className="path-step tone-muted">
          <span className="path-label">End of recorded evidence</span>
        </li>
      </ol>
      <p className="finding-meta">
        {plural(u.failed_attempts, "failed recorded attempt")}
      </p>
      <PreviousPass period={u} onJump={onJump} />
      <div className="windows">
        {u.changes_before_failure === null ? null : (
          <ChangeWindowView title="Before the first failure" window={u.changes_before_failure} onJump={onJump} />
        )}
        <ChangeWindowView
          title="Observed after the latest failure"
          window={u.changes_after_last_failure}
          onJump={onJump}
        />
      </div>
    </article>
  );
}

function PreviousPass({
  period,
  onJump,
}: {
  period: Recovery | UnresolvedFailure;
  onJump: JumpHandler;
}) {
  if (period.previous_pass_event_id !== null && period.previous_pass_sequence !== null) {
    return (
      <p className="finding-meta">
        Previous passing run of this check:{" "}
        <SeqButton
          sequence={period.previous_pass_sequence}
          eventId={period.previous_pass_event_id}
          onJump={onJump}
          label="Previous pass"
        />
      </p>
    );
  }
  return period.previous_pass_note === null ? null : (
    <p className="finding-meta">{period.previous_pass_note}</p>
  );
}

function PathStep({
  label,
  sequence,
  eventId,
  tone,
  onJump,
}: {
  label: string;
  sequence: number;
  eventId: string;
  tone: "danger" | "success";
  onJump: JumpHandler;
}) {
  return (
    <li className={`path-step tone-${tone}`}>
      <span className="path-label">{label}</span>
      <SeqButton sequence={sequence} eventId={eventId} onJump={onJump} label={label} />
    </li>
  );
}

function Incomplete({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.incomplete_commands;
  return (
    <section className="finding-section" aria-labelledby="inc-title">
      <SectionHead id="inc-title" title="Incomplete commands" count={total} />
      {items.length === 0 ? (
        <p className="finding-empty">Every recorded command start has a recorded finish.</p>
      ) : (
        <ul className="finding-rows">
          {items.map((c) => (
            <IncompleteRow key={c.command_id} command={c} onJump={onJump} />
          ))}
        </ul>
      )}
      <TruncationNote shown={items.length} total={total} noun="incomplete commands" />
    </section>
  );
}

function IncompleteRow({ command: c, onJump }: { command: IncompleteCommand; onJump: JumpHandler }) {
  return (
    <li className="finding-row tone-warning" data-testid="incomplete">
      <div className="finding-row-head">
        <Chip tone="warning">INCOMPLETE</Chip>
        <SeqButton sequence={c.started_sequence} eventId={c.started_event_id} onJump={onJump} label="Command start" />
        <span className="mono clip">{c.command_summary}</span>
        <ObservedBadge />
      </div>
      <p className="finding-meta">{c.summary}</p>
      {c.working_directory === null ? null : (
        <p className="finding-meta">
          Working directory <span className="mono">{c.working_directory}</span>
        </p>
      )}
    </li>
  );
}

function Errors({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.errors_observed;
  return (
    <section className="finding-section" aria-labelledby="err-title">
      <SectionHead id="err-title" title="Errors observed" count={total} />
      {items.length === 0 ? (
        <p className="finding-empty">No error events were recorded.</p>
      ) : (
        <ul className="finding-rows">
          {items.map((e) => (
            <ErrorRow key={e.event_id} error={e} onJump={onJump} />
          ))}
        </ul>
      )}
      <TruncationNote shown={items.length} total={total} noun="errors" />
    </section>
  );
}

function ErrorRow({ error: e, onJump }: { error: ErrorObserved; onJump: JumpHandler }) {
  return (
    <li className="finding-row tone-warning" data-testid="error-observed">
      <div className="finding-row-head">
        <Chip tone="warning">ERROR</Chip>
        <SeqButton sequence={e.sequence} eventId={e.event_id} onJump={onJump} />
        <span className="clip">{e.summary}</span>
        <ObservedBadge />
      </div>
      <dl className="finding-facts">
        <Fact label="Source" value={e.source} mono />
        {e.operation === null ? null : <Fact label="Operation" value={e.operation} mono />}
        {e.error === null ? null : <Fact label="Error" value={e.error} mono />}
        {e.path === null ? null : <Fact label="Path" value={e.path} mono />}
      </dl>
    </li>
  );
}

function Warnings({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.warnings;
  return (
    <section className="finding-section" aria-labelledby="warn-title">
      <SectionHead id="warn-title" title="Evidence notes and warnings" count={total} />
      {items.length === 0 ? (
        <p className="finding-empty">No evidence notes.</p>
      ) : (
        <ul className="finding-rows">
          {items.map((w, i) => (
            <WarningRow key={`${w.code}-${i}`} warning={w} onJump={onJump} />
          ))}
        </ul>
      )}
      <TruncationNote shown={items.length} total={total} noun="notes" />
    </section>
  );
}

function WarningRow({ warning: w, onJump }: { warning: ForensicWarning; onJump: JumpHandler }) {
  const tone = w.severity === "warning" ? "warning" : "info";
  return (
    <li className={`finding-row tone-${tone}`} data-testid="warning">
      <div className="finding-row-head">
        <Chip tone={tone}>{w.severity === "warning" ? "WARNING" : "NOTE"}</Chip>
        <span className="mono dim">{w.code}</span>
      </div>
      <p className="finding-meta">{w.message}</p>
      {w.event_ids.length === 0 ? null : (
        <div className="warning-refs">
          {w.event_ids.map((id) => (
            <button
              key={id}
              type="button"
              className="seq-link mono"
              onClick={() => onJump({ eventId: id, sequence: null })}
              aria-label={`Event ${id}: show in timeline`}
            >
              {id.slice(0, 12)}…
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

function FindingHead({
  tone,
  label,
  children,
}: {
  tone: "danger" | "success";
  label: string;
  children?: ReactNode;
}) {
  return (
    <div className="finding-head">
      <Chip tone={tone}>{label}</Chip>
      {children}
      <ObservedBadge />
    </div>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono === true ? "mono" : undefined}>{value}</dd>
    </div>
  );
}