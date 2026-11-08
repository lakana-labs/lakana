/**
 * Mounts the interface into `index.html`.
 *
 * @packageDocumentation
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

const root = document.getElementById("root");
if (root === null) {
  throw new Error('index.html has no element with id "root": the interface has nowhere to mount.');
}
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
