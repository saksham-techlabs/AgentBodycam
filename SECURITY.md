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
inside `<project>/.agentbodycam/` (`analyze`, `replay-api` and `replay` write nothing):

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

**Store permissions (not encryption).** Session data under `.agentbodycam/` is still
**unencrypted**. Treat recordings as sensitive. On POSIX, AgentBodycam creates and hardens
its own store directories to mode `0700` and regular store files (`session.json`,
`events.jsonl`, `.gitignore`, lock files, `active.json`, …) to mode `0600`, so other local
accounts cannot read them when parent directories are traversable. The project root and
paths outside `.agentbodycam/` are never chmod'd. On Windows, local file access remains
governed by Windows ACLs inherited from the user's filesystem/project environment;
AgentBodycam does not install a separate ACL subsystem. Same-user processes remain inside
the trust boundary on every platform.

How Git is read:

- Only read-only commands are run: `git rev-parse`, `git status --porcelain=v2`,
  `git merge-base --is-ancestor` and `git log`. AgentBodycam never commits, checks out,
  resets, stashes, fetches, pulls, pushes or changes Git configuration.
- Commands run without a shell, with a fixed argument list, a timeout and an output cap,
  using the first absolute `git` / `git.exe` on `PATH` (never the current directory, never
  a relative PATH entry, and on Windows never a project-local `git.cmd` / `git.bat`). The
  same discovery policy is used by `doctor` and by the recorder.
- Observation disables lazy fetching / remote object retrieval: every Git subprocess runs
  with `GIT_NO_LAZY_FETCH=1` and with `protocol.http|https|git|ssh|file.allow=never`, so a
  partial clone with missing promised objects yields unavailable or problem evidence
  instead of contacting a remote. Also `GIT_OPTIONAL_LOCKS=0` (the index is never
  rewritten), `core.fsmonitor=false`, no terminal prompts, and `GIT_DIR`-style redirection
  variables removed.
- Git may still run clean filters configured by the repository (for example Git LFS) while
  computing status, exactly as a plain `git status` would. That is separate from remote
  object fetch; AgentBodycam does not claim Git can never execute anything.

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
  keys/tokens and long random-looking strings become `<redacted>`; absolute paths inside
  the project become `<project>` / `<project>/rel` (so an originally absolute path cannot
  be mistaken for one typed relative), anything else becomes `<external-path>`, and the
  project and home directories are never stored verbatim. Each argv item also records a
  privacy-safe origin (`literal` / `project_path` / `external_path` / `embedded_path`) so
  a generated placeholder is never indistinguishable from the same characters typed by the
  user; origins never store the original absolute path. Drive and UNC paths (`C:\...`,
  `\\server\...`) count as absolute on every platform, and on Windows so do driveless
  rooted paths with two or more segments (`/home/<name>/...`, `\Users\<name>\...`);
  single-segment switches such as `/c` are kept. The argument list is capped (64 items,
  256 characters each, 4096 in total). Redaction is pattern-based and conservative but
  cannot recognise every secret: avoid passing secrets on the command line, as you would
  for shell history.
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
requests; the only socket it ever opens is the loopback listener of the Replay API and
Replay UI below.

## Local Replay API (`agentbodycam replay-api`)

A small, read-only HTTP/JSON API for the Replay UI. It is a local development API.
Communication with it is **local IPC over the loopback interface**, not network access:

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
  Requests with a foreign or `null` `Origin`, `Sec-Fetch-Site: cross-site|same-site`, a
  repeated `Origin`, `Host` or `Sec-Fetch-Site` header, or a `Host` header other than this
  server's loopback address and port (DNS rebinding) are refused with `403`. Every response is JSON with `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a restrictive
  `Content-Security-Policy` and `Cross-Origin-Resource-Policy: same-origin`. The Replay UI
  is served from this same local origin, so CORS is not needed.
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
- The token keeps out web pages and programs that do not have it. It cannot protect against
  programs running as your own user account, which can read `.agentbodycam/` directly.
  On POSIX, other local users are blocked from reading the store by owner-only directory
  and file modes (see above); that is separate from this token. Data leaves your machine
  only if you deliberately share it.

## Local Replay UI (`agentbodycam replay`)

The Replay UI (a preview, under development) is a React application whose production build
ships inside the Python package (`agentbodycam/ui/static/`). `agentbodycam replay` serves
it, together with exactly the same Replay API, from one loopback origin. Node.js is used
only to build it; nothing is fetched at run time. `replay-api` serves no UI at all.

**Static files.** At startup the server loads a fixed allowlist from the packaged build
directory into memory: `index.html`, plus top-level and `assets/` files with simple names
and an allowed type (`.js`, `.css`, `.svg`, `.png`, `.ico`, `.webp`, `.woff2`). Symlinks
and Windows reparse points are skipped, and sizes and counts are bounded. Requests are
matched against that table by exact raw path; a request path is never mapped to the file
system, never percent-decoded and never normalised, so `..`, `%2e%2e`, `%2f`, `%5c`,
absolute paths, project files and `.agentbodycam/` files cannot be reached (404). The
document is served only for `/` and `/session/<valid-session-id>`. Query strings on UI
routes are refused. Assets keep the API's headers (`nosniff`, `no-store`, the API CSP,
`Cross-Origin-Resource-Policy: same-origin`).

**Content Security Policy.** The UI document gets its own policy, and the API policy is
unchanged:
`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self';
connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none';
form-action 'none'; frame-ancestors 'none'`. There is no `unsafe-inline`, `unsafe-eval`,
`data:`, `blob:` or remote source. The build contains no inline script or style, and the
UI uses no external CDN, web fonts or analytics. The document also gets
`X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`,
`Referrer-Policy: no-referrer` and a restrictive `Permissions-Policy`.

**Token handoff.** `replay` does not print the token. The page obtains it from
`GET /ui/bootstrap`, which answers `{"api_version": 1, "token": "..."}` only when the
request:

- carries `X-AgentBodycam-UI: bootstrap` (a custom header, so a cross-origin page would
  need a CORS preflight, which is never approved: OPTIONS is 405 and no
  `Access-Control-*` header exists);
- carries the browser's fetch metadata for a same-origin script fetch
  (`Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors` or `same-origin`,
  `Sec-Fetch-Dest: empty`), so links, navigations, iframes, `<script>`/`<img>` tags and
  tools that send no such headers (such as plain `curl`) are refused with 403;
- passes the API's `Host` (DNS rebinding) and `Origin` checks; each header must appear
  exactly once, and a query string is refused.

The page keeps the token in a private field of its API client, in memory only. It is never
placed in a URL or query string, `localStorage`, `sessionStorage`, IndexedDB or cookies,
never logged, never written to disk, and is not returned by any `/api` route. Reloading the
page fetches it again; it stops working when the server stops. When the server restarts,
the first rejected request fetches a new token and is retried once; requests rejected at
the same time share that one bootstrap instead of each fetching their own. Bearer
authentication on `/api` routes is unchanged.

**Threat model.**

- *Other web sites* in your browser cannot obtain the token or read API responses: the
  bootstrap needs a custom header plus same-origin fetch metadata, there is no CORS, and
  foreign `Origin`/`Host` headers (including DNS rebinding) are refused.
- *Programs running as your own user* can already read `.agentbodycam/` directly; the UI
  does not change that.
- *Other local users reading `.agentbodycam/` on disk*: on POSIX, owner-only store modes
  block direct filesystem reads by other accounts. On Windows, access depends on ACLs.
  That filesystem boundary is separate from the Replay UI.
- *Other local users or programs that can connect to the loopback port* remain the
  bootstrap trade-off for `replay`. Unlike `replay-api` (whose token appears only in your
  terminal), anyone who can reach the port and deliberately forge the request headers
  above can obtain the token while `replay` is running. Only use `replay` on a machine
  where you trust other local accounts for loopback access, or use `replay-api`, which
  offers no bootstrap. The server never listens beyond loopback, so other machines cannot
  reach it.

## Local exports (SVG share card, JSON summary, WebM replay clip)

The Replay UI's Export panel creates files only when you click a download button. They are
generated in your browser and saved as local files; nothing is uploaded, copied to the
clipboard or sent anywhere.

- **One public-safe model.** All three formats are built from a dedicated model that
  copies named report fields only: counts, event sequence numbers, the recording state,
  check kinds and results, and labels rebuilt from structured fields (`Test failed:
  pytest`). Check names appear only when they are one of the names AgentBodycam generates
  for recognised tools (`pytest`, `npm test`, `cargo build`, ...); anything else, including
  the program name of an explicit `--check` on an unrecognised command, becomes a generic
  label. Free-text summaries are never copied.
- **Never exported:** the project name or path, any absolute or relative file path, user
  or host names, session and event IDs, timestamps, the bearer token, argv, environment
  variables, command output, raw payloads, hashes or SHAs, Git branch, upstream or remote,
  file contents and diffs.
- **SVG:** an explicit element tree serialised from an allowlist of presentational
  elements and attribute names, checked at run time. No script, `foreignObject`, links,
  `href`, event handlers, styles, images or external URLs; paint may only reference a
  gradient in the same file; every text node and attribute value is XML-escaped.
- **JSON:** a fixed list of keys (`"format": "agentbodycam-share-summary"`, `"version": 1`).
- **WebM:** a generated animation, **not a screen capture**. A scene plan is built from
  the same public-safe model only (never the raw report), drawn frame by frame with Canvas
  2D on an off-screen canvas using bounded strings, and encoded by the browser's own
  `MediaRecorder`. No screen, window, tab, camera or microphone capture API is used, and
  no network request is made. The UI's Content Security Policy was not loosened for it.

An exported file is meant to be shareable, but it still shows that a session happened,
its size and which checks failed or passed; review it before sharing.

## Threat model summary

In scope:

- *Web pages in your browser* reading recorded evidence or the token: prevented by the
  loopback-only server, `Host`/`Origin`/fetch-metadata checks, no CORS and the bootstrap
  rules above.
- *Leaks through what AgentBodycam writes or shows*: recorded evidence excludes file
  contents, output, environment and recognisable secrets; API responses, errors, logs and
  exports exclude absolute paths, tokens and raw payloads; the UI renders fixed error text,
  never backend or exception messages.
- *Hostile project contents* (odd file names, symlinks, junctions, huge files, secret
  files): links are not followed, secret file names are skipped, files over 256 MiB are
  not hashed, events and API pages are size-capped, and every string the UI or an export
  shows is escaped or rebuilt.

Out of scope (AgentBodycam cannot defend against them):

- Programs or people running as your own user account: they can read `.agentbodycam/`
  and your project directly.
- With `agentbodycam replay` running, other local accounts that can reach the loopback
  port can obtain the token via the documented bootstrap trade-off (see above). That is
  separate from POSIX owner-only store modes, which block other users' direct reads of
  `.agentbodycam/` on disk.
- A compromised machine, browser or Python installation.
- Secrets typed in a form the redaction patterns do not recognise (for example a short
  password after an unrecognised flag such as `-p`). Avoid secrets on command lines.

## Known limitations

- Session data under `.agentbodycam/` is stored **unencrypted** and includes the absolute
  project path (which can contain your user name) and file SHA-256 hashes. POSIX
  owner-only modes and Windows ACLs are access control, not encryption.
- Redaction is pattern-based and conservative, not a guarantee.
- The Replay server re-reads a session's whole event log for every timeline page and
  report request, so very large sessions (tens of thousands of events) respond slowly.
  This is a performance limitation, not a security issue.
- No release has been made. An independent audit tracked findings ABD-001 … ABD-008;
  ABD-001 … ABD-006 are addressed in-tree, ABD-007 and ABD-008 remain open.

## Automated checks

The security and privacy regression tests (path safety, command redaction, Replay
authentication and browser protections, static file serving, export privacy, repository
hygiene and the no-network guarantees) are part of the normal test suite. They are not
optional: `python scripts/check.py` runs them, and the CI configuration runs it on Windows,
Linux and macOS with every supported Python version. CI workflows have read-only
repository access and use no secrets.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Until a dedicated private reporting channel is published in this file, contact the
maintainer privately through the contact details listed on the project's hosting page.
Include steps to reproduce, affected version/commit and potential impact.

Do not send real session recordings (`.agentbodycam/` directories, `events.jsonl`,
`session.json`), exports of private projects, tokens or other secrets. Reproduce the
problem with a small throwaway project, or describe it, and redact paths and names.

You can expect an initial acknowledgement on a best-effort basis; this is a volunteer
project with no guaranteed response time.
