import { useEffect, useRef } from "react";

export const LIVE_POLL_INTERVAL_MS = 2500;

/**
 * Run `task` every `intervalMs` while `enabled`: a simple, bounded refresh for live
 * sessions (no WebSockets or server push).
 *
 * - The next run is scheduled only after the previous one settled, so requests never
 *   pile up.
 * - Nothing runs while the page is hidden; polling resumes when it is visible again.
 * - Disabling, changing the interval or unmounting stops it immediately.
 */
export function usePolling(
  task: () => Promise<unknown>,
  enabled: boolean,
  intervalMs: number = LIVE_POLL_INTERVAL_MS,
): void {
  const taskRef = useRef(task);
  taskRef.current = task;

  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    let running = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = (): void => {
      if (stopped || running || timer !== undefined || document.hidden) return;
      timer = setTimeout(tick, intervalMs);
    };
    const tick = (): void => {
      timer = undefined;
      if (stopped || document.hidden) return;
      running = true;
      void taskRef.current()
        .catch(() => undefined)
        .finally(() => {
          running = false;
          schedule();
        });
    };
    const onVisibilityChange = (): void => {
      if (document.hidden) {
        clearTimeout(timer);
        timer = undefined;
      } else {
        schedule();
      }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    schedule();
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [enabled, intervalMs]);
}
