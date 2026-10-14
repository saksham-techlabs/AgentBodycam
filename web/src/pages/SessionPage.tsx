import type { ReactNode } from "react";

import { ApiError } from "../api/errors";
import type { SessionDetail } from "../api/types";
import { useClient } from "../app/ClientContext";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorBanner, ErrorState, LoadingState } from "../components/States";
import { TopBar } from "../components/TopBar";
import { usePolling } from "../hooks/usePolling";
import { useResource } from "../hooks/useResource";
import { formatCount, formatDuration, formatUtc, plural } from "../lib/format";
import { presentation, visualState, type Tone } from "../lib/sessionState";

/** Replay views planned after this foundation; shown, but not available yet. */
const FUTURE_VIEWS = ["Timeline", "Findings", "Files", "Git", "Final state"];

/** Errors after which stale data must not stay on screen: the session itself is gone. */
function isFatal(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.kind === "not_found" || error.kind === "bad_request" || error.kind === "server")
  );
}

export function SessionPage({ sessionId, onBack }: { sessionId: string; onBack: () => void }) {
  const client = useClient();
  const detail = useResource(`session:${sessionId}`, (signal) =>
    client.session(sessionId, signal),
  );
  const data = detail.data;
  const live = data?.recording_state === "recording";
  usePolling(detail.reload, live && detail.error === null);

  const backButton = (
    <button type="button" className="button" onClick={onBack}>
      Back to sessions
    </button>
  );
  const fatal = detail.error !== null && isFatal(detail.error);

  return (
    <>
      <TopBar
        title={
          <nav className="crumbs" aria-label="Breadcrumb">
            <a
              href="/"
              onClick={(event) => {
                event.preventDefault();
                onBack();
              }}
            >
              Sessions
            </a>
            <span aria-hidden="true">/</span>
            <span className="crumb-current mono" aria-current="page">
              {sessionId}
            </span>
          </nav>
        }
        meta={
          data === null || fatal ? null : (
            <>
              <StatusBadge state={visualState(data.recording_state)} />
              <span className="topbar-note mono">{plural(data.event_count, "event")}</span>
            </>
          )
        }
        onRefresh={() => void detail.reload()}
        refreshing={detail.refreshing}
        updatedAt={fatal ? null : detail.updatedAt}
        autoRefresh={live && detail.error === null}
      />
      <div className="page">
        {detail.loading ? (
          <LoadingState label="Loading session…" rows={3} />
        ) : data === null || fatal ? (
          <ErrorState error={detail.error} action={backButton} />
        ) : (
          <>
            {detail.error === null ? null : <ErrorBanner error={detail.error} />}
            <Overview detail={data} />
          </>
        )}
      </div>
    </>
  );
}

function Overview({ detail }: { detail: SessionDetail }) {
  const state = visualState(detail.recording_state);
  const { description } = presentation(state);
  const duration = formatDuration(detail.started_at, detail.ended_at);
  return (
    <>
      <section className="hero" aria-labelledby="session-title">
        <p className="eyebrow">Session</p>
        <h1 id="session-title" className="mono hero-id">
          {detail.session_id}
        </h1>
        <div className="hero-state">
          <StatusBadge state={state} />
          <p>{description}</p>
        </div>
        <dl className="facts">
          <Fact label="Started" value={formatUtc(detail.started_at)} mono />
          <Fact
            label="Ended"
            value={
              detail.ended_at !== null
                ? formatUtc(detail.ended_at)
                : state === "live"
                  ? "Live: still recording"
                  : "No end recorded"
            }
            mono={detail.ended_at !== null}
          />
          <Fact label="Duration" value={duration ?? "—"} mono />
          <Fact label="Recorded status" value={detail.status} mono />
          <Fact
            label="Analysis"
            value={detail.analysis_complete ? "Complete" : "Partial snapshot"}
          />
        </dl>
      </section>

      <div className="tabs" role="tablist" aria-label="Replay views">
        <button type="button" role="tab" aria-selected="true" className="tab is-active">
          Overview
        </button>
        {FUTURE_VIEWS.map((view) => (
          <button
            key={view}
            type="button"
            role="tab"
            aria-selected="false"
            className="tab"
            disabled
            title="In development"
          >
            {view}
          </button>
        ))}
        <span className="tabs-note">Detailed replay views are in development.</span>
      </div>

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
          hint="contradictory evidence excluded from findings"
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

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd className={mono === true ? "mono" : undefined}>{value}</dd>
    </div>
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
