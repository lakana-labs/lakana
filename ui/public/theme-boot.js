// Sets the stored theme and density on <html> before the interface's own script runs, so a
// reload never flashes the wrong theme. The content security policy forbids inline script,
// so this is a file. It duplicates only the read of "lakana.preferences" (see
// src/preferences/preferences.ts); anything it does not recognise leaves the defaults.
(() => {
  try {
    const raw = window.localStorage.getItem("lakana.preferences");
    /** @type {unknown} */
    const stored = JSON.parse(raw ?? "null");
    if (typeof stored !== "object" || stored === null) {
      return;
    }
    const root = document.documentElement;
    const theme = "theme" in stored ? stored.theme : undefined;
    if (theme === "light" || theme === "dark") {
      root.setAttribute("data-theme", theme);
    }
    const density = "density" in stored ? stored.density : undefined;
    if (density === "roomy" || density === "compact") {
      root.setAttribute("data-density", density);
    }
  } catch {
    // Blocked storage or a corrupt value: the defaults stay.
  }
})();
