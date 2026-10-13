# Security Policy

## Project status

AgentBodycam is in early development and is **not ready for production use**.
There are no supported releases yet.

## Design commitments

AgentBodycam is intended to record sensitive information about your projects
(file contents, diffs, command output). The project commits to:

- **Local-first**: recorded data stays on your machine by default.
- **No telemetry**: AgentBodycam does not phone home.
- **No account or cloud backend** is required.
- **Explicit sharing only**: nothing is exported or uploaded without a deliberate user action.
- **Optional AI only**: any future AI analysis will be opt-in, and will disclose what data
  would be sent before sending it.
- **Read-only observation**: the recorder is meant to observe a project, not modify it.

## What is stored today

`agentbodycam session create` and `agentbodycam record` write only inside
`<project>/.agentbodycam/`:

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
in mind before sharing a session directory. No AgentBodycam code makes network requests.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Until a dedicated private reporting channel is published in this file, contact the
maintainer privately through the contact details listed on the project's hosting page.
Include steps to reproduce, affected version/commit and potential impact.

You can expect an initial acknowledgement on a best-effort basis; this is a volunteer
project with no guaranteed response time.
