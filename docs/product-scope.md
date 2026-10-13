# Product Scope

> Status: **planning document**. The project is in early development. Nothing listed
> under v0.1 is implemented yet unless explicitly marked as done.

## One-liner

**Your AI coding agent now has a bodycam.** AgentBodycam records what happens to a
project while a coding agent works on it, then helps a human reconstruct and replay
the session from evidence.

## Current state (v0.0.1)

Done:

- Python package skeleton, CLI entry point.
- `agentbodycam --help`, `agentbodycam --version`, `agentbodycam doctor` (read-only
  environment check).
- Lint, format, type-check and test tooling.

Not done: everything else below.

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
- Video/GIF generation (planned later in the share/export pipeline).
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

- React/TypeScript cinematic replay UI.
- Shareable clips and cards with redaction.
- Optional AI analyst (local or BYOK).
- Agent-specific adapters where officially supported data exists.
