import { presentation, type VisualState } from "../lib/sessionState";

export function StatusBadge({ state }: { state: VisualState }) {
  const { label, tone } = presentation(state);
  return (
    <span className={`badge tone-${tone}`} data-state={state}>
      <span className={state === "live" ? "badge-dot pulse" : "badge-dot"} aria-hidden="true" />
      {label}
    </span>
  );
}
