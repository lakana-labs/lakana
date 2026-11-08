// The lint rules of the interface (docs/agent/conventions.md, "The ui/ project").
import js from "@eslint/js";
import jsdoc from "eslint-plugin-jsdoc";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

const SERVER_ONLY_THROUGH_API =
  "Only ui/src/api/ talks to the server (the REST API is the only way in)";

// The TSDoc tags jsdoc does not know by itself.
const TSDOC_TAGS = [
  "alpha",
  "beta",
  "decorator",
  "defaultValue",
  "eventProperty",
  "experimental",
  "label",
  "packageDocumentation",
  "privateRemarks",
  "remarks",
  "sealed",
  "typeParam",
  "virtual",
];

const requireJsdoc = {
  "jsdoc/require-jsdoc": [
    "error",
    {
      publicOnly: true,
      require: {
        FunctionDeclaration: true,
        ArrowFunctionExpression: true,
        ClassDeclaration: true,
        MethodDefinition: true,
      },
      contexts: [
        "TSInterfaceDeclaration",
        "TSTypeAliasDeclaration",
        "TSPropertySignature",
        "TSEnumDeclaration",
      ],
    },
  ],
  "jsdoc/check-tag-names": ["error", { definedTags: TSDOC_TAGS }],
  // TSDoc style: a blank line between the summary and the tags, none between tags.
  "jsdoc/tag-lines": ["error", "never", { startLines: 1 }],
};

export default defineConfig(
  { ignores: ["coverage/", "test-results/", "playwright-report/", "node_modules/"] },
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: { allowDefaultProject: ["eslint.config.js"] },
        tsconfigRootDir: import.meta.dirname,
      },
      globals: { ...globals.browser, ...globals.node },
    },
  },
  reactHooks.configs.flat.recommended,
  jsxA11y.flatConfigs.strict,
  // TSDoc on every export and every props member; JavaScript files carry their types in JSDoc.
  {
    ...jsdoc.configs["flat/recommended-typescript-error"],
    files: ["**/*.{ts,tsx}"],
    rules: { ...jsdoc.configs["flat/recommended-typescript-error"].rules, ...requireJsdoc },
  },
  {
    ...jsdoc.configs["flat/recommended-typescript-flavor-error"],
    files: ["**/*.{js,mjs}"],
    rules: { ...jsdoc.configs["flat/recommended-typescript-flavor-error"].rules, ...requireJsdoc },
  },
  {
    rules: {
      "no-restricted-globals": [
        "error",
        ...["fetch", "XMLHttpRequest", "WebSocket", "EventSource"].map((name) => ({
          name,
          message: SERVER_ONLY_THROUGH_API,
        })),
      ],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "fetch", message: SERVER_ONLY_THROUGH_API },
        { object: "globalThis", property: "fetch", message: SERVER_ONLY_THROUGH_API },
        { object: "navigator", property: "sendBeacon", message: SERVER_ONLY_THROUGH_API },
      ],
      "no-restricted-imports": [
        "error",
        {
          paths: ["axios", "ky", "ofetch", "superagent"].map((name) => ({
            name,
            message: `${SERVER_ONLY_THROUGH_API}; it uses fetch, not ${name}.`,
          })),
        },
      ],
    },
  },
  {
    files: ["src/api/**"],
    rules: { "no-restricted-globals": "off", "no-restricted-properties": "off" },
  },
  {
    // eslint-plugin-jsx-a11y ships no types, so its config is `any` in this one file.
    files: ["eslint.config.js"],
    rules: {
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
    },
  },
);
