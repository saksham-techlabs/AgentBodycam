import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import type { ForensicReport } from "../api/types";
import { SvgTree } from "../components/SvgTree";
import { planReplayClip, type ReplayClipTimeline } from "../lib/clipPlan";
import { detectClipSupport, type ClipSupport } from "../lib/clipRecorder";
import { downloadText, JSON_FILENAME, JSON_MIME, SVG_FILENAME, SVG_MIME, WEBM_FILENAME } from "../lib/download";
import { buildShareCard } from "../lib/shareCard";
import { buildShareModel, buildShareSummary, serializeSummary } from "../lib/shareModel";
import { serializeSvg } from "../lib/svg";
import { isClipBusy, useClipExport, type ClipStatus } from "./useClipExport";

type Format = "card" | "clip" | "json";

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function formatBytes(bytes: number): string {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`;
}

function statusText(status: ClipStatus): string {
  switch (status.phase) {
    case "idle":
      return "Ready";
    case "preparing":
      return "Preparing…";
    case "rendering":
      return `Rendering ${status.percent}%`;
    case "encoding":
      return "Encoding…";
    case "done":
      return `Done · saved ${WEBM_FILENAME} (${formatBytes(status.bytes)})`;
    case "error":
      return status.message;
  }
}

function ClipPanel({ timeline, support, status }: { timeline: ReplayClipTimeline; support: ClipSupport; status: ClipStatus }) {
  const seconds = Math.round(timeline.durationMs / 1000);
  const percent = status.phase === "rendering" ? status.percent : status.phase === "encoding" || status.phase === "done" ? 100 : 0;
  return (
    <div className="export-clip" data-testid="clip-panel">
      <p className="export-clip-lede">Generated replay clip from recorded evidence.</p>
      <dl className="export-clip-facts">
        <div>
          <dt>Format</dt>
          <dd className="mono">
            WebM · {timeline.width}×{timeline.height} · {timeline.fps} fps
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd className="mono">About {seconds} seconds</dd>
        </div>
        <div>
          <dt>Browser support</dt>
          <dd data-testid="clip-support">
            {support.supported ? `Supported · ${support.codec}` : support.reason}
          </dd>
        </div>
      </dl>
      <ol className="export-clip-scenes" aria-label="Clip scenes">
        {timeline.scenes.map((scene) => (
          <li key={scene.startFrame}>{scene.title}</li>
        ))}
      </ol>
      <p className="export-clip-safe">
        Built only from the share card's approved data: counts, event numbers and check results.
        {timeline.live ? " This session is still recording, so the clip is a live snapshot." : ""}
      </p>
      {/* Always laid out, so starting an export never shifts the buttons below it. */}
      <div
        className={status.phase === "idle" ? "export-clip-progress is-idle" : "export-clip-progress"}
        aria-hidden={status.phase === "idle" ? true : undefined}
      >
        <div
          className="export-clip-bar"
          role="progressbar"
          aria-label="Replay clip export progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <span style={{ width: `${percent}%` }} />
        </div>
      </div>
      <p
        className={status.phase === "error" ? "export-clip-status is-error" : "export-clip-status"}
        role="status"
        aria-live="polite"
        data-testid="clip-status"
      >
        {status.phase === "idle" ? "" : statusText(status)}
      </p>
    </div>
  );
}

/**
 * Local, privacy-safe export of a session: a share card (SVG), a generated replay clip
 * (WebM) or a JSON summary. All are generated in the browser from `buildShareModel`, which
 * copies only approved fields; the full report never reaches the rendering code.
 */
export function ExportDialog({ report, onClose }: { report: ForensicReport; onClose: () => void }) {
  const titleId = useId();
  const noteId = useId();
  const previewId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const clipButtonRef = useRef<HTMLButtonElement>(null);
  const backdropPress = useRef(false);
  const [format, setFormat] = useState<Format>("card");

  const model = useMemo(() => buildShareModel(report), [report]);
  const card = useMemo(() => buildShareCard(model), [model]);
  const json = useMemo(() => serializeSummary(buildShareSummary(model)), [model]);
  const timeline = useMemo(() => planReplayClip(model), [model]);
  const support = useMemo(() => detectClipSupport(), []);
  const clip = useClipExport(timeline);
  const busy = isClipBusy(clip.status);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) return;
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    const active = document.activeElement;
    if (active === dialogRef.current || !(active instanceof HTMLElement) || !focusable.includes(active)) {
      // Focus sits on the dialog itself (after a click on plain content) or on a control
      // that was just disabled: Tab must still wrap inside the dialog.
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const cancelClip = (): void => {
    clip.cancel();
    // The Cancel button is about to be disabled; keep focus inside the dialog.
    clipButtonRef.current?.focus();
  };

  return (
    // A click outside closes the dialog only when it both starts and ends on the backdrop,
    // and never while a clip is being exported: losing a running export takes Close or Escape.
    <div
      className="export-backdrop"
      onMouseDown={(e) => {
        backdropPress.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        const outside = backdropPress.current && e.target === e.currentTarget;
        backdropPress.current = false;
        if (outside && !busy) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="export-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={noteId}
        data-testid="export-dialog"
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <header className="export-head">
          <div>
            <p className="eyebrow">Export</p>
            <h2 id={titleId} className="export-title">
              Export replay summary
            </h2>
          </div>
          <button ref={closeRef} type="button" className="button button-ghost" onClick={onClose}>
            Close
          </button>
        </header>

        <div className="export-formats" role="group" aria-label="Export format">
          <button
            type="button"
            className={format === "card" ? "export-format is-active" : "export-format"}
            aria-pressed={format === "card"}
            disabled={busy}
            onClick={() => setFormat("card")}
          >
            Share card
            <span className="export-format-note">SVG · 1200×630</span>
          </button>
          <button
            type="button"
            className={format === "clip" ? "export-format is-active" : "export-format"}
            aria-pressed={format === "clip"}
            onClick={() => setFormat("clip")}
          >
            Replay clip
            <span className="export-format-note">
              WebM · {timeline.width}×{timeline.height}
            </span>
          </button>
          <button
            type="button"
            className={format === "json" ? "export-format is-active" : "export-format"}
            aria-pressed={format === "json"}
            disabled={busy}
            onClick={() => setFormat("json")}
          >
            JSON summary
            <span className="export-format-note">Counts and check results</span>
          </button>
        </div>

        <div className="export-note" id={noteId} data-testid="export-privacy-note">
          <span className="badge-dot tone-success" aria-hidden="true" />
          <div>
            <strong>Public-safe export</strong>
            <p>Source code, paths, command arguments and identifiers are excluded.</p>
          </div>
        </div>

        {format === "card" ? (
          <figure className="export-preview" data-testid="share-card-preview">
            <SvgTree root={card} className="share-card" labelledBy={previewId} />
            <figcaption id={previewId} className="visually-hidden">
              Share card preview
            </figcaption>
          </figure>
        ) : format === "clip" ? (
          <ClipPanel timeline={timeline} support={support} status={clip.status} />
        ) : (
          <pre className="export-json mono" data-testid="json-preview" tabIndex={0} aria-label="JSON summary preview">
            {json}
          </pre>
        )}

        <footer className="export-actions">
          <p className="export-local">Generated in this browser. Nothing is uploaded.</p>
          {format === "card" ? (
            <button
              type="button"
              className="button button-primary"
              onClick={() => downloadText(SVG_FILENAME, SVG_MIME, serializeSvg(card))}
            >
              Download SVG
            </button>
          ) : format === "clip" ? (
            <div className="export-clip-actions">
              {/* The Cancel slot keeps its place while idle, so it never appears under the pointer. */}
              <button
                type="button"
                className={busy ? "button button-ghost" : "button button-ghost is-placeholder"}
                onClick={busy ? cancelClip : undefined}
                disabled={!busy}
                aria-hidden={busy ? undefined : true}
                tabIndex={busy ? undefined : -1}
              >
                Cancel
              </button>
              <button
                ref={clipButtonRef}
                type="button"
                className="button button-primary"
                disabled={!support.supported}
                aria-disabled={busy}
                aria-busy={busy}
                onClick={busy ? undefined : clip.start}
              >
                Export WebM
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="button button-primary"
              onClick={() => downloadText(JSON_FILENAME, JSON_MIME, json)}
            >
              Download JSON summary
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
