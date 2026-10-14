# Product Scope

> Status: **planning document**. The project is in early development and not ready for
> production use. "Current state" below lists what exists today; everything else is a goal.

## One-liner

**Your AI coding agent now has a bodycam.** AgentBodycam records what happens to a
project while a coding agent works on it, then helps a human reconstruct and replay
the session from evidence.

## Current state (v0.0.1)

Done (early development, not production-ready):

- CLI foundation: `--help`, `--version`, `doctor` (read-only environment check).
- Local append-only event store and session management (`session create|list|show|active`).
- Universal recorder: file changes and read-only Git state (`record`).
- Explicitly wrapped commands and deterministic check results (`run -- <command>`).
- Deterministic local forensics (`analyze`): timeline, first observable failure, observed
  recoveries, failures unresolved in the recording, last recorded state.
- Local, authenticated, read-only Replay API (`replay-api`).
- Local Replay UI (`replay`, under development): dashboard, timeline, playback and
  scrubber, findings, files, last recorded Git / final state.
- Local privacy-safe export generated in the browser: SVG share card, JSON summary and a
  generated WebM replay clip (an animation of recorded evidence, not a screen capture).

Not done:

- Git diffs and file contents (only paths, sizes, hashes and Git status are recorded).
- GIF and MP4 export, hosted sharing.
- Agent-specific adapters.
- Optional AI analyst.

## v0.1 target boundaries

### In scope

- **Universal mode** that relies only on observable project-level evidence:
  - file changes with timestamps,
  - Git diffs and commits,
  - commands/processes where observable (e.g. run through an AgentBodycam wrapper),
  - test/build pass/fail evidence from those commands,
  - errors captured from those commands.
- Local append-only event store for sessions.
- Deterministic local forensics: timeline, first observable failure, retries/recovery,
  final project state.
- A way to view a session locally (CLI report first; replay UI may follow in v0.1 or later).
- Works on Windows, Linux and macOS.

### Hard requirements for v0.1

Core v0.1 must work **without**:

- an OpenAI API key,
- an Anthropic API key,
- a Gemini API key,
- any other paid or hosted AI API,
- a hosted backend,
- an account or login,
- network access.

It must send **no telemetry**.

### Out of scope for v0.1

- AI-generated analysis of any kind (planned later as an optional, opt-in feature
  with local or bring-your-own-key models).
- Hosted sharing service.
- GIF/MP4 generation and hosted sharing (a local, generated WebM replay clip exists).
- Deep agent-specific integrations beyond what is clearly available (see below).

## Agent integrations: what we can and cannot promise

AgentBodycam does not depend on any coding agent's internals. Universal mode observes
the project, not the agent, and therefore works regardless of which agent made the changes.

Deeper integrations with Cursor, Codex, Claude Code or other agents (prompts, tool calls,
agent reasoning, internal retries) **depend entirely on what data each tool makes
available locally or through official, documented interfaces**. Specifically:

- We will not claim support for an agent until a working adapter exists and is tested.
- We will not reverse-engineer or intercept private agent traffic.
- If an agent's data format changes or becomes unavailable, its adapter may degrade;
  universal mode must keep working.
- Adapters must state exactly which events they provide and where the data comes from.

As of today, **no agent-specific adapter exists**.

## Truthfulness rule

AgentBodycam reports observed facts and labels inferences as inferences.

- Observed fact: "Tests started failing after this file changed."
- Inference: "This file change caused the failure."

The product must never present an inference as proven fact. See
[architecture.md](architecture.md#2-observed-fact-vs-inference).

## Later (post-v0.1) ideas

- GIF/MP4 export and optional hosted sharing of the privacy-safe exports.
- File contents and diffs, with redaction.
- Optional AI analyst (local or BYOK).
- Agent-specific adapters where officially supported data exists.
