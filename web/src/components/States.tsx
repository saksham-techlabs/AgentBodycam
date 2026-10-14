import type { ReactNode } from "react";

import { describeError } from "../lib/errorMessages";

export function LoadingState({ label, rows = 4 }: { label: string; rows?: number }) {
  return (
    <div className="state-loading" role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton-row" aria-hidden="true">
          <span className="skeleton w-12" />
          <span className="skeleton w-40" />
          <span className="skeleton w-24" />
          <span className="skeleton w-10" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="state-panel" role="status">
      <div className="state-icon" aria-hidden="true">
        <span />
      </div>
      <h2>{title}</h2>
      <div className="state-detail">{children}</div>
    </div>
  );
}

export function ErrorState({ error, action }: { error: unknown; action?: ReactNode }) {
  const { title, detail } = describeError(error);
  return (
    <div className="state-panel tone-danger" role="alert">
      <div className="state-icon" aria-hidden="true">
        <span />
      </div>
      <h2>{title}</h2>
      <p className="state-detail">{detail}</p>
      {action === undefined ? null : <div className="state-actions">{action}</div>}
    </div>
  );
}

/** A non-blocking notice above data that is still shown (e.g. a failed refresh). */
export function ErrorBanner({ error }: { error: unknown }) {
  const { title, detail } = describeError(error);
  return (
    <div className="banner tone-warning" role="alert">
      <strong>{title}.</strong> {detail} Showing the last data received.
    </div>
  );
}
