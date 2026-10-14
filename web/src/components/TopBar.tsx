import type { ReactNode } from "react";

import { formatUtcTime } from "../lib/format";

interface TopBarProps {
  title: ReactNode;
  meta?: ReactNode;
  onRefresh: () => void;
  refreshing: boolean;
  updatedAt: number | null;
  autoRefresh?: boolean;
}

export function TopBar({ title, meta, onRefresh, refreshing, updatedAt, autoRefresh }: TopBarProps) {
  return (
    <header className="topbar">
      <div className="topbar-title">{title}</div>
      <div className="topbar-meta">
        {meta}
        {autoRefresh === true ? (
          <span className="topbar-note" title="Refreshing while the recording is live">
            <span className="badge-dot pulse tone-info" aria-hidden="true" /> Auto-refresh
          </span>
        ) : null}
        {updatedAt === null ? null : (
          <span className="topbar-note mono">Updated {formatUtcTime(updatedAt)} UTC</span>
        )}
        <button
          type="button"
          className="button"
          onClick={onRefresh}
          aria-busy={refreshing}
          disabled={refreshing}
        >
          <svg
            className={refreshing ? "icon spin" : "icon"}
            viewBox="0 0 16 16"
            aria-hidden="true"
            focusable="false"
          >
            <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" />
            <path d="M13.5 2.5v3h-3" />
          </svg>
          Refresh
        </button>
      </div>
    </header>
  );
}
