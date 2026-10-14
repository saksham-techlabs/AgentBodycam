import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayApiClient } from "../api/client";
import type { ForensicReport, GitStateSummary } from "../api/types";
import { App } from "../app/App";
import { CAUSAL, eventId, item, longTimeline } from "../test/fixtures";
import {
  CRASHED_ID,
  CREATED_ID,
  FakeServer,
  STOPPED_ID,
  TOKEN,
  UNKNOWN_ID,
} from "../test/fakeServer";

let server: FakeServer;

beforeEach(() => {
  server = new FakeServer();
});

async function openSession(sessionId: string, tab?: RegExp) {
  window.history.replaceState(null, "", `/session/${sessionId}`);
  const view = render(<App client={new ReplayApiClient(server.fetch)} />);
  await screen.findByRole("heading", { name: sessionId });
  if (tab !== undefined) fireEvent.click(screen.getByRole("tab", { name: tab }));
  return view;
}

async function openTimeline(sessionId = STOPPED_ID) {
  await openSession(sessionId, /^Timeline/);
  await screen.findAllByTestId("timeline-row");
}

function rows(): HTMLElement[] {
  return screen.queryAllByTestId("timeline-row");
}

function row(sequence: number): HTMLElement {
  const label = `#${sequence.toLocaleString("en-US")}`;
  const found = rows().find((r) => r.querySelector(".tl-seq")?.textContent === label);
  if (found === undefined) throw new Error(`row #${sequence} not rendered`);
  return found;
}

function sequences(): number[] {
  return rows().map((r) => Number(r.querySelector(".tl-seq")?.textContent?.slice(1).replace(",", "")));
}

function withReport(sessionId: string, mutate: (report: ForensicReport) => void): void {
  const report = server.reportFor(sessionId)!;
  mutate(report);
  server.reports.set(sessionId, report);
}

// -- tabs ----------------------------------------------------------------------------------

describe("session tabs", () => {
  it("are all available and keyboard operable", async () => {
    await openSession(STOPPED_ID);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace(/\d+$/, ""))).toEqual([
      "Overview",
      "Timeline",
      "Findings",
      "Files",
      "Git / Final State",
    ]);
    for (const tab of tabs) expect(tab).toBeEnabled();
    const overview = screen.getByRole("tab", { name: /^Overview/ });
    expect(overview).toHaveAttribute("aria-selected", "true");
    expect(overview).toHaveAttribute("tabindex", "0");

    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowRight" });
    const timeline = screen.getByRole("tab", { name: /^Timeline/ });
    expect(timeline).toHaveAttribute("aria-selected", "true");
    expect(timeline).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "tab-timeline");

    fireEvent.keyDown(timeline, { key: "End" });
    expect(screen.getByRole("tab", { name: /^Git \/ Final State/ })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: /^Overview/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: /^Git/ })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(screen.getByRole("tab", { name: /^Overview/ })).toHaveFocus();
  });

  it("keep only the session ID in the URL", async () => {
    await openTimeline();
    fireEvent.click(screen.getByRole("tab", { name: /^Findings/ }));
    expect(window.location.pathname).toBe(`/session/${STOPPED_ID}`);
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain(TOKEN);
  });
});

// -- timeline ------------------------------------------------------------------------------

describe("timeline", () => {
  it("renders events in strict sequence order", async () => {
    await openTimeline();
    expect(sequences()).toEqual(Array.from({ length: 21 }, (_, i) => i + 1));
    expect(screen.getByTestId("timeline-count")).toHaveTextContent("Showing 21 loaded events");
    expect(screen.getByTestId("timeline-loaded")).toHaveTextContent("Loaded 21 of 21 recorded events");
    expect(screen.getByText("End of the recorded timeline.")).toBeInTheDocument();
  });

  it("styles each event kind with a text label, not colour alone", async () => {
    await openTimeline();
    const expectations: [number, string, string][] = [
      [1, "SESSION START", "tone-neutral"],
      [2, "GIT", "tone-neutral"],
      [3, "COMMAND STARTED", "tone-neutral"],
      [4, "COMMAND FINISHED", "tone-neutral"],
      [5, "CHECK FAILED", "tone-danger"],
      [6, "FILE MODIFIED", "tone-info"],
      [10, "FILE CREATED", "tone-info"],
      [11, "FILE DELETED", "tone-info"],
      [14, "CHECK PASSED", "tone-success"],
      [18, "ERROR", "tone-warning"],
      [21, "SESSION STOP", "tone-neutral"],
    ];
    for (const [sequence, label, tone] of expectations) {
      const r = row(sequence);
      expect(r.querySelector(".tl-kind")).toHaveTextContent(label);
      expect(r).toHaveClass(tone);
      expect(r).toHaveTextContent("OBSERVED");
    }
    expect(row(11)).not.toHaveClass("tone-danger");
    expect(row(6)).toHaveTextContent("src/app.py");
    expect(row(6)).toHaveTextContent("02:15:06.412");
  });

  it("highlights the first observable failure and jumps to it", async () => {
    await openTimeline();
    expect(row(5)).toHaveClass("has-first-failure");
    expect(row(5)).toHaveTextContent("FIRST OBSERVABLE FAILURE");
    expect(rows().filter((r) => r.classList.contains("has-first-failure"))).toHaveLength(1);
    const strip = screen.getByTestId("first-failure-strip");
    expect(strip).toHaveTextContent("#5");
    expect(strip).toHaveTextContent("pytest failed (exit 1)");

    fireEvent.click(within(strip).getByRole("button", { name: "Jump to first failure" }));
    const detail = await screen.findByTestId("event-detail");
    expect(detail).toHaveTextContent("#5");
    expect(row(5)).toHaveAttribute("aria-current", "true");
    await waitFor(() => expect(row(5)).toHaveFocus());
  });

  it("says plainly when no failure was recorded", async () => {
    await openTimeline(UNKNOWN_ID);
    expect(screen.getByTestId("no-first-failure")).toHaveTextContent(
      "No observable check failure was recorded.",
    );
    expect(document.body.textContent).not.toMatch(/no bugs/i);
  });

  it("marks the recovery path and unresolved failures", async () => {
    await openTimeline();
    expect(row(5)).toHaveTextContent("FIRST FAILED RUN");
    expect(row(9)).toHaveTextContent("LAST FAILED RUN");
    expect(row(14)).toHaveClass("has-recovered");
    expect(row(14)).toHaveTextContent("OBSERVED RECOVERY");
    expect(row(17)).toHaveClass("has-unresolved");
    expect(row(17)).toHaveTextContent("UNRESOLVED IN THIS RECORDING");
    expect(document.body.textContent).not.toMatch(/still broken/i);

    fireEvent.click(row(14));
    const detail = screen.getByTestId("event-detail");
    expect(detail).toHaveTextContent("pytest later passed after 2 failed recorded attempts");
    fireEvent.click(row(17));
    expect(screen.getByTestId("event-detail")).toHaveTextContent(
      "no later passing run was recorded before the evidence ends",
    );
  });

  it("filters loaded events without reordering them", async () => {
    await openTimeline();
    const filter = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
    filter("Checks");
    expect(sequences()).toEqual([5, 9, 14, 17]);
    expect(screen.getByRole("button", { name: "Checks" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("timeline-count")).toHaveTextContent("Showing 4 loaded events (of 21 loaded)");
    filter("Files");
    expect(sequences()).toEqual([6, 10, 11]);
    filter("Git");
    expect(sequences()).toEqual([2, 20]);
    filter("Commands");
    expect(sequences()).toEqual([3, 4, 7, 8, 12, 13, 15, 16, 19]);
    filter("Errors");
    expect(sequences()).toEqual([18]);
    filter("Failures only");
    expect(sequences()).toEqual([5, 9, 17]);
    filter("All");
    expect(sequences()).toHaveLength(21);
  });

  it("searches loaded summaries, paths and types", async () => {
    await openTimeline();
    const search = screen.getByRole("searchbox", { name: "Search loaded events" });
    fireEvent.change(search, { target: { value: "helpers" } });
    expect(sequences()).toEqual([10]);
    fireEvent.change(search, { target: { value: "check.passed" } });
    expect(sequences()).toEqual([14]);
    fireEvent.change(search, { target: { value: "no such text" } });
    expect(screen.getByText("No matching loaded events")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "" } });
    expect(sequences()).toHaveLength(21);
  });

  it("says search covers loaded events only while more are unloaded", async () => {
    await openTimeline(CRASHED_ID);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "module_7" } });
    expect(screen.getByTestId("timeline-count")).toHaveTextContent(
      "Filters and search cover loaded events only.",
    );
  });

  it("opens a detail panel with safe fields only", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await openTimeline();
    fireEvent.click(row(6));
    const detail = screen.getByTestId("event-detail");
    const labels = within(detail)
      .getAllByRole("term")
      .map((t) => t.textContent);
    expect(labels).toEqual([
      "Sequence",
      "Event ID",
      "Time (UTC)",
      "Type",
      "Category",
      "Source",
      "Path",
      "Command ID",
      "Evidence",
    ]);
    expect(detail).toHaveTextContent(eventId(6));
    expect(detail).toHaveTextContent("file.modified");
    expect(detail).toHaveTextContent("filesystem");
    expect(detail).toHaveTextContent("src/app.py");
    expect(detail).not.toHaveTextContent(/sha256|hash|argv|stdout|stderr/i);

    fireEvent.click(within(detail).getByRole("button", { name: "Copy event ID" }));
    expect(writeText).toHaveBeenCalledWith(eventId(6));
    expect(await within(detail).findByRole("button", { name: "Copied" })).toBeInTheDocument();

    fireEvent.keyDown(within(detail).getByRole("button", { name: "Copied" }), { key: "Escape" });
    expect(screen.queryByTestId("event-detail")).toBeNull();
    expect(row(6)).toHaveFocus();
  });

  it("moves between events with the keyboard", async () => {
    await openTimeline();
    const first = row(1);
    expect(first.tagName).toBe("BUTTON");
    expect(first).toHaveAttribute("tabindex", "0");
    expect(row(2)).toHaveAttribute("tabindex", "-1");
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(row(2)).toHaveFocus();
    fireEvent.keyDown(row(2), { key: "End" });
    expect(row(21)).toHaveFocus();
    fireEvent.keyDown(row(21), { key: "ArrowUp" });
    expect(row(20)).toHaveFocus();
    fireEvent.keyDown(row(20), { key: "Home" });
    expect(row(1)).toHaveFocus();
    fireEvent.click(row(1));
    expect(row(1)).toHaveAttribute("aria-current", "true");
  });

  it("pages through a long session without duplicates and renders a bounded window", async () => {
    await openTimeline(CRASHED_ID);
    expect(screen.getByTestId("timeline-loaded")).toHaveTextContent("Loaded 100 of 250 recorded events");
    const rendered = rows().length;
    expect(rendered).toBeGreaterThan(5);
    expect(rendered).toBeLessThan(60);

    fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
    await screen.findByText("Loaded 200 of 250 recorded events");
    fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
    await screen.findByText("Loaded 250 of 250 recorded events");
    expect(screen.queryByRole("button", { name: "Load more events" })).toBeNull();

    const timelineRequests = server.pathsRequested().filter((p) => p.includes("/timeline"));
    expect(timelineRequests).toEqual([
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=0&limit=100`,
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=100&limit=100`,
      `/api/v1/sessions/${CRASHED_ID}/timeline?after_sequence=200&limit=100`,
    ]);
    const ids = rows().map((r) => r.getAttribute("data-event-id"));
    expect(new Set(ids).size).toBe(ids.length);
    expect(rows().length).toBeLessThan(60);

    fireEvent.keyDown(row(1), { key: "End" });
    expect(row(250)).toHaveFocus();
    expect(rows().length).toBeLessThan(60);
  });

  it("rejects a malformed first page", async () => {
    server.override = (path) =>
      path.includes("/timeline")
        ? {
            status: 200,
            body: {
              api_version: 1,
              session_id: STOPPED_ID,
              after_sequence: 0,
              limit: 100,
              items: [item(2, "file.modified", "b"), item(1, "file.modified", "a")],
              next_after_sequence: 1,
              has_more: true,
              total: 21,
            },
          }
        : undefined;
    await openSession(STOPPED_ID, /^Timeline/);
    expect(await screen.findByText("Unexpected response.")).toBeInTheDocument();
    expect(rows()).toHaveLength(0);
  });

  it("rejects a later page that overlaps what is loaded", async () => {
    await openTimeline(CRASHED_ID);
    const overlap = longTimeline(250).slice(49, 149);
    server.override = (path) =>
      path.includes("after_sequence=100")
        ? {
            status: 200,
            body: {
              api_version: 1,
              session_id: CRASHED_ID,
              after_sequence: 100,
              limit: 100,
              items: overlap.map((i, n) => ({ ...i, sequence: 101 + n })),
              next_after_sequence: 200,
              has_more: true,
              total: 250,
            },
          }
        : undefined;
    fireEvent.click(screen.getByRole("button", { name: "Load more events" }));
    expect(await screen.findByText("Unexpected response.")).toBeInTheDocument();
    expect(screen.queryByText(/Loaded 200/)).toBeNull();
    expect(rows().length).toBeGreaterThan(0);
  });

  it("shows an empty recording", async () => {
    await openSession(CREATED_ID, /^Timeline/);
    expect(await screen.findByText("No events recorded")).toBeInTheDocument();
  });

  it("keeps very long summaries and paths on fixed-height rows", async () => {
    const long = "x".repeat(5000);
    const path = `${"deep/".repeat(200)}file.py`;
    server.timelines.set(STOPPED_ID, [item(1, "file.modified", long, { path })]);
    withReport(STOPPED_ID, (r) => {
      r.first_observable_failure = null;
      r.recoveries = { items: [], total: 0, truncated: false };
      r.unresolved_failures = { items: [], total: 0, truncated: false };
    });
    await openTimeline();
    const r = row(1);
    expect(r.parentElement).toHaveStyle({ height: "64px" });
    expect(r.querySelector(".tl-summary")).toHaveTextContent(long);
    expect(r.querySelector(".tl-path")).toHaveTextContent(path);
    fireEvent.click(r);
    expect(screen.getByTestId("event-detail").querySelector(".detail-summary")).toHaveTextContent(long);
  });

  it("loads pages until a referenced event is reached", async () => {
    server.timelines.set(CRASHED_ID, longTimeline(1200));
    withReport(CRASHED_ID, (r) => {
      r.first_observable_failure = {
        event_id: eventId(1100, "b"),
        sequence: 1100,
        failure_type: "command_interrupted",
        summary: "Command interrupted: make",
        command_id: "cmd-9",
        check_kind: null,
        check_name: null,
        check_signature: null,
        exit_code: null,
        reason: "interrupted",
        evidence_event_ids: [eventId(1100, "b")],
        finding_evidence: "observed",
      };
    });
    await openSession(CRASHED_ID, /^Findings/);
    fireEvent.click(await screen.findByRole("button", { name: "Jump to event #1100 in timeline" }));
    const detail = await screen.findByTestId("event-detail");
    expect(detail).toHaveTextContent("#1,100");
    await waitFor(() => expect(row(1100)).toHaveFocus());
    const pages = server.pathsRequested().filter((p) => p.includes("/timeline"));
    expect(pages.length).toBeLessThanOrEqual(4);
    expect(pages.every((p, i) => i === 0 || p.includes("limit=500"))).toBe(true);
  });

  it("explains a reference that is not in a stopped session's timeline", async () => {
    withReport(STOPPED_ID, (r) => {
      r.first_observable_failure = { ...r.first_observable_failure!, event_id: eventId(99), sequence: 99 };
    });
    await openSession(STOPPED_ID, /^Findings/);
    fireEvent.click(await screen.findByRole("button", { name: "Jump to event #99 in timeline" }));
    expect(await screen.findByText("Event #99 is not in this session's timeline.")).toBeInTheDocument();
  });
});

// -- findings ------------------------------------------------------------------------------

describe("findings", () => {
  it("shows the first observable failure with evidence references", async () => {
    await openSession(STOPPED_ID, /^Findings/);
    const first = await screen.findByTestId("first-failure");
    expect(first).toHaveTextContent("FIRST OBSERVABLE FAILURE");
    expect(first).toHaveTextContent("pytest failed (exit 1)");
    expect(first).toHaveTextContent("OBSERVED");
    expect(first).toHaveTextContent("CHECK FAILED");
    expect(first).toHaveTextContent("Exit code1");
    fireEvent.click(within(first).getByRole("button", { name: "Jump to event #5 in timeline" }));
    expect(await screen.findByTestId("event-detail")).toHaveTextContent("#5");
    expect(screen.getByRole("tab", { name: /^Timeline/ })).toHaveAttribute("aria-selected", "true");
  });

  it("shows the no-failure state", async () => {
    await openSession(UNKNOWN_ID, /^Findings/);
    expect(await screen.findByText("No observable check failure was recorded.")).toBeInTheDocument();
    expect(screen.queryByTestId("first-failure")).toBeNull();
  });

  it("shows observed recoveries with their recorded order and change windows", async () => {
    await openSession(STOPPED_ID, /^Findings/);
    const recovery = await screen.findByTestId("recovery");
    expect(recovery).toHaveTextContent("OBSERVED RECOVERY");
    expect(recovery).toHaveTextContent("pytest later passed after 2 failed recorded attempts.");
    expect(recovery).toHaveTextContent("First failed#5");
    expect(recovery).toHaveTextContent("Last failed#9");
    expect(recovery).toHaveTextContent("Later passed#14");
    expect(recovery).toHaveTextContent("No earlier passing result for this exact check was recorded.");

    const windows = within(recovery).getAllByTestId("change-window");
    expect(windows).toHaveLength(2);
    const between = windows[0]!;
    expect(between).toHaveTextContent("Observed between failure and later pass");
    expect(between).toHaveTextContent("3 file changes");
    expect(between).toHaveTextContent("1 created");
    expect(between).toHaveTextContent("1 modified");
    expect(between).toHaveTextContent("1 deleted");
    for (const path of ["src/app.py", "src/helpers.py", "src/legacy.py"]) {
      expect(between).toHaveTextContent(path);
    }
    expect(windows[1]).toHaveTextContent("Observed between last failure and later pass");
    expect(windows[1]).toHaveTextContent("2 file changes");
  });

  it("shows failures unresolved in this recording", async () => {
    await openSession(STOPPED_ID, /^Findings/);
    const unresolved = await screen.findByTestId("unresolved");
    expect(unresolved).toHaveTextContent("UNRESOLVED IN THIS RECORDING");
    expect(unresolved).toHaveTextContent("ruff");
    expect(unresolved).toHaveTextContent("Latest failed#17");
    expect(unresolved).toHaveTextContent("End of recorded evidence");
    expect(unresolved).toHaveTextContent("not the project as it is now");
    expect(within(unresolved).getByTestId("change-window")).toHaveTextContent("0 file changes");
  });

  it("shows incomplete commands, errors and evidence notes", async () => {
    await openSession(STOPPED_ID, /^Findings/);
    const incomplete = await screen.findByTestId("incomplete");
    expect(incomplete).toHaveTextContent("INCOMPLETE");
    expect(incomplete).toHaveTextContent("Command started: npm run dev");
    expect(incomplete).toHaveTextContent("no finish event was recorded");

    const error = screen.getByTestId("error-observed");
    expect(error).toHaveTextContent("Scanner could not read logs/locked.txt");
    expect(error).toHaveTextContent("PermissionError");
    expect(error).toHaveTextContent("read");

    const warnings = screen.getAllByTestId("warning");
    expect(warnings[0]).toHaveTextContent("WARNING");
    expect(warnings[0]).toHaveTextContent("check_contradicts_command_finished");
    expect(warnings[1]).toHaveTextContent("NOTE");
    expect(within(warnings[0]!).getAllByRole("button")).toHaveLength(2);
  });

  it("shows truncation of bounded lists and change windows", async () => {
    withReport(STOPPED_ID, (r) => {
      const recovery = r.recoveries.items[0]!;
      recovery.changes_between_failure_and_pass = {
        ...recovery.changes_between_failure_and_pass,
        file_change_count: 45,
        created: 43,
        file_changes_truncated: true,
      };
      r.recoveries = { items: [recovery], total: 5, truncated: true };
    });
    await openSession(STOPPED_ID, /^Findings/);
    expect(
      await screen.findByText("Showing the first 3 of 45 file changes observed in this window."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Showing 1 of 5 recoveries/)).toBeInTheDocument();
  });

  it("generates no causal language anywhere", async () => {
    await openSession(STOPPED_ID);
    const texts: string[] = [];
    for (const tab of [/^Overview/, /^Timeline/, /^Findings/, /^Files/, /^Git/]) {
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      await waitFor(() => expect(screen.queryByRole("status", { name: /Loading/ })).toBeNull());
      if (tab.source.includes("Timeline")) {
        await screen.findAllByTestId("timeline-row");
        for (const r of rows()) {
          fireEvent.click(r);
          texts.push(screen.getByTestId("event-detail").textContent ?? "");
        }
      }
      texts.push(document.body.textContent ?? "");
    }
    const all = texts.join("\n");
    expect(all).not.toMatch(CAUSAL);
    expect(all).not.toMatch(/AI says|inferred/i);
  });
});

// -- files ---------------------------------------------------------------------------------

describe("files", () => {
  it("shows observed counts and the path list", async () => {
    await openSession(STOPPED_ID, /^Files/);
    expect(await screen.findByText("Observed during this recording")).toBeInTheDocument();
    const stats = Object.fromEntries(
      [...document.querySelectorAll(".stat")].map((s) => [
        s.querySelector("dt")?.textContent,
        s.querySelector("dd")?.textContent,
      ]),
    );
    expect(stats).toEqual({
      "File events": "3",
      Created: "1",
      Modified: "1",
      Deleted: "1",
      "Unique paths": "3",
    });
    const paths = screen.getAllByTestId("path-row");
    expect(paths.map((p) => p.querySelector(".path-cell")?.textContent)).toEqual([
      "src/app.py",
      "src/helpers.py",
      "src/legacy.py",
    ]);
    expect(paths[0]).toHaveTextContent("PRESENT");
    expect(paths[2]).toHaveTextContent("DELETED");
    expect(paths[2]).toHaveTextContent("#11");
    expect(document.body).toHaveTextContent("not a view of the current file system");
    expect(document.body.textContent).not.toMatch(/Current filesystem/);
  });

  it("shows a truncated path list", async () => {
    withReport(STOPPED_ID, (r) => {
      r.file_change_summary.paths = { ...r.file_change_summary.paths, total: 450, truncated: true };
    });
    await openSession(STOPPED_ID, /^Files/);
    expect(await screen.findByText(/Showing 3 of 450 paths/)).toBeInTheDocument();
  });

  it("jumps from a path to its last observed event", async () => {
    await openSession(STOPPED_ID, /^Files/);
    const legacy = (await screen.findAllByTestId("path-row"))[2]!;
    fireEvent.click(within(legacy).getByRole("button", { name: "Event #11: show in timeline" }));
    expect(await screen.findByTestId("event-detail")).toHaveTextContent("Deleted src/legacy.py");
  });
});

// -- git / final state ---------------------------------------------------------------------

describe("git and final state", () => {
  function git(state: Partial<GitStateSummary> | null): void {
    withReport(STOPPED_ID, (r) => {
      r.latest_git_state = state === null ? null : { ...r.latest_git_state!, ...state };
    });
  }

  async function gitPanel(): Promise<HTMLElement> {
    await openSession(STOPPED_ID, /^Git/);
    return screen.findByTestId("git-state");
  }

  it("shows a dirty last recorded Git state as history", async () => {
    const panel = await gitPanel();
    expect(panel).toHaveTextContent("Last recorded Git state");
    expect(panel).toHaveTextContent("not the current Git state");
    expect(panel).toHaveTextContent("DIRTY");
    expect(panel).toHaveTextContent("main");
    expect(panel).toHaveTextContent("1a2b3c4");
    expect(panel).toHaveTextContent("Unstaged2");
    expect(panel).toHaveTextContent("Untracked1");
    expect(panel).toHaveTextContent("Staged0");
    expect(panel).toHaveTextContent("Conflicted0");
    expect(panel).toHaveTextContent("origin/main");
    expect(panel).toHaveTextContent("1 / 0");
    expect(panel).toHaveTextContent("#20");
    expect(panel).toHaveTextContent("2 snapshots");
    expect(document.body.textContent).not.toMatch(/Current Git state/);
  });

  it("shows a clean state", async () => {
    git({ state: "clean", unstaged: 0, untracked: 0 });
    expect(await gitPanel()).toHaveTextContent("CLEAN");
  });

  it("shows detached and unborn heads", async () => {
    git({ detached: true, branch: null });
    const panel = await gitPanel();
    expect(panel).toHaveTextContent("DETACHED");
    expect(panel).toHaveTextContent("(none)");
  });

  it("shows an unborn branch", async () => {
    git({ unborn: true, head: null, state: "clean", upstream: null, ahead: null, behind: null });
    const panel = await gitPanel();
    expect(panel).toHaveTextContent("UNBORN");
    expect(panel).toHaveTextContent("No commits yet");
    expect(panel).toHaveTextContent("None recorded");
  });

  it("shows a recording without Git snapshots", async () => {
    git(null);
    expect(await gitPanel()).toHaveTextContent("No Git snapshot was recorded in this session.");
  });

  it("shows a project that was not a repository", async () => {
    git({
      repository: false,
      unavailable_reason: "not_a_repository",
      branch: null,
      detached: null,
      unborn: null,
      head: null,
      state: null,
    });
    const panel = await gitPanel();
    expect(panel).toHaveTextContent("NO REPOSITORY");
    expect(panel).toHaveTextContent("not_a_repository");
  });

  it("shows final check states with text results", async () => {
    await openSession(STOPPED_ID, /^Git/);
    const checks = await screen.findAllByTestId("check-state");
    expect(checks).toHaveLength(2);
    expect(checks[0]).toHaveTextContent("pytest");
    expect(checks[0]).toHaveTextContent("PASS");
    expect(checks[0]).toHaveTextContent("312#14");
    expect(checks[1]).toHaveTextContent("ruff");
    expect(checks[1]).toHaveTextContent("FAIL");
    expect(checks[1]).toHaveTextContent("101#17");
  });

  it("marks an ungrouped check without guessing its group", async () => {
    withReport(STOPPED_ID, (r) => {
      r.final_check_states.items.push({
        ...r.final_check_states.items[1]!,
        check_signature: null,
        identity_established: false,
        display_name: "pytest (ungrouped, event #9)",
        latest_event_id: eventId(9),
        latest_sequence: 9,
      });
      r.final_check_states.total = 3;
    });
    await openSession(STOPPED_ID, /^Git/);
    const checks = await screen.findAllByTestId("check-state");
    expect(checks[2]).toHaveTextContent("UNGROUPED");
    expect(checks[2]).toHaveTextContent(
      "This run could not safely be matched with other runs of the same check.",
    );
    expect(checks[0]).not.toHaveTextContent("UNGROUPED");
  });

  it("shows the command summary", async () => {
    await openSession(STOPPED_ID, /^Git/);
    const summary = await screen.findByTestId("command-summary");
    const values = Object.fromEntries(
      [...summary.querySelectorAll(".stat")].map((s) => [
        s.querySelector("dt")?.textContent,
        s.querySelector("dd")?.textContent,
      ]),
    );
    expect(values).toEqual({
      "Commands started": "5",
      "Commands finished": "4",
      "Incomplete commands": "1",
      "Checks passed": "1",
      "Checks failed": "3",
      "Checks ungrouped": "0",
      "Check signatures": "2",
    });
  });
});

// -- report failures -----------------------------------------------------------------------

describe("report failures", () => {
  it("keeps the timeline usable when the report cannot be loaded", async () => {
    server.override = (path) =>
      path.endsWith("/report") ? { status: 200, body: { schema_version: 1 } } : undefined;
    await openTimeline();
    expect(screen.getByText(/markers are not shown/)).toBeInTheDocument();
    expect(sequences()).toHaveLength(21);
    fireEvent.click(screen.getByRole("tab", { name: /^Findings/ }));
    expect(screen.getByText("Unexpected response")).toBeInTheDocument();
  });
});
