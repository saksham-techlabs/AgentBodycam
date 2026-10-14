import { useEffect, useState, type ReactNode } from "react";

import type { TimelineItem } from "../api/types";
import { Chip } from "../components/Evidence";
import { formatCount, formatUtc } from "../lib/format";
import { eventPresentation, formatClock, type Marker } from "../lib/timeline";

/**
 * The safe fields of one timeline event, exactly as the API exposes them. There is no
 * payload, file content or hash to show: the API never sends any.
 */
export function EventDetail({
  item,
  markers,
  onClose,
}: {
  item: TimelineItem;
  markers: readonly Marker[];
  onClose: () => void;
}) {
  const kind = eventPresentation(item);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setCopied(false);
  }, [item.event_id]);

  const copy = (): void => {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (clipboard === undefined) return;
    clipboard.writeText(item.event_id).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  };

  return (
    <aside
      className="detail"
      aria-labelledby="detail-title"
      data-testid="event-detail"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="detail-head">
        <div>
          <p className="eyebrow">Event</p>
          <h2 id="detail-title" className="detail-title mono">
            #{formatCount(item.sequence)}
          </h2>
        </div>
        <button type="button" className="button button-small" onClick={onClose} aria-label="Close event details">
          Close
        </button>
      </div>
      <div className="detail-kind">
        <Chip tone={kind.tone}>{kind.label}</Chip>
        <span className="evidence-badge">{item.evidence.toUpperCase()}</span>
      </div>
      <p className="detail-summary">{item.summary}</p>
      {markers.length === 0 ? null : (
        <ul className="detail-markers" aria-label="Report findings at this event">
          {markers.map((marker) => (
            <li key={`${marker.kind}-${marker.label}`} className={`tone-${marker.tone}`}>
              <Chip tone={marker.tone}>{marker.label}</Chip>
              <span>{marker.detail}</span>
            </li>
          ))}
        </ul>
      )}
      <dl className="detail-fields">
        <Field label="Sequence" mono>
          {formatCount(item.sequence)}
        </Field>
        <Field label="Event ID" mono>
          <span className="detail-id">{item.event_id}</span>
          <button type="button" className="button button-small" onClick={copy}>
            {copied ? "Copied" : "Copy event ID"}
          </button>
        </Field>
        <Field label="Time (UTC)" mono>
          {formatUtc(item.timestamp)} <span className="dim">({formatClock(item.timestamp)})</span>
        </Field>
        <Field label="Type" mono>
          {item.type}
        </Field>
        <Field label="Category" mono>
          {item.category}
        </Field>
        <Field label="Source" mono>
          {item.source}
        </Field>
        <Field label="Path" mono>
          {item.path ?? <span className="dim">None</span>}
        </Field>
        <Field label="Command ID" mono>
          {item.command_id ?? <span className="dim">None</span>}
        </Field>
        <Field label="Evidence" mono>
          {item.evidence}
        </Field>
      </dl>
      <p className="footnote">
        Order is by sequence number; times are display metadata. No payload or file content
        is recorded in this view.
      </p>
    </aside>
  );
}

function Field({ label, mono, children }: { label: string; mono?: boolean; children: ReactNode }) {
  return (
    <div className="detail-field">
      <dt>{label}</dt>
      <dd className={mono === true ? "mono" : undefined}>{children}</dd>
    </div>
  );
}
