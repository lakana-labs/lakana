import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ManagedNote } from "./ManagedNote";

const SCOPE = {
  scope: "instance",
  file_name: "access.yaml",
  file_sha256: "ab12",
  source_commit: null,
  applied_by: "admin",
  applied_at: "2026-11-18T09:00:00Z",
};

describe("ManagedNote", () => {
  it("names the file, the time and the emergency command", () => {
    const command = 'lakana grant revoke GRANT_ID --override-managed --reason "WHY"';
    const { container } = render(<ManagedNote scope={SCOPE} command={command} />);

    expect(container).toHaveTextContent("⌂ Git The whole instance is managed by access.yaml");
    expect(container).toHaveTextContent(command);
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
    expect(screen.getByText("lakana access apply").tagName).toBe("CODE");
  });

  it("shows markup in the file's and the environment's names as text", () => {
    const file = "<strong>access</strong>.<code>yaml</code>";
    const { container } = render(
      <ManagedNote scope={{ ...SCOPE, scope: "<code>prod</code>", file_name: file }} command="x" />,
    );

    expect(container).toHaveTextContent(`The environment <code>prod</code> is managed by ${file}`);
    expect(container.querySelector("strong")).toBeNull();
    expect([...container.querySelectorAll("code")].map((code) => code.textContent)).toEqual([
      "lakana access apply",
      "x",
    ]);
  });

  it("names a managed environment, marked", () => {
    const { container } = render(
      <ManagedNote scope={{ ...SCOPE, scope: "pr‮od" }} command="lakana" />,
    );

    expect(container).toHaveTextContent("The environment prU+202Eod is managed by access.yaml");
  });
});
