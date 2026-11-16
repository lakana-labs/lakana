/**
 * Stubs for the end-to-end tests of an artifact's page: the artifact `prod.ml.model` with a
 * current version of two files and a deleted one, the bytes of each file, the reader's open
 * upload, the same artifact as a deployed project declares it, the tree with the namespace `ml`
 * and the list's row of kind `ARTIFACT`, and the step-up pair a version's delete asks for.
 *
 * @packageDocumentation
 */
import { createHash } from "node:crypto";

import type { Page } from "@playwright/test";

import { mutatingRequests, stubStepUpCeremony } from "./manage";
import { signInAsAdmin, STUBBED, TABLE_DETAIL } from "./session";

/** The path of the namespace `prod.ml`. */
const ML = "/api/v1/environments/prod/namespaces/ml";

/** The table entry of `ml.model`, which the page reads first. */
const MODEL_ENTRY = `${ML}/tables/model`;

/** The artifact `ml.model`. */
const MODEL_PATH = `${ML}/artifacts/model`;

/** The id of `ml.model`. */
const MODEL_UUID = "3e7a9c51-0b2d-4f68-a1c4-7d9e2b5f8a03";

/** Where `ml.model` is defined, which its Overview reads. */
const DEFINITION = `${MODEL_ENTRY}/definition`;

/** The current version of `ml.model`, which Delete… removes. */
export const CURRENT_PATH = `${MODEL_PATH}/versions/2`;

/** The reader's open uploads in `prod`, asked for by the artifact's name. */
const UPLOADS = "/api/v1/environments/prod/artifacts/uploads";

/** The reader's one open upload of `ml.model`. */
const UPLOAD_ID = "6f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f";

/** The lineage route, which the stubs of `session.ts` answer for `fact_sales` only. */
const LINEAGE = "/api/v1/environments/prod/graph/lineage";

/** The list of the Catalog. */
const TABLES = "/api/v1/tables";

/** A file of version 2 with its bytes. */
interface StoredFile {
  readonly path: string;
  readonly bytes: string;
}

/** The files of version 2: the model's weights, deep in a directory, and its configuration. */
const FILES: readonly StoredFile[] = [
  { path: "config.json", bytes: '{"layers": 3}' },
  { path: "models/v2/model.pkl", bytes: "weights of the model" },
];

/**
 * The SHA-256 of a text's UTF-8 bytes, as the manifest names a file.
 *
 * @param text - The text.
 * @returns The hash in lowercase hex.
 */
function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

/** The manifest's entries of version 2. */
const MANIFEST = FILES.map(({ path, bytes }) => ({
  path,
  size: Buffer.byteLength(bytes),
  sha256: sha256(bytes),
}));

/** The path a file's bytes are read from. */
const BLOBS = MANIFEST.map((file) => `${CURRENT_PATH}/blobs/${file.sha256}`);

/** The path of the bytes of `models/v2/model.pkl`. */
export const MODEL_BLOB = BLOBS[1] ?? "";

/** The paths these stubs answer, beside those of `session.ts`. */
export const ARTIFACTS_STUBBED = [
  ...STUBBED,
  MODEL_ENTRY,
  MODEL_PATH,
  `${MODEL_PATH}/versions`,
  CURRENT_PATH,
  ...BLOBS,
  UPLOADS,
  `${MODEL_PATH}/uploads/${UPLOAD_ID}`,
  DEFINITION,
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/** Where a deployed project declares `ml.model`, in the git-owned variant. */
const DEFINED_BY = { repository: "analytics", path: "assets/ml/model.py" };

/**
 * An artifact's table entry, as `GET …/tables/{name}` answers it: no schema, no rows.
 *
 * @param definedBy - Where a project declares it, or null.
 * @returns The entry.
 */
function entry(definedBy: typeof DEFINED_BY | null): Record<string, unknown> {
  return {
    ...TABLE_DETAIL,
    name: "model",
    namespace: "ml",
    uuid: MODEL_UUID,
    kind: "ARTIFACT",
    metadata_location: null,
    schema: [],
    snapshot_id: null,
    model: null,
    row_count: null,
    size_bytes: 33,
    file_count: null,
    description: null,
    description_source: null,
    description_language: null,
    health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
    defined_by: definedBy,
    actions: ["drop"],
  };
}

/**
 * A version of `ml.model`: version 2 is live with two files, version 1 is deleted.
 *
 * @param number - 1 or 2.
 * @param deleted - When it was deleted and by whom, or null.
 * @param withFiles - Whether its files are listed, as `GET …/versions/{n}` lists them.
 * @returns The version, as the server sends it.
 */
function version(
  number: number,
  deleted: { readonly at: string; readonly by: string } | null,
  withFiles = false,
): Record<string, unknown> {
  return {
    version: number,
    manifest_sha256: sha256(`manifest ${String(number)}`),
    size_bytes: number === 2 ? 33 : 12,
    file_count: 2,
    is_directory: true,
    task_run_id: null,
    run_id: null,
    project_version_id: null,
    commit_sha: number === 2 ? "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234" : null,
    created_by: "ana.nowak",
    created_at: number === 2 ? "2026-11-14T06:00:00Z" : "2026-11-01T08:00:00Z",
    deleted_at: deleted?.at ?? null,
    deleted_by: deleted?.by ?? null,
    files: withFiles ? MANIFEST : [],
    deleted_versions: [],
    actions: deleted === null ? ["delete"] : [],
  };
}

/**
 * An artifact, as `GET …/artifacts/{name}` answers it.
 *
 * @param current - Its current version, or null when every version is deleted.
 * @param definedBy - Where a project declares it, or null.
 * @returns The artifact.
 */
function artifact(
  current: number | null,
  definedBy: typeof DEFINED_BY | null,
): Record<string, unknown> {
  return {
    name: "model",
    namespace: "ml",
    uuid: MODEL_UUID,
    kind: "ARTIFACT",
    current_version: current,
    versions: 2,
    size_bytes: current === null ? 0 : 33,
    created_at: "2026-11-01T08:00:00Z",
    defined_by: definedBy,
    keep_versions: definedBy === null ? null : 5,
    actions: ["drop"],
  };
}

/** The list's row of `ml.model`, of kind `ARTIFACT`. */
const MODEL_ROW = {
  environment: "prod",
  namespace: "ml",
  name: "model",
  uuid: MODEL_UUID,
  kind: "ARTIFACT",
  model_kind: null,
  state: "published",
  owner: { name: "ana.nowak", kind: "USER" },
  created_at: "2026-11-01T08:00:00Z",
  row_count: null,
  size_bytes: 33,
  last_write_at: "2026-11-14T06:00:00Z",
  last_read_at: null,
  readers_7d: 0,
  health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
  match: null,
};

/** When and by whom version 2 is deleted, once a test deletes it. */
const DELETED_NOW = { at: "2026-11-16T09:00:00Z", by: "admin" };

/** What an administrator may do on each namespace. */
const PLACE_ACTIONS = ["create_namespace", "create_external_table", "drop"];

/**
 * Answers a GET with JSON and lets any other method through to the routes registered before.
 *
 * @param page - The page.
 * @param path - The path.
 * @param json - The answer, read when the request arrives.
 */
async function answer(page: Page, path: string, json: () => unknown): Promise<void> {
  await page.route(
    (url) => url.pathname === path,
    (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: json() }) : route.fallback(),
  );
}

/**
 * Signs in as an administrator and answers the artifact page's routes like a server would: the
 * tree with `prod.ml`, the list's `ml.model` row, the two artifacts and their entries, the
 * versions, the files and their bytes, the reader's open upload and its withdrawal, a version's
 * delete (which frees one file and keeps the other for the periodic clean-up) and a drop. What a
 * write changes shows in the reads after it.
 *
 * @param page - The page.
 */
export async function stubArtifacts(page: Page): Promise<void> {
  await signInAsAdmin(page);
  const state = { current: 2 as number | null, uploads: [UPLOAD_ID], dropped: false };
  await page.route(
    (url) => url.pathname === "/api/v1/tables/tree",
    (route) => {
      const count = state.dropped ? 0 : 1;
      return route.fulfill({
        json: {
          count: 1 + 4 + count,
          environments: [
            {
              name: "dev",
              count: 1,
              actions: ["create_namespace"],
              namespaces: [{ path: "gold", count: 1, actions: PLACE_ACTIONS }],
            },
            {
              name: "prod",
              count: 4 + count,
              actions: ["create_namespace"],
              namespaces: [
                { path: "gold", count: 3, actions: PLACE_ACTIONS },
                { path: "gold.sales", count: 1, actions: PLACE_ACTIONS },
                { path: "bronze", count: 0, actions: PLACE_ACTIONS },
                { path: "ml", count, actions: PLACE_ACTIONS },
              ],
            },
          ],
        },
      });
    },
  );
  await page.route(
    (url) => url.pathname === TABLES,
    async (route) => {
      if (new URL(route.request().url()).searchParams.get("namespace") !== "ml") {
        await route.fallback();
        return;
      }
      await route.fulfill({
        json: { items: state.dropped ? [] : [MODEL_ROW], next_cursor: null, capped: false },
      });
    },
  );
  await answer(page, MODEL_ENTRY, () => entry(null));
  await answer(page, DEFINITION, () => ({
    defined: false,
    repository: null,
    path: null,
    commit: null,
    line: null,
    project_version_id: null,
    deployed_at: null,
    browse_url: null,
  }));
  await answer(page, `${MODEL_PATH}/versions`, () => [
    version(2, state.current === null ? DELETED_NOW : null),
    version(1, { at: "2026-11-10T09:00:00Z", by: "kim.schulz" }),
  ]);
  await answer(page, CURRENT_PATH, () => version(2, null, true));
  for (const [index, path] of BLOBS.entries()) {
    await page.route(
      (url) => url.pathname === path,
      (route) =>
        route.fulfill({
          contentType: "application/octet-stream",
          body: Buffer.from(FILES[index]?.bytes ?? ""),
        }),
    );
  }
  await page.route(
    (url) => url.pathname === UPLOADS,
    (route) =>
      route.fulfill({
        json: {
          items:
            new URL(route.request().url()).searchParams.get("name") === "ml.model"
              ? state.uploads.map((id) => ({
                  upload_id: id,
                  artifact: "ml.model",
                  created_at: "2026-11-15T10:00:00Z",
                  expires_at: "2026-11-16T10:00:00Z",
                }))
              : [],
          next_cursor: null,
        },
      }),
  );
  await page.route(
    (url) => url.pathname === `${MODEL_PATH}/uploads/${UPLOAD_ID}`,
    (route) => {
      state.uploads = [];
      return route.fulfill({ status: 204 });
    },
  );
  await page.route(
    (url) => url.pathname === CURRENT_PATH,
    (route) => {
      if (route.request().method() !== "DELETE") {
        return route.fallback();
      }
      state.current = null;
      return route.fulfill({
        json: {
          ...version(2, DELETED_NOW),
          freed_files: 1,
          freed_bytes: 20,
          kept_files: 1,
          kept_bytes: 13,
          kept: [{ reason: "left_to_sweep", files: 1, bytes: 13, version: null }],
        },
      });
    },
  );
  await page.route(
    (url) => url.pathname === MODEL_PATH,
    (route) => {
      if (route.request().method() === "DELETE") {
        state.dropped = true;
        return route.fulfill({ status: 204 });
      }
      return route.fulfill({ json: artifact(state.current, null) });
    },
  );
  await page.route(
    (url) => url.pathname === LINEAGE,
    (route) => {
      const node = new URL(route.request().url()).searchParams.get("node") ?? "";
      return node.endsWith(MODEL_UUID)
        ? route.fulfill({
            json: {
              nodes: [{ id: node, kind: "artifact", name: "ml.model", distance: 0 }],
              edges: [],
              next_cursor: null,
            },
          })
        : route.fallback();
    },
  );
}

/**
 * Makes the delete of version 2 ask for a passkey touch once, answers the touch, and then lets
 * the delete through to {@link stubArtifacts}. Called after it.
 *
 * @param page - The page.
 * @returns The mutating requests that reached the stubs, in order, as `METHOD path`.
 */
export async function stubStepUpDeleteVersion(page: Page): Promise<string[]> {
  const sent = mutatingRequests(page, CURRENT_PATH);
  const touched = await stubStepUpCeremony(page);
  await page.route(
    (url) => url.pathname === CURRENT_PATH,
    (route) =>
      route.request().method() !== "DELETE" || touched()
        ? route.fallback()
        : route.fulfill({
            status: 403,
            json: { type: "StepUpRequired", detail: "Confirm with your passkey." },
          }),
  );
  return sent;
}

/**
 * Makes `ml.model` an artifact a deployed project declares: Drop is shown but disabled with the
 * file that declares it, and Delete… on its versions stays. Called after {@link stubArtifacts}.
 *
 * @param page - The page.
 */
export async function stubGitOwnedArtifact(page: Page): Promise<void> {
  await answer(page, MODEL_ENTRY, () => entry(DEFINED_BY));
  await answer(page, MODEL_PATH, () => artifact(2, DEFINED_BY));
  await answer(page, DEFINITION, () => ({
    defined: true,
    repository: DEFINED_BY.repository,
    path: DEFINED_BY.path,
    commit: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    line: 1,
    project_version_id: "7f3e2d1c-0b9a-4876-9543-210fedcba987",
    deployed_at: "2026-11-12T05:50:00Z",
    browse_url: null,
  }));
}
