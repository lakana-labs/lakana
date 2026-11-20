import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BROWSER_DOWNLOAD_LIMIT, RESULT_MEDIA_TYPES, XLSX_MAX_ROWS } from "../api/queries";
import type { QueryRunOut } from "../api/queries";
import { api, DownloadCut } from "../api/client";
import { saveBlob } from "../shell/saveFile";
import { aRun, RUN_ID } from "../test/queries";
import { fakeServer } from "../test/server";
import { pinTimeZone } from "../test/timeZone";
import { Downloads } from "./Downloads";

vi.mock("../shell/saveFile", async (original) => ({
  ...(await original<typeof import("../shell/saveFile")>()),
  saveBlob: vi.fn(),
}));

// The file names carry the local date of the run's end, which the expected names fix.
pinTimeZone("Europe/Berlin");

const RESULT = `GET /api/v1/environments/prod/queries/${RUN_ID}/result`;

/**
 * Renders the downloads of a run.
 *
 * @param run - The run.
 * @param environment - Its environment.
 * @param name - The saved query's name.
 * @returns The user.
 */
function show(
  run: QueryRunOut = aRun(),
  environment = "prod",
  name: string | null = "Daily sales",
) {
  const user = userEvent.setup();
  render(<Downloads environment={environment} run={run} name={name} />);
  return user;
}

afterEach(() => {
  vi.mocked(saveBlob).mockReset();
  vi.restoreAllMocks();
});

describe("Downloads", () => {
  it("downloads CSV and saves it under the result's name", async () => {
    const blob = new Blob(["one\n1\n"], { type: "text/csv" });
    const server = fakeServer({ [RESULT]: { status: 200, blob } });
    const user = show();

    await user.click(screen.getByRole("button", { name: "Download CSV" }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    const [saved, name] = vi.mocked(saveBlob).mock.calls[0] as [Blob, string];
    expect(saved.size).toBe(blob.size);
    expect(name).toBe("Daily-sales-2026-11-16.csv");
    expect(server.sent.at(-1)?.path).toContain("format=csv");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("downloads a spreadsheet with the xlsx accept header", async () => {
    const blob = new Blob(["PK"], { type: RESULT_MEDIA_TYPES.xlsx });
    const server = fakeServer({ [RESULT]: { status: 200, blob } });
    const user = show(aRun(), "prod", null);

    await user.click(screen.getByRole("button", { name: "Download spreadsheet" }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    expect(vi.mocked(saveBlob).mock.calls[0]?.[1]).toBe(`query-${RUN_ID}-2026-11-16.xlsx`);
    expect(server.sent.at(-1)?.headers["accept"]).toBe(RESULT_MEDIA_TYPES.xlsx);
    expect(server.sent.at(-1)?.path).toContain("format=xlsx");
  });

  it("shows the servers refusal and saves nothing", async () => {
    fakeServer({
      [RESULT]: { status: 422, body: { detail: "The result has a value too long for a cell." } },
    });
    const user = show();

    await user.click(screen.getByRole("button", { name: "Download spreadsheet" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The result has a value too long for a cell.",
    );
    expect(saveBlob).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Download spreadsheet" })).toBeEnabled();
  });

  it("refuses a file of the wrong type", async () => {
    fakeServer({ [RESULT]: { status: 200, blob: new Blob(["<html>"], { type: "text/html" }) } });
    const user = show();

    await user.click(screen.getByRole("button", { name: "Download CSV" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server sent something other than the file; try again.",
    );
    expect(saveBlob).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Download CSV" })).toBeEnabled();
  });

  it("says the download broke off on DownloadCut", async () => {
    vi.spyOn(api, "download").mockRejectedValue(new DownloadCut());
    const user = show();

    await user.click(screen.getByRole("button", { name: "Download CSV" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The download broke off; try again.",
    );
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("clears the sentence when the next download starts", async () => {
    const server = fakeServer({
      [RESULT]: { status: 503, body: { detail: "The server is busy; try again." } },
    });
    const user = show();
    await user.click(screen.getByRole("button", { name: "Download CSV" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy; try again.");

    server.route({ [RESULT]: { status: 200, blob: new Blob(["a"], { type: "text/csv" }) } });
    await user.click(screen.getByRole("button", { name: "Download CSV" }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers the command instead of a download above 256 MiB and quotes it", () => {
    const server = fakeServer({});
    show(aRun({ result_bytes: BROWSER_DOWNLOAD_LIMIT + 1 }), "my env", null);

    expect(screen.queryByRole("button", { name: /^Download/u })).toBeNull();
    expect(
      screen.getByText(/too large to download in the browser \(268\.4 MB\)/u),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        new RegExp(`--env 'my env' --format csv --out 'query-${RUN_ID}-2026-11-16.csv'`, "u"),
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        new RegExp(`--env 'my env' --format xlsx --out 'query-${RUN_ID}-2026-11-16.xlsx'`, "u"),
      ),
    ).toBeInTheDocument();
    expect(server.sent).toHaveLength(0);
  });

  it("replaces the spreadsheet download above 1,048,575 rows", () => {
    const server = fakeServer({});
    show(aRun({ result_rows: XLSX_MAX_ROWS + 1 }));

    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download spreadsheet" })).toBeNull();
    expect(
      screen.getByText("Too many rows for a spreadsheet (more than 1,048,575); download CSV."),
    ).toBeInTheDocument();
    expect(server.sent).toHaveLength(0);
  });

  it("allows a spreadsheet of exactly 1,048,575 rows", () => {
    show(aRun({ result_rows: XLSX_MAX_ROWS }));

    expect(screen.getByRole("button", { name: "Download spreadsheet" })).toBeInTheDocument();
  });

  it("shows both downloads for a run without recorded sizes", () => {
    show(aRun({ result_rows: null, result_bytes: null }));

    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download spreadsheet" })).toBeInTheDocument();
  });

  it("the pressed button is pending and the other stays usable", async () => {
    let finish: (blob: Blob) => void = () => undefined;
    vi.spyOn(api, "download").mockReturnValue(
      new Promise<Blob>((resolve) => {
        finish = resolve;
      }),
    );
    const user = show();

    await user.click(screen.getByRole("button", { name: /^Download CSV/u }));

    expect(screen.getByRole("button", { name: /^Download CSV/u })).toHaveAttribute("data-pending");
    expect(screen.getByRole("button", { name: "Download spreadsheet" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Download spreadsheet" })).not.toHaveAttribute(
      "data-pending",
    );

    finish(new Blob(["a"], { type: "text/csv" }));
    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledOnce();
    });
    expect(screen.getByRole("button", { name: "Download CSV" })).not.toHaveAttribute(
      "data-pending",
    );
  });
});
