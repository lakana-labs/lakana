/**
 * What the server answers the artifact page with, for tests: `prod/ml/model`, an artifact of three
 * versions (the newest has two files, one of them `models/model.pkl`, and the oldest is deleted),
 * one open upload of the reader's, and the tree with the namespace `ml`.
 *
 * @packageDocumentation
 */
import type {
  ArtifactFileOut,
  ArtifactKeptOut,
  ArtifactOut,
  ArtifactUploadOut,
  ArtifactVersionDeletedOut,
  ArtifactVersionOut,
  TableTreeOut,
} from "../api";

import { ALL_ACTIONS, TREE } from "./catalog";
import type { FakeRoute } from "./server";
import { table } from "./table";

/** The artifact's path on the server. */
const ARTIFACT_PATH = "/api/v1/environments/prod/namespaces/ml/artifacts/model";

/** The file `models/model.pkl` of version 3: the bytes `"weights"`. */
export const WEIGHTS: ArtifactFileOut = {
  path: "models/model.pkl",
  size: 7,
  sha256: "9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c",
};

/** The file `config.json` of version 3: the bytes `"config"`. */
const CONFIG: ArtifactFileOut = {
  path: "config.json",
  size: 6,
  sha256: "b79606fb3afea5bd1609ed40b622142f1c98125abcfe89a76a661b0e8e343910",
};

/**
 * The artifact `prod/ml/model`, as the server answers it: three versions, the newest current,
 * declared by no project, and the reader may drop it.
 *
 * @param overrides - The fields that differ.
 * @returns The artifact.
 */
export function artifact(overrides: Partial<ArtifactOut> = {}): ArtifactOut {
  return {
    name: "model",
    namespace: "ml",
    uuid: "3e7a9c51-0b2d-4f68-a1c4-7d9e2b5f8a03",
    kind: "ARTIFACT",
    current_version: 3,
    versions: 3,
    size_bytes: 26,
    created_at: "2026-11-01T08:00:00Z",
    defined_by: null,
    keep_versions: null,
    actions: ["drop"],
    ...overrides,
  };
}

/**
 * A live version, as a listing shows it: without its files, and the reader may delete it.
 *
 * @param overrides - The fields that differ.
 * @returns The version.
 */
export function version(overrides: Partial<ArtifactVersionOut> = {}): ArtifactVersionOut {
  return {
    version: 3,
    manifest_sha256: "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00",
    size_bytes: 13,
    file_count: 2,
    is_directory: true,
    task_run_id: "7b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
    run_id: "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    project_version_id: "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
    commit_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    created_by: "ana.nowak",
    created_at: "2026-11-14T06:00:00Z",
    deleted_at: null,
    deleted_by: null,
    files: [],
    deleted_versions: [],
    actions: ["delete"],
    ...overrides,
  };
}

/**
 * A deleted version: it stays in the list without files or actions.
 *
 * @param overrides - The fields that differ.
 * @returns The version.
 */
export function deletedVersion(overrides: Partial<ArtifactVersionOut> = {}): ArtifactVersionOut {
  return version({
    version: 1,
    created_at: "2026-11-01T08:00:00Z",
    deleted_at: "2026-11-10T09:00:00Z",
    deleted_by: "kim.schulz",
    actions: [],
    ...overrides,
  });
}

/**
 * One open upload of the reader's.
 *
 * @param overrides - The fields that differ.
 * @returns The upload.
 */
export function upload(overrides: Partial<ArtifactUploadOut> = {}): ArtifactUploadOut {
  return {
    upload_id: "6f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f",
    artifact: "ml.model",
    created_at: "2026-11-15T10:00:00Z",
    expires_at: "2026-11-16T10:00:00Z",
    ...overrides,
  };
}

/** The file of version 3 that stays in storage, because version 2 has it too. */
const KEPT: ArtifactKeptOut = { reason: "other_version", files: 1, bytes: 7, version: 2 };

/** What deleting version 3 answers: one file freed, the other kept because version 2 has it. */
export const DELETED: ArtifactVersionDeletedOut = {
  ...version({
    deleted_at: "2026-11-16T09:00:00Z",
    deleted_by: "admin",
    actions: [],
  }),
  freed_files: 1,
  freed_bytes: 6,
  kept_files: 1,
  kept_bytes: 7,
  kept: [KEPT],
};

/** The tree of `test/catalog.ts` with the namespace `ml` in `prod`, which holds the artifact. */
const ARTIFACT_TREE: TableTreeOut = {
  ...TREE,
  count: TREE.count + 1,
  environments: TREE.environments.map((environment) =>
    environment.name === "prod"
      ? {
          ...environment,
          count: environment.count + 1,
          namespaces: [...environment.namespaces, { path: "ml", count: 1, actions: ALL_ACTIONS }],
        }
      : environment,
  ),
};

/**
 * The routes of the artifact page: the table entry of kind `ARTIFACT`, the artifact, its
 * versions, version 3 with its files, the reader's open uploads (two pages), the bytes of each
 * file, deleting a version, withdrawing an upload and the tree. The artifact's drop is
 * `manageRoutes()`'s.
 *
 * @returns The routes.
 */
export function artifactRoutes(): Record<string, FakeRoute> {
  return {
    "GET /api/v1/tables/tree": { status: 200, body: ARTIFACT_TREE },
    "GET /api/v1/environments/prod/namespaces/ml/tables/model": {
      status: 200,
      body: table({
        name: "model",
        namespace: "ml",
        uuid: "3e7a9c51-0b2d-4f68-a1c4-7d9e2b5f8a03",
        kind: "ARTIFACT",
        metadata_location: null,
        schema: [],
        snapshot_id: null,
        model: null,
        row_count: null,
        size_bytes: 26,
        file_count: null,
        actions: ["drop"],
        description: null,
        description_source: null,
        health_summary: null,
      }),
    },
    [`GET ${ARTIFACT_PATH}`]: { status: 200, body: artifact() },
    [`GET ${ARTIFACT_PATH}/versions`]: {
      status: 200,
      body: [
        version(),
        version({ version: 2, created_at: "2026-11-07T06:00:00Z" }),
        deletedVersion(),
      ],
    },
    [`GET ${ARTIFACT_PATH}/versions/3`]: {
      status: 200,
      body: version({ files: [CONFIG, WEIGHTS] }),
    },
    [`GET ${ARTIFACT_PATH}/versions/3/blobs/${WEIGHTS.sha256}`]: {
      status: 200,
      blob: new Blob(["weights"]),
    },
    [`GET ${ARTIFACT_PATH}/versions/3/blobs/${CONFIG.sha256}`]: {
      status: 200,
      blob: new Blob(["config"]),
    },
    [`DELETE ${ARTIFACT_PATH}/versions/3`]: { status: 200, body: DELETED },
    [`DELETE ${ARTIFACT_PATH}/uploads/${upload().upload_id}`]: { status: 204 },
    "GET /api/v1/environments/prod/artifacts/uploads": (request) => ({
      status: 200,
      body: request.path.includes("cursor=")
        ? {
            items: [upload({ upload_id: "0d9c8b7a-6f5e-4d3c-8b2a-190817161514" })],
            next_cursor: null,
          }
        : { items: [upload()], next_cursor: "c1" },
    }),
  };
}
