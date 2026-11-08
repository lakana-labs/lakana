import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    // Only unit tests: Playwright owns e2e/*.spec.ts.
    include: ["src/**/*.test.{ts,tsx}", "build/**/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      // main.tsx only mounts <App/>, like `if __name__ == "__main__"` in Python; App.tsx is
      // only the browser router's wiring (the routes are tested through appRoutes, the wiring
      // by the Playwright tests).
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx", "src/App.tsx"],
      reporter: ["text-summary", "json-summary"],
      thresholds: { lines: 90, branches: 90, functions: 90, statements: 90 },
    },
  },
});
