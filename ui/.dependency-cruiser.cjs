// The import rules of the interface (docs/agent/conventions.md, "The ui/ project").
// ESLint, not this file, keeps `fetch` inside src/api/: it is a global, not an import.
const TEST = "\\.test\\.(ts|tsx)$";

/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: "no-circular",
      severity: "error",
      comment: "Two modules that import each other cannot be understood or tested apart.",
      from: {},
      to: { circular: true },
    },
    {
      name: "no-orphans",
      severity: "error",
      comment: "A module nothing imports is dead code (tests and test setup excepted).",
      from: { orphan: true, pathNot: [TEST, "^src/test/", "\\.d\\.ts$"] },
      to: {},
    },
    {
      name: "not-to-unresolvable",
      severity: "error",
      comment: "An import that resolves to nothing fails in the browser.",
      from: {},
      to: { couldNotResolve: true },
    },
    {
      name: "not-to-dev-dep",
      severity: "error",
      comment: "Code that ships may not import a development dependency.",
      from: { path: "^src/", pathNot: [TEST, "^src/test/"] },
      to: { dependencyTypes: ["npm-dev"], dependencyTypesNot: ["type-only"] },
    },
    {
      name: "no-non-package-json",
      severity: "error",
      comment: "An npm package that package.json does not list works only by accident.",
      from: {},
      to: { dependencyTypes: ["npm-no-pkg", "npm-unknown"] },
    },
    {
      name: "api-is-a-leaf",
      severity: "error",
      comment: "src/api/ talks to the server and imports nothing of the interface.",
      from: { path: "^src/api/" },
      to: { pathNot: ["^src/api/", "^node_modules/"] },
    },
    {
      name: "no-test-from-src",
      severity: "error",
      comment: "Code that ships may not import a test or the test setup.",
      from: { path: "^src/", pathNot: [TEST, "^src/test/"] },
      to: { path: [TEST, "^src/test/"] },
    },
  ],
  options: {
    doNotFollow: { path: "node_modules" },
    tsPreCompilationDeps: true,
    tsConfig: { fileName: "tsconfig.json" },
    enhancedResolveOptions: {
      exportsFields: ["exports"],
      conditionNames: ["import", "require", "node", "default", "types"],
      mainFields: ["module", "main", "types", "typings"],
    },
  },
};
