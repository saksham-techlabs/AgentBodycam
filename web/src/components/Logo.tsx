/** The AgentBodycam mark: a camera lens with a recording indicator. Inline SVG, no asset. */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg
      className="logo"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="1.5" y="1.5" width="29" height="29" rx="8" className="logo-frame" />
      <circle cx="16" cy="16" r="7.5" className="logo-lens" />
      <circle cx="16" cy="16" r="3.2" className="logo-iris" />
      <circle cx="25" cy="7" r="2.2" className="logo-rec" />
    </svg>
  );
}
