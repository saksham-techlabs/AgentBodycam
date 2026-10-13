# AgentBodycam

**Your AI coding agent now has a bodycam.**

> **EARLY DEVELOPMENT / NOT READY FOR PRODUCTION**
>
> AgentBodycam can record file-system changes and read-only Git state changes in a project
> (`agentbodycam record`), plus commands you explicitly launch through
> `agentbodycam run -- <command>` and whether those that are tests, builds, lints or type
> checks passed or failed. It does not observe arbitrary terminal commands, does not
> integrate with any specific agent, and does not analyse sessions, replay them, export
> them or provide a UI. It has no AI features. Expect breaking changes.
>
> Implemented: file recording, read-only Git recording, wrapped command recording,
> deterministic check outcome recording.
> Still not implemented: forensic analysis, replay API, UI, export, agent adapters, AI
> analysis.

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
| Local forensics engine | Not started |
| Replay UI | Not started |
| Share/export | Not started |
| Agent-specific integrations (Cursor, Codex, Claude Code, ...) | None exist |
| Optional AI analysis | Not started |

No external API, account or network access is needed for anything that exists today, and
AgentBodycam itself makes no network requests. (A command you wrap with `agentbodycam run`
is your own program and may of course use the network.)

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
- Not yet: agent integrations, forensics, replay/UI, export or AI.

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
  two observations in order; AgentBodycam never claims the edit fixed anything. Each run is
  an independent check with its own `check_id`; failure-then-success patterns are left to
  the future forensics engine.
- Check names are generated from the recognised tool (`pytest`, `npm build`, `ruff`), never
  from your arguments, so secrets in arguments never appear in them.

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

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security and privacy

AgentBodycam is intended to handle sensitive project data locally. See
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
