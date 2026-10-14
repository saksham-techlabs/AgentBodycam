# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Local, authenticated, read-only Replay API: `agentbodycam replay-api [--project DIR]
  [--host 127.0.0.1|::1|localhost] [--port N]` (standard library `ThreadingHTTPServer`, no
  new dependencies). Transport-independent `ReplayService` (`core/replay.py`) over
  `EventStore` and `analyze_session()` (no second analysis), HTTP layer in
  `core/replay_server.py`, response models in `models/replay.py`. Routes (GET/HEAD):
  `/api/v1/health` (unauthenticated, nothing sensitive), `/api/v1/project`,
  `/api/v1/sessions` (newest first), `/api/v1/sessions/<id>`,
  `/api/v1/sessions/<id>/report` (byte-identical to `analyze --json`) and
  `/api/v1/sessions/<id>/timeline?after_sequence=N&limit=N` (1-500, strictly validated,
  same `TimelineItem` as the report, no raw payloads). Loopback binding only (public and
  LAN addresses refused); a new `secrets.token_urlsafe(32)` bearer token per start, memory
  only, compared in constant time; no CORS, foreign `Origin`/`Sec-Fetch-Site` and
  DNS-rebinding `Host` headers refused; `no-store`, `nosniff` and other defensive headers;
  every other method is 405; JSON errors with fixed messages; no default access log. Live
  sessions report `analysis_complete: false`; crashed ones `ended_without_stop`. Starting
  the server creates nothing; requests write nothing and run no process.
- `core.forensics.timeline_item` is now public (the replay timeline reuses it).
- Local deterministic forensics engine (`core/forensics.py`, typed report in
  `models/forensics.py`) and `agentbodycam analyze [--project DIR] [--json] <session-id>`.
  Read-only over stored evidence (writes nothing, runs no Git, no AI, no network).
  Reconstructs the sequence-ordered timeline, the first observable failure (`check.failed`,
  or a wrapped command with outcome `spawn_error`/`signaled`/`interrupted` and no check;
  a plain non-zero exit is not a failure), retries, observed recoveries and failures
  unresolved in the recording for the *same* check (SHA-256 signature over the check kind
  and name and the recorded executable, redacted argv and working directory; lossy argv is
  never grouped), file changes observed between the last pass and the first failure and
  between a failure and the later pass, incomplete commands, `error.observed` events, the
  files observed changing and the last recorded Git state. Contradictory command/check
  relationships are reported as warnings and excluded, never repaired. Live sessions are
  analysed as a snapshot (`analysis_complete: false`); crashed ones are reported as
  `ended_without_stop`. Every finding is `observed` and lists its evidence event IDs;
  lists are bounded with explicit `total`/`truncated`. Identical session bytes give an
  identical report. Findings state order only, never causes.
- Deterministic check result recording: `agentbodycam run --check test|build|lint|typecheck
  -- <command>` (authoritative), plus conservative automatic recognition of a fixed
  whitelist (`pytest`, `python -m pytest|unittest|build|mypy|pyright|ruff check`,
  `cargo|go|dotnet test|build`, `ruff check`, `eslint`, `mypy`, `pyright`,
  `tsc --noEmit`, and the `test`/`build`/`lint`/`typecheck` scripts of npm, pnpm and
  yarn), also behind an explicit `cmd /c` or `sh -c` wrapper with plain words only.
  Unrecognised commands stay plain commands. After `command.finished` is recorded,
  `check.passed` (exited with status 0) or `check.failed` (`nonzero_exit`, `signaled`,
  `interrupted`, `spawn_error`) is appended with a random `check_id` and the command's
  `command_id`, through the same live-recording guard; never after `session.stopped`.
  Check names are generated from known tools, never from arguments. Output is not read
  and no cause is inferred. Classification is a pure module (`core/checks.py`) and never
  changes the argv that is run.
- `looks_secret()` in `core/redact.py`.
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

- SECURITY.md describes what is recorded accurately (metadata such as paths, hashes, Git
  state and redacted arguments; never file contents, diffs or command output) and
  documents the Replay API's security model. The no-network test now separates
  prohibited external network activity from the Replay API's loopback IPC.
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

- Automatic check classification no longer turns informational or discovery-only runs of
  recognised tools (`pytest --version`, `mypy --help`, `ruff check --help`,
  `pytest --collect-only`, `cargo test --no-run`, `go test -list X`,
  `dotnet test --list-tests`, `tsc --noEmit --showConfig`, `eslint --print-config`,
  `npm test -- --help`, ...) into `check.passed` events. They stay plain commands;
  explicit `--check` still classifies them.
- Several processes creating `.agentbodycam/` subdirectories (e.g. `locks/`) at the same
  moment could fail with `FileExistsError` (check-then-create race in the store).
- A process could append an event after `session.stopped`: the recorder released the event
  lock between appending `session.stopped` and marking the session stopped.
