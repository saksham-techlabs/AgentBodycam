import { useCallback, useEffect, useRef, useState } from "react";

import type { TimelineItem } from "../api/types";
import { DEFAULT_SPEED, lowerBound, stepInterval, type PlaybackSpeed } from "../lib/playback";
import { lastSequence } from "../lib/timeline";
import type { TimelineResource } from "./useTimeline";

/** Why the current event changed; decides how the timeline scrolls and focuses. */
export type MoveCause = "tick" | "step" | "seek" | "jump" | "follow";

export type SeekResult = "reached" | "absent" | "cap" | "cancelled" | "error";

export interface SeekTarget {
  sequence: number;
  /** When known, the event must be exactly this one; otherwise the event at `sequence`. */
  eventId: string | null;
}

export interface Playback {
  /** The event currently being replayed (the cursor), or null before playback starts. */
  current: TimelineItem | null;
  /** Index of `current` in the loaded timeline. */
  position: number | null;
  playing: boolean;
  speed: PlaybackSpeed;
  followLive: boolean;
  /** Playback is waiting for the next timeline page. */
  loadingNext: boolean;
  /** A seek is loading pages to reach an unloaded event. */
  seeking: boolean;
  /** The last page request made for playback failed; playback paused where it was. */
  stalled: boolean;
  /** The first timeline page has been read, so `total` is known. */
  ready: boolean;
  /** Events known to exist (the server's total, never less than what is loaded). */
  total: number;
  loaded: number;
  atStart: boolean;
  /** The cursor is on the last loaded event and no more pages exist. */
  atEnd: boolean;
  /** Live recording and the cursor is on the newest recorded event. */
  liveEdge: boolean;
  /** Stopped recording and the cursor is on its last event. */
  ended: boolean;
  canPlay: boolean;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  setSpeed: (speed: PlaybackSpeed) => void;
  setFollowLive: (on: boolean) => void;
  step: (dir: 1 | -1) => Promise<void>;
  seek: (target: SeekTarget, cause: MoveCause) => Promise<SeekResult>;
  /** Make a loaded event current without moving the view (it was picked in the view). */
  select: (eventId: string) => void;
}

interface State {
  current: TimelineItem | null;
  position: number | null;
  playing: boolean;
  speed: PlaybackSpeed;
  followLive: boolean;
  loadingNext: boolean;
  seeking: boolean;
  stalled: boolean;
}

const INITIAL: State = {
  current: null,
  position: null,
  playing: false,
  speed: DEFAULT_SPEED,
  followLive: false,
  loadingNext: false,
  seeking: false,
  stalled: false,
};

/**
 * The replay engine: a cursor over the *loaded* timeline that advances one event per
 * `stepInterval(speed)` while playing. Reaching the end of the loaded pages requests the
 * next page (one request at a time, through `useTimeline`); a failed request pauses with
 * the cursor unchanged. Seeking to an unloaded event reuses `loadUntil` (capped and
 * cancellable). Playback pauses when the page is hidden and never follows a live
 * recording unless `followLive` is on and the cursor is already at the live edge.
 *
 * Every move is reported through `onMove`, so the host decides how to select and reveal
 * the event; this hook owns no view state.
 */
export function usePlayback({
  timeline,
  live,
  onMove,
}: {
  timeline: TimelineResource;
  live: boolean;
  onMove: (item: TimelineItem, cause: MoveCause) => void;
}): Playback {
  const [state, setState] = useState<State>(INITIAL);

  // Latest values for callbacks and timers that outlive a render.
  const timelineRef = useRef(timeline);
  timelineRef.current = timeline;
  const liveRef = useRef(live);
  liveRef.current = live;
  const onMoveRef = useRef(onMove);
  onMoveRef.current = onMove;
  // Updated synchronously so consecutive calls in one task see each other's effect.
  const positionRef = useRef<number | null>(null);
  const playingRef = useRef(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const setPlaying = useCallback((playing: boolean) => {
    playingRef.current = playing;
    setState((s) => (s.playing === playing ? s : { ...s, playing }));
  }, []);

  const moveTo = useCallback((item: TimelineItem, position: number, cause: MoveCause | null) => {
    positionRef.current = position;
    setState((s) => ({ ...s, current: item, position, stalled: false }));
    if (cause !== null) onMoveRef.current(item, cause);
  }, []);

  const pause = useCallback(() => setPlaying(false), [setPlaying]);

  const play = useCallback(() => {
    const data = timelineRef.current.data;
    const first = data.items[0];
    if (first === undefined) return;
    const position = positionRef.current;
    const atEnd = position !== null && position >= data.items.length - 1 && !data.hasMore;
    if (atEnd && liveRef.current) return;
    if (position === null || atEnd) moveTo(first, 0, "tick");
    setState((s) => ({ ...s, stalled: false }));
    setPlaying(true);
  }, [moveTo, setPlaying]);

  const toggle = useCallback(() => {
    if (playingRef.current) pause();
    else play();
  }, [pause, play]);

  /** One playback tick: the next loaded event, else the next page, else stop. */
  const advance = useCallback(async () => {
    if (!playingRef.current) return;
    if (document.hidden) {
      setPlaying(false);
      return;
    }
    const next = (positionRef.current ?? -1) + 1;
    let data = timelineRef.current.data;
    if (next >= data.items.length && data.hasMore) {
      setState((s) => ({ ...s, loadingNext: true }));
      const { result, data: loaded } = await timelineRef.current.loadMore();
      if (!aliveRef.current) return;
      setState((s) => ({ ...s, loadingNext: false, stalled: result === "error" }));
      if (result !== "loaded" && result !== "end") {
        setPlaying(false);
        return;
      }
      if (!playingRef.current) return;
      data = loaded;
    }
    const item = data.items[next];
    if (item === undefined) {
      setPlaying(false);
      return;
    }
    moveTo(item, next, "tick");
  }, [moveTo, setPlaying]);

  useEffect(() => {
    if (!state.playing || state.loadingNext) return undefined;
    const timer = setTimeout(() => void advance(), stepInterval(state.speed));
    return () => clearTimeout(timer);
  }, [state.playing, state.loadingNext, state.speed, state.position, advance]);

  useEffect(() => {
    const onVisibilityChange = (): void => {
      if (document.hidden) setPlaying(false);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [setPlaying]);

  const step = useCallback(
    async (dir: 1 | -1) => {
      setPlaying(false);
      const position = positionRef.current;
      const next = position === null ? (dir === 1 ? 0 : -1) : position + dir;
      if (next < 0) return;
      let data = timelineRef.current.data;
      if (next >= data.items.length) {
        if (!data.hasMore) return;
        const { result, data: loaded } = await timelineRef.current.loadMore();
        if (!aliveRef.current) return;
        if (result !== "loaded") {
          if (result === "error") setState((s) => ({ ...s, stalled: true }));
          return;
        }
        if (positionRef.current !== position) return;
        data = loaded;
      }
      const item = data.items[next];
      if (item !== undefined) moveTo(item, next, "step");
    },
    [moveTo, setPlaying],
  );

  const seek = useCallback(
    async (target: SeekTarget, cause: MoveCause): Promise<SeekResult> => {
      setPlaying(false);
      let data = timelineRef.current.data;
      const isLoaded = (): boolean =>
        target.eventId !== null
          ? data.index.has(target.eventId)
          : lastSequence(data) >= target.sequence;
      if (!isLoaded()) {
        setState((s) => ({ ...s, seeking: true }));
        const { result, data: loaded } = await timelineRef.current.loadUntil(target.sequence);
        if (!aliveRef.current) return "cancelled";
        setState((s) => ({ ...s, seeking: false, stalled: result === "error" }));
        if (result === "cancelled") return "cancelled";
        data = loaded;
        if (!isLoaded()) return result === "error" ? "error" : result === "cap" ? "cap" : "absent";
      }
      const position =
        target.eventId !== null
          ? data.index.get(target.eventId)
          : lowerBound(data.items, target.sequence);
      const item = position === undefined ? undefined : data.items[position];
      if (position === undefined || item === undefined) return "absent";
      moveTo(item, position, cause);
      return "reached";
    },
    [moveTo, setPlaying],
  );

  const select = useCallback(
    (eventId: string) => {
      setPlaying(false);
      const data = timelineRef.current.data;
      const position = data.index.get(eventId);
      const item = position === undefined ? undefined : data.items[position];
      if (position !== undefined && item !== undefined) moveTo(item, position, null);
    },
    [moveTo, setPlaying],
  );

  const setSpeed = useCallback((speed: PlaybackSpeed) => {
    setState((s) => (s.speed === speed ? s : { ...s, speed }));
  }, []);

  const setFollowLive = useCallback((followLive: boolean) => {
    setState((s) => ({ ...s, followLive }));
  }, []);

  // Follow live: only when enabled, paused and sitting on the newest event when new
  // events arrive; playback itself never races the live poller. Following can only be
  // switched on while live, and it still applies to the final read after the recording
  // stopped, so a follower ends on the recording's last event.
  const { items, hasMore, pages, total: serverTotal } = timeline.data;
  const previousLength = useRef(items.length);
  useEffect(() => {
    const before = previousLength.current;
    previousLength.current = items.length;
    if (!state.followLive || playingRef.current || items.length <= before) return;
    const newest = items[items.length - 1];
    if (positionRef.current === before - 1 && newest !== undefined) {
      moveTo(newest, items.length - 1, "follow");
    }
  }, [items, state.followLive, moveTo]);

  const loaded = items.length;
  const total = Math.max(serverTotal, loaded, lastSequence(timeline.data));
  const { position } = state;
  const atEnd = position !== null && position >= loaded - 1 && !hasMore && pages > 0;
  const liveEdge = live && atEnd;

  return {
    ...state,
    ready: pages > 0,
    total,
    loaded,
    atStart: position === null || position === 0,
    atEnd,
    liveEdge,
    ended: !live && atEnd && !state.playing,
    canPlay: loaded > 0 && !liveEdge,
    play,
    pause,
    toggle,
    setSpeed,
    setFollowLive,
    step,
    seek,
    select,
  };
}
