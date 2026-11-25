import { describe, expect, it, vi } from "vitest";

import {
  BROWSER_DOWNLOAD_LIMIT,
  DownloadMismatch,
  deleteVersion,
  downloadFile,
  getArtifact,
  getVersion,
  listUploads,
  listVersions,
  withdrawUpload,
} from "./artifacts";
import { api } from "./client";

import {
  DELETED,
  WEIGHTS,
  artifact,
  artifactRoutes,
  deletedVersion,
  upload,
  version,
} from "../test/artifacts";
import { fakeServer, withoutSessionReads } from "../test/server";

const REF = { environment: "prod", namespace: "ml", name: "model" };
const BASE = "/api/v1/environments/prod/namespaces/ml/artifacts/model";

describe("artifacts api", () => {
  it("reads an artifact, its versions and one version with its files", async () => {
    const server = fakeServer(artifactRoutes());

    expect(await getArtifact(REF)).toEqual(artifact());
    const versions = await listVersions(REF);
    const three = await getVersion(REF, 3);

    expect(versions.map((each) => each.version)).toEqual([3, 2, 1]);
    expect(versions[2]).toEqual(deletedVersion());
    expect(three).toEqual(version({ files: three.files }));
    expect(three.files.map((file) => file.path)).toContain("models/model.pkl");
    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      ["GET", BASE, undefined],
      ["GET", `${BASE}/versions`, undefined],
      ["GET", `${BASE}/versions/3`, undefined],
    ]);
  });

  it("deletes a version, and sends break_glass_reason only when set", async () => {
    const server = fakeServer(artifactRoutes());

    expect(await deleteVersion(REF, 3, null)).toEqual(DELETED);
    await deleteVersion(REF, 3, "fix it now please");

    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      ["DELETE", `${BASE}/versions/3`, undefined],
      ["DELETE", `${BASE}/versions/3?break_glass_reason=fix+it+now+please`, undefined],
    ]);
  });

  it("lists your uploads of one artifact with the cursor from the body", async () => {
    const server = fakeServer(artifactRoutes());

    const first = await listUploads("prod", "ml.model", null);
    const second = await listUploads("prod", "ml.model", first.nextCursor);

    expect(first).toEqual({ items: [upload()], nextCursor: "c1" });
    expect(second.nextCursor).toBeNull();
    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([
      ["GET", "/api/v1/environments/prod/artifacts/uploads?name=ml.model&limit=100", undefined],
      [
        "GET",
        "/api/v1/environments/prod/artifacts/uploads?name=ml.model&limit=100&cursor=c1",
        undefined,
      ],
    ]);
  });

  it("withdraws an upload", async () => {
    const server = fakeServer(artifactRoutes());

    await expect(withdrawUpload(REF, upload().upload_id)).resolves.toBeUndefined();

    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([["DELETE", `${BASE}/uploads/6f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f`, undefined]]);
  });

  it("downloads a file and checks its size and hash", async () => {
    const server = fakeServer(artifactRoutes());

    const blob = await downloadFile(REF, 3, WEIGHTS);

    expect(await blob.text()).toBe("weights");
    expect(
      withoutSessionReads(server).map((request) => [request.method, request.path, request.body]),
    ).toEqual([["GET", `${BASE}/versions/3/blobs/${WEIGHTS.sha256}`, undefined]]);
    expect(withoutSessionReads(server)[0]?.headers.accept).toBe("application/octet-stream");
    expect(BROWSER_DOWNLOAD_LIMIT).toBe(268_435_456);
  });

  it("throws DownloadMismatch for another size and for another hash", async () => {
    const server = fakeServer(artifactRoutes());
    const route = `GET ${BASE}/versions/3/blobs/${WEIGHTS.sha256}`;

    server.route({ [route]: { status: 200, blob: new Blob(["weight"]) } });
    const shorter = downloadFile(REF, 3, WEIGHTS);
    await expect(shorter).rejects.toBeInstanceOf(DownloadMismatch);
    await expect(shorter).rejects.toThrow("models/model.pkl");

    server.route({ [route]: { status: 200, blob: new Blob(["weightz"]) } });
    await expect(downloadFile(REF, 3, WEIGHTS)).rejects.toBeInstanceOf(DownloadMismatch);
  });

  it("reads no more of a body than the manifest's size, and refuses a longer one", async () => {
    const server = fakeServer(artifactRoutes());
    const route = `GET ${BASE}/versions/3/blobs/${WEIGHTS.sha256}`;
    const read = vi.spyOn(api, "download");

    server.route({ [route]: { status: 200, blob: new Blob(["weights and more"]) } });
    const longer = downloadFile(REF, 3, WEIGHTS);
    await expect(longer).rejects.toBeInstanceOf(DownloadMismatch);
    await expect(longer).rejects.toThrow("models/model.pkl");

    expect(read).toHaveBeenCalledWith(`${BASE}/versions/3/blobs/${WEIGHTS.sha256}`, WEIGHTS.size);
  });

  it("refuses a file larger than the browser downloads without asking the server", async () => {
    const server = fakeServer(artifactRoutes());

    await expect(
      downloadFile(REF, 3, { ...WEIGHTS, size: BROWSER_DOWNLOAD_LIMIT + 1 }),
    ).rejects.toBeInstanceOf(RangeError);

    expect(withoutSessionReads(server)).toEqual([]);
  });

  it("encodes every part of a path", async () => {
    const server = fakeServer({});
    const ref = { environment: "pro d", namespace: "a b", name: "m/x" };

    await Promise.allSettled([
      getArtifact(ref),
      getVersion(ref, 3),
      withdrawUpload(ref, "u/1"),
      listUploads("pro d", "a b.m/x", null),
    ]);

    expect(withoutSessionReads(server).map((request) => request.path)).toEqual([
      "/api/v1/environments/pro%20d/namespaces/a%20b/artifacts/m%2Fx",
      "/api/v1/environments/pro%20d/namespaces/a%20b/artifacts/m%2Fx/versions/3",
      "/api/v1/environments/pro%20d/namespaces/a%20b/artifacts/m%2Fx/uploads/u%2F1",
      "/api/v1/environments/pro%20d/artifacts/uploads?name=a+b.m%2Fx&limit=100",
    ]);
  });
});
