import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { Chip } from "../components/Evidence";
import type { Playback, SeekTarget } from "../hooks/usePlayback";
import { describeError } from "../lib/errorMessages";
import { formatCount, plural } from "../lib/format";
import {
  PLAYBACK_SPEEDS,
  adjacentImportant,
  formatSpeed,
  isOwnSpaceControl,
  isTextEntry,
  markerPercent,
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
  onSeek: (target: SeekTarget) => void;
}

/**
 * Replay controls over the recorded event log: play/pause, step, important-event
 * navigation, speed, a sequence-based scrubber and the event currently being replayed.
 * This replays recorded evidence events; there is no screen video.
 */
export function PlaybackBar({ playback, important, markers, live, pageError, onSeek }: PlaybackBarProps) {
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

  const empty = playback.loaded === 0;
  const playLabel = playing ? "Pause" : ended ? "Replay" : "Play";

  return (
    <section className="playback" aria-label="Replay controls" data-testid="playback">
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
          <button
            type="button"
            className="button button-small"
            onClick={() => void playback.step(-1)}
            disabled={empty || playback.position === null || playback.position === 0}
            aria-label="Previous event"
          >
            ‹
          </button>
          <button
            type="button"
            className="button button-small"
            onClick={() => void playback.step(1)}
            disabled={empty || playback.atEnd}
            aria-label="Next event"
          >
            ›
          </button>
          <button
            type="button"
            className="button button-small"
            onClick={() => goImportant(-1)}
            disabled={previous === null}
            aria-label="Previous important event"
          >
            Prev important
          </button>
          <button
            type="button"
            className="button button-small"
            onClick={() => goImportant(1)}
            disabled={next === null}
            aria-label="Next important event"
          >
            Next important
          </button>
        </div>
        <span className="playback-counter mono" data-testid="playback-counter">
          {current === null ? "—" : `#${formatCount(current.sequence)}`} / {formatCount(total)}
        </span>
        <StatusChip playback={playback} />
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
      <CurrentEvent playback={playback} markers={markers} live={live} pageError={pageError} />
      <p className="playback-keys">
        <kbd>Space</kbd> play/pause · <kbd>←</kbd> <kbd>→</kbd> event · <kbd>Shift</kbd>+
        <kbd>←</kbd> <kbd>→</kbd> important event
      </p>
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

function StatusChip({ playback: p }: { playback: Playback }) {
  const [text, tone] = p.seeking
    ? ["LOADING EVENTS…", "info"]
    : p.loadingNext
      ? ["LOADING NEXT PAGE…", "info"]
      : p.stalled
        ? ["PAUSED · PAGE NOT LOADED", "warning"]
        : p.playing
          ? ["PLAYING", "info"]
          : p.liveEdge
            ? ["LIVE EDGE", "danger"]
            : p.ended
              ? ["END OF RECORDING", "neutral"]
              : ["PAUSED", "muted"];
  return (
    <span className={`playback-status chip tone-${tone}`} data-testid="playback-status">
      {text}
    </span>
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

  return (
    <div className="scrubber" data-testid="scrubber">
      <div className="scrub-track-wrap">
        <div className="scrub-track" aria-hidden="true">
          <span className="scrub-loaded" style={{ width: `${loadedPct}%` }} data-testid="scrub-loaded" />
          <span className="scrub-played" style={{ width: `${playedPct}%` }} />
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
      <MarkerRail marks={marks} onSeek={onSeek} />
      <div className="scrub-scale mono">
        <span>#1</span>
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
  onSeek,
}: {
  marks: readonly ScrubberMarker[];
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
          className={`scrub-marker mk-${m.kind.replace("_", "-")} tone-${m.tone}`}
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

function CurrentEvent({
  playback,
  markers,
  live,
  pageError,
}: {
  playback: Playback;
  markers: MarkerIndex;
  live: boolean;
  pageError: unknown;
}) {
  const item = playback.current;
  if (item === null) {
    return (
      <section className="now-card is-idle" aria-label="Current replay event" data-testid="current-event">
        <p className="eyebrow">Now replaying</p>
        <p className="dim">
          {idleText(playback, live, pageError)} Each step shows one recorded evidence event; no
          screen video is recorded.
        </p>
      </section>
    );
  }
  const kind = eventPresentation(item);
  const found = markersFor(markers, item);
  return (
    <section
      className={`now-card tone-${kind.tone}`}
      aria-label="Current replay event"
      data-testid="current-event"
    >
      <div className="now-head">
        <p className="eyebrow">Now replaying</p>
        <span className="mono now-seq">#{formatCount(item.sequence)}</span>
        <span className="mono now-time">{formatClock(item.timestamp)} UTC</span>
        <Chip tone={kind.tone}>{kind.label}</Chip>
        <span className="mono now-type">{item.type}</span>
        <span className="evidence-badge">{item.evidence.toUpperCase()}</span>
      </div>
      <p className="now-summary">{item.summary}</p>
      {item.path === null && found.length === 0 ? null : (
        <div className="now-meta">
          {item.path === null ? null : <span className="mono now-path">{item.path}</span>}
          {found.map((marker) => (
            <Chip key={`${marker.kind}-${marker.label}`} tone={marker.tone}>
              {marker.label}
            </Chip>
          ))}
        </div>
      )}
    </section>
  );
}

// -- keyboard --------------------------------------------------------------------------------

/**
 * Space: play/pause. Left/Right: previous/next event. Shift+Left/Right: previous/next
 * important event. Ignored while typing, with Ctrl/Alt/Meta, when another handler already
 * used the key (tabs, marker rail, timeline rows) and, for Space, on controls that Space
 * activates themselves (buttons, links, checkboxes).
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
      if (event.key === " ") {
        if (event.shiftKey || event.repeat || isOwnSpaceControl(event.target)) return;
        event.preventDefault();
        ref.current.toggle();
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement) return;
      event.preventDefault();
      const dir = event.key === "ArrowRight" ? 1 : -1;
      if (event.shiftKey) ref.current.important(dir);
      else ref.current.step(dir);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
