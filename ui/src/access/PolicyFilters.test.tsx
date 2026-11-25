import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { describe, expect, it, type Mock, vi } from "vitest";

import { NO_POLICY_FILTER, type PolicyFilter } from "../api/policies";
import { initI18n } from "../i18n/i18n";
import { PolicyFilters } from "./PolicyFilters";

initI18n();

const ENVIRONMENTS = [
  { name: "dev", protected: false },
  { name: "prod", protected: true },
];

/**
 * The filters with the filter kept as the page keeps it.
 *
 * @param props - The first filter and the spy.
 * @param props.start - The first filter.
 * @param props.onChange - Told of every change.
 * @param props.environments - The environments listed; null while not read.
 * @returns The filters.
 */
function Harness({
  start,
  onChange,
  environments = ENVIRONMENTS,
}: {
  start: PolicyFilter;
  onChange: Mock;
  environments?: typeof ENVIRONMENTS | null;
}): JSX.Element {
  const [filter, setFilter] = useState(start);
  return (
    <PolicyFilters
      filter={filter}
      environments={environments}
      onChange={(next, step) => {
        onChange(next, step);
        setFilter(next);
      }}
    />
  );
}

/**
 * Renders the filters.
 *
 * @param start - The filter shown.
 * @param environments - The environments listed; null while not read.
 * @returns The change handler and a user-event session.
 */
function renderFilters(
  start: PolicyFilter = NO_POLICY_FILTER,
  environments: typeof ENVIRONMENTS | null = ENVIRONMENTS,
) {
  const onChange = vi.fn();
  const user = userEvent.setup();
  render(<Harness start={start} onChange={onChange} environments={environments} />);
  return { onChange, user };
}

describe("PolicyFilters", () => {
  it("writes each choice to the filter", async () => {
    const { onChange, user } = renderFilters();

    await user.click(screen.getByRole("button", { name: /^Environment/ }));
    await user.click(await screen.findByRole("option", { name: "dev" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...NO_POLICY_FILTER, environment: "dev" }, "push");
    const table = screen.getByRole("combobox", { name: "Table (namespace.table)" });
    await user.type(table, "gold.station_ranking{Enter}");

    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_POLICY_FILTER, environment: "dev", namespace: "gold", table: "station_ranking" },
      "replace",
    );
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("applies the table when the field is left, and clears it with the environment", async () => {
    const { onChange, user } = renderFilters({ ...NO_POLICY_FILTER, environment: "dev" });

    await user.type(screen.getByRole("combobox", { name: "Table (namespace.table)" }), "a.b.c");
    await user.tab();
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_POLICY_FILTER, environment: "dev", namespace: "a.b", table: "c" },
      "replace",
    );
    await user.click(screen.getByRole("button", { name: /^Environment/ }));
    await user.click(await screen.findByRole("option", { name: "All environments" }));

    expect(onChange).toHaveBeenLastCalledWith(NO_POLICY_FILTER, "push");
    expect(screen.getByRole("combobox", { name: "Table (namespace.table)" })).toHaveValue("");
  });

  it("the table waits for an environment", () => {
    renderFilters();

    expect(screen.getByRole("combobox", { name: "Table (namespace.table)" })).toBeDisabled();
  });

  it("refuses a table without a namespace", async () => {
    const { onChange, user } = renderFilters({ ...NO_POLICY_FILTER, environment: "dev" });
    const table = screen.getByRole("combobox", { name: "Table (namespace.table)" });

    await user.type(table, "station_ranking{Enter}");

    expect(
      screen.getByText("Write the table as namespace.table, such as gold.station_ranking."),
    ).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    await user.type(table, "x");
    expect(screen.queryByText(/Write the table as/)).not.toBeInTheDocument();
  });

  it("emptying the table lists every table again, and the same table changes nothing", async () => {
    const start = { ...NO_POLICY_FILTER, environment: "dev", namespace: "gold", table: "t" };
    const { onChange, user } = renderFilters(start);
    const table = screen.getByRole("combobox", { name: "Table (namespace.table)" });

    await user.type(table, "{Enter}");
    expect(onChange).not.toHaveBeenCalled();
    await user.clear(table);
    await user.type(table, "{Enter}");

    expect(onChange).toHaveBeenLastCalledWith(
      { ...start, namespace: null, table: null },
      "replace",
    );
  });

  it("filters by who", async () => {
    const { onChange, user } = renderFilters();

    await user.click(screen.getByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "A group" }));
    await user.type(screen.getByRole("combobox", { name: "Name" }), "analysts{Enter}");

    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_POLICY_FILTER, who: { kind: "group", name: "analysts" } },
      "replace",
    );
  });

  it("takes a typed environment from a person who may list none", async () => {
    const { onChange, user } = renderFilters(NO_POLICY_FILTER, []);
    const environment = screen.getByRole("textbox", { name: "Environment" });

    await user.type(environment, "dev{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(
      { ...NO_POLICY_FILTER, environment: "dev" },
      "replace",
    );
    await user.type(environment, "{Enter}");
    expect(onChange).toHaveBeenCalledTimes(1);
    await user.clear(environment);
    await user.tab();

    expect(onChange).toHaveBeenLastCalledWith(NO_POLICY_FILTER, "replace");
  });

  it("offers the picker while the environments are read", () => {
    renderFilters(NO_POLICY_FILTER, null);

    expect(screen.getByRole("button", { name: /^Environment/ })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Environment" })).toBeNull();
  });
});
