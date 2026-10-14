# Security Policy

## Project status

AgentBodycam is in early development and is **not ready for production use**.
There are no supported releases yet.

## Design commitments

AgentBodycam records potentially sensitive project metadata such as file paths, file
hashes, Git state and redacted command arguments. It does not store file contents, diffs or
command output; the "What is stored today" section below is the authoritative list. The
project commits to:

- **Local-first**: recorded data stays on your machine by default.
- **No telemetry**: AgentBodycam does not phone home.
- **No account or cloud backend** is required.
- **Explicit sharing only**: nothing is exported or uploaded without a deliberate user action.
- **Optional AI only**: any future AI analysis will be opt-in, and will disclose what data
  would be sent before sending it.
- **Read-only observation**: the recorder is meant to observe a project, not modify it.

## What is stored today

`agentbodycam session create`, `agentbodycam record` and `agentbodycam run` write only
inside `<project>/.agentbodycam/` (`analyze` and `replay-api` write nothing):

- `.gitignore` containing `*`, so session data is not committed by accident.
- `sessions/<id>/session.json`: session ID, absolute project path, start/end time, status,
  AgentBodycam version, and OS/Python platform details. No hostname, username or
  environment variables.
- `sessions/<id>/events.jsonl`: an append-only event log. `record` adds session start/stop
  events and one event per observed file creation, modification or deletion: the
  project-relative path, size, modification time and SHA-256 of the file. **File contents
  are never stored.** Events are size-capped.
- When the project is inside a Git repository, `git.snapshot` events: repository root path,
  branch, HEAD commit SHA, detached/unborn state, upstream branch *name* and local
  ahead/behind counts, and the project-relative paths and status codes of staged, unstaged,
  untracked and conflicted files. When HEAD moves, the SHAs and **subject lines** of the
  commits it brought in (at most 20). No diffs, patches, commit bodies, author names or
  emails, remote URLs, `.git/config` contents, credentials or environment variables.

How Git is read:

- Only read-only commands are run: `git rev-parse`, `git status --porcelain=v2`,
  `git merge-base --is-ancestor` and `git log`. AgentBodycam never commits, checks out,
  resets, stashes, fetches, pulls, pushes or changes Git configuration.
- Commands run without a shell, with a fixed argument list, a timeout and an output cap,
  using the first absolute `git` on `PATH` (never one in the current directory), with
  `GIT_OPTIONAL_LOCKS=0` (the index is never rewritten), `core.fsmonitor=false`, no
  terminal prompts, and `GIT_DIR`-style redirection variables removed.
- Git may still run clean filters configured by the repository (for example Git LFS) while
  computing status, exactly as a plain `git status` would.

Wrapped commands (`agentbodycam run -- <command> [args...]`):

- Only commands explicitly launched this way are recorded; AgentBodycam does not observe
  any other terminal or process activity.
- The command runs exactly as given: an argument list with no shell (`shell=False`), the
  inherited environment, stdin, stdout and stderr. On Windows, batch scripts (`.bat`,
  `.cmd`, e.g. `npm.cmd`) are refused rather than silently run through `cmd.exe`; run
  `cmd /c ...` explicitly if you want a shell.
- `command.started` stores a **redacted** copy of the arguments: values of options and
  assignments with sensitive names (`--token X`, `--api-key=X`, `password=X`,
  `Authorization: ...`), URL credentials and sensitive query parameters, recognisable API
  keys/tokens and long random-looking strings become `<redacted>`; absolute paths become
  project-relative or `<external-path>`, and the project and home directories are never
  stored verbatim. The argument list is capped (64 items, 256 characters each, 4096 in
  total). Redaction is pattern-based and conservative but cannot recognise every secret:
  avoid passing secrets on the command line, as you would for shell history.
- `command.finished` stores the exit code or signal, duration, child PID, an outcome and,
  for a start failure, only the exception type and errno.
- **Never stored:** command output (stdout/stderr), stdin, environment variables, the
  absolute working directory or executable path.
- The wrapped command itself is yours and may use the network or do anything else;
  AgentBodycam's own code still makes no network requests.

What `record` does **not** see or store:

- Anything under `.agentbodycam/`, `.git/`, `.venv/`, `venv/`, `node_modules/`, caches, and
  other default-ignored locations (see `docs/architecture.md`, section 3.1).
- Likely secret files: `.env`, `.env.*`, `.envrc`, `*.pem`, `*.key`, `*.p12`, `*.pfx` and
  SSH private-key names. They are skipped by name before being opened, so neither their
  path, contents nor hash is recorded. In Git snapshots such paths are only counted
  (`redacted_paths`), never named.
- Anything reached through a symlink or junction. Links are never followed, so the
  recorder never reads outside the project root.

A SHA-256 of a small or guessable file can confirm a guess of its contents; treat session
directories as sensitive. Error events contain only the exception type, errno and the OS
error text, never the absolute path or file contents. Git error events contain the
operation, an error kind, the exit code and the first line of Git's error message (at most
300 characters) with the project and repository paths replaced by placeholders and any
`user:password@` in URLs redacted.

The project path can include your username (for example `C:\Users\<name>\...`); keep this
in mind before sharing a session directory. No AgentBodycam code makes outbound network
requests; the only socket it ever opens is the loopback listener of the Replay API below.

## Local Replay API (`agentbodycam replay-api`)

A small, read-only HTTP/JSON API for the future Replay UI. It is a local development API;
there is no Replay UI yet. Communication with it is **local IPC over the loopback
interface**, not network access:

- **Loopback only.** It binds `127.0.0.1` by default (`::1` or `localhost`, which is bound
  as `127.0.0.1` without a DNS lookup, can be chosen). `0.0.0.0`, `::`, LAN addresses and
  host names are refused with an error, so it cannot be exposed to your network by
  accident. The port defaults to 0 (a free port chosen by the operating system). It runs in
  the foreground only and stops with Ctrl+C; no service or background process is created.
- **Temporary bearer token.** Every start generates a new `secrets.token_urlsafe(32)`
  token, printed once in the terminal that started the server. It exists only in that
  process's memory: it is never written to `.agentbodycam/`, session files, Git or logs, and
  it stops working when the server stops. Every endpoint that returns project or session
  data requires `Authorization: Bearer <token>` (compared in constant time); only
  `/api/v1/health` (`{"status": "ok", "api_version": 1, "product": "AgentBodycam"}`) is
  unauthenticated. Tokens in URLs or query strings are never accepted.
- **Read-only.** Only GET and HEAD exist; POST, PUT, PATCH, DELETE, OPTIONS and anything
  else get `405`. Requests never write, create or repair anything (starting the server on a
  project without recordings creates nothing), run no Git command or other process and make
  no outbound connection.
- **Browser protections.** No CORS: no `Access-Control-*` header is ever sent (no
  wildcard, no preflight approval), so a web page on another origin cannot read responses.
  Requests with a foreign `Origin`, `Sec-Fetch-Site: cross-site|same-site`, or a `Host`
  header other than this server's loopback address and port (DNS rebinding) are refused
  with `403`. Every response is JSON with `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a restrictive
  `Content-Security-Policy` and `Cross-Origin-Resource-Policy: same-origin`. The future UI
  is meant to be served from this same local origin, so CORS will not be needed.
- **What it returns.** Session IDs, status, times and event counts; the same forensic
  report as `agentbodycam analyze --json`; and a paginated timeline of the report's safe
  per-event view (sequence, ID, time, type, category, source, summary, project-relative
  path, command ID). Raw event payloads (hashes, argv, Git root paths), the absolute
  project path, platform details, host and user names, environment variables, file
  contents and command output are never returned.
- **No leaks in errors or logs.** Errors are `{"error": {"code", "message"}}` with fixed
  messages: no paths, tracebacks or exception text. The default HTTP access log is
  disabled; the terminal shows only the method, route template and status of each request
  (never the raw path, query string, session ID or headers).
- The token keeps out web pages and other local users' programs that do not have it. It
  cannot protect against programs running as your own user account, which can read
  `.agentbodycam/` directly anyway. Data leaves your machine only if you deliberately share
  it.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Until a dedicated private reporting channel is published in this file, contact the
maintainer privately through the contact details listed on the project's hosting page.
Include steps to reproduce, affected version/commit and potential impact.

You can expect an initial acknowledgement on a best-effort basis; this is a volunteer
project with no guaranteed response time.
