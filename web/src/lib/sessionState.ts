import type { RecordingState, SessionSummary } from "../api/types";

/** What a session looks like in the UI: the backend's recording state, or unreadable. */
export type VisualState = "live" | "stopped" | "ended_without_stop" | "unknown" | "not_started" | "unreadable";

/** Colour language: info = neutral recorded evidence, success = pass, danger = failed. */
export type Tone = "info" | "neutral" | "success" | "warning" | "danger" | "muted";

export interface StatePresentation {
  label: string;
  tone: Tone;
  description: string;
}

const PRESENTATION: Record<VisualState, StatePresentation> = {
  live: {
    label: "LIVE",
    tone: "info",
    description: "Recording in progress. Counts cover the evidence recorded so far.",
  },
  stopped: {
    label: "STOPPED",
    tone: "neutral",
    description: "The recording was stopped normally. Analysis is complete.",
  },
  ended_without_stop: {
    label: "ENDED WITHOUT STOP",
    tone: "warning",
    description:
      "The recorder ended without stopping the session (for example it crashed or was " +
      "killed). Everything recorded up to then is analysed.",
  },
  unknown: {
    label: "UNKNOWN",
    tone: "warning",
    description:
      "The session is marked as recording, but whether a recorder is still live could " +
      "not be established. Analysis may be incomplete.",
  },
  not_started: {
    label: "NOT STARTED",
    tone: "muted",
    description: "The session was created, but recording never started.",
  },
  unreadable: {
    label: "UNREADABLE",
    tone: "danger",
    description: "The session's stored evidence could not be read, so it cannot be opened.",
  },
};

const BY_RECORDING_STATE: Record<RecordingState, VisualState> = {
  recording: "live",
  stopped: "stopped",
  ended_without_stop: "ended_without_stop",
  unknown: "unknown",
  not_started: "not_started",
};

export function visualState(recordingState: RecordingState | null): VisualState {
  return recordingState === null ? "unreadable" : BY_RECORDING_STATE[recordingState];
}

export function presentation(state: VisualState): StatePresentation {
  return PRESENTATION[state];
}

export function summaryState(session: SessionSummary): VisualState {
  return session.readable ? visualState(session.recording_state) : "unreadable";
}
