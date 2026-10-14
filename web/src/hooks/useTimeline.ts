import { useCallback, useEffect, useRef, useState } from "react";

import { TIMELINE_MAX_LIMIT, TIMELINE_PAGE_SIZE, type ReplayApiClient } from "../api/client";
import { ApiError, isAbort } from "../api/errors";
import { InvalidResponseError } from "../api/validate";
import { EMPTY_TIMELINE, appendPage, lastSequence, type LoadedTimeline } from "../lib/timeline";

/** At most this many pages are fetched to reach one referenced event (25,000 events). */
export const JUMP_PAGE_CAP = 50;

export type LoadUntilResult = "reached" | "end" | "cap" | "cancelled" | "error";
export type LoadMoreResult = "loaded" | "end" | "cancelled" | "error";

type FetchResult = "loaded" | "cancelled" | "error";

export interface TimelineResource {
  data: LoadedTimeline;
  /** The first page was requested. */
  started: boolean;
  /** A page request is in flight. */
  loading: boolean;
  /** Error of the latest page request; cleared by the next success. */
  error: unknown;
  /** Request the first page, once. */
  start: () => void;
  /** Request the next page if there is one; resolves with the outcome and the loaded data. */
  loadMore: () => Promise<{ result: LoadMoreResult; data: LoadedTimeline }>;
  /** Live sessions: fetch events recorded after the loaded end, if everything is loaded. */
  poll: () => Promise<void>;
  /** Load pages until `sequence` is loaded, the log ends, the cap is hit or `cancel()`. */
  loadUntil: (sequence: number) => Promise<{ result: LoadUntilResult; data: LoadedTimeline }>;
  cancelLoadUntil: () => void;
}

interface State {
  data: LoadedTimeline;
  started: boolean;
  loading: boolean;
  error: unknown;
}

/**
 * The paginated timeline of one session, loaded on demand through the `next_after_sequence`
 * cursor only. At most one page request is in flight; every page is validated against what
 * is loaded (`appendPage`) before it is shown. Unmounting aborts the request in flight.
 */
export function useTimeline(client: ReplayApiClient, sessionId: string): TimelineResource {
  const [state, setState] = useState<State>({
    data: EMPTY_TIMELINE,
    started: false,
    loading: false,
    error: null,
  });
  const dataRef = useRef<LoadedTimeline>(EMPTY_TIMELINE);
  const errorRef = useRef<unknown>(null);
  const inflightRef = useRef<Promise<FetchResult> | null>(null);
  const controllerRef = useRef(new AbortController());
  const jumpRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    return () => controller.abort();
  }, []);

  /** Fetch the page after the cursor; resolves "loaded" when a page was appended. */
  const fetchNext = useCallback(
    (limit: number): Promise<FetchResult> => {
      if (inflightRef.current !== null) return inflightRef.current;
      const signal = controllerRef.current.signal;
      setState((s) => ({ ...s, started: true, loading: true }));
      const run = (async () => {
        try {
          const page = await client.timeline(
            sessionId,
            { afterSequence: dataRef.current.cursor, limit },
            signal,
          );
          const data = appendPage(dataRef.current, page);
          if (signal.aborted) return "cancelled";
          dataRef.current = data;
          errorRef.current = null;
          setState({ data, started: true, loading: false, error: null });
          return "loaded";
        } catch (caught) {
          if (signal.aborted || isAbort(caught)) return "cancelled";
          const error = caught instanceof InvalidResponseError ? new ApiError("invalid_response") : caught;
          errorRef.current = error;
          setState((s) => ({ ...s, loading: false, error }));
          return "error";
        } finally {
          inflightRef.current = null;
        }
      })();
      inflightRef.current = run;
      return run;
    },
    [client, sessionId],
  );

  const start = useCallback(() => {
    if (dataRef.current.pages === 0 && inflightRef.current === null && errorRef.current === null) {
      void fetchNext(TIMELINE_PAGE_SIZE);
    }
  }, [fetchNext]);

  const loadMore = useCallback(async () => {
    if (dataRef.current.pages > 0 && !dataRef.current.hasMore) {
      return { result: "end" as const, data: dataRef.current };
    }
    const result = await fetchNext(TIMELINE_PAGE_SIZE);
    return { result, data: dataRef.current };
  }, [fetchNext]);

  const poll = useCallback(async () => {
    const data = dataRef.current;
    const error = errorRef.current;
    if (data.pages === 0 || data.hasMore) return;
    if (error instanceof ApiError && error.kind === "invalid_response") return;
    await fetchNext(TIMELINE_PAGE_SIZE);
  }, [fetchNext]);

  const loadUntil = useCallback(
    async (sequence: number) => {
      const token = ++jumpRef.current;
      let fetched = 0;
      for (;;) {
        const data = dataRef.current;
        if (lastSequence(data) >= sequence) return { result: "reached" as const, data };
        if (data.pages > 0 && !data.hasMore) return { result: "end" as const, data };
        if (fetched >= JUMP_PAGE_CAP) return { result: "cap" as const, data };
        const fetchedPage = await fetchNext(TIMELINE_MAX_LIMIT);
        fetched += 1;
        if (token !== jumpRef.current || controllerRef.current.signal.aborted) {
          return { result: "cancelled" as const, data: dataRef.current };
        }
        if (fetchedPage !== "loaded") return { result: "error" as const, data: dataRef.current };
      }
    },
    [fetchNext],
  );

  const cancelLoadUntil = useCallback(() => {
    jumpRef.current += 1;
  }, []);

  return {
    data: state.data,
    started: state.started,
    loading: state.loading,
    error: state.error,
    start,
    loadMore,
    poll,
    loadUntil,
    cancelLoadUntil,
  };
}
