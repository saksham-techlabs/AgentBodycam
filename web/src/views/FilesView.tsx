import type { FileChange, FileChangeSummary } from "../api/types";
import { Chip, ObservedBadge, SectionHead, SeqButton, TruncationNote, type JumpHandler } from "../components/Evidence";
import { formatCount } from "../lib/format";

const CHANGE_LABEL: Record<FileChange, string> = {
  created: "created",
  modified: "modified",
  deleted: "deleted",
};

/**
 * Files observed changing during the recording, from the report only. Nothing here reads
 * the project: "present" means present at the last recorded observation, not now.
 */
export function FilesView({ summary, onJump }: { summary: FileChangeSummary; onJump: JumpHandler }) {
  const { items, total } = summary.paths;
  return (
    <div className="files-view">
      <section className="panel" aria-labelledby="files-title">
        <SectionHead id="files-title" title="Observed during this recording">
          <ObservedBadge />
        </SectionHead>
        <p className="dim section-desc">{summary.description}</p>
        <dl className="stat-strip">
          <Stat label="File events" value={summary.total_events} />
          <Stat label="Created" value={summary.created_events} />
          <Stat label="Modified" value={summary.modified_events} />
          <Stat label="Deleted" value={summary.deleted_events} />
          <Stat label="Unique paths" value={summary.unique_paths} />
        </dl>
      </section>

      <section className="panel panel-flush" aria-labelledby="paths-title">
        <div className="panel-pad">
          <SectionHead id="paths-title" title="Paths" count={total} />
        </div>
        {items.length === 0 ? (
          <p className="finding-empty panel-pad">No file changes were observed in this recording.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Path (project-relative)</th>
                  <th scope="col" className="num">Change events</th>
                  <th scope="col">First observed</th>
                  <th scope="col">Last observed</th>
                  <th scope="col" className="num">Last event</th>
                  <th scope="col">At last observation</th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.path} data-testid="path-row">
                    <td className="mono path-cell" title={p.path}>
                      {p.path}
                    </td>
                    <td className="mono num">{formatCount(p.change_events)}</td>
                    <td>{CHANGE_LABEL[p.first_change]}</td>
                    <td>{CHANGE_LABEL[p.last_change]}</td>
                    <td className="num">
                      <SeqButton sequence={p.last_sequence} eventId={p.last_event_id} onJump={onJump} />
                    </td>
                    <td>
                      {p.present_at_last_observation ? (
                        <Chip tone="info">PRESENT</Chip>
                      ) : (
                        <Chip tone="neutral">DELETED</Chip>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="panel-pad">
          <TruncationNote shown={items.length} total={total} noun="paths" />
        </div>
      </section>
      <p className="footnote">
        Paths and change kinds come from recorded file events. File contents are not
        recorded or shown, and this is not a view of the current file system.
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd className="mono">{formatCount(value)}</dd>
    </div>
  );
}
