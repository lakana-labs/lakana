/**
 * Mounts the interface into `index.html`.
 *
 * @packageDocumentation
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { initI18n } from "./i18n/i18n";
import "./styles/tokens.css";
import "./styles/fonts.css";
import "./styles/base.css";
import "./status/status.css";
import "./shell/shell.css";
import "./shell/screen.css";
import "./sign-in/sign-in.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error('index.html has no element with id "root": the interface has nowhere to mount.');
}
initI18n();
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
