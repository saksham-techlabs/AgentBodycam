/**
 * Draws one replay clip frame onto a 2D canvas. A pure function of the clip plan and the
 * frame number: known shapes and the plan's own strings only, in the share card's system
 * font stacks. No images, no SVG rasterization, no DOM capture and no web fonts.
 */

import {
  sceneAt,
  type ClipMarker,
  type ClipTone,
  type ReplayClipScene,
  type ReplayClipTimeline,
} from "./clipPlan";
import { formatNumber, MONO, SANS, SHARE_COLORS as C } from "./shareCard";

/** The subset of `CanvasRenderingContext2D` the clip uses. */
export type ClipCanvasContext = Pick<
  CanvasRenderingContext2D,
  | "fillStyle"
  | "strokeStyle"
  | "lineWidth"
  | "font"
  | "textAlign"
  | "textBaseline"
  | "globalAlpha"
  | "save"
  | "restore"
  | "fillRect"
  | "fillText"
  | "beginPath"
  | "moveTo"
  | "lineTo"
  | "arc"
  | "arcTo"
  | "closePath"
  | "fill"
  | "stroke"
  | "createLinearGradient"
  | "createRadialGradient"
  | "measureText"
>;

const LEFT = 72;
const RIGHT = 1208;
const INNER = RIGHT - LEFT;
const CENTER = 640;
const ENTER_FRAMES = 12;
const EXIT_FRAMES = 8;
const STAT_STAGGER = 6;
const COUNT_FRAMES = 30;
const MARKER_FRAMES = 24;

const TONE: Record<ClipTone, string> = {
  text: C.text,
  muted: C.text3,
  blue: C.blue,
  red: C.red,
  green: C.green,
  amber: C.amber,
  slate: C.slate,
};

const clamp01 = (t: number): number => Math.min(Math.max(t, 0), 1);
const easeOut = (t: number): number => 1 - (1 - clamp01(t)) ** 3;

interface TextOptions {
  size: number;
  color: string;
  weight?: number;
  mono?: boolean;
  align?: CanvasTextAlign;
  maxWidth?: number;
}

function text(ctx: ClipCanvasContext, content: string, x: number, y: number, o: TextOptions): void {
  ctx.font = `${o.weight ?? 400} ${o.size}px ${o.mono === true ? MONO : SANS}`;
  ctx.fillStyle = o.color;
  ctx.textAlign = o.align ?? "left";
  ctx.fillText(content, x, y, o.maxWidth ?? INNER);
}

function width(ctx: ClipCanvasContext, content: string, size: number, weight: number, mono = false): number {
  ctx.font = `${weight} ${size}px ${mono ? MONO : SANS}`;
  return ctx.measureText(content).width;
}

function roundRect(ctx: ClipCanvasContext, x: number, y: number, w: number, h: number, r: number): void {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function circle(ctx: ClipCanvasContext, x: number, y: number, r: number): void {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
}

/** Fills with `color` at `alpha` relative to the current global alpha. */
function withAlpha(ctx: ClipCanvasContext, alpha: number, draw: () => void): void {
  ctx.save();
  ctx.globalAlpha *= alpha;
  draw();
  ctx.restore();
}

function wordmark(ctx: ClipCanvasContext, x: number, y: number, size: number, align: "left" | "center"): void {
  const a = width(ctx, "Agent", size, 700);
  const b = width(ctx, "Bodycam", size, 700);
  const start = align === "center" ? x - (a + b) / 2 : x;
  text(ctx, "Agent", start, y, { size, color: C.text, weight: 700 });
  text(ctx, "Bodycam", start + a, y, { size, color: C.blue, weight: 700 });
}

function background(ctx: ClipCanvasContext, tl: ReplayClipTimeline): void {
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, tl.width, tl.height);
  const glow = ctx.createRadialGradient(tl.width * 0.85, tl.height * 0.05, 0, tl.width * 0.85, tl.height * 0.05, tl.width * 0.75);
  glow.addColorStop(0, "rgba(90, 167, 255, 0.14)");
  glow.addColorStop(1, "rgba(90, 167, 255, 0)");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, tl.width, tl.height);
  roundRect(ctx, 20, 20, tl.width - 40, tl.height - 40, 22);
  ctx.strokeStyle = C.border;
  ctx.lineWidth = 1;
  ctx.stroke();
}

function hud(ctx: ClipCanvasContext, tl: ReplayClipTimeline): void {
  roundRect(ctx, LEFT, 50, 44, 44, 11);
  ctx.fillStyle = C.panel;
  ctx.fill();
  ctx.strokeStyle = C.borderStrong;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  circle(ctx, LEFT + 22, 72, 10.5);
  ctx.strokeStyle = C.blue;
  ctx.lineWidth = 2.5;
  ctx.stroke();
  circle(ctx, LEFT + 22, 72, 4.4);
  ctx.fillStyle = C.blue;
  ctx.fill();
  circle(ctx, LEFT + 35, 59, 3.2);
  ctx.fillStyle = C.red;
  ctx.fill();
  wordmark(ctx, LEFT + 62, 81, 26, "left");

  const tone = TONE[tl.stateTone];
  const pillWidth = width(ctx, tl.stateLabel, 13, 600, true) + 44;
  roundRect(ctx, RIGHT - pillWidth, 56, pillWidth, 32, 16);
  withAlpha(ctx, 0.1, () => {
    ctx.fillStyle = tone;
    ctx.fill();
  });
  withAlpha(ctx, 0.45, () => {
    ctx.strokeStyle = tone;
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  circle(ctx, RIGHT - pillWidth + 18, 72, 4);
  ctx.fillStyle = tone;
  ctx.fill();
  text(ctx, tl.stateLabel, RIGHT - 16, 77, { size: 13, color: tone, weight: 600, mono: true, align: "right" });
}

/** The clip's own progress along the bottom edge, with a tick at each scene boundary. */
function progress(ctx: ClipCanvasContext, tl: ReplayClipTimeline, frame: number, index: number): void {
  const y = 640;
  const played = LEFT + INNER * clamp01((frame + 1) / tl.totalFrames);
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.border;
  ctx.beginPath();
  ctx.moveTo(LEFT, y);
  ctx.lineTo(RIGHT, y);
  ctx.stroke();
  ctx.strokeStyle = C.blue;
  ctx.beginPath();
  ctx.moveTo(LEFT, y);
  ctx.lineTo(played, y);
  ctx.stroke();
  ctx.lineWidth = 1;
  ctx.strokeStyle = C.borderStrong;
  for (const s of tl.scenes.slice(1)) {
    const x = LEFT + (INNER * s.startFrame) / tl.totalFrames;
    ctx.beginPath();
    ctx.moveTo(x, y - 5);
    ctx.lineTo(x, y + 5);
    ctx.stroke();
  }
  circle(ctx, played, y, 4);
  ctx.fillStyle = C.blue;
  ctx.fill();
  const pad = (n: number): string => String(n).padStart(2, "0");
  text(ctx, "RECORDED EVIDENCE REPLAY", LEFT, 670, { size: 12, color: C.text3, mono: true, weight: 600 });
  text(ctx, `${pad(index + 1)} / ${pad(tl.scenes.length)}`, RIGHT, 670, {
    size: 12,
    color: C.text3,
    mono: true,
    weight: 600,
    align: "right",
  });
}

/** The recorded-event axis (#1 … #N) with the finding markers. */
function eventTrack(ctx: ClipCanvasContext, tl: ReplayClipTimeline, markers: ClipMarker[], y: number, local: number): void {
  ctx.lineWidth = 2;
  ctx.strokeStyle = C.borderStrong;
  ctx.beginPath();
  ctx.moveTo(LEFT, y);
  ctx.lineTo(RIGHT, y);
  ctx.stroke();
  ctx.lineWidth = 1;
  for (let i = 0; i <= 48; i += 1) {
    const x = LEFT + (i / 48) * INNER;
    const h = i % 8 === 0 ? 7 : 4;
    ctx.beginPath();
    ctx.moveTo(x, y - h);
    ctx.lineTo(x, y + h);
    ctx.stroke();
  }
  const span = Math.max(tl.totalEvents, 1);
  for (const m of markers) {
    const target = LEFT + (Math.min(Math.max(m.sequence, 1), span) / span) * INNER;
    const x = m.animate ? LEFT + (target - LEFT) * easeOut(local / MARKER_FRAMES) : target;
    const color = TONE[m.tone];
    withAlpha(ctx, 0.18, () => {
      circle(ctx, x, y, 12);
      ctx.fillStyle = color;
      ctx.fill();
    });
    circle(ctx, x, y, 6);
    ctx.fillStyle = color;
    ctx.fill();
  }
  text(ctx, "#1", LEFT, y + 32, { size: 14, color: C.text3, mono: true });
  text(ctx, `#${formatNumber(span)}`, RIGHT, y + 32, { size: 14, color: C.text3, mono: true, align: "right" });
}

function eyebrow(ctx: ClipCanvasContext, label: string, tone: ClipTone, y: number): void {
  text(ctx, label, LEFT, y, { size: 16, color: TONE[tone], weight: 600 });
}

function drawScene(ctx: ClipCanvasContext, tl: ReplayClipTimeline, scene: ReplayClipScene, local: number, dy: number): void {
  switch (scene.kind) {
    case "intro": {
      wordmark(ctx, CENTER, 320 + dy, 88, "center");
      text(ctx, scene.tagline, CENTER, 372 + dy, { size: 18, color: C.text3, weight: 600, align: "center" });
      const line = 160 * easeOut((local - 8) / 24);
      ctx.strokeStyle = C.blue;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(CENTER - line / 2, 410 + dy);
      ctx.lineTo(CENTER + line / 2, 410 + dy);
      ctx.stroke();
      text(ctx, scene.subtitle, CENTER, 462 + dy, { size: 26, color: C.text2, align: "center" });
      return;
    }
    case "stats": {
      eyebrow(ctx, scene.eyebrow, "blue", 176 + dy);
      const gap = 20;
      const w = (INNER - gap * (scene.stats.length - 1)) / scene.stats.length;
      scene.stats.forEach((s, i) => {
        const t = local - i * STAT_STAGGER;
        const x = LEFT + i * (w + gap);
        const rise = (1 - easeOut(t / ENTER_FRAMES)) * 12;
        withAlpha(ctx, clamp01(t / ENTER_FRAMES), () => {
          roundRect(ctx, x, 214 + dy + rise, w, 200, 14);
          ctx.fillStyle = C.panel;
          ctx.fill();
          ctx.strokeStyle = C.border;
          ctx.lineWidth = 1;
          ctx.stroke();
          const shown = Math.round(s.value * easeOut(t / COUNT_FRAMES));
          text(ctx, formatNumber(shown), x + 26, 318 + dy + rise, {
            size: 72,
            color: TONE[s.tone],
            weight: 600,
            mono: true,
            maxWidth: w - 52,
          });
          text(ctx, s.label, x + 26, 372 + dy + rise, { size: 20, color: C.text2, maxWidth: w - 52 });
        });
      });
      eventTrack(ctx, tl, scene.markers, 512, local);
      return;
    }
    case "moment": {
      const tone = TONE[scene.tone];
      ctx.fillStyle = tone;
      ctx.fillRect(LEFT, 156 + dy, 4, 238);
      text(ctx, scene.eyebrow, LEFT + 28, 190 + dy, { size: 18, color: tone, weight: 600 });
      text(ctx, scene.big, LEFT + 24, 316 + dy, { size: 120, color: TONE[scene.bigTone], weight: 600, mono: true });
      text(ctx, scene.label, LEFT + 28, 378 + dy, { size: 34, color: C.text2, maxWidth: INNER - 28 });
      eventTrack(ctx, tl, scene.markers, 512, local);
      return;
    }
    case "final": {
      eyebrow(ctx, scene.eyebrow, "blue", 190 + dy);
      if (scene.tag !== null) {
        text(ctx, scene.tag, RIGHT, 190 + dy, { size: 15, color: C.blue, weight: 600, mono: true, align: "right" });
      }
      text(ctx, scene.verdict, LEFT - 4, 296 + dy, { size: 84, color: TONE[scene.verdictTone], weight: 700 });
      text(ctx, scene.detail, LEFT, 348 + dy, { size: 26, color: C.text2 });
      if (scene.note !== null) text(ctx, scene.note, LEFT, 390 + dy, { size: 20, color: C.red });
      let x = LEFT;
      for (const c of scene.checks) {
        const label = c.passed ? "PASS" : "FAIL";
        const w = width(ctx, label, 15, 700, true) + width(ctx, c.name, 17, 500) + 44;
        roundRect(ctx, x, 440 + dy, w, 46, 10);
        ctx.fillStyle = C.panel;
        ctx.fill();
        ctx.strokeStyle = C.border;
        ctx.lineWidth = 1;
        ctx.stroke();
        text(ctx, label, x + 16, 469 + dy, { size: 15, color: c.passed ? C.green : C.red, weight: 700, mono: true });
        text(ctx, c.name, x + 28 + width(ctx, label, 15, 700, true), 469 + dy, { size: 17, color: C.text, weight: 500 });
        x += w + 12;
      }
      if (scene.moreChecks > 0) {
        text(ctx, `+${formatNumber(scene.moreChecks)} more`, x + 4, 469 + dy, { size: 16, color: C.text3 });
      }
      return;
    }
    case "outro": {
      text(ctx, scene.headline, CENTER, 330 + dy, { size: 46, color: C.text, weight: 700, align: "center" });
      text(ctx, scene.line, CENTER, 386 + dy, { size: 24, color: C.text2, align: "center" });
      if (scene.tag !== null) {
        text(ctx, scene.tag, CENTER, 440 + dy, { size: 15, color: C.blue, weight: 600, mono: true, align: "center" });
      }
      return;
    }
  }
}

/** Draws frame `frame` (clamped to the clip) of `tl`. */
export function drawClipFrame(ctx: ClipCanvasContext, tl: ReplayClipTimeline, frame: number): void {
  const { scene, index, local } = sceneAt(tl, frame);
  const last = index === tl.scenes.length - 1;
  const enter = easeOut(local / ENTER_FRAMES);
  const exit = last ? 1 : clamp01((scene.frames - local) / EXIT_FRAMES);
  ctx.save();
  ctx.globalAlpha = 1;
  ctx.textBaseline = "alphabetic";
  background(ctx, tl);
  hud(ctx, tl);
  ctx.save();
  ctx.globalAlpha = Math.min(enter, exit);
  drawScene(ctx, tl, scene, local, (1 - enter) * 16);
  ctx.restore();
  progress(ctx, tl, scene.startFrame + local, index);
  ctx.restore();
}
