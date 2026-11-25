import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { type NameChoice, type NameMatches, NamePicker, type NameSearch } from "./NamePicker";

/**
 * A suggestion without data.
 *
 * @param name - The name.
 * @param detail - The quiet word.
 * @returns The suggestion.
 */
function aChoice(name: string, detail: string | null = "Person"): NameChoice<null> {
  return { key: name, name, detail, data: null };
}

/** A search a test answers by hand: each call waits until the test settles it. */
interface HeldSearch {
  /** The search to hand to the picker. */
  readonly search: NameSearch<null>;
  /** Every call: the text, its signal, and how to answer it. */
  readonly calls: {
    readonly text: string;
    readonly signal: AbortSignal;
    readonly answer: (matches: NameMatches<null>) => void;
    readonly fail: (error: unknown) => void;
  }[];
}

/**
 * A search whose answers the test gives.
 *
 * @returns The search and its calls.
 */
function heldSearch(): HeldSearch {
  const calls: HeldSearch["calls"] = [];
  const search: NameSearch<null> = (text, signal) =>
    new Promise((resolve, reject) => {
      calls.push({ text, signal, answer: resolve, fail: reject });
    });
  return { search, calls };
}

/** Properties of {@link Harness}. */
interface HarnessProps {
  /** The search, or null. */
  readonly search: NameSearch<null> | null;
  /** The value at the start. */
  readonly start?: string;
  /** A refusal to show. */
  readonly error?: string | null;
  /** Called with each chosen suggestion. */
  readonly onPick?: (choice: NameChoice<null>) => void;
}

/**
 * The picker with its value held in state, inside a box that records the keys reaching it.
 *
 * @param props - The search and the start.
 * @param props.search - The search.
 * @param props.start - The value at the start.
 * @param props.error - A refusal.
 * @param props.onPick - Called with a chosen suggestion.
 * @returns The picker.
 */
function Harness({ search, start = "", error = null, onPick }: HarnessProps): JSX.Element {
  const [value, setValue] = useState(start);
  return (
    <div>
      <NamePicker
        label="Person or service account"
        help="The name they sign in as."
        value={value}
        onChange={setValue}
        search={search}
        noMatch={(text) => `No principal named “${text}”. Check the spelling.`}
        error={error}
        {...(onPick === undefined ? {} : { onPick })}
      />
      <output data-testid="value">{JSON.stringify(value)}</output>
    </div>
  );
}

/** The keys that reached the document: past the picker and every handler on the page. */
const outsideKeys: string[] = [];

/**
 * Records a key that reached the document.
 *
 * @param event - The key.
 */
function record(event: KeyboardEvent): void {
  outsideKeys.push(event.key);
}

beforeEach(() => {
  outsideKeys.length = 0;
  document.addEventListener("keydown", record);
});

afterEach(() => {
  document.removeEventListener("keydown", record);
});

/**
 * The value the picker holds, as the harness shows it.
 *
 * @returns The value.
 */
function held(): unknown {
  return JSON.parse(screen.getByTestId("value").textContent);
}

describe("NamePicker", () => {
  it("is a labelled combobox with its help line and asks once typing pauses", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);

    const field = screen.getByRole("combobox", { name: "Person or service account" });
    expect(field).toHaveAccessibleDescription("The name they sign in as.");
    await user.type(field, " mar ");

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.text).toBe("mar");
    expect(screen.getByRole("status")).toHaveTextContent("Looking for names…");
    act(() => {
      calls[0]?.answer({
        choices: [aChoice("maria"), aChoice("ci-bot", "Service account")],
        more: false,
      });
    });

    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "mariaPerson",
      "ci-botService account",
    ]);
    expect(field).toHaveAttribute("aria-expanded", "true");
  });

  it("names its list in the interface's words, not React Aria's", async () => {
    // React Aria alone would name it in the browser's language: "Empfehlungen".
    vi.spyOn(navigator, "language", "get").mockReturnValue("de-DE");
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    act(() => {
      window.dispatchEvent(new Event("languagechange"));
    });

    await user.type(screen.getByRole("combobox"), "ma");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });

    expect(screen.getByRole("listbox")).toHaveAttribute("aria-label", "Suggestions");
    vi.restoreAllMocks();
    act(() => {
      window.dispatchEvent(new Event("languagechange"));
    });
  });

  it("forgets the suggestions of an older search, such as another environment's tables", async () => {
    const user = userEvent.setup();
    const first = heldSearch();
    const second = heldSearch();
    const { rerender } = render(<Harness search={first.search} />);
    await user.type(screen.getByRole("combobox"), "st");
    await waitFor(() => {
      expect(first.calls).toHaveLength(1);
    });
    act(() => {
      first.calls[0]?.answer({ choices: [aChoice("station_ranking")], more: false });
    });
    await screen.findByRole("option");
    await user.keyboard("{Escape}");

    rerender(<Harness search={second.search} />);
    await user.keyboard("{ArrowDown}");

    await waitFor(() => {
      expect(second.calls).toHaveLength(1);
    });
    expect(screen.queryAllByRole("option")).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("Looking for names…");
    act(() => {
      first.calls[0]?.answer({ choices: [aChoice("late")], more: false });
    });
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("drops an older text's answer and cancels its request", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    const field = screen.getByRole("combobox");

    await user.type(field, "ma");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    await user.type(field, "x");
    await waitFor(() => {
      expect(calls).toHaveLength(2);
    });
    expect(calls[0]?.signal.aborted).toBe(true);
    expect(calls[1]?.text).toBe("max");
    act(() => {
      calls[1]?.answer({ choices: [aChoice("max")], more: false });
      calls[0]?.answer({ choices: [aChoice("maria")], more: false });
    });

    expect(await screen.findByRole("option", { name: /max/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /maria/ })).toBeNull();
  });

  it("drops an older text's failure as it drops its answer", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    const field = screen.getByRole("combobox");

    await user.type(field, "ma");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    await user.type(field, "x");
    await waitFor(() => {
      expect(calls).toHaveLength(2);
    });
    act(() => {
      calls[0]?.fail(new ApiError(503, "ServiceUnavailable", "The catalog is busy."));
      calls[1]?.answer({ choices: [aChoice("max")], more: false });
    });

    expect(await screen.findByRole("option", { name: "max" })).toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent("could not be read");
  });

  it("says when nothing matches, in the list and to a screen reader", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);

    await user.type(screen.getByRole("combobox"), "zed");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [], more: false });
    });

    const sentence = "No principal named “zed”. Check the spelling.";
    expect(await screen.findByRole("status")).toHaveTextContent(sentence);
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("says when it shows only the first matches", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);

    await user.type(screen.getByRole("combobox"), "a");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [aChoice("ann"), aChoice("bea")], more: true });
    });

    const line = "The first 2 matches. Type more of the name to narrow them down.";
    expect(await screen.findAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent(line);
  });

  it("says when the suggestions could not be read, and keeps the typed text", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);

    await user.type(screen.getByRole("combobox"), "ben");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.fail(new ApiError(503, "ServiceUnavailable", "The catalog is busy. Try again."));
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Suggestions could not be read: The catalog is busy. Try again. You can still type the whole name.",
      );
    });
    expect(held()).toBe("ben");
  });

  it("puts the exact name of a chosen suggestion in the field and hands it over", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    const onPick = vi.fn();
    render(<Harness search={search} onPick={onPick} />);
    const field = screen.getByRole("combobox");

    await user.type(field, "MAR");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [aChoice("maria"), aChoice("marek")], more: false });
    });
    await screen.findAllByRole("option");
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");

    expect(held()).toBe("marek");
    expect(field).toHaveValue("marek");
    expect(onPick).toHaveBeenCalledWith(aChoice("marek"));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("marks hidden characters in a suggestion and in a chosen name", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    const spoof = "adm‮nimda";

    await user.type(screen.getByRole("combobox"), "adm");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [aChoice(spoof), aChoice("admin")], more: false });
    });
    const [first] = await screen.findAllByRole("option");
    expect(first).toHaveTextContent("admU+202EnimdaPerson");
    // The name a screen reader speaks (VoiceOver takes it for React Aria's announcements) is
    // marked too.
    expect(first).toHaveAccessibleName("admU+202Enimda");
    await user.click(first as HTMLElement);

    expect(held()).toBe(spoof);
    expect(screen.getByRole("combobox")).toHaveAccessibleDescription(
      "The name they sign in as. With its hidden characters shown: admU+202Enimda",
    );
  });

  it("closes an open list on Escape and keeps the text, without the key leaving the field", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    const field = screen.getByRole("combobox");

    await user.type(field, "ma");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [aChoice("maria")], more: false });
    });
    await screen.findByRole("option");
    outsideKeys.length = 0;
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(field).toHaveValue("ma");
    expect(outsideKeys).not.toContain("Escape");
    await user.keyboard("{Escape}");
    expect(outsideKeys).toContain("Escape");
    expect(field).toHaveValue("ma");
  });

  it("sends the form on Enter with the list open and nothing highlighted, as other fields do", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    const sent = vi.fn();
    render(
      <form
        onSubmit={(event) => {
          event.preventDefault();
          sent();
        }}
      >
        <Harness search={search} />
      </form>,
    );

    await user.type(screen.getByRole("combobox"), "ma");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    act(() => {
      calls[0]?.answer({ choices: [aChoice("maria")], more: false });
    });
    await screen.findByRole("option");
    await user.keyboard("{Enter}");

    expect(sent).toHaveBeenCalledTimes(1);
    expect(held()).toBe("ma");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(held()).toBe("maria");
    expect(sent).toHaveBeenCalledTimes(1);
  });

  it("takes a pasted exact name as it is and suggests it", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);
    const field = screen.getByRole("combobox");

    await user.click(field);
    await user.paste("maria");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.text).toBe("maria");
    act(() => {
      calls[0]?.answer({ choices: [aChoice("maria")], more: false });
    });

    expect(await screen.findByRole("option", { name: /maria/ })).toBeInTheDocument();
    expect(held()).toBe("maria");
  });

  it("opens on Arrow Down with the first suggestions for an empty field", async () => {
    const user = userEvent.setup();
    const { search, calls } = heldSearch();
    render(<Harness search={search} />);

    await user.click(screen.getByRole("combobox"));
    await user.keyboard("{ArrowDown}");
    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0]?.text).toBe("");
    act(() => {
      calls[0]?.answer({ choices: [], more: false });
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Nothing to suggest yet. Type part of a name.",
      );
    });
  });

  it("is a plain field that asks nothing without a search", async () => {
    const user = userEvent.setup();
    render(<Harness search={null} />);
    const field = screen.getByRole("combobox");

    await user.type(field, "ben{ArrowDown}");

    expect(field).toHaveValue("ben");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("shows a refusal under the field and marks it invalid", () => {
    render(<Harness search={null} start="ben" error="No principal named 'ben'." />);

    const field = screen.getByRole("combobox");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(
      expect.stringContaining("No principal named 'ben'.") as string,
    );
  });
});
