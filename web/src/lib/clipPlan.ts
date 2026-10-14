/**
 * The replay clip's scene plan: a pure function of the privacy-safe `ShareCardModel`.
 *
 * Every string a clip can show is either a fixed phrase from this module or a value the
 * share model already approved (counts, event sequence numbers, rebuilt check labels and
 * allowlisted check names). The plan never sees the forensic report, and drawing and
 * encoding only ever receive this plan.
 */

import type { RecordingState } from "../api/types";
import { formatNumber, STATE_PILL, VERDICT } from "./shareCard";
import { bound, MAX_CHECK_NAME_CHARS, MAX_LABEL_CHARS, type ShareCardModel, type ShareMoment } from "./shareModel";

export const CLIP_WIDTH = 1280;
export const CLIP_HEIGHT = 720;
export const CLIP_FPS = 30;
/** Hard upper bound: a share clip, never an event-by-event recording. */
export const MAX_CLIP_SECONDS = 15;
export const MAX_CLIP_FRAMES = MAX_CLIP_SECONDS * CLIP_FPS;
export const MAX_CLIP_CHECKS = 4;

/** Scene lengths in frames at `CLIP_FPS`: at most 12 s in total, 8 s without findings. */
export const SCENE_FRAMES = {
  intro: 54,
  stats: 75,
  moment: 60,
  final: 66,
  outro: 45,
} as const;

export type ClipTone = "text" | "muted" | "blue" | "red" | "green" | "amber" | "slate";

export interface ClipStat {
  value: number;
  label: string;
  tone: ClipTone;
}

export interface ClipMarker {
  sequence: number;
  tone: ClipTone;
  /** Slides into place during the scene rather than being shown already placed. */
  animate: boolean;
}

export interface ClipCheck {
  name: string;
  passed: boolean;
}

interface SceneBase {
  /** Storyboard name shown in the export dialog. */
  title: string;
  startFrame: number;
  frames: number;
}

export type ReplayClipScene =
  | (SceneBase & { kind: "intro"; tagline: string; subtitle: string })
  | (SceneBase & { kind: "stats"; eyebrow: string; stats: ClipStat[]; markers: ClipMarker[] })
  | (SceneBase & {
      kind: "moment";
      eyebrow: string;
      tone: ClipTone;
      big: string;
      bigTone: ClipTone;
      label: string;
      markers: ClipMarker[];
    })
  | (SceneBase & {
      kind: "final";
      eyebrow: string;
      tag: string | null;
      verdict: string;
      verdictTone: ClipTone;
      detail: string;
      note: string | null;
      checks: ClipCheck[];
      moreChecks: number;
    })
  | (SceneBase & { kind: "outro"; headline: string; line: string; tag: string | null });

export interface ReplayClipTimeline {
  width: number;
  height: number;
  fps: number;
  totalFrames: number;
  durationMs: number;
  live: boolean;
  stateLabel: string;
  stateTone: ClipTone;
  totalEvents: number;
  scenes: ReplayClipScene[];
}

const STATE_TONE: Record<RecordingState, ClipTone> = {
  recording: "blue",
  stopped: "slate",
  ended_without_stop: "amber",
  unknown: "amber",
  not_started: "slate",
};

const VERDICT_TONE = { pass: "green", fail: "red", partial: "amber", none: "slate" } as const;

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

function eventNumber(m: ShareMoment): string {
  return m.sequence > 0 ? `#${formatNumber(m.sequence)}` : "—";
}

function marker(m: ShareMoment | null, tone: ClipTone, animate: boolean): ClipMarker[] {
  return m === null || m.sequence <= 0 ? [] : [{ sequence: m.sequence, tone, animate }];
}

type Draft = ReplayClipScene extends infer S ? (S extends ReplayClipScene ? Omit<S, "startFrame"> : never) : never;

/** Plans the clip. Deterministic: the same model always yields the same timeline. */
export function planReplayClip(model: ShareCardModel): ReplayClipTimeline {
  const live = model.recordingState === "recording";
  const pill = STATE_PILL[model.recordingState];
  const qualifier = live ? " · live snapshot" : model.analysisComplete ? "" : " · partial analysis";
  const failure = model.firstFailure;
  const recovery = model.recovery;

  const drafts: Draft[] = [
    {
      kind: "intro",
      title: "Intro",
      frames: SCENE_FRAMES.intro,
      tagline: "YOUR AI CODING AGENT HAS A BODYCAM",
      subtitle: `Recorded evidence replay${qualifier}`,
    },
    {
      kind: "stats",
      title: "Session stats",
      frames: SCENE_FRAMES.stats,
      eyebrow: live ? "SESSION SO FAR" : "SESSION AT A GLANCE",
      stats: [
        { value: model.totalEvents, label: plural(model.totalEvents, "recorded event", "recorded events"), tone: "text" },
        { value: model.changedFiles, label: plural(model.changedFiles, "file changed", "files changed"), tone: "text" },
        {
          value: model.observableFailures,
          label: plural(model.observableFailures, "observable failure", "observable failures"),
          tone: model.observableFailures > 0 ? "red" : "text",
        },
        {
          value: model.recoveries,
          label: plural(model.recoveries, "observed recovery", "observed recoveries"),
          tone: model.recoveries > 0 ? "green" : "text",
        },
      ],
      markers: [...marker(failure, "red", false), ...marker(recovery, "green", false)],
    },
  ];

  if (failure !== null) {
    drafts.push({
      kind: "moment",
      title: "First observable failure",
      frames: SCENE_FRAMES.moment,
      eyebrow: "FIRST OBSERVABLE FAILURE",
      tone: "red",
      big: eventNumber(failure),
      bigTone: "text",
      label: bound(failure.label, MAX_LABEL_CHARS),
      markers: marker(failure, "red", true),
    });
  }
  if (recovery !== null) {
    drafts.push({
      kind: "moment",
      title: "Observed recovery",
      frames: SCENE_FRAMES.moment,
      eyebrow: "OBSERVED RECOVERY",
      tone: "green",
      big: eventNumber(recovery),
      bigTone: "text",
      label: bound(recovery.label, MAX_LABEL_CHARS),
      markers: [...marker(failure, "red", false), ...marker(recovery, "green", true)],
    });
  } else if (model.unresolved > 0) {
    drafts.push({
      kind: "moment",
      title: "Unresolved in this recording",
      frames: SCENE_FRAMES.moment,
      eyebrow: "UNRESOLVED IN THIS RECORDING",
      tone: "red",
      big: formatNumber(model.unresolved),
      bigTone: "red",
      label: plural(model.unresolved, "observable failure with no observed recovery", "observable failures with no observed recovery"),
      markers: marker(failure, "red", false),
    });
  }

  const verdict = VERDICT[model.verdict];
  drafts.push(
    {
      kind: "final",
      title: live ? "Latest recorded state" : "Final recorded state",
      frames: SCENE_FRAMES.final,
      eyebrow: live ? "LATEST RECORDED STATE" : "FINAL RECORDED STATE",
      tag: live ? "LIVE SNAPSHOT" : null,
      verdict: verdict.text,
      verdictTone: VERDICT_TONE[model.verdict],
      detail:
        model.verdict === "none"
          ? "No check results recorded"
          : `${formatNumber(model.finalPassing)} passing · ${formatNumber(model.finalFailing)} failing`,
      note: model.unresolved > 0 ? `${formatNumber(model.unresolved)} unresolved in this recording` : null,
      checks: model.finalChecks.slice(0, MAX_CLIP_CHECKS).map((c) => ({
        name: bound(c.name === "check" ? c.kind : c.name, MAX_CHECK_NAME_CHARS),
        passed: c.result === "passed",
      })),
      moreChecks: Math.max(0, model.finalChecksTotal - Math.min(model.finalChecks.length, MAX_CLIP_CHECKS)),
    },
    {
      kind: "outro",
      title: "Outro",
      frames: SCENE_FRAMES.outro,
      headline: "Recorded with AgentBodycam",
      line: "Local-first · No source code uploaded",
      tag: live ? "LIVE SNAPSHOT · SO FAR" : null,
    },
  );

  let start = 0;
  const scenes = drafts.map((d) => {
    const scene = { ...d, startFrame: start } as ReplayClipScene;
    start += d.frames;
    return scene;
  });
  const totalFrames = Math.min(start, MAX_CLIP_FRAMES);

  return {
    width: CLIP_WIDTH,
    height: CLIP_HEIGHT,
    fps: CLIP_FPS,
    totalFrames,
    durationMs: Math.round((totalFrames / CLIP_FPS) * 1000),
    live,
    stateLabel: pill.text,
    stateTone: STATE_TONE[model.recordingState],
    totalEvents: model.totalEvents,
    scenes,
  };
}

/** The scene showing at `frame` (clamped to the clip) and the frame's offset within it. */
export function sceneAt(
  timeline: ReplayClipTimeline,
  frame: number,
): { scene: ReplayClipScene; index: number; local: number } {
  const f = Math.min(Math.max(Math.floor(frame), 0), timeline.totalFrames - 1);
  let index = timeline.scenes.findIndex((s) => f < s.startFrame + s.frames);
  if (index === -1) index = timeline.scenes.length - 1;
  const scene = timeline.scenes[index];
  if (scene === undefined) throw new Error("a clip always has scenes");
  return { scene, index, local: f - scene.startFrame };
}

/** Every string the clip may draw, for review and tests. */
export function clipTexts(timeline: ReplayClipTimeline): string[] {
  const out = [timeline.stateLabel];
  for (const s of timeline.scenes) {
    switch (s.kind) {
      case "intro":
        out.push(s.tagline, s.subtitle);
        break;
      case "stats":
        out.push(s.eyebrow, ...s.stats.map((x) => x.label));
        break;
      case "moment":
        out.push(s.eyebrow, s.big, s.label);
        break;
      case "final":
        out.push(s.eyebrow, s.verdict, s.detail, ...s.checks.map((c) => c.name));
        if (s.tag !== null) out.push(s.tag);
        if (s.note !== null) out.push(s.note);
        break;
      case "outro":
        out.push(s.headline, s.line);
        if (s.tag !== null) out.push(s.tag);
        break;
    }
  }
  return out;
}
