# AgentBodycam

**Your AI coding agent now has a bodycam.**

> **EARLY DEVELOPMENT / NOT READY FOR PRODUCTION**
>
> AgentBodycam can record file-system changes and read-only Git state changes in a project
> (`agentbodycam record`), plus commands you explicitly launch through
> `agentbodycam run -- <command>` and whether those that are tests, builds, lints or type
> checks passed or failed, it can produce a deterministic local forensic report of a
> recorded session (`agentbodycam analyze`), it can serve recorded evidence through a
> local, authenticated, read-only Replay API (`agentbodycam replay-api`), and it has a
> local Replay UI under development (`agentbodycam replay`: dashboard, session overview,
> paginated forensic timeline with failure / recovery / unresolved markers, replay
> playback controls and a scrubber, findings, observed files and the last recorded Git /
> final state, presented as a cinematic replay view, plus a local privacy-safe export:
> an SVG share card and a JSON summary). It does not observe arbitrary terminal commands
> and does not integrate with any specific agent. File contents and diffs, GIF/video
> export, hosted sharing and an AI analyst do not exist yet. Expect breaking changes.
>
> Implemented:
> - file recording
> - read-only Git evidence
> - wrapped commands and deterministic check outcomes
> - local deterministic forensic analysis
> - local authenticated Replay API (loopback only, read-only)
> - Replay UI (under development): dashboard, session overview, paginated forensic
>   timeline, first-failure / recovery / unresolved markers, findings view, observed
>   files view, last-recorded Git and final-state view
> - Replay playback (under development): play/pause, 0.5x-4x speed, a sequence-based
>   scrubber with failure / recovery / unresolved markers, important-event navigation
>   and keyboard shortcuts, over the recorded evidence events (no screen video exists)
> - Replay visual styling: a sticky "replay theater" (controls, scrubber, current event)
>   with evidence-driven emphasis for the first observable failure, observed recoveries
>   and unresolved failures
> - local privacy-safe SVG share card (generated in the browser, nothing uploaded)
> - privacy-safe JSON summary export
>
> Still not implemented:
> - possible final micro-polish of the replay view
> - file contents and diffs
> - GIF/video export
> - hosted sharing (exports are local files only)
> - agent-specific adapters
> - optional AI analyst

## What is this?

AI coding agents such as Cursor, Codex and Claude Code can change many files, run
commands and retry things faster than a human can follow. When something breaks, it is
often hard to answer simple questions:

- What changed, and when?
- What was the first thing that observably failed?
- Did the agent retry or recover?
- What state was the project left in?

AgentBodycam aims to answer those questions by recording observable evidence from your
project during an agent session and letting you replay it as a forensic timeline.

It is designed to be:

- **Local-first**: everything runs on your machine.
- **Privacy-first**: no telemetry, no account, no cloud backend.
- **Useful without AI**: the core needs no OpenAI, Anthropic, Gemini or other API key.
- **Honest**: it separates what was *observed* from what is *inferred*, and never presents
  a guess as a proven fact.

## Status

| Area | Status |
|---|---|
| CLI foundation (`--help`, `--version`, `doctor`) | Implemented |
| Event model + local append-only event store | Implemented (safe for concurrent processes) |
| `session create` / `list` / `show` / `active` | Implemented |
| Universal recorder: file changes (`record`) | Implemented (polling, standard library only) |
| Universal recorder: Git state (`record`) | Implemented (read-only Git commands) |
| One active recording per project (OS-backed lease) | Implemented |
| Wrapped command recording (`run -- <command>`) | Implemented (explicitly wrapped commands only) |
| Check outcome recording (`run --check KIND`, `check.passed` / `check.failed`) | Implemented (explicit, plus conservative automatic recognition) |
| Capture of commands run outside `agentbodycam run` | Not planned for universal mode |
| Local forensics engine (`analyze`) | Implemented (deterministic, read-only, observed findings only) |
| Local Replay API (`replay-api`) | Implemented (loopback only, temporary bearer token, read-only, standard library only) |
| Replay UI (`replay`) | Under development: dashboard, overview, paginated timeline with failure/recovery markers, replay playback controls and scrubber in a sticky replay view, findings, observed files, last recorded Git/final state, local privacy-safe export |
| Share/export | Local SVG share card and JSON summary (privacy-safe, generated in the browser); no GIF/video, no hosted sharing |
| Agent-specific integrations (Cursor, Codex, Claude Code, ...) | None exist |
| Optional AI analysis | Not started |

No external API, account or network access is needed for anything that exists today, and
AgentBodycam itself makes no outbound network requests. The Replay API and the Replay UI
only listen on the loopback interface for local clients (local IPC); the UI loads no
remote scripts, fonts or analytics. (A command you wrap with
`agentbodycam run` is your own program and may of course use the network.)

See [docs/product-scope.md](docs/product-scope.md) and
[docs/architecture.md](docs/architecture.md) for the plan.

## Requirements

- Python 3.11 or newer
- Git (optional; needed only for Git state recording. `doctor` reports whether it is
  available)
- Windows, Linux or macOS

## Install (from source, for development)

```bash
git clone <this repository>
cd AgentBodycam
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Usage

```bash
agentbodycam --help
agentbodycam --version
agentbodycam doctor        # read-only environment check
python -m agentbodycam doctor
```

`doctor` prints your Python version, operating system, current directory and whether Git
is available. It does not change anything on your system.

### Recording file and Git changes

Run this in your project, then work (or let your coding agent work) as usual:

```text
$ agentbodycam record
AgentBodycam recording
Session: 20261013T220059Z-509d0c620114df53
Project: C:\Users\me\my project

Baseline snapshot: 1 file
  03:31:00  GIT       Git repository: on branch main at ff4d448, working tree clean
Watching for file and Git changes (polling every 0.5s)...
Press Ctrl+C to stop.

  03:31:02  MODIFIED  app.py
  03:31:04  GIT       Working tree became dirty
  03:31:06  GIT       Git status changed (1 staged, 0 unstaged, 0 untracked)
  03:31:10  GIT       HEAD changed ff4d448 -> 010683c; working tree became clean
  03:31:12  GIT       Branch changed main -> feature/demo

Recording stopped.
File events recorded: 1 (0 created, 1 modified, 0 deleted)
Git snapshots recorded: 5
Session: 20261013T220059Z-509d0c620114df53
```

Outside a Git repository (or without Git installed) file recording works exactly the same
and the header says `Git: not a Git repository (Git state not recorded)`.

```bash
agentbodycam record --project PATH    # record another directory
agentbodycam record --interval 1      # poll every second (default 0.5)
agentbodycam record --duration 60     # stop automatically after 60 seconds
```

What this is, and is not:

- It records **project-level evidence**: which file changed, when the change was detected,
  its size, modification time and SHA-256. It never stores file contents.
- It does **not** know who or what made a change. It never claims "Cursor changed
  `src/app.py`"; it says "`src/app.py` was modified" at a time.
- Files present when recording starts form a baseline and produce no events. A rename is
  recorded as a delete plus a create. Saving a file with identical content is not a
  modification.
- `.git/`, `.agentbodycam/`, virtualenvs, `node_modules/`, caches and likely secret files
  (`.env`, `.env.*`, `*.pem`, `*.key`, ...) are never recorded. Symlinks and junctions are
  never followed. See [the exact ignore policy](docs/architecture.md#31-universal-recorder).
- In a Git repository it also records a `git.snapshot` event whenever the observable Git
  state changes (branch, HEAD commit, detached HEAD, upstream ahead/behind, staged /
  unstaged / untracked / conflicted paths, clean or dirty). Git is polled about every 2
  seconds with read-only commands only; AgentBodycam never commits, checks out, resets,
  stashes, fetches or pushes. It stores no diffs, no commit bodies, no author emails and no
  remote URLs, and it never says *who* made a Git change.
- Only one `record` can run per project. A second one exits with
  `error: AgentBodycam is already recording this project` and the running session's ID.
  If a recorder crashes or is killed, the next `record` starts normally; no cleanup needed.
- Not yet: agent integrations, GIF/video export or AI.

### Recording commands you run

While `agentbodycam record` is running (terminal 1), run commands through
`agentbodycam run` (terminal 2):

```bash
agentbodycam run -- python -m pytest
agentbodycam run -- python script.py
agentbodycam run -- git status
agentbodycam run --project /path/to/app -- python build.py
```

Each wrapped command adds two events to the active session, paired by a random
`command_id`:

```text
session.started
git.snapshot
command.started    Command started: python -m pytest
file.modified      (whatever the command changed, as seen by the recorder)
command.finished   Command finished: python -m pytest (exit 0)
session.stopped
```

- **Only commands launched through `agentbodycam run -- ...` are captured.** AgentBodycam
  does not watch your terminal, your shell history or other processes, and it never claims
  that Cursor, Codex, Claude or anyone else ran a command; it records that the command ran.
- Everything after `--` is passed to the command as an argument list, **without a shell**:
  `&&`, `|`, `>`, `<` and `;` are ordinary arguments. If you want a shell, run it yourself:
  `agentbodycam run -- sh -c "make && make test"` or `agentbodycam run -- cmd /c npm test`.
- On Windows, `npm`, `npx`, `yarn` and similar tools are batch scripts (`npm.cmd`) that
  only `cmd.exe` can run. AgentBodycam refuses to add that shell silently and tells you to
  use `agentbodycam run -- cmd /c npm test` instead. Executables are looked up only in
  absolute `PATH` entries, never implicitly in the current directory.
- The command behaves like a normal terminal command: it inherits stdin, stdout, stderr and
  the environment. **Its output, input and environment are never recorded.**
- Arguments are recorded with likely secrets redacted (`--token X`, `--api-key=X`,
  `password=X`, `https://user:pass@host`, recognisable API keys and tokens) and absolute
  paths rewritten (project-relative, or `<external-path>`). The command itself always
  receives the original arguments. See
  [the redaction rules](docs/architecture.md#wrapped-commands).
- `run` only works while a recording is active for the project. Otherwise it prints
  `error: no active AgentBodycam recording for this project` and **does not run the
  command**. If the recording stops while the command is running, the command still
  finishes normally and a warning says the finish event was not recorded.
- The command runs in the current directory if it is inside the project, otherwise in the
  project root.
- Exit status: `run` exits with the command's own exit status (128+N if a POSIX signal N
  ended it). AgentBodycam's own failures use 125 (no active recording or other setup
  error), 126 (cannot execute), 127 (executable not found) and 130 (interrupted before the
  command started).
- Ctrl+C reaches the command as usual; `run` waits for the command to exit, records how it
  ended (`outcome: interrupted`), then exits with its status, so it never leaves the
  command running on its own.

### Recording check results (tests, builds, lints, type checks)

A wrapped command that is a **check** also gets a `check.passed` or `check.failed` event,
right after its `command.finished`:

```bash
agentbodycam run --check test -- python -m pytest
agentbodycam run --check build -- python -m build
agentbodycam run --check lint -- python -m ruff check .
agentbodycam run --check typecheck -- python -m mypy .
agentbodycam run --check test -- cmd /c npm test        # Windows

agentbodycam run -- python -m pytest                   # recognised automatically
```

```text
command.started    Command started: python -m pytest
command.finished   Command finished: python -m pytest (exit 1)
check.failed       Test failed: pytest (exit 1)
file.modified      app.py
command.started    Command started: python -m pytest
command.finished   Command finished: python -m pytest (exit 0)
check.passed       Test passed: pytest
```

- `--check test|build|lint|typecheck` is authoritative. **Use it when certainty matters.**
- Without `--check`, only a conservative whitelist of commands that appear to actually
  execute a check is recognised:
  `pytest`, `python -m pytest`, `python -m unittest`, `cargo test`, `go test`,
  `dotnet test`; `python -m build`, `cargo build`, `go build`, `dotnet build`;
  `ruff check`, `python -m ruff check`, `eslint`; `mypy`, `python -m mypy`, `pyright`,
  `tsc --noEmit`; and the `test`, `build`, `lint` and `typecheck` scripts of `npm`,
  `pnpm` and `yarn` (`npm test`, `npm run build`, `pnpm lint`, `yarn run typecheck`, ...),
  also behind an explicit `cmd /c ...` or `sh -c "..."`. See
  [the exact rules](docs/architecture.md#check-results).
- **Anything else stays a plain command**: `python script.py`, `make`, `bash build.sh`,
  `npm run custom` and so on are never guessed.
- **Informational and discovery-only runs stay plain commands too**: `pytest --version`,
  `mypy --help`, `pytest --collect-only`, `cargo test --no-run`, `go test -list X`,
  `dotnet test --list-tests`, `tsc --noEmit --showConfig`, `npm test -- --help`, ...
  They exit 0 without testing, linting or type checking anything, so they must never count
  as evidence that the project's checks passed. (`--check` still forces a check if you
  really want one.)
- A check **passes only if the process exited by itself with status 0.** A non-zero exit,
  a signal, an interrupt (Ctrl+C) or a failure to start an explicit `--check` command is a
  failure, and the event says which (`reason`).
- Output is **not analysed** (and not stored): no test names, assertion messages or
  compiler errors. The result comes from the exit status only.
- **No cause is inferred.** A `file.modified` between a failing and a passing check is just
  two observations in order; AgentBodycam never claims the edit made the check pass. Each
  run is an independent check with its own `check_id`; failure-then-pass patterns are
  reported by `agentbodycam analyze` (below), again as order only.
- Check names are generated from the recognised tool (`pytest`, `npm build`, `ruff`), never
  from your arguments, so secrets in arguments never appear in them.

### Forensic analysis of a recording

```bash
agentbodycam analyze <session-id>                 # human-readable report
agentbodycam analyze --project PATH <session-id>  # another project directory
agentbodycam analyze --json <session-id>          # the same report as deterministic JSON
```

```text
$ agentbodycam analyze 20261014T001023Z-dfa709c5a0effd8e
AgentBodycam forensic report
Session: 20261014T001023Z-dfa709c5a0effd8e
Recording: stopped (analysis complete)
Started: 2026-10-14T00:10:23.639517Z   Ended: 2026-10-14T00:10:40.379116Z
Events: 19
Findings are observed order only; AgentBodycam does not infer why anything happened.

First observable failure
  #10    Test failed: pytest (exit 1)

Observed recoveries (later pass of the same check)
  pytest failed at event #10 and later passed at event #18 after 2 additional recorded attempts.
    Failed attempts: 2
    File changes observed after the last recorded passing check (#5) and before the first recorded failing check (#10): 1.
      #6     modified  app.py
    File changes observed between the failed check (#10) and the later passing check (#18): 2.
      #11    modified  app.py
      #15    modified  app.py

Final evidence (observed during this recording)
  Files: 3 modified, 0 created, 0 deleted (1 path)
  Last recorded Git state: branch main at 1651314, dirty (0 staged, 1 unstaged, 0 untracked) (event #7)
  Checks (latest recorded result):
    pytest                                   PASS  (event #18)

Timeline (19 of 19 events)
  #1     00:10:23  session.started   Recording started
  ...
```

**Forensics identifies ordering and patterns, not causation.** It may say "pytest failed,
then later passed after two more attempts" and "`app.py` was modified between the failed
check and the later passing check". It never says the agent fixed pytest, that a change
caused a failure, or what the root cause was. It reports:

- an ordered timeline (by event sequence number; timestamps are display only);
- the **first observable failure**: the earliest `check.failed`, or a wrapped command that
  could not start, was ended by a signal or was interrupted. A plain non-zero exit of an
  unclassified command (`git diff --quiet`) is *not* treated as a failure;
- repeated failures and **observed recoveries** (a later pass) of the *same* check, where
  "same" means the same kind, name, recorded (redacted) command line and working
  directory. `pytest tests/auth` and `pytest tests/billing` are different checks. Runs
  whose recorded command line is redacted, truncated or contains rewritten external paths
  are never grouped (a missed recovery is preferred over an invented one);
- failures **unresolved in this recording** (the latest recorded result failed; the
  project may have changed after the recording stopped);
- file changes observed between the last pass and the first failure, and between a
  failure and the later pass (never called suspects);
- commands that started but have no recorded finish, `error.observed` events (kept apart
  from project failures), the files observed changing, and the **last recorded** Git state
  (Git is not run again);
- evidence-quality notes, e.g. contradictory command/check records, which are excluded
  from findings rather than repaired.

Every finding is labelled `observed` and lists the event IDs it is based on. Analysis reads
the stored session only: it writes nothing, runs no Git command, uses no AI and makes no
network request. A session that is still being recorded can be analysed; the report is
then marked `analysis_complete: false`. A session whose recorder crashed is reported as
`ended_without_stop`.

### Replay UI (under development)

```text
$ agentbodycam replay
AgentBodycam Replay (UI preview)
Project: C:\work\app
Listening: http://127.0.0.1:54321
Open in your browser: http://127.0.0.1:54321/
The Replay UI is under development: playback and local privacy-safe export (SVG card, JSON summary) exist; GIF/video export does not.
Access is granted automatically to the page served at this address; the address contains no secret and nothing is stored in your browser.
Read-only, loopback only.
Press Ctrl+C to stop.
```

Open the printed address in your browser. The UI is served by AgentBodycam itself from the
same loopback origin as the Replay API (no separate frontend server, no Node.js needed at
run time). Today it shows:

- the connection and server status;
- a project summary (session count; never the project path) and the session list, with
  each session's recording state: `STOPPED`, `LIVE`, `ENDED WITHOUT STOP` (the recorder
  crashed or was killed), `UNKNOWN`, `NOT STARTED` or `UNREADABLE`;
- a session overview (`/session/<id>`): recording state, start, end or "still recording",
  event count, observable failures, observed recoveries, unresolved failures, warnings,
  event counts per type and command/check totals;
- a **Timeline** tab: the session's evidence in sequence order, loaded 100 events at a
  time from the Replay API ("Load more", or automatically near the end of the list), with
  a marker per event, the first observable failure ("Jump to first failure"), the failed
  and passing runs of each observed recovery and checks unresolved in this recording.
  Filters (All, Files, Git, Commands, Checks, Errors, Failures only) and the search box
  cover loaded events only and never reorder them. Selecting an event shows its safe
  fields (sequence, event ID, time, type, source, path, command ID, evidence);
- **replay playback** above the timeline: Play / Pause (Replay at the end), previous /
  next event, previous / next important event, speed 0.5x / 1x / 2x / 4x and a scrubber
  over event sequence numbers (not time) that shows how many events are loaded, with
  markers for the first observable failure, check failures, observed recoveries and
  unresolved failures. Playback steps through the recorded evidence events one at a time
  at a fixed pace (800 ms per event at 1x); it does not reproduce the real time between
  events, and AgentBodycam records no screen video. The event being replayed is
  highlighted in the timeline, shown in a "Now replaying" card and in the detail panel.
  Keyboard: Space play/pause, ←/→ previous/next event, Shift+←/→ previous/next important
  event (not while typing). Further pages load automatically as playback reaches them;
  for a live recording, playback stops at the LIVE EDGE and only advances to newly
  recorded events if you turn on "Follow live". On desktop-sized windows the controls,
  scrubber and current event form one "replay theater" that stays below the top bar
  while you scroll the timeline. Its colour follows the evidence: blue for an ordinary
  event, green for a passing check or OBSERVED RECOVERY, red for a failure, the FIRST
  OBSERVABLE FAILURE or UNRESOLVED IN THIS RECORDING, amber for an observed error. In the
  timeline, CURRENT marks where playback is and DETAIL marks the event open in the
  detail panel; the rail is tinted up to the current event. Decorative motion is
  disabled when the operating system asks for reduced motion;
- a **Findings** tab: first observable failure, observed recoveries (with the file changes
  observed between the failed and later passing runs), unresolved failures, incomplete
  commands, errors observed and evidence notes; every reference can jump to its event;
- a **Files** tab: the paths observed during this recording and their change counts;
- a **Git / Final State** tab: the last recorded Git state, the final state of each
  check and the command summary;
- an **Export** button (top bar) that opens a local export panel with a preview:
  - **Share card**: a 1200 × 630 SVG (`agentbodycam-replay.svg`) with the event count,
    files changed, failed checks, observed recoveries, unresolved failures, the first
    observable failure and the first observed recovery (event number and a label such as
    `Test failed: pytest`) and the final (or, while live, latest) recorded check state;
  - **JSON summary**: the same facts as `agentbodycam-summary.json`
    (`"format": "agentbodycam-share-summary"`, `"version": 1`), not the raw report.

  Both are generated in your browser from an allowlist of safe fields and saved as local
  files; nothing is uploaded and nothing is copied to the clipboard. They never contain
  the project name or path, session ID, event IDs, timestamps, file paths, command lines,
  Git branch / commit / remote, environment, file contents, hashes, tokens or free-text
  summaries. Check names appear only when they are one of AgentBodycam's generated tool
  names (`pytest`, `npm test`, ...), otherwise a generic label is used. The SVG uses
  system fonts and no scripts, links, external images or remote resources.

All findings are observed facts from the recording: the UI never attributes a failure or
a recovery to a file change. While a session is live the report and timeline refresh
every 2.5 s (never overlapping, paused while the browser tab is hidden); playback pauses
too while the tab is hidden. File contents and diffs, GIF/video export and hosted sharing
are **not implemented yet**.

How the page gets API access: the page asks its own server for the temporary token with a
same-origin `fetch()` (`GET /ui/bootstrap`, which requires a custom header and browser
fetch metadata, so other web sites, links, iframes and plain `curl` requests are refused).
The token is kept in page memory only: never in the URL, `localStorage`,
`sessionStorage`, cookies or on disk, and it is not printed by `replay`. Use `replay-api`
instead if you want the API alone, without the token handoff (see
[SECURITY.md](SECURITY.md#local-replay-ui-agentbodycam-replay)).

```bash
agentbodycam replay --project PATH     # serve another project's recordings
agentbodycam replay --port 8765        # fixed port (default 0: any free port)
```

### Local Replay API (development)

**This is a local development API** (JSON only); it exists so the Replay UI (and your own
scripts) can read recorded evidence. `replay-api` serves no UI.

```text
$ agentbodycam replay-api
AgentBodycam Replay API
Project: C:\work\app
Listening: http://127.0.0.1:54321
Authentication: enabled (send `Authorization: Bearer <token>`)
API token: 3q2-...
The token is temporary: it is held in memory only and stops working when this server stops.
Local development API (read-only, loopback only, JSON only). For the Replay UI, run `agentbodycam replay`.
Press Ctrl+C to stop.
```

```bash
agentbodycam replay-api --project PATH     # serve another project's recordings
agentbodycam replay-api --port 8765        # fixed port (default 0: any free port)
agentbodycam replay-api --host ::1         # 127.0.0.1 (default), ::1 or localhost only

curl -H "Authorization: Bearer <token>" http://127.0.0.1:54321/api/v1/sessions
```

| Route (GET/HEAD) | Returns |
|---|---|
| `/api/v1/health` | `{"status": "ok", "api_version": 1, "product": "AgentBodycam"}` (no token needed) |
| `/api/v1/project` | `{"api_version": 1, "session_count": N}` |
| `/api/v1/sessions` | sessions, newest first: ID, status, recording state, start/end, event count |
| `/api/v1/sessions/<id>` | one session's header: status, recording state, counts |
| `/api/v1/sessions/<id>/report` | exactly the `agentbodycam analyze --json` report |
| `/api/v1/sessions/<id>/timeline?after_sequence=0&limit=100` | a page of the safe timeline (`limit` 1-500) |

- **Loopback only**: `0.0.0.0`, `::` and LAN addresses are refused.
- **Temporary token**: a new random token on every start, printed once, kept in memory
  only, required on every data endpoint as `Authorization: Bearer <token>` (never in the
  URL).
- **Read-only**: GET and HEAD only (everything else is `405`); nothing is written, no Git
  or other process is run, and starting it creates nothing in the project.
- **No CORS**: other web sites cannot read it; cross-origin and foreign-`Host` requests are
  refused. The Replay UI is served from the same local origin.
- No raw event payloads, absolute project paths, host/user names, environment, file
  contents or command output are returned. See [SECURITY.md](SECURITY.md#local-replay-api-agentbodycam-replay-api)
  and [the API reference](docs/architecture.md#35-replay-api-implemented).

### Session store

```bash
agentbodycam session list
agentbodycam session show <session-id>      # metadata, status, start/end time, event count
agentbodycam session active                 # which session is being recorded right now
agentbodycam session create                 # create an empty session (developer command)
agentbodycam session list --project PATH    # operate on another project directory
```

```text
$ agentbodycam session active
Active AgentBodycam recording
Session: 20261013T222158Z-347a8ac7fd0fa27c
Started: 2026-10-13 22:21:58 UTC
PID: 20884
```

A recording counts as active only while its recorder process holds an operating-system
lock on `.agentbodycam/active.lock`; leftover metadata from a crashed recorder is reported
as stale, never as active.

Sessions live in `.agentbodycam/sessions/<session-id>/` (`session.json` + `events.jsonl`).
AgentBodycam writes a `.agentbodycam/.gitignore` so recordings are never committed.
See [docs/architecture.md](docs/architecture.md#32-evidence--event-store-implemented)
for the event schema.

## Development

Run all quality checks (format check, lint, type check, tests) with one command:

```bash
python scripts/check.py
python scripts/check.py --fix   # auto-format and apply safe lint fixes first
```

Or individually:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
```

The Replay UI source lives in `web/` (React + TypeScript + Vite; Node.js 20+ is needed only
to work on it). Its production build is committed to `src/agentbodycam/ui/static/` and
shipped inside the Python package:

```bash
cd web
npm ci
npm run typecheck
npm run test
npm run build      # typechecks, then writes src/agentbodycam/ui/static/
npm run dev        # Vite dev server; proxies /api and /ui/bootstrap to a running
                   # `agentbodycam replay --port 8765` (override: AGENTBODYCAM_DEV_API)
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security and privacy

AgentBodycam is intended to handle sensitive project data locally. See
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
