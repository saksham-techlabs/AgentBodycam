import { useEffect } from "react";

import type { ReplayApiClient } from "../api/client";
import { Sidebar } from "../components/Sidebar";
import { SessionPage } from "../pages/SessionPage";
import { SessionsPage } from "../pages/SessionsPage";
import { ClientProvider } from "./ClientContext";
import { useRoute, type Route } from "./router";

function documentTitle(route: Route): string {
  if (route.name === "session") return `${route.sessionId} · AgentBodycam Replay`;
  if (route.name === "not_found") return "Not found · AgentBodycam Replay";
  return "Sessions · AgentBodycam Replay";
}

export function App({ client }: { client: ReplayApiClient }) {
  const [route, go] = useRoute();
  const home = () => go("/");

  useEffect(() => {
    document.title = documentTitle(route);
  }, [route]);

  return (
    <ClientProvider client={client}>
      <div className="shell">
        <Sidebar onHome={home} homeActive={route.name !== "not_found"} />
        <main className="main">
          {route.name === "sessions" ? (
            <SessionsPage onOpen={go} />
          ) : route.name === "session" ? (
            <SessionPage key={route.sessionId} sessionId={route.sessionId} onBack={home} />
          ) : (
            <NotFound onBack={home} />
          )}
        </main>
      </div>
    </ClientProvider>
  );
}

function NotFound({ onBack }: { onBack: () => void }) {
  return (
    <div className="page">
      <div className="state-panel" role="alert">
        <h2>Page not found</h2>
        <p className="state-detail">This address is not part of the Replay UI.</p>
        <div className="state-actions">
          <button type="button" className="button" onClick={onBack}>
            Back to sessions
          </button>
        </div>
      </div>
    </div>
  );
}
