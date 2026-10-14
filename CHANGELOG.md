# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Browser-local **WebM replay clip** export. The Export panel now offers Share card,
  **Replay clip** (WebM · 1280×720) and JSON summary. The replay clip is a generated
  animation of recorded evidence, not a screen capture: an intro, session stats, the
  first observable failure, the observed recovery (or UNRESOLVED IN THIS RECORDING), the
  final (or, while live, latest) recorded state and an outro, skipping scenes without a
  matching finding. It is planned from the same privacy-safe `ShareCardModel` only
  (`planReplayClip()`, a pure, deterministic scene/timeline model), drawn frame by frame
  with Canvas 2D on an off-screen canvas (known shapes and approved strings only; no
  images, SVG rasterization, WebGL or DOM capture), and encoded by the browser's
  `MediaRecorder` from `canvas.captureStream(30)`, preferring VP9, then VP8, then plain
  WebM. Fixed 30 fps, 8 s without findings, 12 s at most, never more than 15 s. The panel
  shows the format, approximate duration, scene list, a privacy note and browser support;
  unsupported browsers see "Replay clip export is not supported in this browser." Export
  WebM shows Preparing…, Rendering N %, Encoding… and Done, blocks duplicate starts and
  offers Cancel; cancelling, closing the dialog or unmounting stops the frame timer, the
  recorder and every stream track and releases the canvas. Recorder errors, a recorder
  that never stops and zero-byte results are reported and save nothing. The file is
  `agentbodycam-replay.webm`; its object URL is revoked 2 s after the download click so
  large downloads are not cut off. No network request, dependency or CSP change. Still
  not implemented: GIF and MP4 export, hosted sharing, file contents/diffs,
  agent-specific adapters and an optional AI analyst.
- Local privacy-safe replay exports in the Replay UI. An **Export** button in the session
  top bar opens a small accessible panel (labelled dialog, focus moved in and returned to
  the button, Tab kept inside, Escape / Close to dismiss) with two formats and a preview:
  a 1200 × 630 **SVG share card** (`agentbodycam-replay.svg`) and a **JSON summary**
  (`agentbodycam-summary.json`, `"format": "agentbodycam-share-summary"`, `"version": 1`).
  Both are built in the browser from a dedicated `ShareCardModel` that copies only
  approved report fields: counts (events, files changed, failed / passed checks,
  observable failures, observed recoveries, unresolved failures), the sequence number of
  the first observable failure and of the first observed recovery, the recording state
  and the final check results. Labels are rebuilt from structured fields (`Test failed:
  pytest`, `Command could not start`); free-text summaries are never used, and check
  names that are not plain generated tool names are replaced by a generic label. Exports
  never contain the project name or path, session ID, event IDs, timestamps, file paths,
  argv, Git branch / SHA / remote, environment, file contents, hashes, tokens or raw
  payloads. A live session is worded as a snapshot ("LIVE · SO FAR", "LATEST RECORDED
  STATE"). The SVG is produced from an explicit element tree (presentational elements
  only, allowlisted attribute names, every text node and value XML-escaped, only local
  `url(#…)` gradient references, system fonts); the same tree renders the preview.
  Downloads use a local Blob and object URL that is revoked 1 s after the click. No
  network request, clipboard write, dependency or CSP change. PNG export is not offered:
  rasterizing in the page would require loosening the UI's `img-src 'self'` policy.
  Playback keyboard shortcuts are ignored while focus is inside the dialog. Still not
  implemented: GIF/video export, hosted sharing, file contents/diffs, agent-specific
  adapters and an optional AI analyst.
- Replay UI visual styling (still under development; presentation only, no change to
  playback logic, timing, pagination, the API or event schemas). The Timeline tab's
  controls, status, scrubber, marker rail and current event now form one "replay
  theater" that stays below the top bar while the timeline scrolls on windows of at
  least 900 × 720 px (non-sticky with wrapping controls on smaller windows; the timeline
  and detail panel are sized so nothing slides under it). The "Now replaying" card is a
  larger hero whose colour follows the evidence: neutral blue for ordinary events, green
  for passing checks and OBSERVED RECOVERY, red for failures and UNRESOLVED IN THIS
  RECORDING, the strongest (still restrained) red for the FIRST OBSERVABLE FAILURE, amber
  for observed errors; it is derived only from report markers and event types. Timeline
  rows separate CURRENT (playback cursor) from DETAIL (event open in the detail panel),
  and the rail is tinted up to the current event. The scrubber distinguishes the played,
  loaded and known-but-unloaded spans, has a clearer thumb, a legend and an enlarged
  marker for the current event. Clear primary / secondary / tertiary control hierarchy,
  a compact speed group, a subtle keyboard-shortcut hint, a status dot that pulses only
  while playing or loading (LIVE EDGE stays informational), quieter filters and search,
  timeline-shaped loading skeletons and busy indicators. Short (140-200 ms) transitions,
  all disabled under `prefers-reduced-motion`. The top bar now keeps a single line on
  desktop widths. `replay --help` no longer lists the styling as missing. Still not
  implemented: possible final micro-polish, share/export, GIF/video export, file
  contents/diffs, agent-specific adapters and an optional AI analyst.
- Replay UI playback controls (still under development; at this stage without the
  replay visual styling above): a replay control bar above the timeline with Play / Pause (Replay at the
  end), previous / next event, previous / next important event, speed 0.5x / 1x / 2x / 4x
  (default 1x) and a scrubber over event sequence numbers that shows loaded vs. known
  events and markers for the first observable failure, check failures, observed
  recoveries and unresolved failures (clickable; one tab stop with arrow-key navigation).
  Playback is a deterministic visualization of the recorded evidence events at a fixed
  800 ms per event at 1x (1600 / 400 / 200 ms at 0.5x / 2x / 4x), never real time and
  never screen video. The current event is highlighted in the timeline, shown in a "Now
  replaying" card with safe `TimelineItem` fields only and fills the detail panel.
  Reaching the end of the loaded events loads the next page (one request at a time); a
  failed or invalid page pauses playback and keeps the position. Seeking to an unloaded
  event loads pages up to it (same 50-page cap and cancel as "Jump to event"). Jumps
  (first failure, recovery marker, important events) pause playback. Live recordings
  stop at the LIVE EDGE; an opt-in "Follow live" (off by default) advances to new events
  only when paused at the edge. Playback pauses while the tab is hidden, on tab change
  and on session change. Keyboard: Space, ←/→, Shift+←/→ (ignored while typing and on
  controls with their own keyboard handling, such as the scrubber, buttons, tabs and
  checkboxes). The LIVE EDGE status uses the informational tone; red stays reserved for
  failure evidence. Smooth scrolling only without `prefers-reduced-motion`. Engine in
  `web/src/hooks/usePlayback.ts`, pure logic in `web/src/lib/playback.ts`, controls in
  `web/src/views/PlaybackBar.tsx`. No backend, API or schema changes; `replay` help and
  banner text updated to mention playback. Still not implemented at that stage: replay
  visual styling, share/export, GIF/video export, file contents/diffs, agent-specific adapters
  and an optional AI analyst.
- Replay UI functional replay views (still under development): the session page now has
  Overview, Timeline, Findings, Files and Git / Final State tabs (URL stays
  `/session/<id>`). Paginated forensic timeline over
  `GET /api/v1/sessions/<id>/timeline` (100 per page, server cursor only, strict page
  validation, windowed rendering, keyboard navigation), with markers from the report's
  structured references: FIRST OBSERVABLE FAILURE (plus "Jump to first failure"), first
  and last failed runs and OBSERVED RECOVERY for each recovery, UNRESOLVED IN THIS
  RECORDING. Client-side filters and search over loaded events (order preserved), a
  detail panel with safe `TimelineItem` fields and "Copy event ID". Findings view (first
  failure, recoveries with observed change windows, unresolved failures, incomplete
  commands, errors observed, warnings; every reference can jump to its event), observed
  files view and last recorded Git / final check state / command summary view. Live
  sessions refresh the report and append timeline pages every 2.5 s. Typed, runtime-
  validated `ForensicReport` and `TimelinePage` models; `report()` and `timeline()` client
  methods. No backend routes or semantics changed; no raw payloads, file contents, diffs
  or causal wording. At this stage playback and scrubbing were not yet available (added
  by the playback controls entry above).
- Replay UI foundation (under development): `agentbodycam replay [--project DIR]
  [--host 127.0.0.1|::1|localhost] [--port N]` serves a React/TypeScript UI (source in
  `web/`, Vite build committed to `src/agentbodycam/ui/static/` and shipped in the wheel;
  Node.js is build-time only) from the same loopback origin as the unchanged Replay API.
  App shell, connection/server status, session dashboard (project session count, state
  summary, session list with STOPPED / LIVE / ENDED WITHOUT STOP / UNKNOWN / NOT STARTED /
  UNREADABLE states) and session overview (`/session/<id>`: recording state, times, event
  count, failures, recoveries, unresolved failures, warnings, event and command counts).
  Typed, runtime-validated, GET-only API client with abort and error handling; manual
  refresh plus 2.5 s polling of live sessions (paused while the tab is hidden).
  Static files come from an in-memory allowlist loaded at startup (`core/ui_assets.py`);
  request paths are never mapped to the file system. The document has its own strict CSP
  (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`, no remote sources); the API CSP
  is unchanged. The page receives the temporary token from `GET /ui/bootstrap` (custom
  header plus same-origin fetch metadata required; no CORS) and keeps it in memory only;
  `replay` does not print it. At this stage the timeline, detailed views and scrubbing
  were not yet available (added by the entries above).
- `hatchling` in the `dev` extra (the tests build and inspect a real wheel).
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

- Replay UI API client: every authorized request bootstraps a token when none is held;
  on a 401 it drops the token, bootstraps once and retries once (a second 401 is
  reported as unauthorized); abort is honoured at every step.
- `agentbodycam replay` help and banner text describe the new views.
- `replay-api` stays API-only (no UI routes, no token bootstrap); its banner points to
  `agentbodycam replay` for the UI. SECURITY.md documents the UI's threat model, including
  the weaker protection against other local users while `replay` is running.
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

- Replay UI reliability hardening (no new features; forensic semantics, API and privacy
  rules unchanged):
  - WebM export: a second click on Export WebM in a narrow dialog could land on Cancel,
    because Cancel was inserted where Export WebM had been; the run was cancelled and the
    panel went back to Ready. Cancel now has a fixed slot before Export WebM, the buttons
    no longer move, and Cancel returns focus to Export WebM. Every export has a run ID;
    progress, results and errors of a cancelled, closed or superseded run are ignored, so
    a late callback can never overwrite a newer export. A frame-drawing exception now
    ends the export as an error with full cleanup instead of leaving the stream running;
    a failing save step is reported. A backdrop click (or a drag that ends on the
    backdrop) no longer closes the dialog during an export, and Tab stays inside the
    dialog even when focus sits on the dialog itself.
  - Downloads: SVG and JSON object URLs are revoked 1 s after the click (2 s for WebM)
    instead of synchronously, which could cut off a download in some browsers; a failed
    click revokes immediately.
  - Sticky theater: the timeline and detail-panel heights now follow the theater's
    measured height (one `ResizeObserver`), so a playback alert or wrapped controls no
    longer push rows under the theater (previously up to about 40 px at 1024 × 720). The
    control row wraps by the theater's own width; the counter and status stay together.
  - Live sessions: polling continues through transient failures (unreachable or
    restarted server) and retries a failed timeline request, so the view recovers
    without a reload. "Follow live" now also follows the final read after the recording
    stops. An empty live poll no longer copies the event index or re-renders the
    timeline.
  - Fatal errors (session gone, access expired after the one re-bootstrap, access
    refused, server error) stop polling and clear the replay: playback paused, jumps
    cancelled, selection cleared and the export dialog closed.
  - Playback keyboard shortcuts are ignored whenever a modal dialog is open; reduced
    motion also works on engines that only implement the legacy `addListener` API.
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
