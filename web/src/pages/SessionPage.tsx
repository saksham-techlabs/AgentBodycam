import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { ApiError } from "../api/errors";
import type { ForensicReport, SessionDetail, TimelineItem } from "../api/types";
import { useClient } from "../app/ClientContext";
import type { EventRef } from "../components/Evidence";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorBanner, ErrorState, LoadingState } from "../components/States";
import { TopBar } from "../components/TopBar";
import { usePlayback, type MoveCause, type SeekTarget } from "../hooks/usePlayback";
import { usePolling } from "../hooks/usePolling";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { useResource, type Resource } from "../hooks/useResource";
import { useTimeline } from "../hooks/useTimeline";
import { formatCount, formatDuration, formatUtc, plural } from "../lib/format";
import { buildImportant } from "../lib/playback";
import { presentation, visualState } from "../lib/sessionState";
import {
  buildMarkers,
  lastSequence,
  matches,
  normalizeQuery,
  type TimelineFilter,
} from "../lib/timeline";
import { FilesView } from "../views/FilesView";
import { FindingsView } from "../views/FindingsView";
import { GitStateView } from "../views/GitStateView";
import { OverviewView } from "../views/OverviewView";
import { PlaybackBar } from "../views/PlaybackBar";
import { TimelineView, type FocusRequest, type JumpState } from "../views/TimelineView";

type TabId = "overview" | "timeline" | "findings" | "files" | "final";

const TABS: readonly { id: TabId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "timeline", label: "Timeline" },
  { id: "findings", label: "Findings" },
  { id: "files", label: "Files" },
  { id: "final", label: "Git / Final State" },
];

const IDLE: JumpState = { status: "idle" };

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
  const report = useResource(`report:${sessionId}`, (signal) => client.report(sessionId, signal));
  const timeline = useTimeline(client, sessionId);
  const markers = useMemo(() => buildMarkers(report.data), [report.data]);

  const [tab, setTab] = useState<TabId>("overview");
  const [filter, setFilter] = useState<TimelineFilter>("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const [jump, setJump] = useState<JumpState>(IDLE);

  const data = detail.data;
  const live = data?.recording_state === "recording";
  const { reload: reloadDetail } = detail;
  const { reload: reloadReport } = report;
  const { poll: pollTimeline, start: startTimeline } = timeline;

  const refreshAll = useCallback(
    () => Promise.all([reloadDetail(), reloadReport(), pollTimeline()]).then(() => undefined),
    [reloadDetail, reloadReport, pollTimeline],
  );
  usePolling(refreshAll, live && detail.error === null);

  // A recording that just stopped gets one last report and timeline read, so its final
  // events and findings are shown even if they landed after the last live poll.
  const wasLive = useRef(false);
  useEffect(() => {
    if (wasLive.current && !live) {
      void reloadReport();
      void pollTimeline();
    }
    wasLive.current = live;
  }, [live, reloadReport, pollTimeline]);

  useEffect(() => {
    if (tab === "timeline") startTimeline();
  }, [tab, startTimeline]);

  const reducedMotion = useReducedMotion();
  const nonce = useRef(0);
  // The user closed the detail panel: automatic playback steps leave it closed.
  const detailDismissed = useRef(false);

  // Latest values for handlers that outlive renders while pages load or playback runs.
  const latest = useRef({ timeline, filter, query, markers });
  latest.current = { timeline, filter, query, markers };

  /** Every playback move selects the event and brings it into view. */
  const onMove = useCallback((item: TimelineItem, cause: MoveCause) => {
    const { filter: f, query: q, markers: m } = latest.current;
    if (!matches(item, f, normalizeQuery(q), m.failures)) {
      setFilter("all");
      setQuery("");
    }
    const automatic = cause === "tick" || cause === "follow";
    if (!automatic) detailDismissed.current = false;
    if (!detailDismissed.current) setSelectedId(item.event_id);
    nonce.current += 1;
    setFocusRequest({
      eventId: item.event_id,
      nonce: nonce.current,
      mode: cause === "jump" || cause === "seek" ? "jump" : "follow",
      focus: cause === "jump",
    });
  }, []);

  const playback = usePlayback({ timeline, live, onMove });
  const playbackRef = useRef(playback);
  playbackRef.current = playback;
  const important = useMemo(
    () => buildImportant(report.data, timeline.data.items),
    [report.data, timeline.data.items],
  );

  const seekTo = useCallback(async (target: SeekTarget, cause: MoveCause) => {
    const data = latest.current.timeline.data;
    const loaded =
      target.eventId !== null ? data.index.has(target.eventId) : lastSequence(data) >= target.sequence;
    setJump(loaded ? IDLE : { status: "loading", sequence: target.sequence });
    const result = await playbackRef.current.seek(target, cause);
    if (result === "cancelled") return;
    if (result === "reached" || result === "error") setJump(IDLE);
    else setJump({ status: result === "cap" ? "cap" : "absent", sequence: target.sequence });
  }, []);

  const jumpTo = useCallback(
    async (ref: EventRef) => {
      setTab("timeline");
      const data = latest.current.timeline.data;
      const position = data.index.get(ref.eventId);
      const sequence = ref.sequence ?? (position === undefined ? null : data.items[position]?.sequence);
      if (sequence === null || sequence === undefined) {
        playbackRef.current.pause();
        latest.current.timeline.start();
        setJump({ status: "unloaded" });
        return;
      }
      await seekTo({ sequence, eventId: ref.eventId }, "jump");
    },
    [seekTo],
  );
  const onJump = useCallback((ref: EventRef) => void jumpTo(ref), [jumpTo]);
  const onSeek = useCallback((target: SeekTarget) => void seekTo(target, "seek"), [seekTo]);

  const onSelect = useCallback((eventId: string | null) => {
    setSelectedId(eventId);
    detailDismissed.current = eventId === null;
    if (eventId !== null) playbackRef.current.select(eventId);
  }, []);

  const cancelJump = useCallback(() => {
    latest.current.timeline.cancelLoadUntil();
    setJump(IDLE);
  }, []);

  const chooseTab = (id: TabId): void => {
    if (id !== "timeline") playback.pause();
    setTab(id);
    setFocusRequest(null);
  };

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
        onRefresh={() => void refreshAll()}
        refreshing={detail.refreshing || report.refreshing}
        updatedAt={fatal ? null : detail.updatedAt}
        autoRefresh={live && detail.error === null}
      />
      <div className="page page-wide">
        {detail.loading ? (
          <LoadingState label="Loading session…" rows={3} />
        ) : data === null || fatal ? (
          <ErrorState error={detail.error} action={backButton} />
        ) : (
          <>
            {detail.error === null ? null : <ErrorBanner error={detail.error} />}
            <Hero detail={data} />
            <Tabs active={tab} onChoose={chooseTab} detail={data} report={report.data} />
            <div
              className="tab-panel"
              role="tabpanel"
              id={`panel-${tab}`}
              aria-labelledby={`tab-${tab}`}
            >
              {tab === "overview" ? (
                <OverviewView detail={data} />
              ) : tab === "timeline" ? (
                <div className="replay">
                  <PlaybackBar
                    playback={playback}
                    important={important}
                    markers={markers}
                    live={live}
                    pageError={timeline.error}
                    onSeek={onSeek}
                  />
                  <TimelineView
                    timeline={timeline}
                    markers={markers}
                    report={report.data}
                    reportError={report.error}
                    live={live}
                    filter={filter}
                    onFilter={setFilter}
                    query={query}
                    onQuery={setQuery}
                    selectedId={selectedId}
                    onSelect={onSelect}
                    currentId={playback.current?.event_id ?? null}
                    reducedMotion={reducedMotion}
                    focusRequest={focusRequest}
                    jump={jump}
                    onCancelJump={cancelJump}
                    onJump={onJump}
                  />
                </div>
              ) : (
                <ReportTab report={report}>
                  {(r) =>
                    tab === "findings" ? (
                      <FindingsView report={r} onJump={onJump} live={live} />
                    ) : tab === "files" ? (
                      <FilesView summary={r.file_change_summary} onJump={onJump} />
                    ) : (
                      <GitStateView report={r} onJump={onJump} />
                    )
                  }
                </ReportTab>
              )}
            </div>
          </>
        )}
      </div>
    </>
  );
}

function ReportTab({
  report,
  children,
}: {
  report: Resource<ForensicReport>;
  children: (report: ForensicReport) => ReactNode;
}) {
  if (report.loading) return <LoadingState label="Loading findings…" rows={3} />;
  if (report.data === null) return <ErrorState error={report.error} />;
  return (
    <>
      {report.error === null ? null : <ErrorBanner error={report.error} />}
      {children(report.data)}
    </>
  );
}

function Tabs({
  active,
  onChoose,
  detail,
  report,
}: {
  active: TabId;
  onChoose: (id: TabId) => void;
  detail: SessionDetail;
  report: ForensicReport | null;
}) {
  const refs = useRef(new Map<TabId, HTMLButtonElement>());
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const current = TABS.findIndex((t) => t.id === active);
    const moves: Record<string, number> = {
      ArrowRight: (current + 1) % TABS.length,
      ArrowLeft: (current - 1 + TABS.length) % TABS.length,
      Home: 0,
      End: TABS.length - 1,
    };
    const to = moves[event.key];
    const next = to === undefined ? undefined : TABS[to];
    if (next === undefined) return;
    event.preventDefault();
    onChoose(next.id);
    refs.current.get(next.id)?.focus();
  };
  const badges: Partial<Record<TabId, { text: string; tone: string }>> = {
    timeline: { text: formatCount(detail.event_count), tone: "muted" },
    findings: {
      text: formatCount(detail.observable_failure_count),
      tone: detail.observable_failure_count > 0 ? "danger" : "muted",
    },
  };
  if (report !== null) {
    badges.files = { text: formatCount(report.file_change_summary.unique_paths), tone: "muted" };
  }
  return (
    <div className="tabs" role="tablist" aria-label="Replay views" onKeyDown={onKeyDown}>
      {TABS.map((t) => {
        const selected = t.id === active;
        const badge = badges[t.id];
        return (
          <button
            key={t.id}
            ref={(el) => {
              if (el === null) refs.current.delete(t.id);
              else refs.current.set(t.id, el);
            }}
            id={`tab-${t.id}`}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={`panel-${t.id}`}
            tabIndex={selected ? 0 : -1}
            className={selected ? "tab is-active" : "tab"}
            onClick={() => onChoose(t.id)}
          >
            {t.label}
            {badge === undefined ? null : (
              <span className={`tab-badge mono tone-${badge.tone}`}>{badge.text}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function Hero({ detail }: { detail: SessionDetail }) {
  const state = visualState(detail.recording_state);
  const { description } = presentation(state);
  const duration = formatDuration(detail.started_at, detail.ended_at);
  return (
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
        <Fact label="Analysis" value={detail.analysis_complete ? "Complete" : "Partial snapshot"} />
      </dl>
    </section>
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
