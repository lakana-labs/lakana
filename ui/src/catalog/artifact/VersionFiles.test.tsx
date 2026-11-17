import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BROWSER_DOWNLOAD_LIMIT, type ArtifactFileOut, type TableRef } from "../../api";
import { api, DownloadCut, DownloadTooLong } from "../../api/client";
import { artifactRoutes, version, WEIGHTS } from "../../test/artifacts";
import { fakeServer } from "../../test/server";
import { saveBlob } from "../../shell/saveFile";
import { VersionFiles } from "./VersionFiles";

vi.mock("../../shell/saveFile", async (original) => ({
  ...(await original<typeof import("../../shell/saveFile")>()),
  saveBlob: vi.fn(),
}));

/**
 * One item of a list, which the test expects to be there.
 *
 * @param list - The list.
 * @param index - The item's place.
 * @returns The item.
 * @throws {Error} when the list is shorter.
 */
function at<T>(list: readonly T[], index: number): T {
  const item = list[index];
  if (item === undefined) {
    throw new Error(`The list has no item ${String(index)}.`);
  }
  return item;
}

const PLACE: TableRef = { environment: "prod", namespace: "ml", name: "model" };
const BASE = "/api/v1/environments/prod/namespaces/ml/artifacts/model";
const BLOB = `GET ${BASE}/versions/3/blobs/${WEIGHTS.sha256}`;
const MISMATCH =
  "models/model.pkl did not arrive whole, so nothing was saved. Download it again, or use the command above.";

afterEach(() => {
  vi.mocked(saveBlob).mockReset();
  vi.restoreAllMocks();
});

/**
 * Renders the files of version 3 of `prod/ml/model`.
 *
 * @returns The handler called when the version is gone, and a user-event session.
 */
function renderFiles() {
  const onGone = vi.fn();
  const user = userEvent.setup();
  render(<VersionFiles place={PLACE} version={3} id="files-3" onGone={onGone} />);
  return { onGone, user };
}

/**
 * The version 3 route with these files.
 *
 * @param files - The files.
 * @returns The route, keyed.
 */
function withFiles(files: readonly ArtifactFileOut[]) {
  return { [`GET ${BASE}/versions/3`]: { status: 200, body: version({ files }) } };
}

/**
 * The files list's rows below its head.
 *
 * @returns The rows.
 */
async function rows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Files of version 3" });
  return within(region).getAllByRole("row").slice(1);
}

describe("VersionFiles", () => {
  it("lists each file with its size and a Download named after its path", async () => {
    fakeServer(artifactRoutes());
    renderFiles();

    const listed = await rows();
    const config = at(listed, 0);
    const weights = at(listed, 1);
    expect(within(config).getByText("config.json")).toBeVisible();
    expect(within(config).getByText("6 B")).toHaveClass("number");
    expect(within(weights).getByText("7 B")).toBeVisible();
    expect(
      within(weights).getByRole("button", { name: "Download models/model.pkl" }),
    ).toHaveTextContent("Download");
    expect(document.getElementById("files-3")).toContainElement(weights);
  });

  it("shows the whole-version command, quoted", async () => {
    fakeServer(artifactRoutes());
    renderFiles();

    expect(await screen.findByText("Download the whole version with:")).toBeVisible();
    expect(
      screen.getByText("lakana artifact download 'ml.model' './model-v3' --version 3 --env 'prod'"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeVisible();
  });

  it("quotes a quote in the names of the command", async () => {
    const place = { environment: "it's", namespace: "ml", name: "o'brien" };
    fakeServer({
      [`GET /api/v1/environments/it's/namespaces/ml/artifacts/o'brien/versions/3`]: {
        status: 200,
        body: version({ files: [WEIGHTS] }),
      },
    });
    render(<VersionFiles place={place} version={3} id="files-3" onGone={vi.fn()} />);

    expect(
      await screen.findByText(
        "lakana artifact download 'ml.o'\\''brien' './o'\\''brien-v3' --version 3 --env 'it'\\''s'",
      ),
    ).toBeVisible();
  });

  it("downloads, checks and saves the file", async () => {
    const server = fakeServer(artifactRoutes());
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.route({
      [BLOB]: async () => {
        await gate;
        return { status: 200, blob: new Blob(["weights"]) };
      },
    });
    const { user } = renderFiles();

    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    const pending = screen.getByRole("button", { name: /^Downloading models\/model\.pkl…/ });
    expect(pending).toHaveTextContent("Downloading…");
    expect(pending).toHaveAttribute("data-pending");
    expect(saveBlob).not.toHaveBeenCalled();
    release();

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    const [blob, name] = at(vi.mocked(saveBlob).mock.calls, 0);
    expect(name).toBe("model.pkl");
    expect(await blob.text()).toBe("weights");
    expect(screen.getByRole("button", { name: "Download models/model.pkl" })).toHaveFocus();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("saves nothing and says so when the file arrives shorter or with another hash", async () => {
    const server = fakeServer(artifactRoutes());
    const { user } = renderFiles();
    const button = await screen.findByRole("button", { name: "Download models/model.pkl" });

    server.route({ [BLOB]: { status: 200, blob: new Blob(["weight"]) } });
    await user.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(MISMATCH);
    expect(saveBlob).not.toHaveBeenCalled();

    server.route({ [BLOB]: { status: 200, blob: new Blob(["weightz"]) } });
    await user.click(screen.getByRole("button", { name: "Download models/model.pkl" }));

    await waitFor(() => {
      expect(server.sent.filter((request) => request.path.endsWith(WEIGHTS.sha256))).toHaveLength(
        2,
      );
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(MISMATCH);
    expect(saveBlob).not.toHaveBeenCalled();

    server.route({ [BLOB]: { status: 200, blob: new Blob(["weights"]) } });
    await user.click(screen.getByRole("button", { name: "Download models/model.pkl" }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["the connection breaks in the middle", new DownloadCut()],
    ["the body is longer than the manifest", new DownloadTooLong()],
  ])("says the file did not arrive whole when %s", async (_, error) => {
    fakeServer(artifactRoutes());
    vi.spyOn(api, "download").mockRejectedValue(error);
    const { user } = renderFiles();

    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(MISMATCH);
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("shows the real cause of an error that is not a broken body", async () => {
    fakeServer(artifactRoutes());
    vi.spyOn(api, "download").mockRejectedValue(new TypeError("a bug of ours"));
    const { user } = renderFiles();

    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("did not arrive whole");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("offers no Download over 256 MiB and points to the command", async () => {
    fakeServer({
      ...artifactRoutes(),
      ...withFiles([{ ...WEIGHTS, size: BROWSER_DOWNLOAD_LIMIT + 1 }]),
    });
    renderFiles();

    const listed = await rows();
    const big = at(listed, 0);
    expect(within(big).queryByRole("button")).toBeNull();
    expect(big).toHaveTextContent(
      "268.4 MB: larger than the browser downloads (256 MiB). Use the command above.",
    );
  });

  it("offers Download at exactly 256 MiB", async () => {
    fakeServer({
      ...artifactRoutes(),
      ...withFiles([{ ...WEIGHTS, size: BROWSER_DOWNLOAD_LIMIT }]),
    });
    renderFiles();

    const listed = await rows();
    const limit = at(listed, 0);
    expect(within(limit).getByRole("button", { name: "Download models/model.pkl" })).toBeVisible();
  });

  it("shows the server's sentence when the version is gone and reads the versions again", async () => {
    const GONE =
      "Version 3 of ml.model was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list ml.model --env prod` shows which.";
    fakeServer({ ...artifactRoutes(), [BLOB]: { status: 404, body: { detail: GONE } } });
    const { onGone, user } = renderFiles();

    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(GONE);
    // onGone runs in an effect after the render that shows the alert, so wait for it.
    await waitFor(() => {
      expect(onGone).toHaveBeenCalledExactlyOnceWith(GONE);
    });
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("shows the server's sentence for another refusal and stays", async () => {
    const BUSY = "The server is busy with other downloads; try again in a moment.";
    fakeServer({ ...artifactRoutes(), [BLOB]: { status: 503, body: { detail: BUSY } } });
    const { onGone, user } = renderFiles();

    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(BUSY);
    expect(onGone).not.toHaveBeenCalled();
  });

  it("names the version gone when its files cannot be found", async () => {
    const GONE = "ml.model has no version 3.";
    fakeServer({
      ...artifactRoutes(),
      [`GET ${BASE}/versions/3`]: { status: 404, body: { detail: GONE } },
    });
    const { onGone } = renderFiles();

    expect(await screen.findByRole("alert")).toHaveTextContent(GONE);
    // onGone runs in an effect after the render that shows the alert, so wait for it.
    await waitFor(() => {
      expect(onGone).toHaveBeenCalledExactlyOnceWith(GONE);
    });
  });

  it("shows ReadFailed with Try again when the files cannot be read", async () => {
    const server = fakeServer({
      ...artifactRoutes(),
      [`GET ${BASE}/versions/3`]: { status: 500, body: { detail: "The catalog database broke." } },
    });
    const { onGone, user } = renderFiles();

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog database broke.");
    expect(onGone).not.toHaveBeenCalled();
    server.route(artifactRoutes());
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await rows()).toHaveLength(2);
  });

  it("marks a bidi character in a file path", async () => {
    fakeServer({
      ...artifactRoutes(),
      ...withFiles([{ ...WEIGHTS, path: "models/evil\u202Egnp.exe" }]),
    });
    renderFiles();

    const listed = await rows();
    const evil = at(listed, 0);
    expect(evil).toHaveTextContent("models/evilU+202Egnp.exe");
    expect(evil.textContent).not.toContain("\u202E");
    expect(
      within(evil).getByRole("button", { name: "Download models/evilU+202Egnp.exe" }),
    ).toBeVisible();
  });

  it("shows 100 files, then 100 more with the focus on the first new one", async () => {
    const files = Array.from({ length: 250 }, (_unused, index) => ({
      ...WEIGHTS,
      path: `part-${String(index).padStart(3, "0")}.bin`,
    }));
    fakeServer({ ...artifactRoutes(), ...withFiles(files) });
    const { user } = renderFiles();

    expect(await rows()).toHaveLength(100);
    await user.click(screen.getByRole("button", { name: "Show 100 more" }));

    expect(await rows()).toHaveLength(200);
    expect(document.activeElement).toHaveTextContent("part-100.bin");
    expect(document.activeElement).toHaveClass("file-path");

    await user.click(screen.getByRole("button", { name: "Show 50 more" }));

    expect(await rows()).toHaveLength(250);
    expect(document.activeElement).toHaveTextContent("part-200.bin");
    expect(screen.queryByRole("button", { name: /^Show \d+ more$/ })).toBeNull();
  });
});
