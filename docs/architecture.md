# AgentBodycam Architecture

> Status: **design document**. Most of what is described here is not implemented yet.
> Implemented today: the CLI foundation (`--help`, `--version`, `doctor`), the typed
> event/session model and the local append-only, process-safe event store (section 3.2),
> managed with `agentbodycam session create|list|show|active`, the file-system and
> read-only Git state parts of the universal recorder (`agentbodycam record`, section 3.1),
> the one-recorder-per-project active-recording lease (section 3.1, "Active recording
> lease") and explicit wrapped-command recording (`agentbodycam run -- ...`, section 3.1,
> "Wrapped commands") with deterministic check results (`check.passed` / `check.failed`,
> section 3.1, "Check results"). Capture of commands not launched through
> `agentbodycam run`, adapters, forensics, replay, UI, export and AI are not implemented.
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
  `<project>` / `<home>`. `executable` is only the program's file name.
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
                   record, run)
  core/            application logic (doctor, filelock, store, active, ignore, scanner,
                   git_observer, recorder, command, redact, checks; later: forensics)
  models/          typed dataclasses shared across layers (environment, events, session)
  utils/           small dependency-free helpers (today: Git detection and the bounded,
                   shell-free read-only Git command runner)
```

The CLI is built on the standard library `argparse` to keep the runtime dependency
footprint at zero. That choice can be revisited if the command surface grows substantially.
