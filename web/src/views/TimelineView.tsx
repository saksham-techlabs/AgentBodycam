import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import type { ForensicReport, TimelineItem } from "../api/types";
import { Chip, type JumpHandler } from "../components/Evidence";
import { EmptyState } from "../components/States";
import type { TimelineResource } from "../hooks/useTimeline";
import { JUMP_PAGE_CAP } from "../hooks/useTimeline";
import { describeError } from "../lib/errorMessages";
import { formatCount, plural } from "../lib/format";
import {
  FILTERS,
  eventPresentation,
  formatClock,
  markersFor,
  matches,
  normalizeQuery,
  type MarkerIndex,
  type TimelineFilter,
} from "../lib/timeline";
import { EventDetail } from "./EventDetail";

/** Fixed row height: lets the list render only the rows in (or near) the viewport. */
export const ROW_HEIGHT = 64;
const OVERSCAN = 10;
/** Viewport assumed before the list has been measured (and in layout-less test DOMs). */
const FALLBACK_VIEWPORT = 960;
const AUTOLOAD_DISTANCE = ROW_HEIGHT * 6;
const MAX_ROW_CHIPS = 2;

export type JumpState =
  | { status: "idle" }
  | { status: "loading"; sequence: number }
  | { status: "cap"; sequence: number }
  | { status: "absent"; sequence: number }
  | { status: "unloaded" };

export interface FocusRequest {
  eventId: string;
  nonce: number;
}

interface TimelineViewProps {
  timeline: TimelineResource;
  markers: MarkerIndex;
  report: ForensicReport | null;
  reportError: unknown;
  live: boolean;
  filter: TimelineFilter;
  onFilter: (filter: TimelineFilter) => void;
  query: string;
  onQuery: (query: string) => void;
  selectedId: string | null;
  onSelect: (eventId: string | null) => void;
  focusRequest: FocusRequest | null;
  jump: JumpState;
  onCancelJump: () => void;
  onJump: JumpHandler;
}

export function TimelineView(props: TimelineViewProps) {
  const { timeline, markers, filter, query, selectedId, onSelect, focusRequest } = props;
  const { items, index, hasMore, total, pages } = timeline.data;
  const q = normalizeQuery(query);
  const narrowed = filter !== "all" || q !== "";

  const filtered = useMemo(
    () => (narrowed ? items.filter((item) => matches(item, filter, q, markers.failures)) : items),
    [items, filter, q, narrowed, markers.failures],
  );
  const filteredIndex = useMemo(() => {
    if (!narrowed) return index;
    const map = new Map<string, number>();
    filtered.forEach((item, i) => map.set(item.event_id, i));
    return map;
  }, [filtered, index, narrowed]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({ top: 0, height: FALLBACK_VIEWPORT });
  const pendingFocus = useRef<number | null>(null);
  const [focusTick, setFocusTick] = useState(0);

  const measure = useCallback((): number => {
    const height = scrollRef.current?.clientHeight ?? 0;
    return height > 0 ? height : FALLBACK_VIEWPORT;
  }, []);

  /** Scroll row `i` into view (centred-ish for jumps) and move keyboard focus to it. */
  const reveal = useCallback(
    (i: number, mode: "nearest" | "jump") => {
      const el = scrollRef.current;
      const height = measure();
      const current = el?.scrollTop ?? 0;
      const rowTop = i * ROW_HEIGHT;
      let top = current;
      if (mode === "jump") top = Math.max(0, rowTop - Math.floor(height / 3));
      else if (rowTop < current) top = rowTop;
      else if (rowTop + ROW_HEIGHT > current + height) top = rowTop + ROW_HEIGHT - height;
      if (el !== null && el.scrollTop !== top) el.scrollTop = top;
      setViewport({ top, height });
      pendingFocus.current = i;
      setFocusTick((n) => n + 1);
    },
    [measure],
  );

  useLayoutEffect(() => {
    const i = pendingFocus.current;
    if (i === null) return;
    pendingFocus.current = null;
    const row = scrollRef.current?.querySelector<HTMLButtonElement>(`button[data-index="${i}"]`);
    row?.focus({ preventScroll: true });
  }, [focusTick]);

  // Each request (nonce) moves the view once, as soon as its event is in the filtered list;
  // later data or filter changes never move it again.
  const handledNonce = useRef<number | null>(null);
  useEffect(() => {
    if (focusRequest === null || handledNonce.current === focusRequest.nonce) return;
    const i = filteredIndex.get(focusRequest.eventId);
    if (i === undefined) return;
    handledNonce.current = focusRequest.nonce;
    reveal(i, "jump");
  }, [focusRequest, filteredIndex, reveal]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el === null || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => setViewport((v) => ({ top: v.top, height: measure() })));
    observer.observe(el);
    return () => observer.disconnect();
  }, [measure]);

  const { loadMore, loading, error } = timeline;
  const onScroll = (): void => {
    const el = scrollRef.current;
    if (el === null) return;
    setViewport({ top: el.scrollTop, height: measure() });
    const nearEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - AUTOLOAD_DISTANCE;
    if (nearEnd && hasMore && !loading && error === null) void loadMore();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLOListElement>): void => {
    const target = event.target as HTMLElement;
    const current = Number(target.dataset.index);
    if (!Number.isInteger(current) || filtered.length === 0) return;
    const page = Math.max(1, Math.floor(measure() / ROW_HEIGHT) - 1);
    const next: Record<string, number> = {
      ArrowDown: current + 1,
      ArrowUp: current - 1,
      PageDown: current + page,
      PageUp: current - page,
      Home: 0,
      End: filtered.length - 1,
    };
    const to = next[event.key];
    if (to === undefined) return;
    event.preventDefault();
    reveal(Math.min(filtered.length - 1, Math.max(0, to)), "nearest");
  };

  const first = Math.max(0, Math.floor(viewport.top / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(filtered.length, Math.ceil((viewport.top + viewport.height) / ROW_HEIGHT) + OVERSCAN);
  const visible = filtered.slice(first, last);

  const selectedPosition = selectedId === null ? undefined : index.get(selectedId);
  const selected = selectedPosition === undefined ? null : (items[selectedPosition] ?? null);
  const selectedRow = selectedId === null ? undefined : filteredIndex.get(selectedId);
  const tabbable =
    selectedRow !== undefined && selectedRow >= first && selectedRow < last ? selectedRow : first;

  const closeDetail = (): void => {
    const i = selectedId === null ? undefined : filteredIndex.get(selectedId);
    onSelect(null);
    if (i !== undefined) reveal(i, "nearest");
  };

  return (
    <div className="timeline-view">
      <FirstFailureStrip {...props} />
      <Toolbar
        {...props}
        shown={filtered.length}
        loaded={items.length}
        narrowed={narrowed}
        hasMore={hasMore}
      />
      <JumpNotice {...props} />
      <div className={selected === null ? "timeline-layout" : "timeline-layout has-detail"}>
        <div className="timeline-main">
          {pages === 0 && timeline.error === null ? (
            <div className="state-loading" role="status">
              <span className="visually-hidden">Loading timeline…</span>
              <div className="skeleton-row" aria-hidden="true">
                <span className="skeleton w-12" />
                <span className="skeleton w-40" />
              </div>
            </div>
          ) : pages > 0 && items.length === 0 ? (
            <EmptyState title="No events recorded">
              {props.live
                ? "The recording is live; events appear here as they are recorded."
                : "This session's event log is empty."}
            </EmptyState>
          ) : pages > 0 && filtered.length === 0 ? (
            <EmptyState title="No matching loaded events">
              No loaded event matches this {q === "" ? "filter" : "filter and search"}.
              {hasMore ? " More events exist that are not loaded yet." : null}
            </EmptyState>
          ) : pages > 0 ? (
            <div
              className="timeline-scroll"
              ref={scrollRef}
              onScroll={onScroll}
              data-testid="timeline-scroll"
            >
              <ol
                className="timeline-list"
                aria-label="Timeline events in sequence order"
                style={{ height: filtered.length * ROW_HEIGHT }}
                onKeyDown={onKeyDown}
              >
                {visible.map((item, offset) => (
                  <Row
                    key={item.event_id}
                    item={item}
                    position={first + offset}
                    setSize={filtered.length}
                    markers={markers}
                    selected={item.event_id === selectedId}
                    tabbable={first + offset === tabbable}
                    onSelect={onSelect}
                  />
                ))}
              </ol>
            </div>
          ) : null}
          <Footer {...props} total={total} loaded={items.length} />
        </div>
        {selected === null ? null : (
          <EventDetail item={selected} markers={markersFor(markers, selected)} onClose={closeDetail} />
        )}
      </div>
    </div>
  );
}

function Row({
  item,
  position,
  setSize,
  markers,
  selected,
  tabbable,
  onSelect,
}: {
  item: TimelineItem;
  position: number;
  setSize: number;
  markers: MarkerIndex;
  selected: boolean;
  tabbable: boolean;
  onSelect: (eventId: string) => void;
}) {
  const kind = eventPresentation(item);
  const found = markersFor(markers, item);
  const classes = ["tl-row", `tone-${kind.tone}`, `kind-${item.category}`];
  if (selected) classes.push("is-selected");
  for (const marker of found) classes.push(`has-${marker.kind.replace("_", "-")}`);
  const shown = found.slice(0, MAX_ROW_CHIPS);
  return (
    <li
      className="tl-item"
      style={{ top: position * ROW_HEIGHT, height: ROW_HEIGHT }}
      aria-posinset={position + 1}
      aria-setsize={setSize}
    >
      <button
        type="button"
        className={classes.join(" ")}
        data-index={position}
        data-event-id={item.event_id}
        data-type={item.type}
        data-testid="timeline-row"
        aria-current={selected ? "true" : undefined}
        tabIndex={tabbable ? 0 : -1}
        onClick={() => onSelect(item.event_id)}
      >
        <span className="tl-seq mono">#{formatCount(item.sequence)}</span>
        <span className="tl-rail" aria-hidden="true">
          <span className={`tl-node glyph-${kind.glyph}`} />
        </span>
        <span className="tl-body">
          <span className="tl-line">
            <span className="tl-kind">{kind.label}</span>
            <span className="tl-summary">{item.summary}</span>
          </span>
          <span className="tl-line tl-meta">
            <span className="mono tl-time">{formatClock(item.timestamp)}</span>
            {item.path === null ? null : <span className="mono tl-path">{item.path}</span>}
            {shown.map((marker) => (
              <Chip key={`${marker.kind}-${marker.label}`} tone={marker.tone}>
                {marker.label}
              </Chip>
            ))}
            {found.length > shown.length ? (
              <span className="tl-more">+{found.length - shown.length}</span>
            ) : null}
            <span className="tl-evidence">{item.evidence.toUpperCase()}</span>
          </span>
        </span>
      </button>
    </li>
  );
}

function FirstFailureStrip({ report, reportError, onJump }: TimelineViewProps) {
  if (report === null) {
    return reportError === null ? null : (
      <div className="banner tone-warning" role="alert">
        Findings could not be loaded, so failure and recovery markers are not shown.{" "}
        {describeError(reportError).title}.
      </div>
    );
  }
  const first = report.first_observable_failure;
  const recoveries = report.recoveries.total;
  const unresolved = report.unresolved_failures.total;
  return (
    <div className="marker-strip">
      {first === null ? (
        <div className="marker-card tone-muted" data-testid="no-first-failure">
          <span className="marker-card-label">No observable check failure was recorded.</span>
        </div>
      ) : (
        <div className="marker-card tone-danger" data-testid="first-failure-strip">
          <Chip tone="danger">FIRST OBSERVABLE FAILURE</Chip>
          <span className="mono marker-card-seq">#{formatCount(first.sequence)}</span>
          <span className="marker-card-label">{first.summary}</span>
          <button
            type="button"
            className="button button-small"
            onClick={() => onJump({ eventId: first.event_id, sequence: first.sequence })}
          >
            Jump to first failure
          </button>
        </div>
      )}
      <div className="marker-counts" aria-label="Report markers">
        <span className="marker-count tone-success">
          <span className="legend-swatch" aria-hidden="true" />
          {plural(recoveries, "observed recovery", "observed recoveries")}
        </span>
        <span className="marker-count tone-danger">
          <span className="legend-swatch" aria-hidden="true" />
          {formatCount(unresolved)} unresolved in this recording
        </span>
      </div>
    </div>
  );
}

function Toolbar({
  filter,
  onFilter,
  query,
  onQuery,
  shown,
  loaded,
  narrowed,
  hasMore,
}: TimelineViewProps & { shown: number; loaded: number; narrowed: boolean; hasMore: boolean }) {
  return (
    <div className="timeline-toolbar">
      <div className="segmented" role="group" aria-label="Filter loaded events">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            className={f.id === filter ? "segment is-active" : "segment"}
            aria-pressed={f.id === filter}
            onClick={() => onFilter(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <input
        type="search"
        className="search"
        placeholder="Search summary, path, type"
        aria-label="Search loaded events"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        maxLength={200}
        spellCheck={false}
        autoComplete="off"
      />
      <p className="timeline-count" aria-live="polite" data-testid="timeline-count">
        Showing {plural(shown, "loaded event")}
        {narrowed ? ` (of ${formatCount(loaded)} loaded)` : null}
        {narrowed && hasMore ? ". Filters and search cover loaded events only." : null}
      </p>
    </div>
  );
}

function JumpNotice({ jump, onCancelJump, live }: TimelineViewProps) {
  if (jump.status === "idle") return null;
  if (jump.status === "loading") {
    return (
      <div className="banner tone-info" role="status">
        Loading timeline pages up to event #{formatCount(jump.sequence)}…{" "}
        <button type="button" className="button button-small" onClick={onCancelJump}>
          Cancel
        </button>
      </div>
    );
  }
  const text =
    jump.status === "cap"
      ? `Event #${formatCount(jump.sequence)} lies beyond the ${JUMP_PAGE_CAP} pages loaded for this jump. Use “Load more events” to continue.`
      : jump.status === "absent"
        ? live
          ? `Event #${formatCount(jump.sequence)} is not in the timeline yet; the recording is still live.`
          : `Event #${formatCount(jump.sequence)} is not in this session's timeline.`
        : "That event is not in the loaded part of the timeline. Load more events to reach it.";
  return (
    <div className="banner tone-warning" role="status">
      {text}
    </div>
  );
}

function Footer({
  timeline,
  live,
  total,
  loaded,
}: TimelineViewProps & { total: number; loaded: number }) {
  const { loading, error, loadMore } = timeline;
  const { hasMore, pages } = timeline.data;
  if (error !== null) {
    const { title, detail } = describeError(error);
    return (
      <div className="banner tone-warning timeline-footer" role="alert">
        <strong>{title}.</strong> {detail}{" "}
        <button type="button" className="button button-small" onClick={() => void loadMore()}>
          Retry
        </button>
      </div>
    );
  }
  if (pages === 0) return null;
  return (
    <div className="timeline-footer">
      <span className="dim mono" data-testid="timeline-loaded">
        Loaded {formatCount(loaded)} of {plural(total, "recorded event")}
      </span>
      {hasMore ? (
        <button
          type="button"
          className="button"
          onClick={() => void loadMore()}
          disabled={loading}
          aria-busy={loading}
        >
          {loading ? "Loading…" : "Load more events"}
        </button>
      ) : (
        <span className="dim">
          {live ? "All recorded events loaded; new events appear as they are recorded." : "End of the recorded timeline."}
        </span>
      )}
    </div>
  );
}
