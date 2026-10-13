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

`agentbodycam session create` writes only inside `<project>/.agentbodycam/`:

- `.gitignore` containing `*`, so session data is not committed by accident.
- `sessions/<id>/session.json`: session ID, absolute project path, start/end time, status,
  AgentBodycam version, and OS/Python platform details. No hostname, username or
  environment variables.
- `sessions/<id>/events.jsonl`: an empty, append-only event log. Events hold metadata such as
  project-relative paths and short summaries, never file contents, and are size-capped.

The project path can include your username (for example `C:\Users\<name>\...`); keep this
in mind before sharing a session directory. No AgentBodycam code makes network requests.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Until a dedicated private reporting channel is published in this file, contact the
maintainer privately through the contact details listed on the project's hosting page.
Include steps to reproduce, affected version/commit and potential impact.

You can expect an initial acknowledgement on a best-effort basis; this is a volunteer
project with no guaranteed response time.
