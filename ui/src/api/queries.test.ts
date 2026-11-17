import { describe, expect, it } from "vitest";

import { WrongFileType } from "./client";
import {
  BROWSER_DOWNLOAD_LIMIT,
  downloadResult,
  getQueryRun,
  getResultPageAt,
  listMyQueries,
  listQueryEnvironments,
  RECENT_DAYS,
  RECENT_PAGE,
  RESULT_MEDIA_TYPES,
  submitQuery,
  XLSX_MAX_ROWS,
  type ResultFormat,
} from "./queries";

import {
  aListedRun,
  aRun,
  QUERY_ENVIRONMENTS,
  queryRoutes,
  RUN_ID,
  WAITING,
} from "../test/queries";
import { fakeServer } from "../test/server";

const NOW = new Date("2026-11-17T12:00:00Z");
const BASE = "/api/v1/environments/prod/queries";

describe("queries api", () => {
  it("submits the sql and returns the id", async () => {
    const server = fakeServer({ [`POST ${BASE}`]: { status: 202, body: { query_id: RUN_ID } } });

    expect(await submitQuery("prod", "select 1")).toBe(RUN_ID);

    expect(server.sent[0]).toMatchObject({ method: "POST", path: BASE, body: { sql: "select 1" } });
  });

  it("reads a run and one page of its result, as background reads", async () => {
    const server = fakeServer({
      ...queryRoutes(),
      [`GET ${BASE}/${RUN_ID}/result`]: { status: 200, body: { rows: [] } },
    });

    expect(await getQueryRun("prod", RUN_ID)).toEqual(aRun());
    await getResultPageAt("prod", RUN_ID, 100, 50);

    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
    expect(server.sent[1]?.path).toBe(`${BASE}/${RUN_ID}/result?format=json&limit=50&offset=100`);
    expect(server.sent[1]?.headers["x-lakana-background"]).toBe("1");
  });

  it("reads why a queued run waits", async () => {
    const queued = aRun({ status: "QUEUED", started_at: null, waiting_for: WAITING });
    fakeServer({ [`GET ${BASE}/${RUN_ID}`]: { status: 200, body: queued } });

    expect((await getQueryRun("prod", RUN_ID)).waiting_for).toEqual(WAITING);
  });

  it("asks for my written queries of 30 days across environments, newest first", async () => {
    const server = fakeServer(queryRoutes());

    const page = await listMyQueries("kim.lee", null, null, NOW);

    expect(page).toEqual({ items: [aListedRun()], nextCursor: null });
    expect(server.sent[0]?.path).toBe(
      "/api/v1/queries?submitter=kim.lee&origin=written&include_ended=true" +
        "&since=2026-10-18T12%3A00%3A00.000Z&limit=50",
    );
  });

  it("adds environment and cursor only when set", async () => {
    const server = fakeServer(queryRoutes());

    await listMyQueries("kim lee", "dev", "c1", NOW);

    expect(server.sent[0]?.path).toBe(
      "/api/v1/queries?submitter=kim+lee&origin=written&include_ended=true" +
        "&since=2026-10-18T12%3A00%3A00.000Z&limit=50&environment=dev&cursor=c1",
    );
  });

  it("lists the environments one may query", async () => {
    fakeServer(queryRoutes());

    expect(await listQueryEnvironments()).toEqual(QUERY_ENVIRONMENTS);
  });

  it("downloads a CSV with its accept header and checks the type", async () => {
    const server = fakeServer({
      [`GET ${BASE}/${RUN_ID}/result`]: {
        status: 200,
        blob: new Blob(["one\n1\n"], { type: "text/csv; charset=utf-8" }),
      },
    });

    const file = await downloadResult("prod", RUN_ID, "csv");

    expect(await file.text()).toBe("one\n1\n");
    expect(server.sent[0]?.path).toBe(`${BASE}/${RUN_ID}/result?format=csv`);
    expect(server.sent[0]?.headers["accept"]).toBe("text/csv");
    expect(server.sent[0]?.headers["x-lakana-background"]).toBeUndefined();
  });

  it("downloads a spreadsheet and asks for its media type", async () => {
    const server = fakeServer({
      [`GET ${BASE}/${RUN_ID}/result`]: {
        status: 200,
        blob: new Blob(["xlsx"], { type: RESULT_MEDIA_TYPES.xlsx }),
      },
    });

    await downloadResult("prod", RUN_ID, "xlsx");

    expect(server.sent[0]?.headers["accept"]).toBe(RESULT_MEDIA_TYPES.xlsx);
  });

  it("throws WrongFileType for a text/html answer", async () => {
    fakeServer({
      [`GET ${BASE}/${RUN_ID}/result`]: {
        status: 200,
        blob: new Blob(["<html>"], { type: "text/html" }),
      },
    });

    await expect(downloadResult("prod", RUN_ID, "csv")).rejects.toBeInstanceOf(WrongFileType);
  });

  it("encodes the environment and the id", async () => {
    const server = fakeServer();

    await getQueryRun("a b", "x y").catch(() => undefined);

    expect(server.sent[0]?.path).toBe("/api/v1/environments/a%20b/queries/x%20y");
  });

  it("downloads each format under its own media type", async () => {
    const formats: readonly ResultFormat[] = ["csv", "xlsx"];
    const server = fakeServer({
      [`GET ${BASE}/${RUN_ID}/result`]: (request) => ({
        status: 200,
        blob: new Blob(["x"], { type: request.headers["accept"] ?? "" }),
      }),
    });

    for (const format of formats) {
      await downloadResult("prod", RUN_ID, format);
    }

    expect(server.sent.map((each) => each.headers["accept"])).toEqual(
      formats.map((format) => RESULT_MEDIA_TYPES[format]),
    );
  });

  it("keeps the limits the screen reads: 256 MiB, a spreadsheet's rows, 30 days of 50", () => {
    expect(BROWSER_DOWNLOAD_LIMIT).toBe(268_435_456);
    expect(XLSX_MAX_ROWS).toBe(1_048_575);
    expect([RECENT_DAYS, RECENT_PAGE]).toEqual([30, 50]);
  });
});
