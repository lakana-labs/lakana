import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { saveBlob } from "../shell/saveFile";
import { VisibleText } from "../shell/VisibleText";
import { JOB_RUN_ID } from "../test/jobs";
import { LogView } from "./LogView";
import type { TaskLog } from "./useTaskLog";

vi.mock("../shell/VisibleText", async (original) => {
  const real = await original<typeof import("../shell/VisibleText")>();
  return { ...real, VisibleText: vi.fn(real.VisibleText) };
});

vi.mock("../shell/saveFile", async (original) => ({
  ...(await original<typeof import("../shell/saveFile")>()),
  saveBlob: vi.fn(),
}));

const COMMAND = `lakana runs logs '${JOB_RUN_ID}' --task 'silver.orders' --env 'my env'`;

/**
 * A log as the hook gives it.
 *
 * @param over - The fields that differ.
 * @returns The log.
 */
function aLog(over: Partial<TaskLog> = {}): TaskLog {
  return {
    text: "building silver.orders\ndone\n",
    attempts: [],
    complete: false,
    cut: false,
    stopped: false,
    error: null,
    gone: false,
    retry: vi.fn(),
    ...over,
  };
}

function view(log: TaskLog, environment = "prod") {
  return <LogView environment={environment} runId={JOB_RUN_ID} asset="silver.orders" log={log} />;
}

/**
 * Gives the log's region a height, a scroll height and a place, as a browser would.
 *
 * @param region - The region.
 * @param scrollHeight - Its content's height.
 */
function lay(region: HTMLElement, scrollHeight: number): void {
  Object.defineProperty(region, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(region, "scrollHeight", { configurable: true, value: scrollHeight });
}

afterEach(() => {
  vi.mocked(saveBlob).mockReset();
});

describe("LogView", () => {
  it("shows each line as text with hidden characters marked", () => {
    render(view(aLog({ text: "step ‮one\n\ttwo\nthree" })));

    const region = screen.getByRole("region", { name: "Log of silver.orders" });
    expect(region.tagName).toBe("PRE");
    expect(region).not.toHaveAttribute("aria-live");
    expect(region.textContent).toBe("step U+202Eone\n\ttwo\nthree");
    expect(region.querySelectorAll(".char-mark")).toHaveLength(1);
  });

  it("says it is following, then that the log is complete", () => {
    const { rerender } = render(view(aLog()));

    expect(screen.getByRole("status")).toHaveTextContent("Following the log…");
    rerender(view(aLog({ complete: true })));
    expect(screen.getByRole("status")).toHaveTextContent("The log is complete.");
  });

  it("keeps the reader's place when they scrolled up and follows when at the end", () => {
    const { rerender } = render(view(aLog({ text: "a\n" })));
    const region = screen.getByRole("region", { name: "Log of silver.orders" });

    lay(region, 300);
    rerender(view(aLog({ text: "a\nb\n" })));
    expect(region.scrollTop).toBe(300);

    region.scrollTop = 40;
    fireEvent.scroll(region);
    lay(region, 500);
    rerender(view(aLog({ text: "a\nb\nc\n" })));
    expect(region.scrollTop).toBe(40);

    region.scrollTop = 400;
    fireEvent.scroll(region);
    lay(region, 700);
    rerender(view(aLog({ text: "a\nb\nc\nd\n" })));
    expect(region.scrollTop).toBe(700);
  });

  it("Save log saves the whole log under the task and run", async () => {
    const user = userEvent.setup();
    render(view(aLog({ complete: true })));

    await user.click(screen.getByRole("button", { name: "Save log" }));

    expect(saveBlob).toHaveBeenCalledTimes(1);
    const [blob, name] = vi.mocked(saveBlob).mock.calls[0] as [Blob, string];
    expect(name).toBe(`silver.orders-${JOB_RUN_ID.slice(0, 8)}.log`);
    expect(await blob.text()).toBe("building silver.orders\ndone\n");
  });

  it("offers Save log only once the log is complete", () => {
    render(view(aLog()));

    expect(screen.queryByRole("button", { name: "Save log" })).toBeNull();
  });

  it("offers the command instead of Save log once lines were dropped", () => {
    render(view(aLog({ complete: true, cut: true }), "my env"));

    expect(screen.getByText("Earlier lines are not shown here.")).toBeInTheDocument();
    expect(screen.getByText(COMMAND)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save log" })).toBeNull();
  });

  it("says a log too long to follow is not followed, with the command", () => {
    render(view(aLog({ stopped: true, cut: true }), "my env"));

    expect(screen.getByText("This log is too long to follow here.")).toBeInTheDocument();
    expect(screen.queryByText("Earlier lines are not shown here.")).toBeNull();
    expect(screen.getByText(COMMAND)).toBeInTheDocument();
    expect(screen.queryByText("Following the log…")).toBeNull();
    expect(screen.queryByText("The log is complete.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save log" })).toBeNull();
  });

  it("says when the task wrote nothing", () => {
    render(view(aLog({ text: "", complete: true })));

    expect(screen.getByText("The task wrote nothing to its log.")).toBeInTheDocument();
    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save log" })).toBeNull();
  });

  it("shows why a read failed with Try again", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    render(view(aLog({ error: "The log store is down.", retry })));

    expect(screen.getByRole("alert")).toHaveTextContent("The log store is down.");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("draws again only the lines at the end when the log grows", () => {
    const lines = (from: number, count: number): string =>
      Array.from({ length: count }, (_, i) => `line ${String(from + i)}\n`).join("");
    const { rerender } = render(view(aLog({ text: lines(0, 450) })));
    const region = screen.getByRole("region", { name: "Log of silver.orders" });
    expect(region.textContent).toBe(lines(0, 450));
    vi.mocked(VisibleText).mockClear();

    rerender(view(aLog({ text: lines(0, 460) })));

    expect(region.textContent).toBe(lines(0, 460));
    // Parts of 200 lines: the first two stay as they were; only the third, now 60 lines and the
    // empty one after the last line end, is drawn again.
    expect(vi.mocked(VisibleText).mock.calls.length).toBe(61);
  });
});
