# AgentBodycam

**Your AI coding agent now has a bodycam.**

> **EARLY DEVELOPMENT / NOT READY FOR PRODUCTION**
>
> AgentBodycam can record file-system changes and read-only Git state changes in a project
> (`agentbodycam record`). It does not yet capture commands, tests or builds, integrate with
> any specific agent, analyse sessions, replay them, export them or provide a UI. It has no
> AI features. Expect breaking changes.

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
| Event model + local append-only event store | Implemented |
| `session create` / `list` / `show` | Implemented |
| Universal recorder: file changes (`record`) | Implemented (polling, standard library only) |
| Universal recorder: Git state (`record`) | Implemented (read-only Git commands) |
| Universal recorder: command/test/build capture | Not started |
| Local forensics engine | Not started |
| Replay UI | Not started |
| Share/export | Not started |
| Agent-specific integrations (Cursor, Codex, Claude Code, ...) | None exist |
| Optional AI analysis | Not started |

No external API, account or network access is needed for anything that exists today.

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
- Not yet: command/test/build capture, agent integrations, forensics, replay/UI, export or AI.

### Session store

```bash
agentbodycam session list
agentbodycam session show <session-id>      # metadata, status, start/end time, event count
agentbodycam session create                 # create an empty session (developer command)
agentbodycam session list --project PATH    # operate on another project directory
```

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
