# AgentBodycam

**Your AI coding agent now has a bodycam.**

> **EARLY DEVELOPMENT / NOT READY FOR PRODUCTION**
>
> AgentBodycam can record file-system changes in a project (`agentbodycam record`).
> It does not yet monitor Git, capture commands, integrate with any specific agent, analyse
> sessions or provide a UI. Expect breaking changes.

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
| Universal recorder: Git monitoring | Not started |
| Universal recorder: command/test capture | Not started |
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
- Git (optional today; `doctor` reports whether it is available)
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

### Recording file changes

Run this in your project, then work (or let your coding agent work) as usual:

```text
$ agentbodycam record
AgentBodycam recording
Session: 20261013T214353Z-b74908602141a3c0
Project: C:\Users\me\my project

Baseline snapshot: 2 files
Watching for file changes (polling every 0.5s)...
Press Ctrl+C to stop.

  03:13:55  CREATED   src/new.py
  03:13:55  MODIFIED  src/app.py
  03:13:56  DELETED   docs/old.md

Recording stopped.
File events recorded: 3 (1 created, 1 modified, 1 deleted)
Session: 20261013T214353Z-b74908602141a3c0
```

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
- Not yet: Git monitoring, command/test capture, agent integrations, analysis, replay or UI.

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
