import type { ReactNode } from "react";

import type { SessionDetail } from "../api/types";
import { formatCount } from "../lib/format";
import { visualState, type Tone } from "../lib/sessionState";

export function OverviewView({ detail }: { detail: SessionDetail }) {
  const state = visualState(detail.recording_state);
  return (
    <>
      {detail.analysis_complete ? null : (
        <div className="banner tone-info" role="note">
          {state === "live"
            ? "Live snapshot: these counts cover the evidence recorded so far and refresh automatically."
            : "Analysis is incomplete: these counts cover only the evidence that could be confirmed."}
        </div>
      )}

      <section className="metrics" aria-label="Session overview">
        <Metric label="Events" value={detail.event_count} tone="info" hint="recorded events" />
        <Metric
          label="Observable failures"
          value={detail.observable_failure_count}
          tone={detail.observable_failure_count > 0 ? "danger" : "muted"}
          hint="failed checks and commands that did not complete"
        />
        <Metric
          label="Recoveries"
          value={detail.recovery_count}
          tone={detail.recovery_count > 0 ? "success" : "muted"}
          hint="failures later followed by a pass of the same check"
        />
        <Metric
          label="Unresolved"
          value={detail.unresolved_failure_count}
          tone={detail.unresolved_failure_count > 0 ? "danger" : "muted"}
          hint="latest recorded result still failing in this recording"
        />
        <Metric
          label="Warnings"
          value={detail.warning_count}
          tone={detail.warning_count > 0 ? "warning" : "muted"}
          hint="evidence notes and contradictory evidence excluded from findings"
        />
      </section>

      <div className="panels">
        <EventCounts counts={detail.event_counts} total={detail.event_count} />
        <section className="panel" aria-labelledby="commands-title">
          <h2 id="commands-title" className="panel-title">
            Commands and checks
          </h2>
          <dl className="kv">
            <Kv label="Commands started" value={detail.command_summary.commands_started} />
            <Kv label="Commands finished" value={detail.command_summary.commands_finished} />
            <Kv
              label="Incomplete commands"
              value={detail.command_summary.incomplete_commands}
              tone={detail.command_summary.incomplete_commands > 0 ? "warning" : undefined}
            />
            <Kv
              label="Checks passed"
              value={detail.command_summary.checks_passed}
              tone={detail.command_summary.checks_passed > 0 ? "success" : undefined}
            />
            <Kv
              label="Checks failed"
              value={detail.command_summary.checks_failed}
              tone={detail.command_summary.checks_failed > 0 ? "danger" : undefined}
            />
          </dl>
        </section>
      </div>
      <p className="footnote">
        Counts describe what was observed and in which order. AgentBodycam does not infer
        why anything happened.
      </p>
    </>
  );
}

function Metric({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone: Tone;
  hint: string;
}) {
  return (
    <div className={`metric tone-${tone}`} data-testid={`metric-${label}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value mono">{formatCount(value)}</div>
      <div className="metric-hint">{hint}</div>
    </div>
  );
}

function Kv({ label, value, tone }: { label: string; value: number; tone?: Tone | undefined }) {
  return (
    <div className={tone === undefined ? "kv-row" : `kv-row tone-${tone}`}>
      <dt>{label}</dt>
      <dd className="mono">{formatCount(value)}</dd>
    </div>
  );
}

function EventCounts({ counts, total }: { counts: Record<string, number>; total: number }) {
  const entries = Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
  let body: ReactNode;
  if (entries.length === 0) {
    body = <p className="dim">No events recorded.</p>;
  } else {
    body = (
      <ul className="bars">
        {entries.map(([type, n]) => (
          <li key={type}>
            <span className="mono bars-label">{type}</span>
            <span className="bars-track" aria-hidden="true">
              <Bar fraction={total > 0 ? n / total : 0} />
            </span>
            <span className="mono num">{formatCount(n)}</span>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <section className="panel" aria-labelledby="events-title">
      <h2 id="events-title" className="panel-title">
        Recorded events by type
      </h2>
      {body}
    </section>
  );
}

/** React applies `style` through the CSSOM, which the UI's CSP allows (no inline markup). */
function Bar({ fraction }: { fraction: number }) {
  const percent = Math.max(2, Math.round(fraction * 100));
  return <span className="bars-fill" style={{ width: `${percent}%` }} />;
}
