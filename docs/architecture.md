# AgentBodycam Architecture

> Status: **design document**. Most of what is described here is not implemented yet.
> Implemented today: the CLI foundation (`--help`, `--version`, `doctor`), the typed
> event/session model and the local append-only event store (section 3.2), managed with
> `agentbodycam session create|list|show`, and the file-system part of the universal
> recorder (`agentbodycam record`, section 3.1). Git monitoring, command capture, adapters,
> forensics, replay, UI, export and AI are not implemented. This document describes the
> intended direction so future work stays coherent.

## 1. Goals and principles

AgentBodycam records and reconstructs what happened during a coding-agent session
(Cursor, Codex, Claude Code and similar tools) so a human can answer:
*what changed, when, what broke first, and what state did the project end in?*

Guiding principles:

- **Local-first.** All recording, storage and analysis run on the user's machine.
- **Privacy-first.** No telemetry, no accounts, no cloud backend. Nothing leaves the
  machine unless the user explicitly exports or shares it.
- **Useful without AI.** The core must deliver value with zero external API calls.
  AI analysis is an optional layer on top, never a dependency.
- **Evidence over narrative.** Every claim shown to the user must trace back to recorded
  evidence. Inference is labelled as inference.
- **Honest about visibility.** We only claim to see what we can actually observe.
  We do not claim access to proprietary agent internals unless an adapter can
  genuinely obtain them through local or officially available data.
- **Cross-platform.** Windows, Linux and macOS are first-class targets.

## 2. Observed fact vs. inference

This distinction is the most important rule in the project.

| | Example | Source |
|---|---|---|
| **OBSERVED FACT** | "Tests started failing after this file changed." | Two recorded events with timestamps: a file write at T1, a failing test run at T2 > T1. |
| **INFERENCE** | "This file change caused the failure." | A hypothesis derived from facts. Plausible, not proven. |

Rules that follow from this:

1. The event store holds **only observations** (what was seen, when, and by which source).
2. Every derived finding produced by the forensics engine or the AI analyst carries an
   explicit kind (`observed` or `inferred`), a confidence description, and links to the
   evidence events it is based on.
3. The UI, exports and share cards must visually distinguish facts from inferences and
   must never render an inference with the wording or styling of a fact.
4. Temporal ordering ("after") is observable. Causation ("because") is not, and is always
   an inference.
5. Absence of evidence is reported as absence of evidence ("no test run was recorded
   between T1 and T2"), not as evidence of absence.

## 3. Component overview

```text
 ┌──────────────────────┐    ┌───────────────────────────┐
 │ 1. Universal Recorder│    │ 3. Agent-specific Adapters │
 │  (project-level      │    │  (optional, per tool, only │
 │   observation)       │    │   where data is available) │
 └──────────┬───────────┘    └─────────────┬─────────────┘
            │ normalized events            │ normalized events
            ▼                              ▼
       ┌────────────────────────────────────────┐
       │ 2. Evidence / Event Store              │
       │    (local, append-only, observations)  │
       └───────────────────┬────────────────────┘
                           ▼
       ┌────────────────────────────────────────┐
       │ 4. Local Forensics Engine              │
       │    (deterministic, offline, no AI)     │
       └───────────────────┬────────────────────┘
                           ▼
       ┌────────────────────────────────────────┐
       │ 5. Replay API (local only)             │
       └──────┬──────────────────────┬──────────┘
              ▼                      ▼
 ┌──────────────────────┐  ┌──────────────────────────┐
 │ 6. React/TS Replay UI│  │ 7. Share / Export pipeline│
 └──────────────────────┘  └──────────────────────────┘

       8. Optional AI Analyst  ── reads facts from 4/5, writes
                                  labelled inferences only
```

### 3.1 Universal Recorder

Agent-agnostic. Works with any coding agent because it watches the **project**, not the agent.

**Implemented:** session lifecycle and file created / modified / deleted observation
(code: `core/recorder.py`, `core/scanner.py`, `core/ignore.py`; CLI: `agentbodycam record`).

**Planned, not implemented:**

- Git state: HEAD, branch, staged/unstaged diffs, commits made during the session.
- Commands and processes **where observable** — for example commands launched through an
  AgentBodycam wrapper (`agentbodycam run -- pytest`). We will not claim to see processes
  the recorder cannot actually observe.
- Test/build outcomes captured from wrapped commands (exit codes, durations, output).
- Native OS file watching (lower latency), `.gitignore` support, a user ignore file, and
  a dedicated rename event.

#### Lifecycle

`agentbodycam record [--project DIR] [--interval SECONDS] [--duration SECONDS]`:

1. `EventStore.create_session()` creates a new session (`created`). Each run is a new session.
2. `EventStore.mark_recording()` sets status `recording` (atomic `session.json` rewrite).
3. `session.started` is appended (payload: recorder kind, interval, hash limit, the exact
   ignore policy in force).
4. A **baseline snapshot** is taken. Files already present produce no events.
5. The project is polled every `interval` seconds (default 0.5). Each poll is a full
   snapshot diffed against the previous one; one event is appended per change.
6. On stop, a final scan is made (for requested stops), then `session.stopped` is appended
   (payload: reason, scan count, baseline size, per-kind event counts, errors observed),
   then `EventStore.mark_stopped()` sets status `stopped` and `ended_at`.

Stop reasons: `requested` (Ctrl+C, SIGTERM or Ctrl+Break, handled as a flag so a scan or
append is never interrupted half-way), `duration`, `interrupted` (a second Ctrl+C forces
`KeyboardInterrupt`; no final scan), `error` (unexpected exception; the session is still
closed when possible, then the error propagates). A crash or kill -9 leaves the session in
`recording` with no `ended_at`.

#### Change detection

Polling with the standard library only (no runtime dependencies). Per regular file the
scanner records `size`, `mtime_ns`, a file identity (`st_ino`) and a SHA-256.

- If `(size, mtime_ns, file identity)` is unchanged since the last scan, the previous hash
  is reused and the file is not reopened.
- Otherwise the file is re-hashed. **`file.modified` is emitted only if the SHA-256
  differs** (`change_evidence: "sha256"`). Touching a file or rewriting identical bytes
  produces no event. When a hash is unavailable on either side (too large / unreadable),
  a change in size or mtime is reported with `change_evidence: "metadata"`.
- A path that appears is `file.created`; one that disappears is `file.deleted`. A rename is
  a delete plus a create; there is no rename event yet.
- Within one scan, events are ordered created, modified, deleted, each sorted by path.
  `timestamp` is the **detection** time (up to one interval after the change); `mtime` is
  what the OS reported. The `scan` payload field groups events detected together.

Event payloads (`file.created`/`file.modified` describe the new state; `previous` holds the
last known state for `file.modified`/`file.deleted`):

```json
{"size": 13, "mtime_ns": 1791927834651000200, "mtime": "2026-10-13T21:43:54.651000Z",
 "sha256": "cbacb153...", "hash_status": "complete", "detected_by": "polling", "scan": 3,
 "change_evidence": "sha256", "previous": {"size": 13, "sha256": "be1177a0...", "...": "..."}}
```

#### Hashing

- SHA-256, streamed in 1 MiB chunks; files are never loaded whole and contents are
  discarded immediately after hashing. Nothing but the hex digest is kept.
- Files larger than **256 MiB** (`DEFAULT_MAX_HASH_BYTES`) are not hashed:
  `sha256: null`, `hash_status: "too_large"`, and changes are detected from size/mtime.
- Other `hash_status` values: `complete`, `unreadable` (open/read failed persistently),
  `unstable` (the file kept changing while being hashed).
- Files are opened without following links (`O_NOFOLLOW` on POSIX;
  `FILE_FLAG_OPEN_REPARSE_POINT` on Windows) and the opened file's identity is checked
  against the listed one, so a file swapped for a symlink mid-scan is never read through.
- On Windows files are opened with read/write/delete sharing so hashing never blocks an
  editor deleting or renaming the file. (Windows still refuses to rename *another* file
  over a file that is open, so an atomic save that coincides with a hash can fail for that
  instant; editors normally retry.) On POSIX, FIFOs and devices are never opened.

#### Default ignore policy

Matching is per path component (file or directory name), **case-insensitive**. A
matching directory is pruned (never listed); a matching file is never opened. Ignored
entries appear in no event. The exact policy is also written into `session.started`.

| Kind | Names / patterns |
|---|---|
| Ignored names (anywhere in the tree) | `.agentbodycam`, `.git`, `.hg`, `.svn`, `.venv`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox` |
| Sensitive (never opened, hashed or named) | `.env`, `.env.*`, `.envrc`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_rsa.*`, `id_dsa`, `id_dsa.*`, `id_ecdsa`, `id_ecdsa.*`, `id_ed25519`, `id_ed25519.*` |
| Noise | `*.swp`, `*.swo`, `*.swx`, `*~`, `.#*`, `.DS_Store`, `Thumbs.db` |

`.gitignore` is **not** parsed yet: other build output (for example `dist/`) is recorded.
A `.git` *file* (worktrees/submodules) is ignored as well.

#### Path safety

- Paths are built only from the resolved project root plus directory-entry names, and
  stored as normalized project-relative POSIX paths (validated by the event model). Names
  that cannot be represented (e.g. containing `\` on POSIX, or undecodable bytes) are
  skipped and reported as `error.observed` without a path.
- Symlinks are never followed (`lstat`). On Windows, junctions and any other
  name-surrogate reparse point are treated as links and skipped; non-link reparse points
  such as cloud placeholders are scanned normally.
- Only regular files are observed; FIFOs, sockets and devices are skipped.
- The recorder writes nothing except through `EventStore`, which writes only inside
  `.agentbodycam/`.

#### Errors and races

- A file that disappears between listing and hashing, or is replaced or still being written
  while hashed, keeps its previous state for that scan; the next scan settles it. A race
  is never reported as a deletion or modification by itself.
- A read failure (locked, permission denied) is retried silently once. If it persists into
  the next scan, one `error.observed` is recorded (payload: `operation`, `error` type name,
  `errno`, OS `detail` text — never absolute paths or contents), and the file is tracked
  with `hash_status: "unreadable"`. The same ongoing problem is reported once, not on every
  scan; it may be reported again if it clears and recurs.
- A directory that cannot be listed produces one `error.observed`, and the files known
  under it are kept (not reported as deleted).
- One bad file never stops the recording.

### 3.2 Evidence / Event Store (implemented)

Code: `models/events.py`, `models/session.py`, `core/store.py`.

**Storage layout** (inside the project root; never written anywhere else):

```text
.agentbodycam/
  .gitignore                 "*" - recordings are never committed
  sessions/
    <session-id>/
      session.json           session metadata (pretty JSON, sorted keys, atomic rewrite)
      events.jsonl           append-only, one canonical JSON event per line
```

**Session** (`session.json`, `schema_version` 1): `session_id`, `project_root` (absolute),
`started_at` / `ended_at` (UTC), `status` (`created` | `recording` | `stopped`),
`agentbodycam_version`, and `platform` (OS name/release, machine, Python version and
implementation). Hostname, username and environment variables are deliberately not stored.

Session IDs look like `20261014T024500Z-9f2c4e7a1b3d5f60`: UTC start time (so IDs sort
chronologically) plus 64 bits from `secrets`. Only that exact lowercase pattern is accepted,
so an ID can never contain separators, `..`, drive letters or reserved device names.

**Event** (`events.jsonl`, `schema_version` 1):

| Field | Meaning |
|---|---|
| `event_id` | 128-bit random hex |
| `session_id` | owning session |
| `sequence` | 1, 2, 3, ... - authoritative order, contiguous |
| `timestamp` | wall-clock UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `monotonic_ns` | `time.monotonic_ns()` at stamping; only comparable within one process |
| `type` | `session.started`, `session.stopped`, `file.created`, `file.modified`, `file.deleted`, `git.snapshot`, `command.started`, `command.finished`, `check.passed`, `check.failed`, `error.observed` |
| `source` | `agentbodycam`, `filesystem`, `git`, `command` |
| `category` | `session`, `file`, `git`, `command`, `check`, `error` (fixed per type) |
| `evidence` | `observed` or `inferred` - the store accepts **only `observed`** |
| `summary` | short human-readable description |
| `path` | normalized project-relative POSIX path, or `null` |
| `payload` | JSON object with structured details |

Producers submit an `EventDraft` (type, source, summary, path, payload); the store assigns
`event_id`, `sequence`, timestamps and category.

**Guarantees**

- Lines are canonical JSON (sorted keys, compact, UTF-8, no NaN), terminated by `\n`,
  written with a single append followed by `fsync`.
- Reading is strict: blank lines, invalid UTF-8, malformed JSON, duplicate keys, unknown or
  missing fields, a truncated final line, duplicate / out-of-order / missing sequence
  numbers, duplicate event IDs and events from another session all raise
  `CorruptEventLogError` with the file and line number. Nothing is silently skipped.
- Appending to a log that fails validation is refused, so corruption is never extended.
- Every directory and file used is verified to be a real (non-symlinked, non-junction)
  path inside `<project>/.agentbodycam`; a symlink escaping the store is rejected.
- Single writer per session is assumed; a second writer is detected by file-size change and
  the log is re-validated before the next append.
- Events are capped at 256 KiB and must not contain file contents. Large blobs (contents,
  diffs, logs) will be stored content-addressed alongside the log in a later task.

### 3.3 Agent-specific adapters

Optional plug-ins that enrich sessions with agent-level context (prompts, tool calls,
agent messages) **only when that data is locally or officially available** — for example
documented log files, exported transcripts, hooks or official APIs.

- Each adapter declares exactly which event types it can provide and from which source.
- Adapters emit the same normalized event format as the recorder.
- If an agent exposes nothing, AgentBodycam still works in universal mode.
- No adapter is implemented or claimed as supported today.

### 3.4 Local Forensics Engine

Deterministic, offline analysis over the event store. No AI required. Planned outputs:

- Ordered forensic timeline.
- **First observable failure**: the earliest recorded failing test/build/command.
- Changes recorded between the last observed success and the first observed failure
  (presented as a fact about ordering, not as a cause).
- Retry and recovery detection (failure followed by later success of the same check).
- Final project state summary (files changed, diffstat, last known test/build result).
- Every finding labelled `observed` or `inferred` with links to supporting events.

### 3.5 Replay API

- Local-only HTTP/JSON API (bound to `127.0.0.1`) serving sessions, events, blobs and findings.
- Read-only over recorded data.
- Thin layer: the API exposes the store and the forensics engine; it contains no analysis
  logic of its own.

### 3.6 React/TypeScript Replay UI

- Separate frontend package, added in a later task.
- Timeline scrubbing, file/diff views, failure markers, "cinematic" replay of the session.
- Consumes only the Replay API.
- Must clearly style facts and inferences differently (see section 2).

### 3.7 Share / export pipeline

- Export sessions or excerpts as shareable artifacts: static report, clips/cards, and
  later animated or video replays.
- Redaction step before any export (paths, secrets, file contents the user excludes).
- Exports are generated locally; sharing is an explicit user action.

### 3.8 Optional AI Analyst

- Disabled by default. The product must be fully useful without it.
- Local models or bring-your-own-key providers, configured explicitly by the user.
- Reads facts from the forensics engine; writes back only findings labelled `inferred`,
  each citing the evidence it used.
- Clear disclosure of exactly what data would be sent to any external provider before sending.

## 4. Python package layout (current)

```text
src/agentbodycam/
  __init__.py      version
  __main__.py      `python -m agentbodycam`
  cli.py           argparse CLI (help, version, doctor, session create/list/show, record)
  core/            application logic (doctor, store, ignore, scanner, recorder;
                   later: forensics)
  models/          typed dataclasses shared across layers (environment, events, session)
  utils/           small dependency-free helpers (today: read-only git detection)
```

The CLI is built on the standard library `argparse` to keep the runtime dependency
footprint at zero. That choice can be revisited if the command surface grows substantially.
