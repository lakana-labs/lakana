import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { held } from "../test/pending";
import { aSavedQuery } from "../test/queries";
import { fakeServer } from "../test/server";
import { DeleteSavedDialog } from "./DeleteSavedDialog";

const SAVED = aSavedQuery({ name: "Daily \u202Esales" });
const DELETE = `DELETE /api/v1/environments/prod/saved-queries/${SAVED.id}`;

/**
 * Renders the question for `Daily sales` in `prod`.
 *
 * @returns The handlers and a user-event session.
 */
function renderDelete() {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(<DeleteSavedDialog saved={SAVED} onDone={onDone} onCancel={onCancel} />);
  return { onDone, onCancel, user };
}

describe("DeleteSavedDialog", () => {
  it("asks first, then deletes once", async () => {
    const answer = held();
    const server = fakeServer({ [DELETE]: answer.route });
    const { onDone, onCancel, user } = renderDelete();
    expect(screen.getByRole("dialog")).toHaveAccessibleName(
      "Delete the saved query Daily U+202Esales?",
    );
    expect(server.sent).toEqual([]);
    const confirm = screen.getByRole("button", { name: "Delete" });

    await user.click(confirm);
    await user.click(confirm);
    await user.keyboard("{Escape}");
    answer.release({ status: 204 });

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(onCancel).not.toHaveBeenCalled();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([DELETE]);
  });

  it("keeps the dialog with the servers sentence on a refusal", async () => {
    fakeServer({
      [DELETE]: {
        status: 403,
        body: { type: "PermissionDenied", detail: "You may not delete this saved query." },
      },
    });
    const { onDone, onCancel, user } = renderDelete();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You may not delete this saved query.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
