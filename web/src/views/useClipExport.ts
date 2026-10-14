import { useCallback, useEffect, useRef, useState } from "react";

import type { ReplayClipTimeline } from "../lib/clipPlan";
import {
  clipErrorMessage,
  detectClipSupport,
  recordReplayClip,
  SAVE_FAILED_MESSAGE,
  UNSUPPORTED_MESSAGE,
  type ClipProgress,
} from "../lib/clipRecorder";
import { downloadBlob, VIDEO_REVOKE_DELAY_MS, WEBM_FILENAME } from "../lib/download";

export type ClipStatus =
  | { phase: "idle" }
  | ClipProgress
  | { phase: "done"; bytes: number }
  | { phase: "error"; message: string };

export function isClipBusy(status: ClipStatus): boolean {
  return status.phase === "preparing" || status.phase === "rendering" || status.phase === "encoding";
}

/**
 * One replay clip export at a time. Starting while an export runs does nothing; cancelling,
 * or unmounting the dialog, aborts the run, which stops rendering, the recorder and the
 * stream tracks before anything else happens.
 *
 * Every run has its own number. Progress, results and errors of a run apply only while it
 * is still the latest run, so a late callback of a cancelled run can never overwrite (or
 * reset) the status of the run that replaced it.
 */
export function useClipExport(timeline: ReplayClipTimeline): {
  status: ClipStatus;
  start: () => void;
  cancel: () => void;
} {
  const [status, setStatus] = useState<ClipStatus>({ phase: "idle" });
  const running = useRef<AbortController | null>(null);
  const runId = useRef(0);

  useEffect(
    () => () => {
      runId.current += 1;
      running.current?.abort();
      running.current = null;
    },
    [],
  );

  const start = useCallback(() => {
    if (running.current !== null) return;
    const id = ++runId.current;
    const current = (): boolean => runId.current === id;
    const support = detectClipSupport();
    if (!support.supported) {
      setStatus({ phase: "error", message: UNSUPPORTED_MESSAGE });
      return;
    }
    const controller = new AbortController();
    running.current = controller;
    const { signal } = controller;
    setStatus({ phase: "preparing" });
    recordReplayClip(timeline, {
      mimeType: support.mimeType,
      signal,
      onProgress: (progress) => {
        if (current() && !signal.aborted) setStatus(progress);
      },
    }).then(
      (blob) => {
        if (running.current === controller) running.current = null;
        if (!current() || signal.aborted) return;
        try {
          downloadBlob(WEBM_FILENAME, blob, VIDEO_REVOKE_DELAY_MS);
        } catch {
          setStatus({ phase: "error", message: SAVE_FAILED_MESSAGE });
          return;
        }
        setStatus({ phase: "done", bytes: blob.size });
      },
      (error: unknown) => {
        if (running.current === controller) running.current = null;
        if (!current() || signal.aborted) return;
        const message = clipErrorMessage(error);
        setStatus(message === null ? { phase: "idle" } : { phase: "error", message });
      },
    );
  }, [timeline]);

  const cancel = useCallback(() => {
    const controller = running.current;
    if (controller === null) return;
    runId.current += 1;
    running.current = null;
    controller.abort();
    setStatus({ phase: "idle" });
  }, []);

  return { status, start, cancel };
}
