import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

import { Chip } from "../components/Evidence";
import type { Playback, SeekTarget } from "../hooks/usePlayback";
import { describeError } from "../lib/errorMessages";
import { formatCount, plural } from "../lib/format";
import {
  MOMENT_TONES,
  PLAYBACK_SPEEDS,
  adjacentImportant,
  formatSpeed,
  isOwnArrowControl,
  isOwnSpaceControl,
  isTextEntry,
  markerPercent,
  replayMoment,
  scrubberMarkers,
  type ImportantEvent,
  type ScrubberMarker,
} from "../lib/playback";
import { eventPresentation, formatClock, markersFor, type MarkerIndex } from "../lib/timeline";

interface PlaybackBarProps {
  playback: Playback;
  important: readonly ImportantEvent[];
  markers: MarkerIndex;
  live: boolean;
  /** Error of the latest timeline page request, shown when playback stalled on it. */
  pageError: unknown;
  reducedMotion: boolean;
  onSeek: (target: SeekTarget) => void;
}

/**
 * The replay theater: play/pause, step, important-event navigation, speed, a
 * sequence-based scrubber and the event currently being replayed, in one sticky block.
 * This replays recorded evidence events; there is no screen video.
 */
export function PlaybackBar(props: PlaybackBarProps) {
  const { playback, important, markers, live, pageError, reducedMotion, onSeek } = props;
  const { current, playing, ended, total } = playback;
  const sequence = current?.sequence ?? 0;
  const previous = adjacentImportant(important, sequence, -1);
  const next = adjacentImportant(important, current === null ? 0 : sequence, 1);

  const goImportant = (dir: 1 | -1): void => {
    const target = dir === 1 ? next : previous;
    if (target !== null) onSeek({ sequence: target.sequence, eventId: target.eventId });
  };

  usePlaybackShortcuts({
    toggle: playback.toggle,
    step: (dir) => void playback.step(dir),
    important: goImportant,
  });
  const ref = useRef<HTMLElement>(null);
  useTheaterHeight(ref);

  const empty = playback.loaded === 0;
  const playLabel = playing ? "Pause" : ended ? "Replay" : "Play";
  const moment = current === null ? null : replayMoment(current, markersFor(markers, current));
  const classes = ["playback", "replay-theater"];
  if (playing) classes.push("is-playing");
  if (playback.stalled) classes.push("has-alert");

  return (
    <section
      ref={ref}
      className={classes.join(" ")}
      aria-label="Replay controls"
      data-testid="playback"
      data-state={statusOf(playback).state}
      data-moment={moment ?? "none"}
      data-motion={reducedMotion ? "reduced" : "full"}
    >
      <div className="theater-head">
        <p className="theater-title">
          <span className="theater-mark" aria-hidden="true" />
          Replay
        </p>
        <p className="theater-sub">Recorded evidence events, one step per event, in sequence order</p>
        <ShortcutHint />
      </div>
      <div className="playback-row">
        <div className="playback-buttons">
          <button
            type="button"
            className="button play-button"
            onClick={playback.toggle}
            disabled={!playing && !playback.canPlay}
            aria-label={playing ? "Pause replay" : ended ? "Replay from the first event" : "Play replay"}
            data-testid="play-button"
          >
            <span className={playing ? "play-glyph is-pause" : "play-glyph"} aria-hidden="true" />
            {playLabel}
          </button>
          <span className="control-group">
            <button
              type="button"
              className="button button-step"
              onClick={() => void playback.step(-1)}
              disabled={empty || playback.position === null || playback.position === 0}
              aria-label="Previous event"
            >
              ‹
            </button>
            <button
              type="button"
              className="button button-step"
              onClick={() => void playback.step(1)}
              disabled={empty || playback.atEnd}
              aria-label="Next event"
            >
              ›
            </button>
          </span>
          <span className="control-group">
            <button
              type="button"
              className="button button-ghost"
              onClick={() => goImportant(-1)}
              disabled={previous === null}
              aria-label="Previous important event"
            >
              <span aria-hidden="true">⇤</span> Prev important
            </button>
            <button
              type="button"
              className="button button-ghost"
              onClick={() => goImportant(1)}
              disabled={next === null}
              aria-label="Next important event"
            >
              Next important <span aria-hidden="true">⇥</span>
            </button>
          </span>
        </div>
        <span className="playback-readout">
          <span className="playback-counter mono" data-testid="playback-counter">
            <span className="counter-now">{current === null ? "—" : `#${formatCount(current.sequence)}`}</span>
            <span className="counter-of"> / {formatCount(total)}</span>
          </span>
          <StatusChip playback={playback} reducedMotion={reducedMotion} />
        </span>
        <div className="playback-options">
          <div className="segmented" role="group" aria-label="Playback speed">
            {PLAYBACK_SPEEDS.map((speed) => (
              <button
                key={speed}
                type="button"
                className={speed === playback.speed ? "segment is-active" : "segment"}
                aria-pressed={speed === playback.speed}
                aria-label={`Playback speed ${formatSpeed(speed)}`}
                onClick={() => playback.setSpeed(speed)}
              >
                {formatSpeed(speed)}
              </button>
            ))}
          </div>
          {live ? (
            <label className="follow-live">
              <input
                type="checkbox"
                checked={playback.followLive}
                onChange={(event) => playback.setFollowLive(event.target.checked)}
              />
              Follow live
            </label>
          ) : null}
        </div>
      </div>
      <Scrubber playback={playback} important={important} onSeek={onSeek} />
      {playback.stalled ? (
        <p className="banner tone-warning playback-alert" role="alert">
          Playback paused: the next timeline page could not be loaded
          {pageError === null ? "" : ` (${describeError(pageError).title})`}. The current position
          is kept.
        </p>
      ) : null}
      <CurrentEvent
        playback={playback}
        markers={markers}
        live={live}
        pageError={pageError}
        reducedMotion={reducedMotion}
      />
      <p className="visually-hidden" role="status" aria-live="polite" data-testid="playback-announcement">
        {announcement(playback)}
      </p>
    </section>
  );
}

/** Spoken status: changes on user actions and state changes, never on every playback tick. */
function announcement(p: Playback): string {
  const at = p.current === null ? null : `event ${formatCount(p.current.sequence)} of ${formatCount(p.total)}`;
  if (p.seeking) return "Loading timeline pages to reach the requested event.";
  if (p.playing) return `Playing at ${formatSpeed(p.speed)}.`;
  if (p.stalled) return `Playback paused${at === null ? "" : ` at ${at}`}; the next page could not be loaded.`;
  if (!p.ready) return "Loading recorded events.";
  if (at === null) return p.loaded === 0 ? "No events to replay." : "Playback stopped. Press Play to start.";
  if (p.liveEdge) return `At the live edge, ${at}.`;
  if (p.ended) return `End of recording, ${at}.`;
  return `Playback paused at ${at}.`;
}

interface PlaybackStatus {
  text: string;
  tone: string;
  state: "loading" | "stalled" | "playing" | "live-edge" | "ended" | "paused";
}

function statusOf(p: Playback): PlaybackStatus {
  if (p.seeking) return { text: "LOADING EVENTS…", tone: "info", state: "loading" };
  if (p.loadingNext) return { text: "LOADING NEXT PAGE…", tone: "info", state: "loading" };
  if (p.stalled) return { text: "PAUSED · PAGE NOT LOADED", tone: "warning", state: "stalled" };
  if (p.playing) return { text: "PLAYING", tone: "info", state: "playing" };
  if (p.liveEdge) return { text: "LIVE EDGE", tone: "info", state: "live-edge" };
  if (p.ended) return { text: "END OF RECORDING", tone: "neutral", state: "ended" };
  return { text: "PAUSED", tone: "muted", state: "paused" };
}

function StatusChip({ playback, reducedMotion }: { playback: Playback; reducedMotion: boolean }) {
  const { text, tone, state } = statusOf(playback);
  const active = state === "playing" || state === "loading";
  return (
    <span
      className={`playback-status chip tone-${tone}`}
      data-testid="playback-status"
      data-state={state}
    >
      <span
        className={active && !reducedMotion ? "status-dot pulse" : "status-dot"}
        aria-hidden="true"
        data-testid="status-dot"
      />
      {text}
    </span>
  );
}

function ShortcutHint() {
  return (
    <p className="playback-keys">
      <span className="key-hint">
        <kbd>Space</kbd> Play/Pause
      </span>
      <span className="key-hint">
        <kbd>←</kbd>
        <kbd>→</kbd> Step
      </span>
      <span className="key-hint">
        <kbd>Shift</kbd>
        <kbd>←</kbd>
        <kbd>→</kbd> Important event
      </span>
    </p>
  );
}

// -- scrubber --------------------------------------------------------------------------------

function Scrubber({
  playback,
  important,
  onSeek,
}: {
  playback: Playback;
  important: readonly ImportantEvent[];
  onSeek: (target: SeekTarget) => void;
}) {
  const { total, loaded, current } = playback;
  const scaleMax = Math.max(total, 1);
  // A drag into unloaded events seeks once, when it is released.
  const [pending, setPending] = useState<number | null>(null);
  const value = pending ?? current?.sequence ?? 1;
  const marks = useMemo(() => scrubberMarkers(important, scaleMax), [important, scaleMax]);

  const commit = (): void => {
    if (pending === null) return;
    setPending(null);
    onSeek({ sequence: pending, eventId: null });
  };
  const onChange = (raw: string): void => {
    const to = Math.min(scaleMax, Math.max(1, Math.round(Number(raw))));
    if (!Number.isFinite(to)) return;
    if (to <= loaded) {
      setPending(null);
      onSeek({ sequence: to, eventId: null });
    } else {
      setPending(to);
    }
  };

  const loadedPct = loaded === 0 ? 0 : markerPercent(loaded, scaleMax);
  const playedPct = current === null && pending === null ? 0 : markerPercent(value, scaleMax);
  const valueText =
    current === null && pending === null
      ? `No event selected; ${plural(total, "event")} recorded, ${formatCount(loaded)} loaded`
      : `Event ${formatCount(value)} of ${formatCount(total)}; ${formatCount(loaded)} loaded`;
  const classes = ["scrubber"];
  if (current === null && pending === null) classes.push("is-idle");
  if (pending !== null) classes.push("is-pending");

  return (
    <div className={classes.join(" ")} data-testid="scrubber">
      <div className="scrub-track-wrap">
        <div className="scrub-track" aria-hidden="true">
          <span className="scrub-loaded" style={{ width: `${loadedPct}%` }} data-testid="scrub-loaded" />
          <span className="scrub-played" style={{ width: `${playedPct}%` }} data-testid="scrub-played" />
        </div>
        <input
          type="range"
          className="scrub-input"
          min={1}
          max={scaleMax}
          step={1}
          value={value}
          disabled={total === 0}
          aria-label="Replay position by event sequence"
          aria-valuetext={valueText}
          onChange={(event) => onChange(event.target.value)}
          onPointerUp={commit}
          onKeyUp={commit}
          onBlur={commit}
          data-testid="scrubber-input"
        />
      </div>
      <MarkerRail marks={marks} currentSequence={current?.sequence ?? null} onSeek={onSeek} />
      <div className="scrub-scale mono">
        <span>#1</span>
        <span className="scrub-legend" aria-hidden="true">
          <span className="legend-item lg-played">played</span>
          <span className="legend-item lg-loaded">loaded</span>
          <span className="legend-item lg-known">not loaded</span>
          <span className="legend-item lg-failure">failure</span>
          <span className="legend-item lg-recovered">recovery</span>
          <span className="legend-item lg-unresolved">unresolved</span>
        </span>
        <span data-testid="scrub-loaded-label">
          {formatCount(loaded)} / {formatCount(total)} loaded
          {pending === null ? null : ` · release to load up to #${formatCount(pending)}`}
        </span>
        <span>#{formatCount(scaleMax)}</span>
      </div>
    </div>
  );
}

/** Marker buttons in one tab stop; arrow keys move between them (roving tabindex). */
function MarkerRail({
  marks,
  currentSequence,
  onSeek,
}: {
  marks: readonly ScrubberMarker[];
  currentSequence: number | null;
  onSeek: (target: SeekTarget) => void;
}) {
  const [active, setActive] = useState(0);
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const index = Math.min(active, Math.max(0, marks.length - 1));
  if (marks.length === 0) return <div className="scrub-markers is-empty" aria-hidden="true" />;

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    const moves: Record<string, number> = {
      ArrowRight: Math.min(marks.length - 1, index + 1),
      ArrowLeft: Math.max(0, index - 1),
      Home: 0,
      End: marks.length - 1,
    };
    const to = moves[event.key];
    if (to === undefined) return;
    event.preventDefault();
    setActive(to);
    refs.current[to]?.focus();
  };

  return (
    <div
      className="scrub-markers"
      role="toolbar"
      aria-label="Important event markers"
      onKeyDown={onKeyDown}
    >
      {marks.map((m, i) => (
        <button
          key={m.sequence}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          className={`scrub-marker mk-${m.kind.replace("_", "-")} tone-${m.tone}${
            m.sequence === currentSequence ? " is-current" : ""
          }`}
          style={{ left: `${m.percent}%` }}
          tabIndex={i === index ? 0 : -1}
          aria-label={`${m.label}, event #${formatCount(m.sequence)}`}
          title={`${m.label} · #${formatCount(m.sequence)}`}
          data-testid="scrubber-marker"
          data-kind={m.kind}
          data-sequence={m.sequence}
          onFocus={() => setActive(i)}
          onClick={() => onSeek({ sequence: m.sequence, eventId: m.eventId })}
        />
      ))}
    </div>
  );
}

// -- current event ---------------------------------------------------------------------------

function idleText(playback: Playback, live: boolean, pageError: unknown): string {
  if (!playback.ready) {
    return pageError === null ? "Loading recorded events…" : "Recorded events could not be loaded.";
  }
  if (playback.total === 0) {
    return live
      ? "No events recorded yet. Playback covers only events already recorded."
      : "No events to replay.";
  }
  return `Press Play to step through the ${plural(playback.total, "recorded event")} in sequence order.`;
}

/**
 * The focal point of the replay: the current event's safe fields. Its accent follows the
 * evidence (report markers, then event type); the body re-mounts per event so it can fade
 * in, unless reduced motion is requested.
 */
function CurrentEvent({
  playback,
  markers,
  live,
  pageError,
  reducedMotion,
}: {
  playback: Playback;
  markers: MarkerIndex;
  live: boolean;
  pageError: unknown;
  reducedMotion: boolean;
}) {
  const item = playback.current;
  if (item === null) {
    const waiting = !playback.ready && pageError === null;
    return (
      <section
        className={waiting ? "now-card is-idle is-waiting" : "now-card is-idle"}
        aria-label="Current replay event"
        data-testid="current-event"
        data-moment="none"
      >
        <div className="hero-index" aria-hidden="true">
          <span className="hero-seq mono">#—</span>
        </div>
        <div className="hero-body">
          <p className="eyebrow">Now replaying</p>
          <p className="dim">
            {idleText(playback, live, pageError)} Each step shows one recorded evidence event; no
            screen video is recorded.
          </p>
        </div>
      </section>
    );
  }
  const kind = eventPresentation(item);
  const found = markersFor(markers, item);
  const moment = replayMoment(item, found);
  return (
    <section
      className={`now-card tone-${MOMENT_TONES[moment]} moment-${moment}`}
      aria-label="Current replay event"
      data-testid="current-event"
      data-moment={moment}
    >
      <div className="hero-index">
        <p className="eyebrow">Now replaying</p>
        <span className="hero-seq mono now-seq">#{formatCount(item.sequence)}</span>
        <span className="hero-of mono">of {formatCount(playback.total)}</span>
      </div>
      <div
        key={item.event_id}
        className={reducedMotion ? "hero-body" : "hero-body is-entering"}
        data-testid="current-event-body"
      >
        <div className="now-head">
          <Chip tone={kind.tone}>{kind.label}</Chip>
          <span className="mono now-type">{item.type}</span>
          <span className="mono now-time">{formatClock(item.timestamp)} UTC</span>
          <span className="evidence-badge">{item.evidence.toUpperCase()}</span>
        </div>
        <p className="now-summary" title={item.summary}>
          {item.summary}
        </p>
        <div className="now-meta">
          {item.path === null ? null : <span className="mono now-path">{item.path}</span>}
          {found.map((marker) => (
            <span
              key={`${marker.kind}-${marker.label}`}
              className={`chip tone-${marker.tone} hero-marker mk-${marker.kind.replace("_", "-")}`}
            >
              {marker.label}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

// -- layout ----------------------------------------------------------------------------------

export const THEATER_HEIGHT_VAR = "--replay-theater-height";

/**
 * Publishes the theater's rendered height on its container as `--replay-theater-height`,
 * so the sticky layout sizes the timeline and the detail panel below the theater's real
 * height (which grows with a temporary alert or wrapped controls) instead of a guess. One
 * observer, no scroll listeners; without ResizeObserver the CSS fallback height applies.
 */
function useTheaterHeight(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = ref.current;
    const host = el?.parentElement;
    if (el === null || host === null || host === undefined || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    let last = -1;
    const observer = new ResizeObserver(() => {
      const height = Math.ceil(el.getBoundingClientRect().height);
      if (height <= 0 || height === last) return;
      last = height;
      host.style.setProperty(THEATER_HEIGHT_VAR, `${height}px`);
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      host.style.removeProperty(THEATER_HEIGHT_VAR);
    };
  }, [ref]);
}

// -- keyboard --------------------------------------------------------------------------------

/**
 * Space: play/pause. Left/Right: previous/next event. Shift+Left/Right: previous/next
 * important event. Ignored while typing, with Ctrl/Alt/Meta, when another handler already
 * used the key (tabs, marker rail, timeline rows) and on controls with their own keyboard
 * handling: any form control or slider-like widget for arrows, plus buttons, links and
 * checkboxes for Space (the scrubber range input owns both).
 */
function usePlaybackShortcuts(actions: {
  toggle: () => void;
  step: (dir: 1 | -1) => void;
  important: (dir: 1 | -1) => void;
}): void {
  const ref = useRef(actions);
  ref.current = actions;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.isComposing || isTextEntry(event.target)) return;
      // While a modal dialog is open nothing behind it reacts, wherever focus happens to be.
      if (document.querySelector('[aria-modal="true"]') !== null) return;
      if (event.key === " ") {
        if (event.shiftKey || event.repeat || isOwnSpaceControl(event.target)) return;
        event.preventDefault();
        ref.current.toggle();
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (isOwnArrowControl(event.target)) return;
      event.preventDefault();
      const dir = event.key === "ArrowRight" ? 1 : -1;
      if (event.shiftKey) ref.current.important(dir);
      else ref.current.step(dir);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
