// Pull-request title rules for Lakana (the title becomes the squash-commit subject).
// Extends the Conventional Commits preset from conventional-changelog's ecosystem; the
// type list mirrors `changelog-sections` in release-please-config.json and the scope
// list mirrors CLAUDE.md. Checked in CI by .github/workflows/conventional.yml.
export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "type-enum": [
      2,
      "always",
      ["feat", "fix", "perf", "revert", "docs", "deps", "refactor", "test", "ci", "build", "chore"],
    ],
    "scope-enum": [
      2,
      "always",
      [
        "core", "server", "worker", "sdk", "cli", "ui", "catalog", "query", "policy",
        "secrets", "audit", "security", "release", "ci", "docs", "deps",
      ],
    ],
    // Titles name milestones and products ("M4a SDK — …"); don't force lower case.
    "subject-case": [0],
    "header-max-length": [2, "always", 100],
  },
};
