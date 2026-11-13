import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api";
import { DESCRIPTION_MAX, LABEL_MAX, TextEditor, type TextEditorProps } from "./TextEditor";

const TEXTS = { description: "Every sale.", label: "Sales" };

/**
 * Shows the editor with spies for what it hands back.
 *
 * @param overrides - The properties that differ.
 * @returns The spies and the person's keyboard.
 */
function show(overrides: Partial<TextEditorProps> = {}) {
  const onSave = vi.fn<TextEditorProps["onSave"]>().mockResolvedValue(undefined);
  const onDone = vi.fn();
  const user = userEvent.setup();
  render(
    <TextEditor
      texts={TEXTS}
      lang="en"
      named={false}
      onSave={onSave}
      onDone={onDone}
      {...overrides}
    />,
  );
  return { onSave, onDone, user };
}

describe("TextEditor", () => {
  it("states its limits", () => {
    expect(DESCRIPTION_MAX).toBe(4000);
    expect(LABEL_MAX).toBe(100);
  });

  it("starts with the current texts and focus in the description", () => {
    show();

    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Every sale.");
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("Sales");
  });

  it("starts empty for a text there is none of", () => {
    show({ texts: { description: null, label: null } });

    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("");
  });

  it("names the language beside both fields when the table has more than one", () => {
    show({ lang: "de", named: true });

    expect(screen.getByRole("textbox", { name: "Description (German)" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Label (German)" })).toBeVisible();
  });

  it("sends only the field that changed", async () => {
    const { onSave, onDone, user } = show();

    await user.type(screen.getByRole("textbox", { name: "Description" }), " More.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledOnce();
    });
    expect(onSave).toHaveBeenCalledExactlyOnceWith({ description: "Every sale. More." });
  });

  it("sends a changed label alone, from the keyboard with Enter", async () => {
    const { onSave, user } = show();

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!{Enter}");

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledExactlyOnceWith({ label: "Sales!" });
    });
  });

  it("closes without a request when nothing changed", async () => {
    const { onSave, onDone, user } = show();

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(onSave).not.toHaveBeenCalled();
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("refuses a blank text before sending", async () => {
    const { onSave, onDone, user } = show();

    await user.clear(screen.getByRole("textbox", { name: "Description" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "   ");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The description cannot be blank. Write one, or press Cancel to keep the current text.",
    );
    expect(onSave).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("refuses a blank label before sending", async () => {
    const { onSave, user } = show();

    await user.clear(screen.getByRole("textbox", { name: "Label" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The label cannot be blank.");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("counts characters near the limit and not before", async () => {
    const { user } = show({ texts: { description: "a".repeat(3600), label: null } });
    const box = screen.getByRole("textbox", { name: "Description" });
    expect(screen.getByRole("status", { name: "Description" })).toBeEmptyDOMElement();

    await user.type(box, "b".repeat(212));

    expect(screen.getByRole("status", { name: "Description" })).toHaveTextContent("3,812 of 4,000");
  });

  it("refuses a description over the limit and says by how much it is over", async () => {
    const { onSave, user } = show({ texts: { description: "a".repeat(3999), label: null } });

    await user.type(screen.getByRole("textbox", { name: "Description" }), "bb");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This description has 4,001 characters and the most is 4,000.",
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("keeps a label on one line and at most 100 characters", async () => {
    const { onSave, user } = show({ texts: { description: null, label: null } });
    const box = screen.getByRole("textbox", { name: "Label" });

    await user.type(box, "x".repeat(101));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(box.tagName).toBe("INPUT");
    expect(await screen.findByRole("alert")).toHaveTextContent("This label has 101 characters");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("counts a character outside the basic plane once, as the server does", async () => {
    const { onSave, user } = show({ texts: { description: null, label: null } });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "😀".repeat(100));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledOnce();
    });
  });

  it("keeps the text and shows the refusal next to the field", async () => {
    const refusal = new ApiError(422, "ValidationError", "The label may not span several lines.");
    const { onDone, user } = show({ onSave: vi.fn().mockRejectedValue(refusal) });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The label may not span several lines.");
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveAccessibleDescription(
      /The label may not span several lines\./,
    );
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("Sales!");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("names the cause of a failure that is not the server's answer", async () => {
    const { user } = show({ onSave: vi.fn().mockRejectedValue(new Error("boom")) });

    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Something went wrong/);
  });

  it("sends one request when Save is pressed twice", async () => {
    let finish: () => void = () => undefined;
    const onSave = vi.fn<TextEditorProps["onSave"]>(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const { onDone, user } = show({ onSave });

    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    const save = screen.getByRole("button", { name: /Save/ });
    await user.click(save);
    await user.click(save);
    await user.type(screen.getByRole("textbox", { name: "Label" }), "{Enter}");

    expect(onSave).toHaveBeenCalledOnce();
    finish();
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledOnce();
    });
  });

  it("cancels with the button and with Escape, without a request", async () => {
    const { onSave, onDone, user } = show();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.type(screen.getByRole("textbox", { name: "Label" }), "{Escape}");

    expect(onDone).toHaveBeenCalledTimes(2);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not cancel with Escape while a save is open", async () => {
    const onSave = vi.fn<TextEditorProps["onSave"]>(() => new Promise<void>(() => undefined));
    const { onDone, user } = show({ onSave });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!{Enter}");
    await user.keyboard("{Escape}");

    expect(onDone).not.toHaveBeenCalled();
  });

  it("does not cancel on Escape while an input method is composing", () => {
    const { onDone } = show();

    fireEvent.keyDown(screen.getByRole("textbox", { name: "Label" }), {
      key: "Escape",
      isComposing: true,
    });

    expect(onDone).not.toHaveBeenCalled();
  });

  it("shows a refusal under the label when the server names the label, both fields changed", async () => {
    const refusal = new ApiError(
      422,
      "ValidationError",
      "The label has a character it may not hold.",
    );
    const { user } = show({ onSave: vi.fn().mockRejectedValue(refusal) });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveAccessibleDescription(
      /character it may not hold/,
    );
    expect(screen.getByRole("textbox", { name: "Description" })).not.toHaveAccessibleDescription(
      /character it may not hold/,
    );
  });

  it("shows a refusal that names neither field above the buttons when both changed", async () => {
    const refusal = new ApiError(422, "ValidationError", "A text has a character it may not hold.");
    const { user } = show({ onSave: vi.fn().mockRejectedValue(refusal) });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A text has a character");
    expect(screen.getByRole("textbox", { name: "Label" })).not.toHaveAccessibleDescription(
      /character it may not hold/,
    );
    expect(screen.getByRole("textbox", { name: "Description" })).not.toHaveAccessibleDescription(
      /character it may not hold/,
    );
  });

  it("shows an unnamed refusal under the only field that was sent", async () => {
    const refusal = new ApiError(
      422,
      "ValidationError",
      "That text has a character it may not hold.",
    );
    const { user } = show({ onSave: vi.fn().mockRejectedValue(refusal) });

    await user.type(screen.getByRole("textbox", { name: "Label" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveAccessibleDescription(
      /character it may not hold/,
    );
  });

  it("counts the text after the blanks around it are stripped, as the server does", async () => {
    const { onSave, user } = show({ texts: { description: "a".repeat(3999), label: null } });

    await user.type(screen.getByRole("textbox", { name: "Description" }), "b      ");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledOnce();
    });
  });

  it("renders text as text", () => {
    show({ texts: { description: "<img src=x onerror=alert(1)>", label: "<b>x</b>" } });

    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Label" })).toHaveValue("<b>x</b>");
  });
});
