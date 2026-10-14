import type { ConnectionStatus } from "../api/client";
import { useClient, useConnectionStatus } from "../app/ClientContext";
import { useResource } from "../hooks/useResource";
import { Logo } from "./Logo";

const STATUS: Record<ConnectionStatus, { label: string; tone: string }> = {
  connecting: { label: "Connecting…", tone: "muted" },
  online: { label: "Connected", tone: "success" },
  unreachable: { label: "Server unreachable", tone: "danger" },
  unauthorized: { label: "Access expired", tone: "warning" },
  refused: { label: "Access refused", tone: "danger" },
};

function ServerStatus() {
  const client = useClient();
  const status = useConnectionStatus();
  const health = useResource("health", (signal) => client.health(signal));
  const { label, tone } = STATUS[status];
  return (
    <section className="server-status" aria-label="Server status">
      <div className="server-status-row">
        <span className={`badge-dot tone-${tone}`} aria-hidden="true" />
        <span className="server-status-label" data-testid="connection-status">
          {label}
        </span>
      </div>
      <dl className="server-status-facts">
        <div>
          <dt>Server</dt>
          <dd className="mono">{window.location.host}</dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>Loopback · read-only</dd>
        </div>
        {health.data === null ? null : (
          <div>
            <dt>API</dt>
            <dd className="mono">v{health.data.api_version}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

export function Sidebar({ onHome, homeActive }: { onHome: () => void; homeActive: boolean }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <Logo />
        <div>
          <div className="brand-name">AgentBodycam</div>
          <div className="brand-sub">Replay</div>
        </div>
      </div>
      <nav className="nav" aria-label="Main">
        <a
          href="/"
          className="nav-item"
          aria-current={homeActive ? "page" : undefined}
          onClick={(event) => {
            event.preventDefault();
            onHome();
          }}
        >
          <svg className="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <rect x="2" y="2.5" width="12" height="3" rx="1" />
            <rect x="2" y="7" width="12" height="3" rx="1" />
            <rect x="2" y="11.5" width="12" height="2" rx="1" />
          </svg>
          Sessions
        </a>
      </nav>
      <div className="sidebar-foot">
        <ServerStatus />
        <p className="sidebar-version">
          Replay UI <span className="pill">preview</span>
          <br />
          Under development · local only
        </p>
      </div>
    </aside>
  );
}
