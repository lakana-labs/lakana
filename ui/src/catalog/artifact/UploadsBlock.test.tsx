import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { TableRef } from "../../api";
import { artifactRoutes, upload } from "../../test/artifacts";
import { fakeServer, type FakeServer } from "../../test/server";
import { UploadsBlock } from "./UploadsBlock";

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
const UPLOADS = "GET /api/v1/environments/prod/artifacts/uploads";
const FIRST = upload().upload_id;
const SECOND = "0d9c8b7a-6f5e-4d3c-8b2a-190817161514";
const WITHDRAW = `DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/uploads/${FIRST}`;
const GONE = "This upload has expired or does not exist; start the upload again.";

/**
 * Renders the reader's open uploads of `prod/ml/model`.
 *
 * @returns A user-event session and the rendered container.
 */
function renderUploads() {
  const user = userEvent.setup();
  const { container } = render(<UploadsBlock place={PLACE} />);
  return { user, container };
}

/**
 * The list's rows below its head.
 *
 * @returns The rows.
 */
async function rows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Your open uploads of ml.model" });
  return within(region).getAllByRole("row").slice(1);
}

/**
 * Waits until the uploads were read this many times and every answer has arrived.
 *
 * @param server - The fake server.
 * @param times - How many reads.
 */
async function readTimes(server: FakeServer, times: number): Promise<void> {
  await waitFor(() => {
    expect(
      server.sent.filter((request) => request.path.includes("/artifacts/uploads")),
    ).toHaveLength(times);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("UploadsBlock", () => {
  it("shows nothing without an open upload, and nothing on 403", async () => {
    const server = fakeServer({
      ...artifactRoutes(),
      [UPLOADS]: { status: 200, body: { items: [], next_cursor: null } },
    });
    const { container } = renderUploads();

    await readTimes(server, 1);
    expect(container).toBeEmptyDOMElement();
    expect(server.sent.at(-1)?.path).toBe(
      "/api/v1/environments/prod/artifacts/uploads?name=ml.model&limit=100",
    );

    server.route({
      [UPLOADS]: {
        status: 403,
        body: { type: "PermissionDeniedError", detail: "You may not read prod." },
      },
    });
    const denied = renderUploads();
    await readTimes(server, 2);
    expect(denied.container).toBeEmptyDOMElement();
  });

  it("lists each upload with its id, when it began and when it expires", async () => {
    fakeServer(artifactRoutes());
    renderUploads();

    expect(
      await screen.findByRole("heading", { level: 2, name: "Your open uploads" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Uploads you began and did not finish. Each holds one of the artifact's upload places until you withdraw it or it expires. Files it already stored stay until Lakana's periodic clean-up finds that nothing needs them.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Upload" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Began" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Expires" })).toBeVisible();
    const row = at(await rows(), 0);
    expect(row).toHaveTextContent(FIRST);
    expect(row.querySelector(".upload-id")).toHaveTextContent(FIRST);
    const times = [...row.querySelectorAll("time")].map((time) => time.getAttribute("datetime"));
    expect(times).toEqual(["2026-11-15T10:00:00Z", "2099-11-16T10:00:00Z"]);
    // A time ahead is worded as a date, never as "just now".
    expect(within(row).getByText(/2099/, { selector: "time" })).toBeVisible();
    expect(within(row).queryByText("just now", { selector: "time" })).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: `Withdraw upload ${FIRST}` })).toBeVisible();
  });

  it("pages with Show more and the body's cursor", async () => {
    const server = fakeServer(artifactRoutes());
    const { user } = renderUploads();

    expect(await rows()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(async () => {
      expect(await rows()).toHaveLength(2);
    });
    expect(server.sent.at(-1)?.path).toContain("cursor=c1");
    expect(at(await rows(), 1)).toHaveTextContent(SECOND);
    expect(screen.getByRole("button", { name: `Withdraw upload ${SECOND}` })).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("says why when a later page is refused with 403, keeps the rows and offers Show more again", async () => {
    const detail = "You may no longer read prod.";
    const server = fakeServer({
      ...artifactRoutes(),
      [UPLOADS]: (request) =>
        request.path.includes("cursor=")
          ? { status: 403, body: { type: "PermissionDeniedError", detail } }
          : { status: 200, body: { items: [upload()], next_cursor: "c1" } },
    });
    const { user } = renderUploads();

    expect(await rows()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Show more" }));

    expect(await screen.findByText(detail)).toBeVisible();
    expect(server.sent.at(-1)?.path).toContain("cursor=c1");
    expect(await rows()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Show more" })).toBeVisible();
  });

  it("withdraws at once without asking, removes the row and focuses Withdrew upload {id}", async () => {
    const server = fakeServer(artifactRoutes());
    const { user } = renderUploads();

    await user.click(await screen.findByRole("button", { name: `Withdraw upload ${FIRST}` }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(`Withdrew upload ${FIRST}.`);
    await waitFor(() => {
      expect(status).toHaveFocus();
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(FIRST, { selector: ".upload-id" })).toBeNull();
    expect(server.sent.filter((request) => request.method === "DELETE")).toEqual([
      expect.objectContaining({ path: WITHDRAW.slice("DELETE ".length) }),
    ]);
  });

  it("shows the server's 409 for an expired upload word for word and reads the list again", async () => {
    const server = fakeServer({
      ...artifactRoutes(),
      [WITHDRAW]: { status: 409, body: { type: "ConflictError", detail: GONE } },
    });
    const { user } = renderUploads();

    const button = await screen.findByRole("button", { name: `Withdraw upload ${FIRST}` });
    server.route({ [UPLOADS]: { status: 200, body: { items: [], next_cursor: null } } });
    await user.click(button);

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent(GONE);
    expect(refusal.textContent).toBe(GONE);
    await readTimes(server, 2);
    expect(screen.queryByRole("region", { name: "Your open uploads of ml.model" })).toBeNull();
    expect(screen.getByText("You have no open uploads of ml.model.")).toBeVisible();
    expect(refusal).toBeInTheDocument();
    expect(refusal).toHaveFocus();
  });

  it("shows ReadFailed with Try again on another failure", async () => {
    const server = fakeServer({
      ...artifactRoutes(),
      [UPLOADS]: { status: 500, body: { detail: "The catalog database broke." } },
    });
    const { user } = renderUploads();

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog database broke.");
    expect(screen.getByRole("heading", { level: 2, name: "Your open uploads" })).toBeVisible();
    server.route(artifactRoutes());
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await rows()).toHaveLength(1);
  });

  it("shows another refusal of Withdraw word for word and keeps the row", async () => {
    const detail = "You may not write ml.model; ask for WRITE_TABLE.";
    const server = fakeServer({
      ...artifactRoutes(),
      [WITHDRAW]: { status: 403, body: { type: "PermissionDeniedError", detail } },
    });
    const { user } = renderUploads();

    await user.click(await screen.findByRole("button", { name: `Withdraw upload ${FIRST}` }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(at(await rows(), 0)).toHaveTextContent(FIRST);
    expect(
      server.sent.filter((request) => request.path.includes("/artifacts/uploads")),
    ).toHaveLength(1);
  });

  it("marks a bidi character in an upload id", async () => {
    fakeServer({
      ...artifactRoutes(),
      [UPLOADS]: {
        status: 200,
        body: { items: [upload({ upload_id: "evil‮di" })], next_cursor: null },
      },
    });
    renderUploads();

    const row = at(await rows(), 0);
    expect(row).toHaveTextContent("evilU+202Edi");
    expect(row.textContent).not.toContain("‮");
    expect(within(row).getByRole("button", { name: "Withdraw upload evilU+202Edi" })).toBeVisible();
  });
});
