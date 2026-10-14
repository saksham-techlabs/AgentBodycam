import { useCallback } from "react";

import type { SessionSummary } from "../api/types";
import { useClient } from "../app/ClientContext";
import { sessionPath } from "../app/router";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorBanner, ErrorState, LoadingState } from "../components/States";
import { TopBar } from "../components/TopBar";
import { usePolling } from "../hooks/usePolling";
import { useResource } from "../hooks/useResource";
import { formatCount, formatDuration, formatUtc, plural } from "../lib/format";
import { presentation, summaryState, type VisualState } from "../lib/sessionState";

const SUMMARY_ORDER: VisualState[] = [
  "live",
  "stopped",
  "ended_without_stop",
  "unknown",
  "not_started",
  "unreadable",
];

export function SessionsPage({ onOpen }: { onOpen: (path: string) => void }) {
  const client = useClient();
  const sessions = useResource("sessions", (signal) => client.sessions(signal));
  const project = useResource("project", (signal) => client.project(signal));
  const reloadSessions = sessions.reload;
  const reloadProject = project.reload;
  const refresh = useCallback(
    () => Promise.all([reloadSessions(), reloadProject()]),
    [reloadSessions, reloadProject],
  );

  const list = sessions.data?.sessions ?? [];
  const anyLive = list.some((s) => s.readable && s.recording_state === "recording");
  usePolling(refresh, anyLive && sessions.error === null);

  return (
    <>
      <TopBar
        title={<span className="crumb-current">Sessions</span>}
        onRefresh={() => void refresh()}
        refreshing={sessions.refreshing || project.refreshing}
        updatedAt={sessions.updatedAt}
        autoRefresh={anyLive && sessions.error === null}
      />
      <div className="page">
        <div className="page-head">
          <div>
            <p className="eyebrow">Project</p>
            <h1>Recorded sessions</h1>
          </div>
          {project.data === null ? null : (
            <p className="page-head-meta" data-testid="project-summary">
              {plural(project.data.session_count, "recorded session")}
            </p>
          )}
        </div>
        {sessions.loading ? (
          <LoadingState label="Loading sessions…" />
        ) : sessions.data === null ? (
          <ErrorState
            error={sessions.error}
            action={
              <button type="button" className="button" onClick={() => void refresh()}>
                Try again
              </button>
            }
          />
        ) : list.length === 0 ? (
          <EmptyState title="No recorded sessions yet">
            <p>
              Start a recording in this project with <code>agentbodycam record</code>, then
              refresh. Sessions appear here newest first.
            </p>
          </EmptyState>
        ) : (
          <>
            {sessions.error === null ? null : <ErrorBanner error={sessions.error} />}
            <StateSummary sessions={list} />
            <SessionList sessions={list} onOpen={onOpen} />
          </>
        )}
      </div>
    </>
  );
}

function StateSummary({ sessions }: { sessions: SessionSummary[] }) {
  const counts = new Map<VisualState, number>();
  for (const session of sessions) {
    const state = summaryState(session);
    counts.set(state, (counts.get(state) ?? 0) + 1);
  }
  return (
    <ul className="state-summary" aria-label="Sessions by state">
      {SUMMARY_ORDER.filter((state) => counts.has(state)).map((state) => (
        <li key={state} className={`tone-${presentation(state).tone}`}>
          <span className="badge-dot" aria-hidden="true" />
          <span className="mono">{formatCount(counts.get(state) ?? 0)}</span>{" "}
          {presentation(state).label.toLowerCase()}
        </li>
      ))}
    </ul>
  );
}

function SessionList({
  sessions,
  onOpen,
}: {
  sessions: SessionSummary[];
  onOpen: (path: string) => void;
}) {
  return (
    <div className="session-table">
      <div className="session-row session-row-head" aria-hidden="true">
        <span>State</span>
        <span>Session</span>
        <span>Started</span>
        <span>Duration</span>
        <span className="num">Events</span>
        <span>Analysis</span>
      </div>
      <ul className="session-list" aria-label="Sessions">
        {sessions.map((session) => (
          <li key={session.session_id}>
            <SessionRow session={session} onOpen={onOpen} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function SessionRow({
  session,
  onOpen,
}: {
  session: SessionSummary;
  onOpen: (path: string) => void;
}) {
  const state = summaryState(session);
  if (!session.readable) {
    return (
      <button
        type="button"
        className="session-row is-disabled"
        disabled
        aria-disabled="true"
        data-testid={`session-${session.session_id}`}
        title={presentation(state).description}
      >
        <span>
          <StatusBadge state={state} />
        </span>
        <span className="mono session-id">{session.session_id}</span>
        <span className="session-unreadable-note">
          Stored evidence could not be read. This session cannot be opened.
        </span>
      </button>
    );
  }
  const duration = formatDuration(session.started_at, session.ended_at);
  return (
    <button
      type="button"
      className="session-row"
      onClick={() => onOpen(sessionPath(session.session_id))}
      data-testid={`session-${session.session_id}`}
      aria-label={`Open session ${session.session_id} (${presentation(state).label})`}
    >
      <span>
        <StatusBadge state={state} />
      </span>
      <span className="mono session-id">{session.session_id}</span>
      <span className="mono dim" data-label="Started">
        {formatUtc(session.started_at)}
      </span>
      <span className="mono dim" data-label="Duration">
        {duration ?? (state === "live" ? "recording…" : "—")}
      </span>
      <span className="mono num" data-label="Events">
        {formatCount(session.event_count)}
      </span>
      <span data-label="Analysis">
        {session.analysis_complete ? (
          <span className="analysis">Complete</span>
        ) : (
          <span className="analysis tone-warning">Partial</span>
        )}
      </span>
    </button>
  );
}
