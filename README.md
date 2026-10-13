# AgentBodycam

**Your AI coding agent now has a bodycam.**

> **EARLY DEVELOPMENT / NOT READY FOR PRODUCTION**
>
> AgentBodycam does not record anything yet. Only the CLI foundation and the local
> session/event store exist. Expect breaking changes.

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
| Event model + local append-only event store | Implemented (nothing produces events yet) |
| `session create` / `list` / `show` | Implemented (empty sessions only) |
| Universal recorder | Not started |
| Local forensics engine | Not started |
| Replay UI | Not started |
| Share/export | Not started |
| Agent-specific integrations | None exist |
| Optional AI analysis | Not started |

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

Session store (developer commands; nothing is recorded yet):

```bash
agentbodycam session create                 # create an empty session in ./.agentbodycam/
agentbodycam session list
agentbodycam session show <session-id>
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
