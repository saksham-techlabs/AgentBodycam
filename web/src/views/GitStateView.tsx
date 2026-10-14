import type { ReactNode } from "react";

import type { CheckState, CommandSummary, ForensicReport, GitStateSummary } from "../api/types";
import { Chip, ObservedBadge, SectionHead, SeqButton, TruncationNote, type JumpHandler } from "../components/Evidence";
import { formatCount, plural } from "../lib/format";

/** The last recorded Git snapshot, final check results and command counts of the recording. */
export function GitStateView({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  return (
    <div className="final-view">
      <GitPanel git={report.latest_git_state} onJump={onJump} />
      <ChecksPanel report={report} onJump={onJump} />
      <CommandsPanel summary={report.command_summary} />
    </div>
  );
}

function GitPanel({ git, onJump }: { git: GitStateSummary | null; onJump: JumpHandler }) {
  return (
    <section className="panel" aria-labelledby="git-title" data-testid="git-state">
      <SectionHead id="git-title" title="Last recorded Git state">
        <ObservedBadge />
      </SectionHead>
      {git === null ? (
        <p className="finding-empty">No Git snapshot was recorded in this session.</p>
      ) : (
        <>
          <p className="dim section-desc">
            {git.description} Recorded at event{" "}
            <SeqButton sequence={git.sequence} eventId={git.event_id} onJump={onJump} label="Git snapshot" />
            {" · "}
            {plural(git.snapshot_count, "snapshot")} recorded in total.
          </p>
          {git.repository ? <RepositoryFacts git={git} /> : <NoRepository git={git} />}
        </>
      )}
    </section>
  );
}

function NoRepository({ git }: { git: GitStateSummary }) {
  return (
    <dl className="git-grid">
      <GitFact label="Repository" value={<Chip tone="neutral">NO REPOSITORY</Chip>} />
      <GitFact label="Recorded reason" value={git.unavailable_reason ?? "Not recorded"} mono />
    </dl>
  );
}

function RepositoryFacts({ git }: { git: GitStateSummary }) {
  const headState =
    git.unborn === true ? "UNBORN" : git.detached === true ? "DETACHED" : null;
  return (
    <dl className="git-grid">
      <GitFact label="Repository" value="Yes" />
      <GitFact
        label="Branch"
        value={
          <>
            <span className="mono">{git.branch ?? "(none)"}</span>{" "}
            {headState === null ? null : <Chip tone="warning">{headState}</Chip>}
          </>
        }
      />
      <GitFact label="HEAD" value={git.head ?? (git.unborn === true ? "No commits yet" : "Not recorded")} mono={git.head !== null} />
      <GitFact
        label="Working tree"
        value={
          git.state === null ? (
            "Not recorded"
          ) : git.state === "clean" ? (
            <Chip tone="success">CLEAN</Chip>
          ) : (
            <Chip tone="warning">DIRTY</Chip>
          )
        }
      />
      <GitFact label="Staged" value={countText(git.staged)} mono />
      <GitFact label="Unstaged" value={countText(git.unstaged)} mono />
      <GitFact label="Untracked" value={countText(git.untracked)} mono />
      <GitFact label="Conflicted" value={countText(git.conflicted)} mono />
      <GitFact label="Upstream" value={git.upstream ?? "None recorded"} mono={git.upstream !== null} />
      <GitFact
        label="Ahead / behind"
        value={git.ahead === null && git.behind === null ? "—" : `${countText(git.ahead)} / ${countText(git.behind)}`}
        mono
      />
    </dl>
  );
}

function countText(value: number | null): string {
  return value === null ? "—" : formatCount(value);
}

function GitFact({
  label,
  value,
  mono,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="git-fact">
      <dt>{label}</dt>
      <dd className={mono === true ? "mono" : undefined}>{value}</dd>
    </div>
  );
}

function ChecksPanel({ report, onJump }: { report: ForensicReport; onJump: JumpHandler }) {
  const { items, total } = report.final_check_states;
  return (
    <section className="panel panel-flush" aria-labelledby="checks-title">
      <div className="panel-pad">
        <SectionHead id="checks-title" title="Final check states" count={total}>
          <ObservedBadge />
        </SectionHead>
        <p className="dim section-desc">The latest recorded result of each check in this recording.</p>
      </div>
      {items.length === 0 ? (
        <p className="finding-empty panel-pad">No check results were recorded.</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Latest</th>
                <th scope="col" className="num">Attempts</th>
                <th scope="col" className="num">Passed</th>
                <th scope="col" className="num">Failed</th>
                <th scope="col" className="num">Latest event</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <CheckRow key={c.latest_event_id} check={c} onJump={onJump} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="panel-pad">
        <TruncationNote shown={items.length} total={total} noun="checks" />
      </div>
    </section>
  );
}

function CheckRow({ check: c, onJump }: { check: CheckState; onJump: JumpHandler }) {
  return (
    <tr data-testid="check-state">
      <td>
        <span className="mono">{c.display_name}</span>
        {c.identity_established ? null : (
          <>
            {" "}
            <Chip tone="neutral">UNGROUPED</Chip>
            <span className="cell-note">
              This run could not safely be matched with other runs of the same check.
            </span>
          </>
        )}
      </td>
      <td>
        {c.latest_result === "passed" ? (
          <Chip tone="success">PASS</Chip>
        ) : (
          <Chip tone="danger">FAIL</Chip>
        )}
      </td>
      <td className="mono num">{formatCount(c.attempts)}</td>
      <td className="mono num">{formatCount(c.passed)}</td>
      <td className="mono num">{formatCount(c.failed)}</td>
      <td className="num">
        <SeqButton sequence={c.latest_sequence} eventId={c.latest_event_id} onJump={onJump} />
      </td>
    </tr>
  );
}

function CommandsPanel({ summary }: { summary: CommandSummary }) {
  const rows: [string, number][] = [
    ["Commands started", summary.commands_started],
    ["Commands finished", summary.commands_finished],
    ["Incomplete commands", summary.incomplete_commands],
    ["Checks passed", summary.checks_passed],
    ["Checks failed", summary.checks_failed],
    ["Checks ungrouped", summary.checks_ungrouped],
    ["Check signatures", summary.check_signatures],
  ];
  return (
    <section className="panel" aria-labelledby="cmd-title" data-testid="command-summary">
      <SectionHead id="cmd-title" title="Command summary" />
      <dl className="stat-strip stat-strip-7">
        {rows.map(([label, value]) => (
          <div key={label} className="stat">
            <dt>{label}</dt>
            <dd className="mono">{formatCount(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
