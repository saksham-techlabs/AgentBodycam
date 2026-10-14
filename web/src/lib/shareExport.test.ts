import { describe, expect, it } from "vitest";

import type { SessionSummary } from "../api/types";
import { CAUSAL, RICH_TIMELINE, plainReport, richReport } from "../test/fixtures";
import { SESSIONS, STOPPED_ID } from "../test/fakeServer";
import { FORBIDDEN, hostileReport } from "../test/hostile";
import { JSON_FILENAME, SVG_FILENAME } from "./download";
import { buildShareCard, shareCardSvg } from "./shareCard";
import {
  MAX_LABEL_CHARS,
  SUMMARY_FORMAT,
  SUMMARY_KEYS,
  SUMMARY_VERSION,
  bound,
  buildShareModel,
  buildShareSummary,
  safeCheckName,
  serializeSummary,
} from "./shareModel";
import { assertSafeAttr, el, serializeSvg, type SvgNode } from "./svg";

/** Every text node of a card, in document order. */
function texts(node: SvgNode | string): string[] {
  return typeof node === "string" ? [node] : node.children.flatMap(texts);
}

function expectClean(output: string): void {
  for (const value of FORBIDDEN) expect(output, value).not.toContain(value);
}

describe("share model", () => {
  it("is built from approved fields only, with labels from structured data", () => {
    const model = buildShareModel(richReport(STOPPED_ID));
    expect(model).toEqual({
      recordingState: "stopped",
      analysisComplete: true,
      totalEvents: RICH_TIMELINE.length,
      changedFiles: 3,
      fileChangeEvents: 3,
      failedChecks: 3,
      passedChecks: 1,
      observableFailures: 3,
      recoveries: 1,
      unresolved: 1,
      firstFailure: { sequence: 5, label: "Test failed: pytest" },
      recovery: { sequence: 14, label: "Test passed: pytest" },
      finalChecks: [
        { kind: "test", name: "pytest", result: "passed" },
        { kind: "lint", name: "ruff", result: "failed" },
      ],
      finalChecksTotal: 2,
      finalPassing: 1,
      finalFailing: 1,
      verdict: "fail",
    });
  });

  it("never carries identifiers, paths, Git state, argv, tokens or raw text", () => {
    const model = buildShareModel(hostileReport());
    expectClean(JSON.stringify(model));
    expect(model.firstFailure?.label).toBe("Test failed");
    expect(model.recovery?.label).toBe("Test passed");
    expect(model.finalChecks[1]?.name).toBe("check");
  });

  it("allowlists check names conservatively", () => {
    for (const ok of ["pytest", "npm test", "cargo build", "mypy", "go test", "pnpm typecheck"]) {
      expect(safeCheckName(ok)).toBe(ok);
    }
    for (const bad of [
      // Explicit `--check` names taken from a program's file name stay out of exports.
      "python",
      "command",
      "deploy-client-acme",
      "npm start",
      "src/app.py",
      "C:\\tools\\x",
      "deploy.sh",
      "https://x",
      "Pytest",
      "a b c",
      "deadbeefcafe",
      "run 123456",
      "x".repeat(40),
      "<b>",
      "",
    ]) {
      expect(safeCheckName(bad), bad).toBeNull();
    }
  });

  it("bounds long labels", () => {
    expect(bound("x".repeat(100), MAX_LABEL_CHARS)).toHaveLength(MAX_LABEL_CHARS);
    expect(bound("short", MAX_LABEL_CHARS)).toBe("short");
    const r = richReport(STOPPED_ID);
    if (r.first_observable_failure === null) throw new Error("fixture has a first failure");
    r.first_observable_failure.check_kind = "typecheck";
    r.first_observable_failure.check_name = "pnpm typecheck";
    const label = buildShareModel(r).firstFailure?.label ?? "";
    expect(Array.from(label).length).toBeLessThanOrEqual(MAX_LABEL_CHARS);
    expect(label).toBe(bound("Typecheck failed: pnpm typecheck", MAX_LABEL_CHARS));
    for (const t of texts(buildShareCard(buildShareModel(r)))) {
      expect(Array.from(t).length, t).toBeLessThanOrEqual(48);
    }
  });

  it("uses generic labels for non-check failures without command details", () => {
    const r = richReport(STOPPED_ID);
    if (r.first_observable_failure === null) throw new Error("fixture has a first failure");
    r.first_observable_failure.failure_type = "command_spawn_error";
    r.first_observable_failure.reason = "FileNotFoundError: C:\\bin\\secret-tool.exe";
    expect(buildShareModel(r).firstFailure).toEqual({ sequence: 5, label: "Command could not start" });
  });
});

describe("share card SVG", () => {
  const svg = shareCardSvg(buildShareModel(richReport(STOPPED_ID)));

  it("is deterministic and self-contained", () => {
    expect(shareCardSvg(buildShareModel(richReport(STOPPED_ID)))).toBe(svg);
    expect(svg.startsWith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"')).toBe(true);
    expect(svg).toContain('width="1200" height="630" viewBox="0 0 1200 630"');
  });

  it("contains no script, foreignObject, links, styles or remote URLs", () => {
    const hostile = shareCardSvg(buildShareModel(hostileReport()));
    for (const out of [svg, hostile]) {
      expect(out).not.toMatch(/<script|foreignObject|<image|<use|<a[\s>]|<style|href|\son[a-z]+=/i);
      expect(out.match(/https?:\/\/[^"]*/g)).toEqual(["http://www.w3.org/2000/svg"]);
      for (const ref of out.match(/url\([^)]*\)/g) ?? []) expect(ref).toMatch(/^url\(#abc-card-[a-z]+\)$/);
      expect(out).not.toMatch(/@import|<!ENTITY|<!DOCTYPE/i);
    }
  });

  it("escapes text and refuses unsafe attributes", () => {
    const out = serializeSvg(el("svg", {}, el("text", { fill: 'a"b' }, `<script>alert("x")</script> & '`)));
    expect(out).toContain("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &apos;");
    expect(out).toContain('fill="a&quot;b"');
    expect(out).not.toContain("<script");
    expect(() => assertSafeAttr("onload", "x")).toThrow();
    expect(() => assertSafeAttr("href", "#x")).toThrow();
    expect(() => assertSafeAttr("style", "x")).toThrow();
    expect(() => assertSafeAttr("fill", "url(https://evil.test/x)")).toThrow();
    expect(() => assertSafeAttr("x y", "1")).toThrow();
  });

  it("does not expose identifiers, paths, Git state, argv or tokens", () => {
    expectClean(shareCardSvg(buildShareModel(hostileReport())));
    const lines = texts(buildShareCard(buildShareModel(hostileReport())));
    for (const line of lines) {
      expect(line, line).not.toMatch(/[\\/]|\w\.[a-z]{1,4}\b/);
    }
    expect(lines.join("\n")).not.toMatch(/\bmain\b/);
  });

  it("shows the headline fields and the unresolved count factually", () => {
    const lines = texts(buildShareCard(buildShareModel(richReport(STOPPED_ID))));
    const after = (label: string) => lines[lines.indexOf(label) - 1];
    expect(after("RECORDED EVENTS")).toBe(String(RICH_TIMELINE.length));
    expect(after("FILES CHANGED")).toBe("3");
    expect(after("FAILED CHECKS")).toBe("3");
    expect(after("RECOVERIES")).toBe("1");
    expect(after("UNRESOLVED")).toBe("1");
    expect(lines).toEqual(
      expect.arrayContaining([
        "AgentBodycam session replay summary",
        "Agent",
        "Bodycam",
        "SESSION REPLAY",
        "STOPPED",
        "FIRST OBSERVABLE FAILURE",
        "#5",
        "Test failed: pytest",
        "OBSERVED RECOVERY",
        "#14",
        "Test passed: pytest",
        "FINAL RECORDED STATE",
        "Checks: FAIL",
        "1 passing · 1 failing",
        "Recorded locally · No source code uploaded",
        "Recorded with AgentBodycam",
      ]),
    );
  });

  it("works with no failures, recoveries or checks", () => {
    const summary = SESSIONS.find((s): s is Extract<SessionSummary, { readable: true }> => s.readable && s.recording_state === "stopped");
    if (summary === undefined) throw new Error("fixture has a stopped session");
    const model = buildShareModel(plainReport(summary, []));
    expect(model.firstFailure).toBeNull();
    expect(model.recovery).toBeNull();
    expect(model.verdict).toBe("none");
    const lines = texts(buildShareCard(model));
    expect(lines.filter((t) => t === "None recorded")).toHaveLength(2);
    expect(lines).toContain("No checks");
    expect(lines).toContain("No check results recorded");
  });

  it("reports PASS only when every recorded final check passed", () => {
    const r = richReport(STOPPED_ID);
    r.final_check_states.items = r.final_check_states.items.slice(0, 1);
    r.final_check_states.total = 1;
    r.unresolved_failures = { items: [], total: 0, truncated: false };
    expect(texts(buildShareCard(buildShareModel(r)))).toContain("Checks: PASS");
    r.final_check_states.total = 5;
    r.final_check_states.truncated = true;
    expect(buildShareModel(r).verdict).toBe("partial");
  });

  it("words a live session as a snapshot so far, not a final state", () => {
    const r = richReport(STOPPED_ID);
    r.recording_state = "recording";
    r.session_status = "recording";
    r.analysis_complete = false;
    const lines = texts(buildShareCard(buildShareModel(r)));
    expect(lines).toContain("LIVE · SO FAR");
    expect(lines).toContain("SESSION REPLAY · LIVE SNAPSHOT");
    expect(lines).toContain("LATEST RECORDED STATE");
    expect(lines).not.toContain("FINAL RECORDED STATE");
    expect(lines).not.toContain("STOPPED");
  });

  it("uses no causal and no screen-video wording", () => {
    for (const r of [richReport(STOPPED_ID), hostileReport()]) {
      const model = buildShareModel(r);
      const all = [...texts(buildShareCard(model)), serializeSummary(buildShareSummary(model))].join("\n");
      expect(all).not.toMatch(CAUSAL);
      expect(all).not.toMatch(/video|footage|screen ?record|recording of (the|your) screen|watch|camera feed/i);
    }
  });
});

describe("JSON summary", () => {
  it("has a format, a version and only approved keys", () => {
    const summary = buildShareSummary(buildShareModel(richReport(STOPPED_ID)));
    expect(summary.format).toBe(SUMMARY_FORMAT);
    expect(summary.format).toBe("agentbodycam-share-summary");
    expect(summary.version).toBe(SUMMARY_VERSION);
    expect(summary.version).toBe(1);
    expect(Object.keys(summary)).toEqual([...SUMMARY_KEYS]);
    expect(Object.keys(summary.first_observable_failure ?? {})).toEqual(["event", "label"]);
    expect(Object.keys(summary.first_observed_recovery ?? {})).toEqual(["event", "label"]);
    for (const check of summary.final_checks) expect(Object.keys(check)).toEqual(["kind", "name", "result"]);
    expect(summary).toMatchObject({
      events: RICH_TIMELINE.length,
      file_changes: 3,
      observable_failures: 3,
      observed_recoveries: 1,
      unresolved_failures: 1,
      first_observable_failure: { event: 5, label: "Test failed: pytest" },
      first_observed_recovery: { event: 14, label: "Test passed: pytest" },
      final_check_verdict: "fail",
    });
  });

  it("round-trips as JSON and leaks nothing from a hostile report", () => {
    const text = serializeSummary(buildShareSummary(buildShareModel(hostileReport())));
    expect(Object.keys(JSON.parse(text) as object)).toEqual([...SUMMARY_KEYS]);
    expectClean(text);
    expect(text).not.toMatch(/"\w*(session|timestamp|started_at|ended_at|branch|head|sha|path|token|id)\w*":/i);
    expect(text).not.toMatch(/session/i);
  });

  it("uses fixed generic file names", () => {
    expect(SVG_FILENAME).toBe("agentbodycam-replay.svg");
    expect(JSON_FILENAME).toBe("agentbodycam-summary.json");
  });
});
