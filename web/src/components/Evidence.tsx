import type { ReactNode } from "react";

import { formatCount } from "../lib/format";
import type { Tone } from "../lib/sessionState";

/** A reference from a finding to a timeline event. */
export interface EventRef {
  eventId: string;
  sequence: number | null;
}

export type JumpHandler = (ref: EventRef) => void;

/** Every current finding is observed evidence; there is no inference UI. */
export function ObservedBadge() {
  return (
    <span className="evidence-badge" title="Directly observed in the recorded evidence">
      OBSERVED
    </span>
  );
}

export function Chip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`chip tone-${tone}`}>{children}</span>;
}

/** `#14` as a button that opens the event in the timeline. */
export function SeqButton({
  sequence,
  eventId,
  onJump,
  label,
}: {
  sequence: number;
  eventId: string;
  onJump: JumpHandler;
  label?: string;
}) {
  return (
    <button
      type="button"
      className="seq-link mono"
      onClick={() => onJump({ eventId, sequence })}
      aria-label={`${label ?? "Event"} #${sequence}: show in timeline`}
    >
      #{formatCount(sequence)}
    </button>
  );
}

export function JumpButton({ eventRef, onJump }: { eventRef: EventRef; onJump: JumpHandler }) {
  return (
    <button
      type="button"
      className="button button-small"
      onClick={() => onJump(eventRef)}
      aria-label={
        eventRef.sequence === null
          ? "Jump to event in timeline"
          : `Jump to event #${eventRef.sequence} in timeline`
      }
    >
      Jump to event
    </button>
  );
}

export function SectionHead({
  id,
  title,
  count,
  children,
}: {
  id: string;
  title: string;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <div className="section-head">
      <h2 id={id} className="section-title">
        {title}
        {count === undefined ? null : <span className="section-count mono">{formatCount(count)}</span>}
      </h2>
      {children}
    </div>
  );
}

/** "Showing 200 of 431 …" for a bounded backend list that was cut. */
export function TruncationNote({ shown, total, noun }: { shown: number; total: number; noun: string }) {
  if (total <= shown) return null;
  return (
    <p className="truncation" role="note">
      Showing {formatCount(shown)} of {formatCount(total)} {noun}. The report lists at most{" "}
      {formatCount(shown)}; the count covers all of them.
    </p>
  );
}
