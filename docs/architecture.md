# AgentBodycam Architecture

> Status: **design document**. Almost nothing described here is implemented yet.
> As of v0.0.1 only the CLI foundation (`--help`, `--version`, `doctor`) exists.
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

Planned observations:

- File created / modified / deleted / renamed, with timestamps and content hashes.
- Git state: HEAD, branch, staged/unstaged diffs, commits made during the session.
- Commands and processes **where observable** — for example commands launched through an
  AgentBodycam wrapper (`agentbodycam run -- pytest`). We will not claim to see processes
  the recorder cannot actually observe.
- Test/build outcomes captured from wrapped commands (exit codes, durations, output).
- Session boundaries: start, stop, final project snapshot.

Design notes:

- Use OS file-watching where reliable, plus periodic snapshot/diff as a fallback.
- Respect `.gitignore` and a dedicated ignore file; never record secrets-bearing paths
  such as `.env` by default.
- Must be safe: the recorder observes; it does not modify the user's project.

### 3.2 Evidence / Event Store

- Local, append-only log of normalized events per session.
- Each event: id, monotonic sequence number, wall-clock timestamp, source
  (recorder or a named adapter), type, and payload.
- Large blobs (file contents, diffs, logs) stored content-addressed alongside the log.
- Candidate format: SQLite (stdlib `sqlite3`) or JSON Lines plus a blob directory.
  Decision deferred until the recorder exists.
- Lives inside the project (e.g. `.agentbodycam/`) or a user data dir, configurable.
- Schema is versioned so older sessions remain replayable.

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
  cli.py           argparse CLI (help, version, doctor)
  core/            application logic (today: doctor; later: recorder, store, forensics)
  models/          typed dataclasses shared across layers
  utils/           small dependency-free helpers (today: read-only git detection)
```

The CLI is built on the standard library `argparse` to keep the runtime dependency
footprint at zero. That choice can be revisited if the command surface grows substantially.
