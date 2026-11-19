import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { SubjectOut } from "../api/table";
import { initI18n } from "../i18n/i18n";
import { SubjectName } from "./SubjectName";

initI18n();

const MARIA: SubjectOut = {
  principal_id: "p-1",
  group_id: null,
  principal: { id: "p-1", name: "maria", principal_type: "user" },
  group: null,
};

/**
 * Renders a subject in a router.
 *
 * @param subject - Who.
 * @param link - Whether to link a principal.
 * @returns The container.
 */
function show(subject: SubjectOut, link: boolean): HTMLElement {
  return render(
    <MemoryRouter>
      <SubjectName subject={subject} link={link} />
    </MemoryRouter>,
  ).container;
}

describe("SubjectName", () => {
  it("links a person only when asked", () => {
    show(MARIA, true);
    expect(screen.getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
  });

  it("names a person without a link otherwise", () => {
    const container = show(MARIA, false);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(container.textContent).toBe("maria");
  });

  it("names a group and a service account", () => {
    const service = show(
      { ...MARIA, principal: { id: "p-2", name: "loader", principal_type: "service" } },
      false,
    );
    expect(service.textContent).toBe("loaderservice account");
    const group = show(
      { principal_id: null, group_id: "g-1", principal: null, group: "analysts" },
      true,
    );
    expect(group.textContent).toBe("group analysts");
    expect(screen.queryByRole("link", { name: /analysts/ })).not.toBeInTheDocument();
  });

  it("says when the subject is gone", () => {
    const gone = show({ principal_id: null, group_id: "g-9", principal: null, group: null }, true);
    expect(gone.textContent).toBe("a removed person or group g-9");
  });

  it("marks a hidden character", () => {
    const marked = show(
      { principal_id: null, group_id: "g-1", principal: null, group: "an‮al" },
      false,
    );
    expect(marked.textContent).toContain("U+202E");
  });
});
