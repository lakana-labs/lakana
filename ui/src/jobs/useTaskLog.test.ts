import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { createElement, type JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import { getTaskLog, type TaskLogOut } from "../api/jobs";
import { saveBlob } from "../shell/saveFile";
import { anAttempt, aTaskLog, JOB_RUN_ID } from "../test/jobs";
import { LogView } from "./LogView";
import { LOG_CATCHUP_PIECES, LOG_KEEP_CHARS, useTaskLog } from "./useTaskLog";

vi.mock("../api/jobs", async (original) => ({
  ...(await original<typeof import("../api/jobs")>()),
  getTaskLog: vi.fn(),
}));

vi.mock("../shell/saveFile", async (original) => ({
  ...(await original<typeof import("../shell/saveFile")>()),
  saveBlob: vi.fn(),
}));

/**
 * The log of `silver.orders` as its page shows it, followed by the hook.
 *
 * @returns The view.
 */
function Followed(): JSX.Element {
  const log = useTaskLog("prod", JOB_RUN_ID, "silver.orders");
  return createElement(LogView, {
    environment: "prod",
    runId: JOB_RUN_ID,
    asset: "silver.orders",
    log,
  });
}

let visibility: "visible" | "hidden" = "visible";

function show(state: "visible" | "hidden"): void {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
}

async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/**
 * Answers the log's reads with these pieces in turn, and the last one again after them.
 *
 * @param pieces - The pieces.
 */
function answer(...pieces: TaskLogOut[]): void {
  let next = 0;
  vi.mocked(getTaskLog).mockImplementation(() => {
    const piece = pieces[Math.min(next, pieces.length - 1)] as TaskLogOut;
    next += 1;
    return Promise.resolve(piece);
  });
}

/**
 * A piece of a log that goes on.
 *
 * @param text - The piece's text.
 * @param next - The offset after it.
 * @param more - Whether more is stored after it.
 * @returns The piece.
 */
function running(text: string, next: number, more = false): TaskLogOut {
  return aTaskLog({ text, next_offset: next, more, complete: false });
}

/**
 * The offsets the log was read from.
 *
 * @returns The offsets, in order.
 */
function offsets(): number[] {
  return vi.mocked(getTaskLog).mock.calls.map((call) => call[3]);
}

function follow() {
  return renderHook(() => useTaskLog("prod", JOB_RUN_ID, "silver.orders"));
}

beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});

afterEach(() => {
  vi.mocked(getTaskLog).mockReset();
  vi.mocked(saveBlob).mockReset();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useTaskLog", () => {
  it("reads from offset zero and continues from next_offset", async () => {
    const attempts = [anAttempt()];
    answer(running("building\n", 9), aTaskLog({ text: "done\n", next_offset: 14, attempts }));
    const { result } = follow();

    await tick(0);
    expect(getTaskLog).toHaveBeenCalledWith("prod", JOB_RUN_ID, "silver.orders", 0);
    expect(result.current.text).toBe("building\n");
    expect(result.current.complete).toBe(false);

    await tick(2_000);
    expect(offsets()).toEqual([0, 9]);
    expect(result.current.text).toBe("building\ndone\n");
    expect(result.current.attempts).toEqual(attempts);
    expect(result.current.complete).toBe(true);
  });

  it("reads the following pieces at once while more is true", async () => {
    answer(running("a\n", 2, true), running("b\n", 4, true), running("c\n", 6));
    const { result } = follow();

    await tick(0);
    expect(offsets()).toEqual([0, 2, 4]);
    expect(result.current.text).toBe("a\nb\nc\n");
    await tick(1_999);
    expect(offsets()).toEqual([0, 2, 4]);
    await tick(1);
    expect(offsets()).toEqual([0, 2, 4, 6]);
  });

  it("stops following after sixteen pieces and offers the command", async () => {
    // Review Focus 2: forty pieces of 1 MiB each, every one with more after it.
    const mebibyte = `${"x".repeat(1023)}\n`.repeat(1024);
    let next = 0;
    vi.mocked(getTaskLog).mockImplementation(() => {
      next += mebibyte.length;
      return Promise.resolve(running(mebibyte, next, next < 40 * mebibyte.length));
    });
    const { result } = follow();

    await tick(0);
    expect(getTaskLog).toHaveBeenCalledTimes(LOG_CATCHUP_PIECES);
    expect(result.current.stopped).toBe(true);
    expect(result.current.complete).toBe(false);
    expect(result.current.text.length).toBeLessThanOrEqual(LOG_KEEP_CHARS);
    await tick(60_000);
    expect(getTaskLog).toHaveBeenCalledTimes(LOG_CATCHUP_PIECES);
  });

  it("drops whole lines past the limit and says so", async () => {
    // Lines of exactly 100 characters, numbered, so where the cut fell can be read off the text.
    const lines = (from: number, count: number): string =>
      Array.from(
        { length: count },
        (_, i) => `${String(from + i).padStart(6, "0")}${"y".repeat(93)}\n`,
      ).join("");
    const first = lines(0, 12_000);
    const second = lines(12_000, 12_000);
    answer(
      running(first, first.length, true),
      aTaskLog({ text: second, next_offset: 2 * first.length }),
    );
    const { result } = follow();

    await tick(0);
    const whole = first + second;
    const { text } = result.current;
    expect(result.current.cut).toBe(true);
    expect(text.length).toBeLessThanOrEqual(LOG_KEEP_CHARS);
    expect(text.length).toBeGreaterThan(0);
    expect(whole.endsWith(text)).toBe(true);
    expect(whole.at(-text.length - 1)).toBe("\n");
    expect(text.endsWith(`023999${"y".repeat(93)}\n`)).toBe(true);
    expect(result.current.complete).toBe(true);
  });

  it.each([
    ["without a line end", ""],
    ["with its line end", "\n"],
  ])("keeps the end of a single line longer than the limit, %s", async (_, end) => {
    const line = `${"z".repeat(LOG_KEEP_CHARS + 10)}${end}`;
    answer(aTaskLog({ text: line, next_offset: line.length }));
    const { result } = follow();

    await tick(0);
    expect(result.current.cut).toBe(true);
    expect(result.current.text.length).toBeLessThanOrEqual(LOG_KEEP_CHARS);
    expect(result.current.text.length).toBeGreaterThan(LOG_KEEP_CHARS / 2);
  });

  it("starts again on reset", async () => {
    // As the server answers a reset: no text, next_offset at the end of the rewritten log, no
    // more after it (lakana-core runs/service.py, log_window).
    const rewritten = "attempt 2 starts\nattempt 2 builds\n";
    answer(
      running("attempt 1 starts a longer line\n", 31),
      aTaskLog({
        text: "",
        next_offset: rewritten.length,
        more: false,
        complete: false,
        reset: true,
      }),
      aTaskLog({ text: rewritten, next_offset: rewritten.length }),
    );
    const { result } = follow();

    await tick(0);
    expect(result.current.text).toBe("attempt 1 starts a longer line\n");
    await tick(2_000);
    expect(offsets()).toEqual([0, 31, 0]);
    expect(result.current.text).toBe(rewritten);
    expect(result.current.cut).toBe(false);
    expect(result.current.complete).toBe(true);
  });

  it("saves the whole rewritten log after a reset", async () => {
    const rewritten = "attempt 2 starts\nattempt 2 builds\n";
    answer(
      running("attempt 1 starts a longer line\n", 31),
      aTaskLog({
        text: "",
        next_offset: rewritten.length,
        more: false,
        complete: false,
        reset: true,
      }),
      aTaskLog({ text: rewritten, next_offset: rewritten.length }),
    );
    render(createElement(Followed));

    await tick(0);
    await tick(2_000);
    expect(screen.getByRole("region", { name: "Log of silver.orders" }).textContent).toBe(
      rewritten,
    );
    expect(screen.queryByText("Earlier lines are not shown here.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save log" }));
    const [blob] = vi.mocked(saveBlob).mock.calls[0] as [Blob, string];
    expect(await blob.text()).toBe(rewritten);
  });

  it("does not double the text when Try again is pressed while a read is pending", async () => {
    vi.mocked(getTaskLog).mockRejectedValueOnce(
      new ApiError(500, "InternalError", "The log store is down."),
    );
    const { result } = follow();
    await tick(0);
    expect(result.current.error).toBe("The log store is down.");

    const pending: ((piece: TaskLogOut) => void)[] = [];
    vi.mocked(getTaskLog).mockImplementation(
      () =>
        new Promise((resolve) => {
          pending.push(resolve);
        }),
    );
    // The backed-off read starts, and Try again is pressed while it is pending.
    await tick(4_000);
    act(() => {
      result.current.retry();
    });
    await tick(0);
    expect(offsets()).toEqual([0, 0, 0]);
    for (const resolve of pending) {
      resolve(running("a\n", 2));
    }
    await tick(0);
    expect(result.current.text).toBe("a\n");
    await tick(2_000);
    expect(offsets()).toEqual([0, 0, 0, 2]);
  });

  it("reads every two seconds until complete and then stops", async () => {
    answer(running("a\n", 2), running("", 2), aTaskLog({ text: "b\n", next_offset: 4 }));
    const { result } = follow();

    await tick(0);
    await tick(2_000);
    expect(getTaskLog).toHaveBeenCalledTimes(2);
    await tick(2_000);
    expect(getTaskLog).toHaveBeenCalledTimes(3);
    expect(result.current.complete).toBe(true);
    await tick(60_000);
    expect(getTaskLog).toHaveBeenCalledTimes(3);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("pauses while the tab is hidden", async () => {
    answer(running("a\n", 2));
    follow();

    await tick(0);
    act(() => {
      show("hidden");
    });
    await tick(30_000);
    expect(getTaskLog).toHaveBeenCalledTimes(1);
    act(() => {
      show("visible");
    });
    await tick(0);
    expect(getTaskLog).toHaveBeenCalledTimes(2);
  });

  it("stops catching up when the tab is hidden and goes on when it shows", async () => {
    let next = 0;
    vi.mocked(getTaskLog).mockImplementation(() => {
      next += 2;
      if (next === 4) {
        visibility = "hidden";
      }
      return Promise.resolve(running("a\n", next, next < 10));
    });
    const { result } = follow();

    await tick(0);
    expect(offsets()).toEqual([0, 2]);
    expect(result.current.stopped).toBe(false);
    act(() => {
      show("visible");
    });
    await tick(0);
    expect(offsets()).toEqual([0, 2, 4, 6, 8]);
  });

  it("says why a read failed and reads on after Try again", async () => {
    vi.mocked(getTaskLog).mockRejectedValueOnce(
      new ApiError(500, "InternalError", "The log store is down."),
    );
    const { result } = follow();

    await tick(0);
    expect(result.current.error).toBe("The log store is down.");
    expect(result.current.gone).toBe(false);
    answer(running("a\n", 2));
    act(() => {
      result.current.retry();
    });
    await tick(0);
    expect(result.current.error).toBeNull();
    expect(result.current.text).toBe("a\n");
  });

  it("keeps the text and stays quiet while a few reads fail", async () => {
    answer(running("a\n", 2));
    const { result } = follow();
    await tick(0);
    vi.mocked(getTaskLog).mockRejectedValue(
      new ApiError(500, "InternalError", "The log store is down."),
    );

    await tick(2_000);
    expect(result.current.error).toBeNull();
    expect(result.current.text).toBe("a\n");
    await tick(4_000);
    await tick(8_000);
    expect(result.current.error).toBe("The log store is down.");
  });

  it("an unknown task is gone and is not read again", async () => {
    vi.mocked(getTaskLog).mockRejectedValue(
      new ApiError(404, "NotFoundError", "No task gold.sales in this run."),
    );
    const { result } = follow();

    await tick(0);
    expect(result.current.gone).toBe(true);
    expect(result.current.error).toBe("No task gold.sales in this run.");
    await tick(60_000);
    expect(getTaskLog).toHaveBeenCalledTimes(1);
  });

  it("reads nothing more once it was left", async () => {
    let finish: (piece: TaskLogOut) => void = () => undefined;
    vi.mocked(getTaskLog).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { unmount } = follow();
    await tick(0);
    unmount();

    finish(running("a\n", 2, true));
    await tick(10_000);
    expect(getTaskLog).toHaveBeenCalledTimes(1);
  });
});
