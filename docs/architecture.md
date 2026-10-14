# AgentBodycam Architecture

> Status: **design document** for an early-development project that is not ready for
> production use. Parts of what is described here are not implemented yet.
> Implemented today: the CLI foundation (`--help`, `--version`, `doctor`), the typed
> event/session model and the local append-only, process-safe event store (section 3.2),
> managed with `agentbodycam session create|list|show|active`, the file-system and
> read-only Git state parts of the universal recorder (`agentbodycam record`, section 3.1),
> the one-recorder-per-project active-recording lease (section 3.1, "Active recording
> lease") and explicit wrapped-command recording (`agentbodycam run -- ...`, section 3.1,
> "Wrapped commands") with deterministic check results (`check.passed` / `check.failed`,
> section 3.1, "Check results"), the deterministic local forensics engine
> (`agentbodycam analyze`, section 3.4) and the local, authenticated, read-only Replay API
> (`agentbodycam replay-api`, section 3.5). The Replay UI is **under development**
> (`agentbodycam replay`, section 3.6: app shell, connection status, session dashboard,
> overview, paginated forensic timeline with first-failure / recovery / unresolved
> markers, replay playback controls and scrubber, findings, observed files and last
> recorded Git / final state, with the replay styled as a sticky "replay theater"), with a
> local privacy-safe export (SVG share card, JSON summary and a browser-generated WebM
> replay clip, section 3.7). Capture of commands not launched through `agentbodycam run`,
> adapters, file contents and diffs, GIF/MP4 export, hosted sharing and AI are not
> implemented.
> This document describes the intended direction so future work stays coherent.

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

**Implemented:** session lifecycle, file created / modified / deleted observation,
read-only Git state snapshots (code: `core/recorder.py`, `core/scanner.py`,
`core/ignore.py`, `core/git_observer.py`, `utils/git.py`; CLI: `agentbodycam record`), and
commands explicitly launched through `agentbodycam run -- ...` (code: `core/command.py`,
`core/redact.py`; see "Wrapped commands" below) and their test/build/lint/typecheck results
(code: `core/checks.py`; see "Check results" below).

**Planned, not implemented:**

- Git diffs (stored as content-addressed blobs, not inline in events).
- Bounded opt-in output capture for wrapped commands. Commands not launched through
  `agentbodycam run` are not observed, and we will not claim to see processes the recorder
  cannot actually observe.
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
   includes Git), then `EventStore.stop_recording_session()` appends `session.stopped`
   (payload: reason, scan count, baseline size, per-kind event counts, errors observed,
   `git_snapshots`, `git_status`) and sets status `stopped` and `ended_at` **under one
   continuous hold of the session's event lock**, so no other process can append in
   between (see section 3.2, "Recording appends and the stop").

All recorder events are appended with `EventStore.append_recording_event()`.

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

#### Wrapped commands

Code: `core/command.py`, `core/redact.py`. CLI:

```text
agentbodycam run [--project DIR] -- <command> [args...]
```

Universal mode captures **only** commands explicitly launched this way. Terminal commands,
shell history, other processes and agent-internal tool calls are not observed, and no event
ever says who (person or agent) ran a command.

**Parsing.** `main()` splits the command line at the first `--` before argparse sees it, so
everything after it is the command's argv, verbatim (a later `--`, `--project` or `--help`
belongs to the command). `run` without `--` or with nothing after it is a usage error (2).

**Attachment.** `get_active_session()` must report `active`; the session ID and lease ID
are kept. `invalid` (a live recorder is still publishing or already tearing down its
metadata, or a transient read error) is retried with backoff (10 ms doubling to 100 ms)
for at most 1 s; `inactive` and `stale` fail at once. On failure `run` prints
`error: no active AgentBodycam recording for this project` / `Start one with: agentbodycam
record`, exits 125 and **does not run the command**.

**Appends.** Before `command.started` and again before `command.finished`, the attachment
is re-confirmed (same bounded retry), then the event is appended with
`EventStore.append_recording_event(check=...)`: while holding the session's event lock the
store verifies the session is `recording` and the `check` verifies `get_active_session()`
still reports `active` with the **same session ID and lease ID**. If the recording stopped
or was replaced, nothing is appended. Before the start this aborts (`error: ...`, exit 125,
command not run); before the finish the command has already completed normally and `run`
prints `warning: the AgentBodycam recording ended before this command finished; the finish
event was not recorded`. A finish event is never fabricated, backdated, written into a newer
session, or written after `session.stopped`. (If the recorder is killed without stopping,
its session stays `recording`; the lease check then sees `stale` and also skips the
finish.)

**Execution.** `subprocess.Popen(argv, executable=<resolved>, cwd=..., shell=False)`, with
stdin, stdout, stderr and the environment inherited unchanged; nothing from them is read or
stored. `command.started` is appended **before** the child is spawned, so anything the
child changes is detected after it. Executable resolution never involves a shell or the
current directory implicitly:

- a name containing a path separator is resolved against the working directory;
- a bare name is looked up only in absolute `PATH` entries (`.exe`, `.com` on Windows);
- Windows batch scripts (`.bat`, `.cmd`, e.g. `npm.cmd`) are refused with
  `ShellRequiredError` (exit 126, recorded as a spawn error) because `CreateProcess` would
  silently run them through `cmd.exe`. The user can run `cmd /c ...` explicitly.

**Working directory.** The current directory if it is inside the project (recorded as its
normalized project-relative POSIX path, validated like event paths), otherwise the project
root (`"."`). A directory inside `.agentbodycam/` is refused. The absolute path is never
stored.

**Signals.** While the child runs, `run` handles SIGINT and SIGBREAK (Windows) or SIGINT,
SIGTERM and SIGHUP (POSIX) itself and keeps waiting, so it always outlives its child and
records its real end:

- Windows: a console Ctrl+C / Ctrl+Break is delivered by the console to every process
  attached to it, including the child (same console and process group); nothing is
  forwarded. A Python child ended by Ctrl+Break exits with `STATUS_CONTROL_C_EXIT`
  (`0xC000013A` = 3221225786), which is recorded and propagated as is.
- POSIX: SIGTERM is always forwarded to the child. SIGINT and SIGHUP are forwarded only
  when `run` is not in its terminal's foreground process group (a terminal Ctrl+C was
  already delivered to the child by the kernel; forwarding would deliver it twice).
- Nothing is escalated: like a shell, `run` never kills a child that ignores the signal.
  Only the direct child is ever signalled; no process trees or groups are killed.
- An interrupt that arrives before the child is spawned prevents the spawn; the finish
  event then says `outcome: interrupted` with `exit_code: null`, and `run` exits 130.
- A hard kill of `run` itself (SIGKILL, `TerminateProcess`) cannot be handled; the child
  then continues on its own and no finish event is recorded.

Tested with real processes: Ctrl+Break to the wrapper's process group on Windows; SIGINT
and SIGTERM to the wrapper only on POSIX.

**Exit status.** `run` exits with the child's exit status; 128+N when POSIX signal N ended
the child; on Windows NTSTATUS values such as `0xC000013A` are reproduced exactly.
AgentBodycam's own failures: 125 (setup: no active recording, unsafe directory, store
error), 126 (cannot execute: permission, batch script), 127 (not found), 130 (interrupted
before the spawn). argparse usage errors exit 2.

**`command.started`** (`source: command`, `path: null`):

```json
{"command_id": "bd1454a760fd5a4d1c6ca6b53b7b717f", "executable": "python",
 "argv": ["python", "-m", "pytest", "--token", "<redacted>"], "argument_count": 4,
 "argv_redacted": true, "argv_truncated": false, "argv_paths_rewritten": false,
 "working_directory": ".", "shell": false, "stdio": "inherited",
 "output_recorded": false, "environment_recorded": false,
 "wrapper": {"kind": "agentbodycam-run", "pid": 25788}}
```

Summary: `Command started: python -m pytest --token <redacted>` (redacted form only,
at most 300 characters of command text).

**`command.finished`** (`source: command`, `path: null`):

```json
{"command_id": "bd1454a760fd5a4d1c6ca6b53b7b717f", "outcome": "exited", "exit_code": 0,
 "signal": null, "signal_name": null, "duration_ms": 1162, "pid": 28384,
 "interrupts": [], "error": null}
```

| `outcome` | Meaning |
|---|---|
| `exited` | the child exited by itself; `exit_code` holds its status |
| `signaled` | POSIX: a signal ended the child; `signal` / `signal_name` set, `exit_code: null` |
| `interrupted` | `run` received an interrupt (`interrupts` lists them, e.g. `["SIGINT"]`) while the child ran; `exit_code` / `signal` still say how the child ended (both null if it was never started) |
| `spawn_error` | the child could not be started; `exit_code: null`, `pid: null`, `error: {"type": "FileNotFoundError", "errno": 2}` (type and errno only, never the OS message or a path) |

`duration_ms` is measured with a monotonic clock from just before the spawn until the
child was reaped. Summary: `Command finished: python -m pytest (exit 0)`,
`(... interrupted, signal SIGINT)`, `(could not start: FileNotFoundError)`.

**Redaction** (`core/redact.py`, deterministic, applied only to recorded evidence; the
child always receives the original argv):

- Names are normalised (lowercase; `_` and `.` become `-`) and are sensitive if they
  contain `password`, `passwd`, `passphrase`, `secret`, `token`, `apikey`/`api-key`,
  `accesskey`/`access-key`, `privatekey`/`private-key`, `credential`, `auth`, `bearer`,
  `cookie`, `session-key` or `signature`, or equal `pass`, `pwd`, `key`, `pat` or `sig`.
- `--name VALUE` / `-name VALUE`: VALUE becomes `<redacted>` (unless it starts with `--`).
  `--name=VALUE`, `name=VALUE`, `NAME: VALUE` (e.g. `Authorization: Bearer x`) and
  `pass:VALUE`: the value is redacted. `Bearer X` / `Basic X` / `Token X` inside an item,
  and the item after a bare `Bearer`, are redacted.
- URLs: `scheme://userinfo@` becomes `scheme://<redacted>@`; query parameters with
  sensitive names are redacted.
- Anywhere in an item: GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`),
  GitLab (`glpat-`), `sk-...` (OpenAI/Anthropic style), AWS access key IDs, Slack `xox?-`,
  Google `AIza...`, Stripe, npm tokens, JWTs, any 32+ character run of token characters
  mixing upper case, lower case and digits, and whole items containing a private-key block.
  Lower-case hex (Git SHAs) is kept.
- Paths: an absolute path (or `~...`), alone or as the value of `name=`, becomes its
  normalized project-relative POSIX form (`.` for the root) if inside the project, otherwise
  `<external-path>`. The project and home directories embedded elsewhere in an item become
  `<project>` / `<home>`. `executable` is only the program's file name. Drive paths
  (`C:\...`) and UNC paths (`\\server\...`) are absolute on every platform; on Windows a
  driveless rooted path with at least two segments (`/home/me/x`, `\Users\me\x`) is too,
  while single-segment switches such as `/c` or `/p:Name=value` are kept.
- Bounds: at most 64 items, 256 characters per item, 4096 characters in total; redaction
  happens before truncation. `argv_redacted`, `argv_truncated` and `argv_paths_rewritten`
  record what happened; `argument_count` is the real number of arguments.

Redaction is a safety net, not a guarantee: a secret in an unrecognised position or format
can still be recorded.

**Concurrency.** Any number of `agentbodycam run` processes may run against one recording
at once; the store's per-session lock serialises their appends with the recorder's.

#### Check results

Code: `core/checks.py` (pure: no processes, files or output), wired in `core/command.py`.

```text
agentbodycam run [--project DIR] [--check {test,build,lint,typecheck}] -- <command> [args...]
```

A check event records an **observed outcome** ("pytest exited 1", "Test check failed"),
never a cause ("this edit fixed the tests"). Each invocation is an independent check.

**Classification.** `--check KIND` is authoritative and overrides automatic
classification (an invalid KIND is a usage error, exit 2, nothing runs), even for an
informational run such as `pytest --version`. Without it, automatic classification means
"strong evidence that this invocation executes the check", not merely "this program is a
test/lint/build/typecheck tool": the argv is matched against this whitelist of commands
that appear to actually execute a check, and nothing else:

| Kind | Recognised (program base name, case-insensitive, `.exe`/`.cmd`/`.bat`/`.com` ignored) | Name |
|---|---|---|
| test | `pytest ...` | `pytest` |
| test | `PY -m pytest ...`, `PY -m unittest ...` | `pytest`, `unittest` |
| test | `cargo test`, `go test`, `dotnet test` (subcommand first) | `cargo test`, ... |
| build | `PY -m build ...` | `build` |
| build | `cargo build`, `go build`, `dotnet build` | `cargo build`, ... |
| lint | `ruff check ...`, `PY -m ruff check ...` | `ruff` |
| lint | `eslint ...`, `npx eslint ...` | `eslint` |
| typecheck | `mypy ...`, `PY -m mypy ...`, `pyright ...`, `PY -m pyright ...`, `npx pyright ...` | `mypy`, `pyright` |
| typecheck | `tsc ... --noEmit ...` (not `--noEmit false`), `npx tsc ... --noEmit ...` | `tsc` |
| any | `npm test`, `npm run[-script] S`, `pnpm [run] S`, `yarn [run] S`, S in `test`, `build`, `lint`, `typecheck` | `npm test`, `npm build`, `pnpm lint`, ... |

`PY` is `python`, `python3`, `python3.N` or the Windows launcher `py` (optionally with a
version flag such as `-3.12`), followed only by value-less flags from `-B -E -I -O -OO -q
-s -S -u`, then `-m MODULE`. Anything else (`python script.py`, `python -W error -m
pytest`, `npm run custom`, `npm --prefix x test`, `make`, `bash build.sh`, `cargo clippy`,
`ruff format`) is **not classified** and stays a plain command.

**Informational / no-run invocations are not checks.** A whitelisted command is still
left unclassified when one of its own arguments (anywhere, including after `--`) makes it
print help, a version, a listing or its configuration, or do a dry run, instead of
executing the check:

| Tool | Excluded arguments |
|---|---|
| `pytest` | `-h`, `--help`, `-V`, `--version`, `--collect-only`, `--co`, `--fixtures`, `--fixtures-per-test`, `--markers`, `--setup-plan`, `--setup-only` |
| `unittest` | `-h`, `--help` |
| `build` | `-h`, `--help`, `-V`, `--version` |
| `mypy` | `-h`, `--help`, `-V`, `--version` |
| `pyright` | `-h`, `--help`, `--version` |
| `ruff check` | `-h`, `--help`, `-V`, `--version`, `--show-settings`, `--show-files` |
| `eslint` | `-h`, `--help`, `-v`, `--version`, `--print-config`, `--env-info`, `--init` |
| `tsc` (case-insensitive) | `-h`, `--help`, `-?`, `-v`, `--version`, `--all`, `--showConfig`, `--init`, `--listFilesOnly` |
| `cargo test` | `-h`, `--help`, `--no-run`, `--list` (harness, after `--`) |
| `cargo build` | `-h`, `--help` |
| `go test` (`-x` or `--x`) | `-h`, `-help`, `-n`, `-list`, `-list=...` |
| `go build` (`-x` or `--x`) | `-h`, `-help`, `-n` |
| `dotnet test` | `-h`, `--help`, `-?`, `/?`, `--list-tests`, `-t` |
| `dotnet build` | `-h`, `--help`, `-?`, `/?` |
| npm / pnpm / yarn scripts | `-h`, `--help`, `-?`, `-v`, `-V`, `--version` after the script name (the underlying tool is unknown) |

The reason is a forensic invariant: `pytest --version` or `mypy --version` exiting 0 says
nothing about the project's tests or types, so it must never become a `check.passed` that
a future forensic engine could read as "the test suite passed" or "type checking passed".
Matching is exact per argument; a flag's *value* (`pytest -k version`) is not a flag.

**Explicit shell wrappers** are looked through, for classification only (argv passed to
the child is never changed, and no shell is ever added): `cmd [/d] [/q] [/s] /c <command>`
(as separate arguments or one string), and `sh|bash|zsh|dash -c "<command>"` (exactly
those three arguments). The wrapped command must consist of plain words only: quotes,
escapes, `&`, `|`, `<`, `>`, `;`, `^`, `%`, `$`, backticks, globs or newlines leave it
unclassified. One level of wrapping only.

**Names** are fixed strings for recognised tools. For an explicit `--check` on an
unrecognised command the name is the program's lowercase base name (after unwrapping)
if it matches `[a-z0-9][a-z0-9._+-]{0,31}` and does not look like a token, otherwise
`command`. Names are never built from arguments.

**Pass/fail.** `check.passed` only when `outcome == exited` and `exit_code == 0`.
Otherwise `check.failed` with `reason`: `nonzero_exit`, `signaled`, `interrupted` (even if
the child then exited 0) or `spawn_error`. Output is never read.

**When a check event is written.** After `command.finished` was appended, and only then;
the attachment is re-confirmed and the event is appended with the same
`append_recording_event(check=...)` guard (same session ID and lease ID, session still
`recording`). If the finish was not recorded, no check event is attempted; if the
recording ends between the two, the check is skipped with `warning: the AgentBodycam
recording ended before the check result could be recorded`. Nothing is ever written after
`session.stopped` or into another session. An **explicit** check always gets a result,
including `spawn_error` (the requested check could not execute) and an interrupt before
the spawn; an **automatic** one only when the process actually started (a missing `pytest`
or a Windows `npm` refused without `cmd /c` is a command spawn error, not a failed check).

**`check.passed` / `check.failed`** (`source: command`, `path: null`):

```json
{"check_id": "<32 hex>", "command_id": "<the command's command_id>", "kind": "test",
 "name": "pytest", "classification": "automatic", "result": "failed",
 "reason": "nonzero_exit", "exit_code": 1, "duration_ms": 1234}
```

`exit_code` and `duration_ms` are copied from `command.finished` (`exit_code: null` for
signals, spawn errors and interrupts before the spawn). Summaries: `Test passed: pytest`,
`Test failed: pytest (exit 1)`, `Build failed: npm build (signal SIGKILL)`,
`Lint failed: ruff (interrupted, exit 0)`, `Typecheck failed: mypy (could not start)`.

`command_id` answers "which command produced this check?"; `check_id` identifies the check
itself. Concurrent wrapped commands each pair their own IDs, and their check events come
after their own `command.finished`.

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
- **Recording appends and the stop.** `append_recording_event(session_id, draft,
  check=None)` reads `session.json` **while holding the session's event lock** and refuses
  (`SessionNotRecordingError`) unless the status is `recording`; an optional `check`
  callback also runs under the lock and can veto the append. `stop_recording_session()`
  appends `session.stopped` and rewrites `session.json` to `stopped` without releasing that
  lock in between. Status transitions (`mark_recording`, `mark_stopped`) take the same
  lock. So a recording append either happens entirely before the stop or is refused.
- **Nothing after `session.stopped`.** Every append path (including the developer
  `append_event`) refuses (`SessionClosedError`) once the log contains `session.stopped`,
  checked from the revalidated log tail under the lock. This also holds if a crash left
  `session.stopped` written but the status still `recording`.
- On Windows, replacing `session.json` / `active.json` while another process has it open
  fails for an instant; the atomic replace is retried for up to 2 s.
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

### 3.4 Local Forensics Engine (implemented)

Code: `core/forensics.py` (analysis), `models/forensics.py` (typed report). CLI:

```text
agentbodycam analyze [--project DIR] [--json] <session-id>
```

Library API (what the Replay API, section 3.5, calls directly):

```python
analyze_session(store, session_id) -> ForensicReport      # reads session.json + events.jsonl
analyze_events(session, events, recorder_live=...) -> ForensicReport   # pure function
report.to_dict() / report.to_json()                       # deterministic JSON
```

**Core rule: temporal relationships are facts, causation is not.** Findings say "pytest
failed at event #10 and later passed at event #18" or "2 file changes were observed
between the failed and the later passing check". They never say a change caused or
repaired anything, never name a root cause, suspect or actor, and never claim the project
is still failing ("unresolved *in this recording*"). Task 8 produces **observed findings
only** (`finding_evidence: "observed"` on every finding); inferred hypotheses are reserved
for the optional AI analyst.

**Read-only.** Analysis loads `session.json` and `events.jsonl` through `EventStore` and
keeps every result in memory. It writes nothing (no derived events, no session changes, no
cache files), runs no Git or other process, makes no network request and never reads the
project's current files. The only other thing consulted is the read-only active-session
discovery (section 3.1), and only for a session whose status is still `recording`, to tell
a live recording from one whose recorder died.

**Order.** `event.sequence` is the only order used. `monotonic_ns` is never compared
across events and timestamps are display metadata only.

**Timeline.** One item per event: `sequence`, `event_id`, `timestamp`, `type`,
`category`, `source`, `summary`, `path`, `command_id` (command/check events) and
`evidence`. Only already-recorded, already-redacted fields; raw payloads (hashes, argv,
Git root paths) are not copied. Capped at 5000 items in the report (`total` keeps the real
count; the CLI prints at most 40).

**Observable failures.** Conservative:

| Evidence | Failure type |
|---|---|
| `check.failed` | `check_failed` (strongest evidence) |
| `command.finished` with `outcome: spawn_error` | `command_spawn_error` |
| `command.finished` with `outcome: signaled` | `command_signaled` |
| `command.finished` with `outcome: interrupted` | `command_interrupted` |

A command that has a check event is never reported again as a command failure. A plain
non-zero exit of an unclassified command is **not** a failure (`git diff --quiet` exits 1
legitimately). The **first observable failure** is the qualifying event with the lowest
sequence number; it carries its failure type, check kind/name/signature where applicable,
the event's own summary and `evidence_event_ids` (command.started, command.finished and,
for a check, the check event).

**Same-check identity.** Two check runs are the same check only if their signature is
equal:

```text
check_signature = sha256(canonical_json({
    "version": 1, "kind": <check kind>, "name": <generated check name>,
    "executable": <recorded program file name>,
    "argv": <command.started's recorded, redacted argv>,
    "working_directory": <recorded project-relative working directory>}))
```

Only evidence already stored is used; the original process argv is never involved. A run
gets **no signature** (it is "ungrouped": it still counts as an observable failure and
appears in final states, but takes part in no retry/recovery/unresolved finding, and an
info note says so) when its paired `command.started` lacks these fields or its recorded
argv is lossy: `argv_truncated`, `argv_redacted`, `argument_count` not matching, or an
argument after the program rewritten to `<external-path>` (two different paths would look
identical). The program itself (`argv[0]`) is accepted when it was rewritten to
`<external-path>` because absolute interpreter paths always are; the recorded executable
file name stands in for it. So `pytest tests/auth` and `pytest tests/billing`, or the same
argv in two working directories, are different checks; missing a recovery is preferred
over inventing one. When several signatures share a name, `display_name` adds the first 8
hex characters (`pytest [2221cf76]`).

**Relationship validation.** Per `command_id`: `command.started` -> `command.finished` ->
optional check event. Indexes are built in the single pass. Contradictions produce a
`warning` (code, message, event IDs) and **exclude that command from all findings**;
nothing is repaired or guessed:

| Code | Situation |
|---|---|
| `duplicate_command_started` / `duplicate_command_finished` / `duplicate_check` | the same `command_id` twice |
| `command_finished_without_start`, `command_started_after_result` | finish without an earlier start |
| `check_without_command_start`, `check_before_command_finished` | check with no start, or before its finish |
| `check_contradicts_command_finished` | `check.passed`/`check.failed` disagrees with the finish's outcome/exit code |
| `invalid_check_event`, `check_result_mismatch`, `check_without_command`, `invalid_command_event` | unusable payloads |

A `command.started` without a finish is **not** corruption (crash, kill, recording ended):
it is an **incomplete command** (`command_id`, start sequence, executable, summary,
evidence) reported as "Command started but no finish event was recorded." (plus "may still
be running" during a live recording); it is never called a crash. `analyze_events` itself
raises `ForensicInputError` if handed events that are not one session's complete,
contiguous, sequence-ordered log (the store already guarantees this for `analyze_session`).

**Retries, recoveries, unresolved failures.** Runs are grouped by signature (in sequence
order) and walked once, splitting history into failure periods:

- consecutive failures form one period: the first is the initial failure, the rest are
  retries (`failed_attempts`, `retry_count`); "pytest remained failing across 3 recorded
  attempts";
- a period followed by a pass of the same signature is a **recovery** (first/last failed
  and recovered event IDs and sequences, attempts, previous pass if any, change windows,
  `evidence_event_ids`), summarised as "pytest failed at event #10 and later passed at
  event #18 after 2 additional recorded attempts.";
- `PASS FAIL FAIL PASS FAIL PASS` gives two separate recoveries, never one merged result;
- a period still open at the end of the evidence is an **unresolved failure**: "Latest
  recorded pytest result: FAILED (unresolved in this recording)."

**Change windows.** File events (`file.created|modified|deleted`) strictly between two
sequence numbers, with counts per kind and at most 20 listed (`file_changes_truncated`):

- `changes_before_failure`: after the last recorded pass of the *same signature* and
  before the first failure of the period. If no such pass exists it is `null` and
  `previous_pass_note` says "No earlier passing result for this exact check was recorded."
  (no "regression window" is invented);
- `changes_between_failure_and_pass` (first failure to recovery) and
  `changes_after_last_failure` (last failure to recovery, or to the end of the evidence for
  an unresolved failure).

They are described as "file changes observed between ...", never as suspects.

**Final evidence.** `final_check_states`: the last observed result per signature (plus each
ungrouped run), with attempts and pass/fail counts. `file_change_summary`: created /
modified / deleted event counts and per-path first/last observed change, "observed during
this recording", not the current file system. `latest_git_state`: the last recorded
`git.snapshot` (repository, branch, detached/unborn, abbreviated HEAD, clean/dirty and
counts, upstream ahead/behind; the absolute repository root is not exposed), described as
the last recorded Git state, never the current one. `errors_observed`: `error.observed`
events, kept apart from project failures. `command_summary`: counts.

**Session completeness.**

| `recording_state` | When | `analysis_complete` |
|---|---|---|
| `stopped` | the log contains `session.stopped` (or the metadata says `stopped`) | true |
| `recording` | status `recording` and the live recorder (OS lock) is writing this session | false (a snapshot of the evidence read so far) |
| `ended_without_stop` | status `recording`, no live recorder for it, no `session.stopped` | true, with a note |
| `unknown` | status `recording` and liveness could not be established | false |
| `not_started` | status `created` | false |

Nothing is marked stale or repaired; the recorder is never blocked.

**Bounds.** Timeline 5000 items (CLI 40); 20 file changes per window; 60 evidence IDs per
finding (earliest ones plus the last three, `evidence_event_ids_truncated`); 200 items
per finding list, 200 paths, 10 event IDs per warning. Every capped list is
`{"items": [...], "total": N, "truncated": bool}`.

**Determinism.** No random IDs, no current time, no machine-dependent ordering and no
current file-system state in the report; lists follow sequence order (paths: sorted).
Identical session bytes give an identical report and identical JSON (sorted keys, ASCII),
except for `recording_state`/`analysis_complete` of a session whose status is still
`recording`.

**Performance.** One pass over the events builds every index; check series are walked once;
change windows use binary search over prefix counts per change kind (O(log n) plus the 20
listed items). Overall O(n log n) worst case, O(n) in practice; 16 000 events analyse in
well under a second.

### 3.5 Replay API (implemented)

Code: `core/replay.py` (`ReplayService`, transport-independent), `core/replay_server.py`
(HTTP transport), `models/replay.py` (response models). CLI:

```text
agentbodycam replay-api [--project DIR] [--host 127.0.0.1|::1|localhost] [--port N]
```

```text
.agentbodycam stored evidence
        -> EventStore reads + analyze_session()      (no second analysis)
        -> ReplayService                             (read-only queries, typed results)
        -> ReplayServer (http.server, loopback)      (auth, browser checks, JSON)
        -> React/TypeScript Replay UI (section 3.6, under development)
```

**Thin layer.** `ReplayService` has `project()`, `list_sessions()`, `session_detail(id)`,
`report(id)` / `report_dict(id)` and `timeline(id, after_sequence=, limit=)`. Every answer
comes from `EventStore` reads and `analyze_session()`; session list and detail fields
(`recording_state`, `analysis_complete`, counts) are taken from the forensic report, and
timeline items are the report's own `TimelineItem` (`core.forensics.timeline_item`). Each
call opens a fresh `EventStore` view: no shared mutable state, no cache of session data, so
concurrent requests and a live recorder appending to a session are safe (a request sees
every event fully written when it read the log). Store failures become a `ReplayError`
with a stable code and a fixed message (store exception texts contain absolute paths and
are never passed on). The HTTP layer only parses, authenticates, routes and serialises.

**Security boundary.** The API is local IPC, never network access:

| Concern | Decision |
|---|---|
| Binding | `127.0.0.1` by default; `::1` and `localhost` (bound as `127.0.0.1`, no DNS lookup) allowed; `0.0.0.0`, `::`, LAN IPs and names refused (`BindAddressError`, CLI exit 2). Port 0 by default. On Windows the socket uses `SO_EXCLUSIVEADDRUSE`; `HTTPServer`'s `getfqdn()` reverse lookup is skipped. |
| Authentication | `secrets.token_urlsafe(32)` per server instance, memory only, printed once at startup; `Authorization: Bearer <token>` (scheme case-insensitive, exactly one header) compared with `secrets.compare_digest`. Required on every `/api/*` route except `/api/v1/health`. Query-string tokens are unknown parameters (400 when authenticated, 401 otherwise). |
| Browsers | No CORS at all: no `Access-Control-*` header on any response, OPTIONS is 405. A foreign `Origin`, `Origin: null`, or `Sec-Fetch-Site: cross-site|same-site` gets 403 `origin_not_allowed`; a `Host` header other than `127.0.0.1:<port>` / `localhost:<port>` (`[::1]:<port>` for IPv6) gets 403 `host_not_allowed` (DNS rebinding). The Replay UI is served by AgentBodycam from this same origin (section 3.6), so CORS is never needed. |
| Headers | `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; sandbox`, `Cross-Origin-Resource-Policy: same-origin`, `Referrer-Policy: no-referrer`, `Server: AgentBodycam` (no Python version). |
| Methods | GET and HEAD only. POST, PUT, PATCH, DELETE, OPTIONS, TRACE, CONNECT and unknown methods get 405 with `Allow: GET, HEAD`; their bodies are drained (up to 64 KiB) but never parsed. |
| Paths | The request target is never percent-decoded and never mapped to a file. Routes are matched on exact raw segments; a session segment must match the session-ID pattern (`^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$`) or the request gets 400 `invalid_session_id`, so `..`, `%2e%2e`, `%2f`, `%5c` cannot reach the file system. |
| Logging | `BaseHTTPRequestHandler`'s access/error logs are disabled. The optional log callback (the CLI prints it) receives `METHOD ROUTE-TEMPLATE STATUS`, e.g. `GET /api/v1/sessions/{session_id}/report 200`: no raw path, query, session ID, headers or token. Tracebacks are never printed or sent. |
| Errors | Every response, including malformed HTTP (400, 414, 431, 505) and unexpected exceptions (500), is a JSON error; responses are never in header-less HTTP/0.9 form. |
| Lifetime | Foreground only. `ThreadingHTTPServer` with non-daemon request threads joined on close and a 10 s socket timeout. Ctrl+C, Ctrl+Break and SIGTERM stop it cleanly; no service, daemon or browser launch. |

**Routes** (`api_version: 1`; JSON is deterministic: sorted keys, 2-space indent, ASCII):

| Route | Auth | Response |
|---|---|---|
| `GET /api/v1/health` | none | `{"status": "ok", "api_version": 1, "product": "AgentBodycam"}` |
| `GET /api/v1/project` | bearer | `{"api_version": 1, "session_count": N}` (no project path) |
| `GET /api/v1/sessions` | bearer | `{"api_version": 1, "total": N, "sessions": [SessionSummary...]}` newest first |
| `GET /api/v1/sessions/<id>` | bearer | `SessionDetail` |
| `GET /api/v1/sessions/<id>/report` | bearer | `ForensicReport.to_dict()` unchanged: byte-identical to `agentbodycam analyze --json` |
| `GET /api/v1/sessions/<id>/timeline` | bearer | `TimelinePage` |

There is deliberately no raw-event, payload, file or findings route: `/report` already
carries every finding, and raw payloads (hashes, argv, Git root paths) are not needed by a
replay UI.

`SessionSummary`: `session_id`, `readable`, `status`, `recording_state`,
`analysis_complete`, `started_at`, `ended_at`, `event_count`. A session whose metadata or
log cannot be read is listed with `readable: false` and the other fields `null` instead of
failing the whole list. Order: newest first by the start second in the session ID, then
`started_at`, then session ID (all descending).

`SessionDetail`: `api_version`, `session_id`, `status`, `recording_state`,
`analysis_complete`, `started_at`, `ended_at`, `event_count`, `event_counts` (per type),
`command_summary`, `observable_failure_count`, `recovery_count`,
`unresolved_failure_count`, `warning_count`. No `project_root`, platform, host or user.

`TimelinePage` (`?after_sequence=N&limit=N`): `api_version`, `session_id`,
`after_sequence`, `limit`, `items` (`TimelineItem`: `sequence`, `event_id`, `timestamp`,
`type`, `category`, `source`, `summary`, `path`, `command_id`, `evidence`), `total` (events
stored when the page was read), `next_after_sequence` (the last item's sequence, or
`after_sequence` for an empty page) and `has_more` (`next_after_sequence < total`). Items
are the events with `after_sequence < sequence`, at most `limit`, in sequence order;
following `next_after_sequence` visits every event exactly once and reproduces the
report's timeline (which is capped at 5000 items; the paginated timeline is not).
`after_sequence` >= 0 (default 0), `limit` 1-500 (default 100). Parameters are strictly
validated: non-integers, a `+` sign, negative or out-of-range values, empty values,
duplicates, unknown parameters, percent-encoding and query strings on routes that take
none are 400 `invalid_parameter`.

**Errors**: `{"error": {"code": "...", "message": "..."}}` with fixed messages.

| Status | Codes |
|---|---|
| 400 | `bad_request`, `invalid_session_id`, `invalid_parameter` |
| 401 | `unauthorized` (with `WWW-Authenticate: Bearer realm="agentbodycam"`) |
| 403 | `origin_not_allowed`, `host_not_allowed`, `bootstrap_not_allowed` (UI server only, section 3.6) |
| 404 | `not_found` (route), `session_not_found` |
| 405 | `method_not_allowed` |
| 500 | `session_unreadable` (corrupt session), `store_unsafe` (symlinked/junctioned store), `internal_error` |

**Live and crashed sessions.** Nothing special: the report comes from `analyze_session()`,
so a session being recorded is `recording_state: "recording"`, `analysis_complete: false`,
and one whose recorder died is `ended_without_stop` (not repaired). The API never takes
the recording lock beyond the existing instantaneous liveness probe, never blocks or stops
the recorder, and a timeline page contains the events stored when it was read.

**Read-only guarantees** (tested): starting the server on a project without recordings
creates no `.agentbodycam/`; GET/HEAD requests, refused mutations and errors leave every
byte under `.agentbodycam/` unchanged; requests start no process (no Git); the token never
appears on disk, in responses or in the request log; the server binds loopback, performs
no DNS lookup and opens no outbound connection.

**Known limitations.** Every timeline page and report re-reads the session's log (O(events)
per request; fine for local use, no caching by design). Concurrency is one thread per
request without a cap, acceptable on loopback. HTTP/1.0 responses (one request per
connection). The token is printed to the terminal that started the server; anyone who can
read that terminal or run code as the same user can use it.

### 3.6 React/TypeScript Replay UI (under development)

Code: `web/` (React 19, TypeScript, Vite; Node.js is a build-time tool only), built into
`src/agentbodycam/ui/static/` (committed and shipped in the wheel), loaded by
`core/ui_assets.py` and served by `core/replay_server.py`. CLI:

```text
agentbodycam replay [--project DIR] [--host 127.0.0.1|::1|localhost] [--port N]
```

```text
browser page (same loopback origin)
    GET /                      -> packaged index.html (UI CSP)
    GET /assets/<hashed file>  -> packaged JS/CSS (API headers)
    fetch GET /ui/bootstrap    -> {"api_version": 1, "token": ...}   (memory only)
    fetch GET /api/v1/...      -> unchanged Replay API, Authorization: Bearer <token>
```

**Implemented (10A).** App shell with sidebar navigation and connection/server status
(connecting, online, unreachable, access expired, refused; API version; loopback host);
session dashboard (project summary as a session count, never the path; state summary;
session list, newest first, unreadable sessions shown but not openable); session overview
at `/session/<id>` (recording state, started, ended or live, duration, event count,
observable failures, observed recoveries, unresolved failures, warnings, per-type event
counts, command/check totals). Visual states map the API's `recording_state`:
`stopped` -> STOPPED, `recording` -> LIVE, `ended_without_stop` -> ENDED WITHOUT STOP,
`unknown` -> UNKNOWN, `not_started` -> NOT STARTED, `readable: false` -> UNREADABLE.
Colour language: green pass/recovered, red failed, amber warning/incomplete, blue neutral
evidence. Manual refresh; while a session is live the visible view polls every 2.5 s
(setTimeout chain, never overlapping, paused while `document.hidden`, stopped on unmount
or session change). No WebSockets or SSE. Routing uses the History API; the session ID
in `/session/<id>` is validated against the session-ID pattern on both sides.

**Implemented (10B): replay views.** The session page has five keyboard-navigable tabs
(Overview, Timeline, Findings, Files, Git / Final State); the URL stays `/session/<id>`
and the tab is component state (nothing from the report goes into the URL). The views
use only `GET /api/v1/sessions/<id>`, `/report` and `/timeline?after_sequence=N&limit=N`.

- *Timeline* (`views/TimelineView.tsx`, `hooks/useTimeline.ts`, `lib/timeline.ts`). Pages
  of 100 (500 when jumping) are requested with the server's `next_after_sequence` as the
  next cursor; the client never computes a cursor. `appendPage` rejects a page whose
  cursor does not match, whose sequences overlap or go backwards, whose event IDs repeat
  or whose `total` shrinks, as `invalid_response`. One page request is in flight at a
  time. Rows have a fixed height and are windowed (only the visible rows plus an
  overscan of 10 are mounted; no virtualization dependency). Each row shows sequence,
  time, a type label and glyph (so colour is never the only signal), summary, path and
  evidence kind. Tones: neutral/blue evidence, green `check.passed` and recoveries, red
  `check.failed` only, amber errors and warnings; file changes are never red.
  Filters and the search box (summary, path, type) work on loaded events only, keep
  sequence order and say so. The detail panel shows the `TimelineItem` fields only.
- *Markers* come from the report's structured references (`first_failure`,
  `recoveries[]`, `unresolved_failures[]`: event IDs plus sequences), never from text,
  and are applied only when both ID and sequence match a loaded event: FIRST OBSERVABLE
  FAILURE, FIRST / LAST FAILED RUN, OBSERVED RECOVERY, UNRESOLVED IN THIS RECORDING.
  "Jump to event" loads pages until the event is reached (at most 50 pages, cancellable).
- *Findings* (`views/FindingsView.tsx`): first observable failure, observed recoveries
  with their change windows (count, created/modified/deleted breakdown, bounded path list,
  truncation notice, "observed between ..." wording), unresolved failures, incomplete
  commands, errors observed and warnings, each marked OBSERVED.
- *Files* (`views/FilesView.tsx`): `file_changes` counts and the bounded path list,
  described as observed during the recording.
- *Git / Final State* (`views/GitStateView.tsx`): `final_git_state` as the *last recorded*
  Git state, `final_check_states` (ungrouped runs marked and explained) and the command
  summary.
- *Live sessions*: the existing 2.5 s poller refreshes the session detail and report and
  appends new timeline pages from the last cursor (no overlap, paused while hidden, stopped
  on unmount, session change or when recording stops, followed by one final refresh).
  Polling continues through transient failures (an unreachable or restarting server; a
  restarted server's new token is picked up by the client's one re-bootstrap), and a
  failed timeline request is retried on the next refresh, so a live view recovers without
  a reload; a malformed page is never retried. Fatal errors stop polling and replace the
  page with an error state: the session detail answering 400, 401 (after the
  re-bootstrap), 403, 404 or 500, or the report or timeline answering 404. Playback is
  paused, any jump cancelled, the selection cleared and the export dialog closed, so no
  stale replay stays on screen. A live poll that finds nothing new keeps the same loaded
  timeline object (no re-render, no index copy).
  A report briefly ahead of or behind the loaded timeline during recording is expected.

No wording attributes a failure or recovery to a file change; the UI shows no raw event
payloads, file contents, diffs, command output or absolute paths.

**Implemented (10C.1): playback engine and scrubber.** Playback is a visualization of the
recorded evidence events in sequence order, not a recording of the screen (none exists)
and not real-time: timestamps are display metadata only.

- *Engine* (`hooks/usePlayback.ts`). State: the current event and its index in the loaded
  timeline, playing, speed, follow-live, loading-next-page, seeking and stalled flags,
  plus derived total, loaded, start, end, live-edge and ended. One `setTimeout` per step
  (rescheduled after each move, cleared on pause, unmount and session change, which
  remounts the page). Interval model (`lib/playback.ts`): `800 ms / speed`, speeds 0.5x,
  1x, 2x, 4x only. Playback runs over loaded events; at the end of them it calls
  `useTimeline.loadMore()` (one request in flight, shared with paging and live polling)
  and continues; an error or invalid page pauses with the cursor unchanged. Seeking to an
  unloaded sequence or event reuses `useTimeline.loadUntil()` (500-event pages, 50-page
  cap, cancellable). The engine reports every move through `onMove(item, cause)`; the
  session page selects the event (unless the user closed the detail panel during
  automatic playback), resets filters that would hide it and asks the timeline to reveal
  it ("follow": scroll only when the row leaves the view, smooth unless reduced motion;
  "jump": upper third, instant; keyboard focus moves only for explicit jumps).
- *Controls* (`views/PlaybackBar.tsx`): Play / Pause / Replay, previous / next event,
  previous / next important event, `#n / total`, a status chip (PLAYING, PAUSED, END OF
  RECORDING, LIVE EDGE, LOADING NEXT PAGE, PAUSED · PAGE NOT LOADED), a speed group and,
  for live recordings, a "Follow live" checkbox (off by default). The scrubber is a native
  range input over sequence 1..total (the store guarantees contiguous sequences) with a
  hatched "loaded" bar and an "n / total loaded" label; a drag into unloaded events seeks
  once on release. A "Now replaying" card shows the current `TimelineItem`'s safe fields.
- *Important events* (`lib/playback.ts`): only structured evidence: event types
  `session.started`, `session.stopped`, `check.failed`, and the report's first
  observable failure, `check_failed` observable failures, recovered events of
  recoveries and last failed events of unresolved failures (these may lie beyond the
  loaded pages). Scrubber markers (failures, recoveries, unresolved) are positioned by
  sequence and bucketed to at most 200; they form one roving-tabindex toolbar.
- *Live*: playback stops at the newest recorded event (LIVE EDGE). With "Follow live" on
  and playback paused at the edge, new events from the 2.5 s poller move the cursor to
  the newest one, including the final read after the recording stops; playback never
  polls on its own.
- *Keyboard*: Space, ←/→, Shift+←/→ on `document`, ignored in text-entry controls, with
  Ctrl/Alt/Meta, when another handler already handled the key (tabs, marker rail) and,
  for Space, on buttons, links and checkboxes, which keep their own activation.
- *Accessibility and performance*: semantic buttons with labels, `aria-pressed` speeds, a
  labelled range with `aria-valuetext`, a polite status region that changes on user
  actions and state changes but not on every tick. Timeline rows are memoized, so a step
  re-renders only the rows whose current/selected state changed; marker and important-
  event lists are recomputed only when pages or the report change.

**Implemented (10C.2A): replay visual styling.** Presentation only; the playback engine,
timing model, pagination, API and event schemas are unchanged.

- *Replay theater* (`views/PlaybackBar.tsx`, `styles/app.css`): the controls, counter,
  status, scrubber, marker rail and current event form one block that is
  `position: sticky` directly below the top bar (`--topbar-height`; the top bar keeps a
  single line where the theater is sticky) when the window is at least 900 × 720 px.
  There the timeline gets a height (`--replay-timeline-height`) that keeps its rows
  below the theater at every scroll position, and the detail panel scrolls on its own
  (beside the timeline above 1100 px, capped below the theater when stacked). Both are
  derived from the theater's measured height (`--replay-theater-height`, published on
  the `.replay` container by one `ResizeObserver`, removed on unmount; a fixed fallback
  applies without it), so a playback alert or wrapped controls never push rows under the
  theater. The control row wraps by the theater's own width (a CSS container query), not
  the window's. Smaller windows get a normal, non-sticky theater with wrapping controls.
  An opaque square underlay behind the rounded surface keeps scrolled content from
  showing at its corners (no backdrop blur). No scroll listeners.
- *Current-event hero*: same safe `TimelineItem` fields as before. Its emphasis comes from
  `replayMoment()` (`lib/playback.ts`), which reads only report markers, then the recorded
  event type: first observable failure (strongest red), unresolved and other failures
  (red), observed recovery and passing checks (green), observed errors (amber), anything
  else (neutral blue). Never summary text.
- *Timeline rows*: CURRENT (playback cursor: left bar in the row's evidence colour, tint,
  visible CURRENT tag, ▶ before the sequence) and SELECTED (detail panel: neutral ring,
  DETAIL tag, `aria-current`) are composed independently; the rail is drawn per row and
  tinted up to the cursor.
- *Scrubber*: dotted known-but-unloaded span, hatched loaded span, blue played span, a
  haloed thumb and a small legend; the marker of the current event is enlarged. Same
  native range input and roving-tabindex marker toolbar.
- *Status and controls*: status chips carry a decorative dot (pulsing only while playing
  or loading); LIVE EDGE stays informational. Play / Pause / Replay is the primary
  button, single steps secondary, important-event navigation tertiary; speed is a compact
  group; shortcuts are a quiet key hint.
- *Motion*: 140-200 ms transitions (played span, rows, markers) and a short fade of the
  hero body per event. With `prefers-reduced-motion: reduce` the hero fade and pulse are
  not applied (JS) and all transitions and animations are off (CSS).

**Export** (see section 3.7): an Export button in the session top bar opens a local
export panel (SVG share card, WebM replay clip, JSON summary).

**Not implemented yet.** Possible final micro-polish (10C.2B), file contents and diff
viewer, GIF/MP4 export and hosted sharing.

**API client** (`web/src/api/`). GET only, `cache: "no-store"`, `credentials:
"same-origin"`, `redirect: "error"`, `referrerPolicy: "no-referrer"`, an `AbortSignal` per
request (requests are aborted on unmount and on route change). Responses are validated at
runtime against types that mirror `models/replay.py` exactly (`ProjectSummary`,
`SessionSummary`, `SessionsResponse`, `SessionDetail`, `ApiError`); an unexpected shape is
an `invalid_response` error rather than undefined behaviour. 400/401/403/404/500 and
network failures become typed `ApiError` kinds with fixed user-facing messages (backend
messages are not rendered). Report and timeline responses are validated the same way
(`ForensicReport`, `TimelinePage` and their parts mirror `models/forensics.py` and
`models/replay.py`), including internal consistency (ordered sequences, bounded-list
totals, page cursor and `has_more`) and that the echoed session ID and query match the
request. Session IDs and timeline parameters are validated before any request. Every
authorized request bootstraps a token if none is held; on a 401 the client drops the
token, bootstraps exactly once and retries exactly once; a second 401 is reported as
unauthorized. An aborted signal stops the sequence at every step.

**Static serving** (`core/ui_assets.py`). `load_ui_bundle()` reads the packaged `static/`
directory once at startup (through `importlib.resources`, so zipped installs work) into an
immutable in-memory table: `index.html` plus top-level and `assets/` files whose names match
`^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$` and whose suffix is in a fixed MIME map
(`.js`, `.css`, `.svg`, `.png`, `.ico`, `.webp`, `.woff2`; HTML only for the document).
Symlinks and Windows reparse points are skipped; at most 256 files, 8 MiB each, 32 MiB in
total. At request time the raw path is looked up in that table; nothing is decoded,
normalised or opened. Routes on a UI server:

| Path | Response |
|---|---|
| `/`, `/session/<valid-session-id>` | `index.html`, UI document headers (query string: 400) |
| an exact packaged asset path (e.g. `/assets/index-<hash>.js`, `/favicon.svg`) | the asset with its MIME type and the API headers (query string: 400) |
| `/ui/bootstrap` | token handoff (below) |
| `/api/...` | the unchanged Replay API (byte- and header-identical to `replay-api`) |
| anything else (`/index.html`, `/session/x`, traversal, project or `.agentbodycam` paths) | 404 JSON |

`replay-api` constructs the server without a bundle, so none of these UI routes exist there.

**UI CSP** (document only; the API CSP is unchanged): `default-src 'none'; script-src
'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self';
manifest-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none';
frame-ancestors 'none'`. No `unsafe-inline` or `unsafe-eval`: the Vite build emits only
external module scripts and one stylesheet (no module-preload polyfill, no inlined assets),
and React's `style` props are applied through the CSSOM, which `style-src` does not
restrict. No fonts, CDNs or analytics are loaded (system font stack). Other document
headers: `no-store`, `nosniff`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy:
same-origin`, `Cross-Origin-Resource-Policy: same-origin`, `Referrer-Policy: no-referrer`,
`Permissions-Policy`.

**Token bootstrap.** `GET /ui/bootstrap` returns `{"api_version": 1, "token": "<token>"}`
with the API headers only if every check passes, else 403 `bootstrap_not_allowed`:
exactly one `X-AgentBodycam-UI: bootstrap`, `Sec-Fetch-Site: same-origin`,
`Sec-Fetch-Mode: cors|same-origin`, `Sec-Fetch-Dest: empty`, no query string, plus the
usual `Host`/`Origin` checks. The custom header forces a CORS preflight for any
cross-origin caller, which is never approved; the fetch-metadata requirements exclude
navigations, frames, subresource tags and clients that do not send them. The page holds
the token in a private class field only (never URL, Web Storage, cookies or logs); the
token is never part of any `/api` response and `replay` does not print it.

Threat model: other web origins cannot get the token. Same-user programs could already
read `.agentbodycam/`. Other local users or programs that can reach the loopback port and
forge these headers *can* obtain the token while `replay` runs; this is weaker than
`replay-api`, whose token is visible only in the starting terminal, and is documented in
SECURITY.md. `replay-api` remains available for that stronger guarantee.

**Development.** `npm run dev` in `web/` starts Vite (development only) and proxies
`/api/` and `/ui/bootstrap` to `AGENTBODYCAM_DEV_API` (default `http://127.0.0.1:8765`,
run `agentbodycam replay --port 8765`), rewriting `Host` and dropping `Origin` so the
server's checks pass. `npm run build` typechecks and writes `src/agentbodycam/ui/static/`;
the build output is committed so that end users never need Node.js. Tests: Vitest +
Testing Library (`npm run test`) against a fake server that mirrors the real routes, and
`tests/test_replay_ui.py` for serving, headers, path safety, bootstrap, the CLI and
wheel packaging (`tests/test_packaging.py` covers release artifacts and the installed
wheel).

Still to come: possible final micro-polish of the replay view, file/diff views, GIF/MP4
export, and clearly different styling of facts and inferences (section 2).

### 3.7 Share / export pipeline

**Implemented (local, in the Replay UI):** a privacy-safe SVG share card, a
privacy-safe JSON summary and a browser-local WebM replay clip. Everything runs in the
browser from the already-loaded forensic report; no request is made, nothing is
uploaded, nothing is written to the clipboard, and no dependency or CSP change was needed.

```text
ForensicReport ──buildShareModel()──▶ ShareCardModel ──buildShareCard()──▶ SvgNode tree
 (web/src/lib/shareModel.ts)          (approved fields)  (lib/shareCard.ts)     │
                                            │                     ┌─────────────┴─────────────┐
                                            ▼                     ▼                           ▼
                                   buildShareSummary()     <SvgTree> preview          serializeSvg()
                                   → JSON summary          (components/SvgTree.tsx)   (lib/svg.ts)
                                                                                      → .svg download
```

- **Safe model.** `buildShareModel()` is the only code that reads the report. It copies
  named fields only: recording state, analysis completeness, counts (events, unique
  changed paths, file change events, failed / passed checks, observable failures,
  recoveries, unresolved failures), the sequence number of the first observable failure
  and of the earliest observed recovery, and the final check results (kind, name, result;
  at most 12). Rendering code receives only this model, never the report.
- **Sanitization by allowlist.** Free-text summaries, reasons, paths, command IDs, event
  IDs, check signatures, timestamps, Git state, warnings and errors are never copied.
  Labels are rebuilt from structured fields: `check_failed` becomes
  `<Kind> failed: <name>` (`Test failed: pytest`); spawn errors, signals and interrupts
  become fixed texts without the executable. Kinds must be one of
  `test|build|lint|typecheck`; names must be one or two lowercase words of
  `[a-z0-9_+-]` (≤ 24 characters, no dots, slashes, colons, uppercase, long hex or digit
  runs), else a generic label is used. Labels are bounded to 34 characters, counts to
  999,999.
- **Wording.** A stopped session shows FINAL RECORDED STATE; a live one shows
  "LIVE · SO FAR" and LATEST RECORDED STATE. The verdict is PASS only when every recorded
  final check passed and the list is complete, FAIL when any latest result failed or an
  unresolved failure exists, PARTIAL when the list is truncated, otherwise "No checks".
  No causal and no screen-video wording; the footer says "Recorded locally · No source
  code uploaded" and "Recorded with AgentBodycam".
- **SVG.** 1200 × 630, built as an explicit element tree whose tag type admits only
  presentational elements (`svg title desc defs linearGradient radialGradient stop g
  rect circle line path text tspan`). Attribute names must match `^[a-zA-Z][a-zA-Z0-9-]*$`
  and may not be `on*`, `href`, `style` or `src`; `url(...)` values must be local
  `url(#id)` references. Every text node and attribute value is XML-escaped. System font
  stacks only. Deterministic: the same model yields byte-identical output. The in-app
  preview renders the same tree through React, so preview and download match.
- **JSON summary.** `{"format": "agentbodycam-share-summary", "version": 1, ...}` with a
  fixed key list (`SUMMARY_KEYS`): `recording_state`, `analysis_complete`, `events`,
  `file_changes`, `file_change_events`, `failed_checks`, `passed_checks`,
  `observable_failures`, `observed_recoveries`, `unresolved_failures`,
  `first_observable_failure` / `first_observed_recovery` (`{event, label}` or null),
  `final_check_verdict`, `final_checks` (`{kind, name, result}`), `final_checks_total`,
  `final_checks_passing`, `final_checks_failing`. It is not the raw report.
- **Download.** Fixed file names `agentbodycam-replay.svg` and
  `agentbodycam-summary.json`; a Blob, a temporary object URL and a hidden `<a download>`
  that is removed right after the click. The URL is revoked 1 s later (immediately if the
  click throws), so the browser can still read the Blob; the timer is independent of the
  dialog, so closing it cannot leak the URL.
- **PNG** is not offered: drawing the SVG into a canvas would need a `blob:` or `data:`
  image source, which the UI's `img-src 'self'` deliberately forbids.

**WebM replay clip.** A short generated animation of the same safe facts, not a screen
capture: nothing on the screen, page or DOM is recorded. Four separate stages:

```text
ShareCardModel ──planReplayClip()──▶ ReplayClipTimeline ──drawClipFrame(ctx, t, frame)──▶ off-screen canvas
 (approved fields)  (lib/clipPlan.ts)   (scenes, pure data)   (lib/clipDraw.ts, Canvas 2D)        │
                                                                                     canvas.captureStream(30)
                                                                                                  ▼
                       agentbodycam-replay.webm ◀── downloadBlob() ◀── MediaRecorder (lib/clipRecorder.ts)
```

- **Safe data.** `planReplayClip()` takes a `ShareCardModel` only (the clip modules do
  not import the report type) and copies named fields into fixed-template scenes; every
  string a frame can draw is either a fixed phrase or a value the share model already
  approved (counts, event sequence numbers, rebuilt labels, allowlisted check names).
  `clipTexts()` lists them for review and tests.
- **Scenes.** Intro (`AgentBodycam`, `YOUR AI CODING AGENT HAS A BODYCAM`, `Recorded
  evidence replay`), session stats (events, files changed, observable failures, observed
  recoveries, with the finding markers on a `#1 … #N` event axis), FIRST OBSERVABLE
  FAILURE (`#n` and label), OBSERVED RECOVERY (or, when failures have no observed
  recovery, UNRESOLVED IN THIS RECORDING), FINAL RECORDED STATE (verdict, passing /
  failing counts, up to 4 checks, unresolved count) and an outro (`Recorded with
  AgentBodycam`, `Local-first · No source code uploaded`). Scenes without a matching
  finding are skipped. A live session says LATEST RECORDED STATE and LIVE SNAPSHOT and
  never implies the recording is complete. No causal and no screen-recording wording.
- **Duration.** 1280 × 720 at a fixed 30 fps. Scene lengths are fixed in frames (intro
  54, stats 75, each finding 60, final 66, outro 45), so a clip lasts 8 s with no
  findings and 12 s at most; `MAX_CLIP_FRAMES` (15 s) is a hard cap. Session size never
  changes the length: this is a share clip, not an event-by-event recording.
- **Drawing.** `drawClipFrame()` is a pure function of the plan and a frame number, using
  a small `CanvasRenderingContext2D` subset (rectangles, paths, arcs, gradients, text with
  a `maxWidth`) and the share card's palette and system font stacks. No images, no SVG
  rasterization, no `data:`/`blob:` image sources, no WebGL, no DOM capture. Animation is
  restrained: fade and a 16 px slide in (12 frames) and fade out (8 frames) per scene,
  staggered stat tiles with a count-up, finding markers sliding along the event axis and
  a clip progress bar with scene ticks. The UI's own reduced-motion behaviour is
  unchanged; the exported file is animated because the user asked for an animation.
- **Encoding.** `detectClipSupport()` requires `MediaRecorder`, `isTypeSupported` and
  `HTMLCanvasElement.prototype.captureStream`, then picks the first supported type of
  `video/webm;codecs=vp9`, `video/webm;codecs=vp8`, `video/webm`; otherwise the panel says
  "Replay clip export is not supported in this browser." and Export WebM is disabled.
  `recordReplayClip()` draws onto a canvas that is never attached to the page, records
  `captureStream(30)` at 6 Mbit/s with 1 s chunks, and paces frames by elapsed time
  (`performance.now()`), so the clip keeps its planned length even when timers are
  throttled; rendering therefore takes about as long as the clip. Progress is reported as
  Preparing…, Rendering N %, Encoding…, then Done. It resolves with a non-empty
  `video/webm` Blob or rejects with a typed error: `unsupported` (no 2D context, no
  capture stream, recorder construction refused), `recorder` (an `error` event, a throwing
  `start`/`stop`, or no `stop` event within 10 s), `empty` (zero bytes) or `cancelled`.
- **Cleanup.** On every outcome, before the promise settles, the frame timer and encode
  timeout are cleared, the abort listener removed, the recorder's handlers detached and
  the recorder stopped if still active, every stream track stopped, buffered chunks
  dropped and the canvas shrunk to 0 × 0. An exception while drawing a frame settles the
  export as a `recorder` error with the same cleanup. The dialog's `useClipExport` hook
  runs one export at a time (a ref guards against duplicate clicks) and gives every run an
  ID: Cancel, Close / Escape and unmount abort the run and advance the ID, and progress,
  results and errors from any run but the current one are ignored, so a late callback
  can never overwrite a newer export. A failing download step is reported ("The clip was
  created but could not be saved. No file was saved."). Cancel takes a slot that is always
  laid out before Export WebM, so starting an export never moves the button under the
  pointer; Cancel returns focus to Export WebM. While an export runs, a click on the
  backdrop is ignored (Close and Escape still cancel it).
- **Download.** Fixed file name `agentbodycam-replay.webm` (no project, session, user or
  time). The object URL is revoked 2 s after the click rather than immediately: a
  multi-megabyte Blob may still be read by the browser's download manager after `click()`
  returns, and revoking synchronously can abort the download in some browsers. The Blob
  never leaves the page.
- **Known limitations.** Chrome's `MediaRecorder` writes WebM without a duration header,
  so some players show the length only after scanning the file (the frames and
  timestamps are correct). Frame timing depends on the browser keeping up with 30 fps; a
  busy machine can drop frames but not lengthen the clip.

**Planned, not implemented:** GIF and MP4 export, excerpts, user-configurable redaction,
and any hosted sharing (sharing stays an explicit user action with a local file).

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
                   record, run, analyze, replay-api, replay)
  core/            application logic (doctor, filelock, store, active, ignore, scanner,
                   git_observer, recorder, command, redact, checks, forensics, replay,
                   replay_server, ui_assets)
  models/          typed dataclasses shared across layers (environment, events, session,
                   forensics, replay)
  ui/static/       the built Replay UI (generated by `npm run build` in web/; committed)
  utils/           small dependency-free helpers (today: Git detection and the bounded,
                   shell-free read-only Git command runner)
web/               Replay UI source (React + TypeScript + Vite); not part of the package
```

The CLI is built on the standard library `argparse` to keep the runtime dependency
footprint at zero. That choice can be revisited if the command surface grows substantially.

**Packaging.** Hatchling builds a pure-Python `py3-none-any` wheel containing only
`agentbodycam/` (Python modules, `py.typed`, the built UI in `ui/static/`) plus metadata
and the license; the version comes from `agentbodycam.__version__`. The sdist is an
explicit allowlist (sources, tests, scripts, docs, the `web/` sources and lockfile, the
top-level documents and `pyproject.toml`), with `.gitignore` exclusions applied on top,
so untracked local files cannot leak into it. Builds are byte-for-byte reproducible
(hatchling uses fixed archive timestamps) and a wheel built from the sdist is identical
to one built from the checkout. `tests/test_packaging.py` checks all of this and runs the
installed wheel from a fresh virtual environment without Node.js, Git or the checkout.
