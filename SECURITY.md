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

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Until a dedicated private reporting channel is published in this file, contact the
maintainer privately through the contact details listed on the project's hosting page.
Include steps to reproduce, affected version/commit and potential impact.

You can expect an initial acknowledgement on a best-effort basis; this is a volunteer
project with no guaranteed response time.
