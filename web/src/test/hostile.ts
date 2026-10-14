import type { ForensicReport } from "../api/types";
import { STOPPED_ID, TOKEN } from "./fakeServer";
import { eventId, richReport } from "./fixtures";

/** A report whose free-text and identifying fields are full of things that must not leak. */
export function hostileReport(): ForensicReport {
  const r = richReport(STOPPED_ID);
  const first = r.first_observable_failure;
  if (first === null) throw new Error("fixture has a first failure");
  first.check_name = "src/auth/login.ts";
  first.summary = 'Modified C:\\Users\\alice\\proj\\src\\auth\\login.ts <script>alert("x")</script>';
  r.timeline.items = r.timeline.items.map((i) => ({ ...i, summary: `${i.summary} /home/alice/proj` }));
  const recovery = r.recoveries.items[0];
  if (recovery === undefined) throw new Error("fixture has a recovery");
  recovery.name = '<script>alert("x")</script>';
  recovery.summary = "pytest -q --token=sk-live-123 passed in /srv/app";
  const states = r.final_check_states.items;
  if (states[1] !== undefined) states[1].name = "deploy.sh";
  if (r.latest_git_state !== null) {
    r.latest_git_state.branch = "feature/secret-branch";
    r.latest_git_state.head = "deadbee";
    r.latest_git_state.upstream = "origin/feature/secret-branch";
    r.latest_git_state.summary = "Git: feature/secret-branch @ deadbee";
  }
  r.warnings.items.push({ code: "x", severity: "info", message: `Bearer ${TOKEN}`, event_ids: [] });
  r.warnings.total = r.warnings.items.length;
  return r;
}

/** Leak probes by category; none may appear in any export. */
export const LEAKS = {
  "absolute paths": ["C:\\", "/home/", "/srv/", "alice"],
  "relative paths": ["src/", "login.ts", "deploy.sh", "logs/locked.txt"],
  "session and event IDs": [STOPPED_ID, "2222222222222222", eventId(5), eventId(14), "f".repeat(64), "cmd-1", "event_id"],
  "Git SHA and branch": ["1a2b3c4", "deadbee", "feature/secret-branch", "origin/"],
  argv: ["pytest -q", "ruff check .", "npm run dev", "argv"],
  "tokens and raw payloads": [TOKEN, "sk-live", "Bearer", "payload", "<script", "PermissionError"],
  timestamps: ["2026-10-14"],
} as const;

export const FORBIDDEN: readonly string[] = Object.values(LEAKS).flat();
