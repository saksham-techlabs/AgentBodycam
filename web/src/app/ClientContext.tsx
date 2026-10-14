import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";

import type { ConnectionStatus, ReplayApiClient } from "../api/client";

const ClientContext = createContext<ReplayApiClient | null>(null);

export function ClientProvider({
  client,
  children,
}: {
  client: ReplayApiClient;
  children: ReactNode;
}) {
  return <ClientContext.Provider value={client}>{children}</ClientContext.Provider>;
}

export function useClient(): ReplayApiClient {
  const client = useContext(ClientContext);
  if (client === null) throw new Error("useClient() outside <ClientProvider>");
  return client;
}

export function useConnectionStatus(): ConnectionStatus {
  const client = useClient();
  return useSyncExternalStore(client.subscribe, () => client.status);
}
