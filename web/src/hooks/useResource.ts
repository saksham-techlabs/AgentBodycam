import { useCallback, useEffect, useRef, useState } from "react";

import { isAbort } from "../api/errors";

export type Loader<T> = (signal: AbortSignal) => Promise<T>;

export interface Resource<T> {
  /** Latest successful result for the current key (kept while refreshing). */
  data: T | null;
  /** Error of the latest attempt; cleared by the next success. */
  error: unknown;
  /** First load for this key in progress (nothing to show yet). */
  loading: boolean;
  /** A refresh of already-loaded data in progress. */
  refreshing: boolean;
  /** `Date.now()` of the latest success. */
  updatedAt: number | null;
  /** Load again, keeping current data on screen. Resolves when settled; never rejects. */
  reload: () => Promise<void>;
}

interface State<T> {
  key: string;
  data: T | null;
  error: unknown;
  pending: boolean;
  updatedAt: number | null;
}

/**
 * Load data for `key` and keep it fresh on demand. Changing `key` drops the previous
 * data and aborts its request; so does unmounting. At most one request is in flight: a
 * reload aborts the previous one.
 */
export function useResource<T>(key: string, loader: Loader<T>): Resource<T> {
  const [state, setState] = useState<State<T>>(() => ({
    key,
    data: null,
    error: null,
    pending: true,
    updatedAt: null,
  }));
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const keyRef = useRef(key);
  keyRef.current = key;
  const controllerRef = useRef<AbortController | null>(null);

  const run = useCallback(async (): Promise<void> => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const runKey = keyRef.current;
    setState((s) => (s.key === runKey ? { ...s, pending: true } : s));
    try {
      const data = await loaderRef.current(controller.signal);
      if (controller.signal.aborted) return;
      setState({ key: runKey, data, error: null, pending: false, updatedAt: Date.now() });
    } catch (error) {
      if (controller.signal.aborted || isAbort(error)) return;
      setState((s) =>
        s.key === runKey
          ? { ...s, error, pending: false }
          : { key: runKey, data: null, error, pending: false, updatedAt: null },
      );
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }, []);

  useEffect(() => {
    setState((s) =>
      s.key === key ? s : { key, data: null, error: null, pending: true, updatedAt: null },
    );
    void run();
    return () => controllerRef.current?.abort();
  }, [key, run]);

  const current = state.key === key;
  const data = current ? state.data : null;
  const pending = current ? state.pending : true;
  return {
    data,
    error: current ? state.error : null,
    loading: pending && data === null,
    refreshing: pending && data !== null,
    updatedAt: current ? state.updatedAt : null,
    reload: run,
  };
}
