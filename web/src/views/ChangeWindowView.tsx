import type { ChangeWindow } from "../api/types";
import { Chip, SeqButton, type JumpHandler } from "../components/Evidence";
import { formatCount, plural } from "../lib/format";

const CHANGE_LABEL = { created: "CREATED", modified: "MODIFIED", deleted: "DELETED" } as const;

/**
 * File events the report found between two sequence numbers. Shown as observed order only:
 * a window says what changed between two events, never that it explains either of them.
 */
export function ChangeWindowView({
  title,
  window,
  onJump,
}: {
  title: string;
  window: ChangeWindow;
  onJump: JumpHandler;
}) {
  const shown = window.file_changes.length;
  return (
    <div className="window" data-testid="change-window">
      <div className="window-head">
        <span className="window-title">{title}</span>
        <span className="window-count mono">{plural(window.file_change_count, "file change")}</span>
      </div>
      <p className="window-desc">{window.description}</p>
      {window.file_change_count === 0 ? null : (
        <>
          <ul className="window-breakdown" aria-label="Change breakdown">
            <li>
              <span className="mono">{formatCount(window.created)}</span> created
            </li>
            <li>
              <span className="mono">{formatCount(window.modified)}</span> modified
            </li>
            <li>
              <span className="mono">{formatCount(window.deleted)}</span> deleted
            </li>
          </ul>
          <ul className="window-files">
            {window.file_changes.map((change) => (
              <li key={change.event_id}>
                <SeqButton sequence={change.sequence} eventId={change.event_id} onJump={onJump} />
                <Chip tone="info">{CHANGE_LABEL[change.change]}</Chip>
                <span className="mono path" title={change.path ?? undefined}>
                  {change.path ?? "(no path recorded)"}
                </span>
              </li>
            ))}
          </ul>
          {window.file_changes_truncated ? (
            <p className="truncation" role="note">
              Showing the first {formatCount(shown)} of {formatCount(window.file_change_count)}{" "}
              file changes observed in this window.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
