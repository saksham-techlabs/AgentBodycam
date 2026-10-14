import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import type { ForensicReport } from "../api/types";
import { SvgTree } from "../components/SvgTree";
import { downloadText, JSON_FILENAME, JSON_MIME, SVG_FILENAME, SVG_MIME } from "../lib/download";
import { buildShareCard } from "../lib/shareCard";
import { buildShareModel, buildShareSummary, serializeSummary } from "../lib/shareModel";
import { serializeSvg } from "../lib/svg";

type Format = "card" | "json";

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Local, privacy-safe export of a session: a share card (SVG) or a JSON summary. Both are
 * generated in the browser from `buildShareModel`, which copies only approved fields; the
 * full report never reaches the rendering code.
 */
export function ExportDialog({ report, onClose }: { report: ForensicReport; onClose: () => void }) {
  const titleId = useId();
  const noteId = useId();
  const previewId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [format, setFormat] = useState<Format>("card");

  const model = useMemo(() => buildShareModel(report), [report]);
  const card = useMemo(() => buildShareCard(model), [model]);
  const json = useMemo(() => serializeSummary(buildShareSummary(model)), [model]);

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
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="export-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={dialogRef}
        className="export-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={noteId}
        data-testid="export-dialog"
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
            onClick={() => setFormat("card")}
          >
            Share card
            <span className="export-format-note">SVG · 1200×630</span>
          </button>
          <button
            type="button"
            className={format === "json" ? "export-format is-active" : "export-format"}
            aria-pressed={format === "json"}
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
