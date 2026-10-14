import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ReplayApiClient } from "./api/client";
import { App } from "./app/App";
import "./styles/app.css";

const root = document.getElementById("root");
if (root === null) throw new Error("missing #root element");

createRoot(root).render(
  <StrictMode>
    <App client={new ReplayApiClient()} />
  </StrictMode>,
);
