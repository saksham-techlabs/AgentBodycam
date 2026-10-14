/**
 * The 1200×630 share card, laid out as an `SvgNode` tree from a `ShareCardModel` only.
 * Deterministic: the same model always yields the same tree. System font stacks only, no
 * remote fonts or images.
 */

import type { RecordingState } from "../api/types";
import { bound, MAX_LABEL_CHARS, type ShareCardModel, type ShareMoment, type ShareVerdict } from "./shareModel";
import { el, serializeSvg, type SvgNode } from "./svg";

export const CARD_WIDTH = 1200;
export const CARD_HEIGHT = 630;
export const CARD_TITLE = "AgentBodycam session replay summary";

const C = {
  bg: "#06090f",
  panel: "#0d131d",
  border: "#1a2331",
  borderStrong: "#26334a",
  text: "#e4e9f1",
  text2: "#9ba7b9",
  text3: "#66738a",
  blue: "#5aa7ff",
  green: "#3ccf8e",
  red: "#ff5d6c",
  amber: "#f2b14c",
  slate: "#8d9bb3",
};

const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const MONO = 'ui-monospace, "SF Mono", "Cascadia Mono", Consolas, Menlo, monospace';

const LEFT = 64;
const RIGHT = CARD_WIDTH - 64;
const INNER = RIGHT - LEFT;

const STATE_PILL: Record<RecordingState, { text: string; color: string }> = {
  recording: { text: "LIVE · SO FAR", color: C.blue },
  stopped: { text: "STOPPED", color: C.slate },
  ended_without_stop: { text: "ENDED WITHOUT STOP", color: C.amber },
  unknown: { text: "STATE UNKNOWN", color: C.amber },
  not_started: { text: "NOT STARTED", color: C.slate },
};

const VERDICT: Record<ShareVerdict, { text: string; color: string }> = {
  pass: { text: "Checks: PASS", color: C.green },
  fail: { text: "Checks: FAIL", color: C.red },
  partial: { text: "Checks: PARTIAL", color: C.amber },
  none: { text: "No checks", color: C.slate },
};

function formatNumber(n: number): string {
  return n >= 999_999 ? "999,999+" : String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function text(
  x: number,
  y: number,
  content: string,
  opts: { size: number; color: string; mono?: boolean; weight?: number; spacing?: number; anchor?: "start" | "end" },
): SvgNode {
  return el(
    "text",
    {
      x,
      y,
      fill: opts.color,
      "font-family": opts.mono === true ? MONO : SANS,
      "font-size": opts.size,
      "font-weight": opts.weight ?? 400,
      ...(opts.spacing === undefined ? {} : { "letter-spacing": opts.spacing }),
      ...(opts.anchor === "end" ? { "text-anchor": "end" } : {}),
    },
    content,
  );
}

function header(model: ShareCardModel): SvgNode {
  const pill = STATE_PILL[model.recordingState];
  const pillWidth = 40 + Array.from(pill.text).length * 8.8;
  return el(
    "g",
    {},
    el("rect", { x: LEFT, y: 52, width: 44, height: 44, rx: 11, fill: C.panel, stroke: C.borderStrong, "stroke-width": 1.5 }),
    el("circle", { cx: LEFT + 22, cy: 74, r: 10.5, fill: "none", stroke: C.blue, "stroke-width": 2.5 }),
    el("circle", { cx: LEFT + 22, cy: 74, r: 4.4, fill: C.blue }),
    el("circle", { cx: LEFT + 35, cy: 61, r: 3.2, fill: C.red }),
    el(
      "text",
      { x: LEFT + 62, y: 80, "font-family": SANS, "font-size": 28, "font-weight": 700, fill: C.text },
      el("tspan", {}, "Agent"),
      el("tspan", { fill: C.blue }, "Bodycam"),
    ),
    text(LEFT + 63, 102, "YOUR AI CODING AGENT HAS A BODYCAM", { size: 11, color: C.text3, spacing: 3 }),
    el("rect", {
      x: RIGHT - pillWidth,
      y: 58,
      width: pillWidth,
      height: 32,
      rx: 16,
      fill: pill.color,
      "fill-opacity": 0.1,
      stroke: pill.color,
      "stroke-opacity": 0.45,
    }),
    el("circle", { cx: RIGHT - pillWidth + 18, cy: 74, r: 4, fill: pill.color }),
    text(RIGHT - 16, 79, pill.text, { size: 12, color: pill.color, mono: true, weight: 600, spacing: 1.5, anchor: "end" }),
  );
}

function eyebrow(model: ShareCardModel): string {
  if (model.recordingState === "recording") return "SESSION REPLAY · LIVE SNAPSHOT";
  return model.analysisComplete ? "SESSION REPLAY" : "SESSION REPLAY · PARTIAL ANALYSIS";
}

function stats(model: ShareCardModel): SvgNode {
  const tiles: [string, number, string][] = [
    ["RECORDED EVENTS", model.totalEvents, C.text],
    ["FILES CHANGED", model.changedFiles, C.text],
    ["FAILED CHECKS", model.failedChecks, model.failedChecks > 0 ? C.red : C.text],
    ["RECOVERIES", model.recoveries, model.recoveries > 0 ? C.green : C.text],
    ["UNRESOLVED", model.unresolved, model.unresolved > 0 ? C.red : C.text],
  ];
  const gap = 18;
  const width = (INNER - gap * (tiles.length - 1)) / tiles.length;
  return el(
    "g",
    {},
    ...tiles.map(([label, value, color], i) => {
      const x = LEFT + i * (width + gap);
      return el(
        "g",
        {},
        el("rect", { x, y: 196, width, height: 112, rx: 12, fill: C.panel, stroke: C.border }),
        text(x + 22, 258, formatNumber(value), { size: 44, color, mono: true, weight: 600 }),
        text(x + 22, 290, label, { size: 11, color: C.text3, spacing: 1.5, weight: 600 }),
      );
    }),
  );
}

function markerX(sequence: number, total: number): number {
  const span = Math.max(total, 1);
  return LEFT + (Math.min(Math.max(sequence, 1), span) / span) * INNER;
}

function track(model: ShareCardModel): SvgNode {
  const y = 352;
  const ticks: SvgNode[] = [];
  for (let i = 0; i <= 48; i += 1) {
    const x = LEFT + (i / 48) * INNER;
    const major = i % 8 === 0;
    ticks.push(el("line", { x1: x, y1: y - (major ? 7 : 4), x2: x, y2: y + (major ? 7 : 4), stroke: C.borderStrong, "stroke-width": 1 }));
  }
  const marker = (m: ShareMoment | null, color: string): SvgNode | null =>
    m === null || m.sequence === 0
      ? null
      : el(
          "g",
          {},
          el("circle", { cx: markerX(m.sequence, model.totalEvents), cy: y, r: 11, fill: color, "fill-opacity": 0.18 }),
          el("circle", { cx: markerX(m.sequence, model.totalEvents), cy: y, r: 5.5, fill: color }),
        );
  return el(
    "g",
    {},
    el("line", { x1: LEFT, y1: y, x2: RIGHT, y2: y, stroke: "url(#abc-card-track)", "stroke-width": 2 }),
    ...ticks,
    marker(model.firstFailure, C.red),
    marker(model.recovery, C.green),
    text(LEFT, y + 30, "#1", { size: 12, color: C.text3, mono: true }),
    text(RIGHT, y + 30, `#${formatNumber(Math.max(model.totalEvents, 1))}`, { size: 12, color: C.text3, mono: true, anchor: "end" }),
  );
}

function panel(x: number, width: number, accent: string, label: string, big: string, bigColor: string, sub: string): SvgNode {
  return el(
    "g",
    {},
    el("rect", { x, y: 404, width, height: 128, rx: 12, fill: C.panel, stroke: C.border }),
    el("rect", { x, y: 420, width: 3, height: 96, rx: 1.5, fill: accent }),
    text(x + 24, 436, label, { size: 11, color: accent, spacing: 2, weight: 600 }),
    text(x + 24, 480, big, { size: 30, color: bigColor, mono: true, weight: 600 }),
    text(x + 24, 512, bound(sub, MAX_LABEL_CHARS), { size: 17, color: C.text2 }),
  );
}

function moments(model: ShareCardModel): SvgNode {
  const gap = 18;
  const width = (INNER - gap * 2) / 3;
  const live = model.recordingState === "recording";
  const verdict = VERDICT[model.verdict];
  const checksLine =
    model.verdict === "none"
      ? "No check results recorded"
      : `${formatNumber(model.finalPassing)} passing · ${formatNumber(model.finalFailing)} failing`;
  const moment = (m: ShareMoment | null): [string, string, string] =>
    m === null ? ["—", C.text3, "None recorded"] : [`#${formatNumber(m.sequence)}`, C.text, m.label];
  const [fBig, fColor, fSub] = moment(model.firstFailure);
  const [rBig, rColor, rSub] = moment(model.recovery);
  return el(
    "g",
    {},
    panel(LEFT, width, C.red, "FIRST OBSERVABLE FAILURE", fBig, fColor, fSub),
    panel(LEFT + width + gap, width, C.green, "OBSERVED RECOVERY", rBig, rColor, rSub),
    panel(
      LEFT + 2 * (width + gap),
      width,
      C.blue,
      live ? "LATEST RECORDED STATE" : "FINAL RECORDED STATE",
      verdict.text,
      verdict.color,
      checksLine,
    ),
  );
}

function footer(): SvgNode {
  return el(
    "g",
    {},
    el("line", { x1: LEFT, y1: 560, x2: RIGHT, y2: 560, stroke: C.border, "stroke-width": 1 }),
    text(LEFT, 594, "Recorded locally · No source code uploaded", { size: 15, color: C.text2 }),
    text(RIGHT, 594, "Recorded with AgentBodycam", { size: 13, color: C.text3, spacing: 0.5, anchor: "end" }),
  );
}

export function buildShareCard(model: ShareCardModel): SvgNode {
  return el(
    "svg",
    { width: CARD_WIDTH, height: CARD_HEIGHT, viewBox: `0 0 ${CARD_WIDTH} ${CARD_HEIGHT}` },
    el("title", {}, CARD_TITLE),
    el(
      "defs",
      {},
      el(
        "radialGradient",
        { id: "abc-card-glow", cx: 0.85, cy: 0.05, r: 0.75 },
        el("stop", { offset: 0, "stop-color": C.blue, "stop-opacity": 0.16 }),
        el("stop", { offset: 1, "stop-color": C.blue, "stop-opacity": 0 }),
      ),
      el(
        "linearGradient",
        { id: "abc-card-track", x1: 0, y1: 0, x2: 1, y2: 0 },
        el("stop", { offset: 0, "stop-color": C.blue, "stop-opacity": 0.7 }),
        el("stop", { offset: 1, "stop-color": C.blue, "stop-opacity": 0.12 }),
      ),
    ),
    el("rect", { x: 0, y: 0, width: CARD_WIDTH, height: CARD_HEIGHT, fill: C.bg }),
    el("rect", { x: 0, y: 0, width: CARD_WIDTH, height: CARD_HEIGHT, fill: "url(#abc-card-glow)" }),
    el("rect", { x: 16, y: 16, width: CARD_WIDTH - 32, height: CARD_HEIGHT - 32, rx: 20, fill: "none", stroke: C.border }),
    header(model),
    text(LEFT, 170, eyebrow(model), { size: 13, color: C.blue, spacing: 4, weight: 600 }),
    stats(model),
    track(model),
    moments(model),
    footer(),
  );
}

export function shareCardSvg(model: ShareCardModel): string {
  return serializeSvg(buildShareCard(model));
}
