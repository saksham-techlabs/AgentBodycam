# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Wrapped command recording: `agentbodycam run [--project DIR] -- <command> [args...]`
  attaches to the project's active recording (bounded 1 s retry while a recorder is
  publishing its metadata; stale metadata is never used), appends `command.started`, runs
  the command as an argv list with `shell=False` and inherited stdin/stdout/stderr and
  environment, then appends `command.finished` (same random `command_id`; outcome `exited`,
  `signaled`, `interrupted` or `spawn_error`; exit code or signal; duration). Both appends
  re-verify the same session ID and lease ID while holding the session's event lock, so
  nothing is written into another session or after `session.stopped`. Without an active
  recording the command is not run. `run` exits with the command's status (AgentBodycam's
  own failures: 125/126/127/130). Ctrl+C keeps `run` alive until its child exits; SIGTERM
  is forwarded on POSIX. Windows batch scripts are refused instead of being run through a
  hidden `cmd.exe`. Output, stdin and environment are never recorded.
- Deterministic command-line redaction for recorded evidence (`core/redact.py`):
  sensitive option/assignment values, URL credentials and query secrets, recognisable API
  keys/tokens, random-looking strings, absolute paths (project-relative or
  `<external-path>`), with item/length/size caps and `argv_redacted` / `argv_truncated`
  flags.
- `EventStore.append_recording_event()` (checks `recording` status, and an optional
  caller check, while holding the session's event lock) and
  `EventStore.stop_recording_session()` (appends `session.stopped` and marks the session
  `stopped` under one continuous lock hold).
- Process-safe event appends: `EventStore.append_event` serialises writers with a
  per-session OS lock (`.agentbodycam/locks/<session-id>.events.lock`) plus an in-process
  thread lock, revalidates bytes appended by other writers before assigning the next
  sequence, and verifies its own append. Many processes can now append to one session
  without duplicate, missing or interleaved events.
- Standard-library-only cross-platform file lock (`msvcrt.locking` on Windows,
  `fcntl.flock` on POSIX) with bounded timeouts, a non-blocking probe, symlink/junction
  rejection, and automatic release by the OS when the holder dies.
- One recorder per project: `agentbodycam record` holds an OS-backed active-recording
  lease (`.agentbodycam/active.lock`) for its whole lifetime and publishes
  `.agentbodycam/active.json` (session ID, PID, start time, version, lease ID). A second
  recorder fails cleanly without creating a session. Metadata left by a crashed recorder
  is detected as stale and replaced automatically.
- `agentbodycam session active [--project DIR]` (read-only) and the internal
  `get_active_session()` discovery API.
- Read-only Git evidence recording in `agentbodycam record`: a `git.snapshot` event is
  appended after the session starts and whenever the observable Git state changes (branch,
  HEAD commit, detached/unborn HEAD, upstream ahead/behind from local refs, staged /
  unstaged / untracked / conflicted paths, clean/dirty). HEAD moves record the previous and
  new SHA, their relation and the subjects of new commits. Uses only `rev-parse`,
  `status --porcelain=v2 -z`, `merge-base --is-ancestor` and `log`, without a shell, with
  timeouts, output caps and `GIT_OPTIONAL_LOCKS=0`; Git is polled every 2 s. Stores no
  diffs, commit bodies, author emails, remote URLs or credentials; sensitive paths are only
  counted. Non-Git projects, a missing Git executable and Git failures never stop file
  recording; failures become de-duplicated `error.observed` events.
- Universal file recorder: `agentbodycam record [--project DIR] [--interval S]
  [--duration S]`. Creates a new session, marks it `recording`, records `session.started`,
  takes a baseline snapshot, then polls (standard library only) and records `file.created`,
  `file.modified` (only when the SHA-256 differs, or size/mtime when no hash is available)
  and `file.deleted` with size, mtime and streaming SHA-256 evidence. Never stores file
  contents. Clean stop on Ctrl+C appends `session.stopped` and sets `ended_at`.
- Default ignore policy (`.agentbodycam`, `.git`, virtualenvs, `node_modules`, caches,
  editor noise) and sensitive-file exclusion (`.env`, `.env.*`, `.envrc`, `*.pem`, `*.key`,
  ...). Symlinks and junctions are never followed.
- `EventStore.mark_recording()` / `mark_stopped()` for atomic session status transitions.
- Typed evidence event model (`Event`, `EventDraft`) with observed/inferred evidence kinds,
  and a `Session` model with safe random, time-sortable session IDs.
- Local append-only event store in `.agentbodycam/sessions/<id>/` (`session.json`,
  `events.jsonl`), with strict validation, path-traversal and symlink protection, and an
  automatic `.agentbodycam/.gitignore`. Only observed evidence is accepted.
- CLI: `agentbodycam session create | list | show`.
- Project foundation: `src` layout Python package, `pyproject.toml`, MIT license.
- Minimal CLI: `agentbodycam --help`, `--version` and a read-only `doctor` command
  (Python version, OS, current directory, Git availability).
- Quality tooling: Ruff (lint + format), mypy (strict), pytest, and `scripts/check.py`.
- Architecture and product-scope documents.

### Changed

- `EventStore` no longer assumes a single writer per session.
- The recorder appends through `append_recording_event()` and stops through
  `stop_recording_session()`.
- No event of any kind can be appended once a session log contains `session.stopped`
  (`SessionClosedError`), even if the session status was left `recording` by a crash.
- `mark_recording()` / `mark_stopped()` take the session's event lock.
- On Windows, atomic replacement of `session.json` / `active.json` is retried briefly when
  another process has the file open; active-session discovery treats a transient read
  error as `invalid` instead of raising.

### Fixed

- A process could append an event after `session.stopped`: the recorder released the event
  lock between appending `session.stopped` and marking the session stopped.
