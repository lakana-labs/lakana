import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { describe, expect, it, type Mock, vi } from "vitest";

import { type GrantFilter, NO_FILTER } from "../api/grants";
import { GrantFilters } from "./GrantFilters";

const ENVIRONMENTS = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/**
 * Renders the filters.
 *
 * @param filter - The filter shown.
 * @returns The change handler and a user-event session.
 */
function renderFilters(filter: GrantFilter = NO_FILTER) {
  const onChange = vi.fn();
  const user = userEvent.setup();
  const view = render(<Harness start={filter} onChange={onChange} />);
  return { onChange, user, view };
}

/**
 * The filters with the filter kept as the page keeps it.
 *
 * @param props - The first filter and the spy.
 * @param props.start - The first filter.
 * @param props.onChange - Told of every change.
 * @returns The filters.
 */
function Harness({ start, onChange }: { start: GrantFilter; onChange: Mock }): JSX.Element {
  const [filter, setFilter] = useState(start);
  return (
    <GrantFilters
      filter={filter}
      environments={ENVIRONMENTS}
      onChange={(next, step) => {
        onChange(next, step);
        setFilter(next);
      }}
    />
  );
}

/**
 * Chooses an option of a filter's menu.
 *
 * @param user - The user-event session.
 * @param menu - The menu's label.
 * @param option - The option's words.
 */
async function choose(
  user: ReturnType<typeof userEvent.setup>,
  menu: RegExp,
  option: string,
): Promise<void> {
  await user.click(screen.getByRole("button", { name: menu }));
  await user.click(await screen.findByRole("option", { name: option }));
}

describe("GrantFilters", () => {
  it("writes each choice to the address", async () => {
    const { onChange, user } = renderFilters();

    await choose(user, /Who$/, "A group");
    expect(onChange).not.toHaveBeenCalled();
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(name).toHaveAccessibleDescription("Applied on Enter or when you leave the field.");
    await user.type(name, "analysts{Enter}");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, who: { kind: "group", name: "analysts" } },
      "replace",
    );
  });

  it("applies the name when the field is left, and once only", async () => {
    const filter: GrantFilter = { ...NO_FILTER, who: { kind: "group", name: "analysts" } };
    const { onChange, user } = renderFilters(filter);

    const name = screen.getByRole("textbox", { name: "Name" });
    await user.type(name, "{Enter}");
    await user.tab();
    expect(onChange).not.toHaveBeenCalled();
    await user.clear(name);
    await user.type(name, "ops");
    await user.tab();
    expect(onChange).toHaveBeenCalledWith(
      { ...NO_FILTER, who: { kind: "group", name: "ops" } },
      "replace",
    );
  });

  it("goes back to anyone, and to anything", async () => {
    const filter: GrantFilter = {
      who: { kind: "principal", name: "maria" },
      what: { kind: "role", role: "ANALYST" },
      where: null,
    };
    const { onChange, user } = renderFilters(filter);

    await choose(user, /Who$/, "Anyone");
    expect(onChange).toHaveBeenLastCalledWith({ ...filter, who: null }, "push");
    await choose(user, /What$/, "Any role or action");
    expect(onChange).toHaveBeenLastCalledWith(NO_FILTER, "push");
  });

  it("a person chosen after a name keeps the name", async () => {
    const filter: GrantFilter = { ...NO_FILTER, who: { kind: "group", name: "maria" } };
    const { onChange, user } = renderFilters(filter);

    await choose(user, /Who$/, "A person");

    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, who: { kind: "principal", name: "maria" } },
      "push",
    );
  });

  it("the What menu has the roles and the actions in two sections", async () => {
    const { onChange, user } = renderFilters();

    await user.click(screen.getByRole("button", { name: /What$/ }));
    const listbox = await screen.findByRole("listbox");
    const groups = within(listbox).getAllByRole("group");
    expect(groups).toHaveLength(2);
    expect(within(groups[0] as HTMLElement).getAllByRole("option")).toHaveLength(5);
    expect(within(groups[1] as HTMLElement).getAllByRole("option")).toHaveLength(26);
    await user.click(within(groups[1] as HTMLElement).getByRole("option", { name: "Run queries" }));
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, what: { kind: "action", action: "SUBMIT_QUERY" } },
      "push",
    );
    await choose(user, /What$/, "ANALYST");
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, what: { kind: "role", role: "ANALYST" } },
      "push",
    );
  });

  it("Where asks for what each kind needs", async () => {
    const { onChange, user } = renderFilters();

    await choose(user, /On exactly$/, "The whole instance");
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, where: { kind: "INSTANCE" } },
      "push",
    );

    await choose(user, /On exactly$/, "An environment");
    expect(screen.getByRole("button", { name: /^Environment/ })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Namespace" })).toBeNull();
    await choose(user, /^Environment/, "dev");
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, where: { kind: "ENVIRONMENT", environment: "dev" } },
      "push",
    );

    await choose(user, /On exactly$/, "A table");
    expect(screen.getByRole("textbox", { name: "Namespace" })).toBeInTheDocument();
    const calls = onChange.mock.calls.length;
    await user.type(screen.getByRole("textbox", { name: "Namespace" }), "sales{Enter}");
    expect(onChange).toHaveBeenCalledTimes(calls);
    await user.type(screen.getByRole("textbox", { name: "Table" }), "orders{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(
      {
        ...NO_FILTER,
        where: { kind: "TABLE", environment: "dev", namespace: "sales", table: "orders" },
      },
      "replace",
    );

    await choose(user, /On exactly$/, "A namespace");
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_FILTER, where: { kind: "NAMESPACE", environment: "dev", namespace: "sales" } },
      "push",
    );
    expect(screen.queryByRole("textbox", { name: "Table" })).toBeNull();

    await choose(user, /On exactly$/, "Anywhere");
    expect(onChange).toHaveBeenLastCalledWith(NO_FILTER, "push");
  });

  it("waits for an environment before an environment's grants are asked for", async () => {
    const { onChange, user } = renderFilters();

    await choose(user, /On exactly$/, "A namespace");

    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows a filter from elsewhere, such as Clear filters", () => {
    const filter: GrantFilter = {
      who: { kind: "principal", name: "maria" },
      what: null,
      where: { kind: "TABLE", environment: "dev", namespace: "sales", table: "orders" },
    };
    const onChange = vi.fn();
    const view = render(
      <GrantFilters filter={filter} environments={ENVIRONMENTS} onChange={onChange} />,
    );
    expect(screen.getByRole("textbox", { name: "Table" })).toHaveValue("orders");

    view.rerender(
      <GrantFilters filter={NO_FILTER} environments={ENVIRONMENTS} onChange={onChange} />,
    );

    expect(screen.queryByRole("textbox", { name: "Table" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
  });
});
