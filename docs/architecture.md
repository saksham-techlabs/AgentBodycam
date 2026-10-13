# AgentBodycam Architecture

> Status: **design document**. Most of what is described here is not implemented yet.
> Implemented today: the CLI foundation (`--help`, `--version`, `doctor`), the typed
> event/session model and the local append-only, process-safe event store (section 3.2),
> managed with `agentbodycam session create|list|show|active`, the file-system and
> read-only Git state parts of the universal recorder (`agentbodycam record`, section 3.1),
> and the one-recorder-per-project active-recording lease (section 3.1, "Active recording
> lease"). Command/test/build capture, adapters, forensics, replay, UI, export and AI are
> not implemented. This document describes the intended direction so future work stays
> coherent.

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

**Implemented:** session lifecycle, file created / modified / deleted observation, and
read-only Git state snapshots (code: `core/recorder.py`, `core/scanner.py`,
`core/ignore.py`, `core/git_observer.py`, `utils/git.py`; CLI: `agentbodycam record`).

**Planned, not implemented:**

- Git diffs (stored as content-addressed blobs, not inline in events).
- Commands and processes **where observable** — for example commands launched through an
  AgentBodycam wrapper (`agentbodycam run -- pytest`). We will not claim to see processes
  the recorder cannot actually observe.
- Test/build outcomes captured from wrapped commands (exit codes, durations, output).
- Native OS file watching (lower latency), `.gitignore` support, a user ignore file, and
  a dedicated rename event.

#### Lifecycle

`agentbodycam record [--project DIR] [--interval SECONDS] [--duration SECONDS]`:

0. The project's active-recording lease is acquired (see below). If another live recorder
   holds it, `record` fails with `error: AgentBodycam is already recording this project`
   and the other session's ID, and creates nothing.
1. `EventStore.create_session()` creates a new session (`created`). Each run is a new session.
2. `EventStore.mark_recording()` sets status `recording` (atomic `session.json` rewrite).
3. `session.started` is appended (payload: recorder kind, interval, hash limit, the exact
   ignore policy in force).
4. A **baseline snapshot** is taken. Files already present produce no events.
5. Git is observed once; if the project is in a Git repository the first `git.snapshot` is
   appended (see "Git state" below).
6. The project is polled every `interval` seconds (default 0.5). Each poll is a full
   snapshot diffed against the previous one; one event is appended per change. Git is
   looked at in the same poll when at least `git_interval` (2 s) has passed since the last
   Git observation.
7. On stop, a final scan is made (for requested stops and duration expiry; it always
   includes Git), then `session.stopped` is appended (payload: reason, scan count, baseline
   size, per-kind event counts, errors observed, `git_snapshots`, `git_status`), then
   `EventStore.mark_stopped()` sets status `stopped` and `ended_at`.

After `session.started` is appended, `active.json` is published; on stop it is removed after
`session.stopped` and the `stopped` status are written, and then the lease is released
(also on errors).

`session.started` also records `git: {enabled, executable_found, interval_seconds}`.

Stop reasons: `requested` (Ctrl+C, SIGTERM or Ctrl+Break, handled as a flag so a scan or
append is never interrupted half-way), `duration`, `interrupted` (a second Ctrl+C forces
`KeyboardInterrupt`; no final scan), `error` (unexpected exception; the session is still
closed when possible, then the error propagates). A crash or kill -9 leaves the session in
`recording` with no `ended_at`, and may leave `active.json` behind, but the OS releases the
active lock, so the next `record` starts normally.

#### Active recording lease

Code: `core/active.py`, `core/filelock.py`. One recorder per project.

```text
.agentbodycam/
  active.lock     OS lock held exclusively for the recorder's whole lifetime; never deleted
  active.json     pointer to the session being recorded (atomic write; untrusted)
```

- **The OS lock is the only authority** for "a recording is alive". It is released by the
  operating system when the recorder exits, crashes or is killed. `active.json` on its own
  proves nothing: without a held lock it is *stale*. PIDs are stored for display only and
  are never used to judge liveness (PIDs are reused).
- On start, the recorder waits at most 1 s for the lock (a discovery probe can hold it for
  an instant), then gives up with "already recording". Once it holds the lock, any existing
  `active.json` is stale by definition; it is reported (`Note: replaced stale ...`) and
  atomically replaced. No manual cleanup is ever needed.
- On stop, `active.json` is removed only if its `lease_id` is still ours, then the lock is
  released, even if closing the session failed.

`active.json` (`schema_version` 1, canonical JSON; unknown or missing fields are rejected):

```json
{"agentbodycam_version": "0.0.1", "lease_id": "<32 hex>", "pid": 20884,
 "schema_version": 1, "session_id": "20261013T222158Z-347a8ac7fd0fa27c",
 "started_at": "2026-10-13T22:21:58.123456Z"}
```

No hostname, username, environment variables, credentials or absolute paths are stored; the
project is implied by where the file lives. `started_at` equals the session's `started_at`.

**Discovery** (`get_active_session(project_root)`, CLI `agentbodycam session active`) is
read-only and creates nothing. It returns:

| State | Meaning |
|---|---|
| `active` | the lock is held by a live process, `active.json` is valid, and the named session exists, is `recording` and has the same `started_at` |
| `inactive` | no live recorder and no metadata |
| `stale` | metadata present (valid or not) but nobody holds the lock |
| `invalid` | the lock is held but metadata is missing (recorder starting/stopping), malformed, or names a missing, mismatched or non-`recording` session |

Only `active` may ever be used to attach to a session. The probe briefly takes the lock
when it is free, then re-checks after reading so a recorder that stops mid-inspection is
not reported as active.

#### Cross-platform file lock

`core/filelock.py`, standard library only:

- POSIX: `fcntl.flock(LOCK_EX | LOCK_NB)`. Windows: `msvcrt.locking(LK_NBLCK)` on one
  byte at offset 1 GiB, beyond end-of-file, because Windows byte-range locks are mandatory
  and locking real bytes would make the (empty) lock file unreadable to other handles.
- Both are owned by the open handle, so separate `FileLock` objects contend even inside
  one process, and both are released by the OS when the handle closes or the process dies.
- Blocking acquisition retries the non-blocking call with exponential backoff (1 ms to
  50 ms), always bounded by a timeout (`LockTimeoutError`); `try_acquire()` and
  `is_locked()` never wait. `is_locked()` never creates the file.
- Lock files are opened without following links (`O_NOFOLLOW` on POSIX), and the opened
  handle's identity is compared with an `lstat` of the path; symlinks, junctions and other
  reparse points, and non-regular files are refused. Lock files are never deleted.
- No fairness between waiters, and no reentrancy.

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

#### Git state

Code: `core/git_observer.py` (observation, parsing, summaries), `utils/git.py` (runner).

**Commands.** Only these read-only commands are ever run, each without a shell, with a
fixed argument list, a timeout (15 s for `status`, 5 s otherwise) and an output cap (8 MiB
for `status`, 256 KiB otherwise):

| When | Command |
|---|---|
| Repository discovery (first time, or after it was unavailable) | `git rev-parse --show-toplevel --absolute-git-dir --git-common-dir` and `git rev-parse --show-prefix` |
| Every Git poll | `git status --porcelain=v2 --branch -z --untracked-files=all --ignore-submodules=all --renames -- . ":(exclude).agentbodycam"` |
| Only when HEAD moved | `git merge-base --is-ancestor A B` (once or twice) and `git log -z --no-show-signature --format=%H%x1f%s --max-count=21 NEW [^OLD] --` |

Every command gets `-c core.fsmonitor=false -c color.ui=never` and runs with
`GIT_OPTIONAL_LOCKS=0` (status never takes `index.lock` or rewrites the index),
`GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, and with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
`GIT_OBJECT_DIRECTORY`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`, `GIT_TRACE*` and similar
redirections removed. The executable is the first `git` (`git.exe` on Windows) in an
**absolute** `PATH` entry; the current directory is never searched. On Windows Git runs
without a console and in its own process group (POSIX: its own session), so Ctrl+C aimed at
the recorder never interrupts it; a forced stop kills a running Git command.

Only SHAs validated as 40/64-hex are ever passed as arguments. Remote URLs, `git config`,
credential helpers and SSH configuration are never queried.

**Recording boundary.** The selected project directory is the boundary. If it is a
subdirectory of a repository, status is limited to it with a pathspec and paths are stored
relative to the project (`project_prefix` says where the project sits in the repository);
branch and HEAD are repository-wide. AgentBodycam's own `.agentbodycam/` is excluded.
Submodules are not inspected (`--ignore-submodules=all`). Linked worktrees work and are
reported as `worktree: "linked"`.

**Change detection.** Each poll builds a canonical snapshot payload. A `git.snapshot`
event is appended only when it differs from the last recorded one, so an unchanged
repository produces no events. The first snapshot after the session starts is always
recorded (if the project is in a repository). A project that is not in a repository
produces no Git events; if it later becomes one (`git init`), a snapshot is recorded, and
if a recorded repository disappears a `{"repository": false, "reason": ...}` snapshot is
recorded once.

**Snapshot payload** (`type: git.snapshot`, `source: git`, `path: null`):

```json
{"repository": true, "root": "C:\\Users\\me\\proj", "project_prefix": "", "worktree": "main",
 "head": {"commit": "010683c4...", "branch": "main", "detached": false, "unborn": false},
 "upstream": {"name": "origin/main", "ahead": 1, "behind": 0},
 "state": "dirty",
 "counts": {"staged": 1, "unstaged": 1, "untracked": 1, "conflicted": 0},
 "staged": [{"path": "main.py", "status": "renamed", "original_path": "app.py"}],
 "unstaged": [{"path": "README.md", "status": "modified"}],
 "untracked": ["notes.md"],
 "conflicted": [],
 "listed_paths_truncated": false, "redacted_paths": 0, "unrepresentable_paths": 0,
 "changed": ["counts", "staged", "state"], "detected_by": "polling", "scan": 12,
 "head_change": {"previous": "ff4d448b...", "current": "010683c4...",
                 "relation": "descendant",
                 "commits": [{"commit": "010683c4...", "subject": "Bump app output"}],
                 "commits_truncated": false}}
```

- `status` values: `modified`, `type_changed`, `added`, `deleted`, `renamed`, `copied`;
  conflicts: `both_modified`, `both_added`, `both_deleted`, `added_by_us`, `added_by_them`,
  `deleted_by_us`, `deleted_by_them`. A file can appear in both `staged` and `unstaged`.
- `upstream` is `null` without an upstream; `ahead`/`behind` come from local refs only
  (never fetched) and are `null` if the upstream ref is missing.
- `head.commit` is `null` (`unborn: true`) in a repository with no commits yet.
- `counts` always cover every path; each list names at most 200 paths
  (`listed_paths_truncated`), and fewer if the event would exceed ~160 KiB. Paths matching
  the sensitive-file patterns are counted in `redacted_paths` and never named.
- `head_change` is present only when HEAD's commit differs from the previous snapshot.
  `relation` is `descendant` (new HEAD contains the old one, e.g. a commit), `ancestor`
  (moved back, e.g. a reset), `diverged`, `no_previous_commit`, `unborn` or `unknown`.
  `commits` lists at most 20 commits reachable from the new HEAD but not the old one,
  newest first, with their subject line (max 200 characters) only — no body, author, email
  or patch.
- `changed` lists the top-level fields that differ from the previous snapshot.

Summaries are factual and never name an actor: `Working tree became dirty`,
`HEAD changed ff4d448 -> 010683c; working tree became clean`, `Branch changed main ->
feature`, `HEAD detached at 010683c`, `Git status changed (1 staged, 0 unstaged, 0
untracked)`.

**Git errors** never stop file recording. A failed command (corrupted repository, dubious
ownership, permission problem), timeout, oversized output, a Git executable that cannot be
started, or even an unexpected exception inside the observer becomes one `error.observed`
event (`source: git`, payload `operation`, `error`, `exit_code`, `errno`, `detail`), the
previous snapshot is kept, and the same ongoing problem is not reported again until it
clears. `detail` is the first line of Git's stderr with project/repository paths replaced
by placeholders and URL credentials redacted. If Git is not installed, nothing is reported
unless the project contains a `.git` entry (then one `error.observed` with
`git_not_found`).

### 3.2 Evidence / Event Store (implemented)

Code: `models/events.py`, `models/session.py`, `core/store.py`.

**Storage layout** (inside the project root; never written anywhere else):

```text
.agentbodycam/
  .gitignore                 "*" - recordings are never committed
  active.lock / active.json  active-recording lease (section 3.1)
  locks/
    <session-id>.events.lock OS lock serialising appends to that session
  sessions/
    <session-id>/
      session.json           session metadata (pretty JSON, sorted keys, atomic rewrite)
      events.jsonl           append-only, one canonical JSON event per line
```

`.agentbodycam/` is in the recorder's ignore list and excluded from Git status, so lock and
metadata files never become file or Git evidence.

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
- **Process-safe appends.** Any number of threads and processes may append to the same
  session. Each append:
  1. takes an in-process lock (so threads never poll each other), then the session's OS
     lock `locks/<session-id>.events.lock` (bounded wait, `StoreLockError` on timeout);
  2. revalidates the log: bytes appended by other writers since this instance last looked
     are parsed with the full strict rules (sequence continuity, unique event IDs, session
     ID, schema); a replaced or shrunken file triggers a full re-read;
  3. assigns `last_sequence + 1`, writes exactly one line with a single append, `fsync`s;
  4. checks the file grew by exactly that line (otherwise it raises and forgets its cache,
     so the next append fully revalidates), updates its cached tail and releases the locks.

  The result is no duplicate or skipped sequence numbers, no lost, overwritten or
  interleaved lines. Lock file names are derived only from validated session IDs.
  Producers that bypass `EventStore` are not protected.
- `monotonic_ns` values from different processes are not comparable; `sequence` is the
  authoritative order.
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
  cli.py           argparse CLI (help, version, doctor, session create/list/show/active,
                   record)
  core/            application logic (doctor, filelock, store, active, ignore, scanner,
                   git_observer, recorder; later: forensics)
  models/          typed dataclasses shared across layers (environment, events, session)
  utils/           small dependency-free helpers (today: Git detection and the bounded,
                   shell-free read-only Git command runner)
```

The CLI is built on the standard library `argparse` to keep the runtime dependency
footprint at zero. That choice can be revisited if the command surface grows substantially.
