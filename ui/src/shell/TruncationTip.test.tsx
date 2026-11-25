import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TruncationTip } from "./TruncationTip";

/**
 * Renders a list row whose name is cut or not, with the tooltip beside it. jsdom lays nothing out,
 * so the widths that tell a cut name are set by hand.
 *
 * @param cut - Whether the name is wider than its box.
 * @returns The name's link.
 */
function renderRow(cut: boolean): HTMLElement {
  render(
    <>
      <table className="list">
        <tbody>
          <tr>
            <td>
              <a className="row-name" href="/catalog/prod/gold.sales/fact_sales_by_store_and_day">
                fact_sales_by_store_and_day
              </a>
            </td>
          </tr>
        </tbody>
      </table>
      <TruncationTip />
    </>,
  );
  const link = screen.getByRole("link");
  Object.defineProperty(link, "scrollWidth", { value: cut ? 400 : 100 });
  Object.defineProperty(link, "clientWidth", { value: 100 });
  link.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
  return link;
}

/**
 * The tooltip, if it shows.
 *
 * @returns Its element, or null.
 */
function tip(): Element | null {
  return document.querySelector(".truncation-tip");
}

describe("TruncationTip", () => {
  it("shows a cut name whole under it while it has the focus, hidden from screen readers", () => {
    const link = renderRow(true);

    act(() => {
      link.focus();
    });

    expect(tip()).toHaveTextContent("fact_sales_by_store_and_day");
    expect(tip()).toHaveAttribute("aria-hidden", "true");
    expect(tip()).toHaveStyle({ top: "38px", left: "10px" });

    act(() => {
      link.blur();
    });
    expect(tip()).toBeNull();
  });

  it("shows it while the pointer rests on the name, not elsewhere in the row", () => {
    const link = renderRow(true);

    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });
    expect(tip()).toHaveTextContent("fact_sales_by_store_and_day");

    fireEvent.pointerMove(link, { clientX: 500, clientY: 30 });
    expect(tip()).toBeNull();
  });

  it("closes on Escape", () => {
    const link = renderRow(true);
    act(() => {
      link.focus();
    });

    fireEvent.keyDown(document, { key: "a" });
    expect(tip()).not.toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });

    expect(tip()).toBeNull();
  });

  it("shows nothing for a name that fits", () => {
    const link = renderRow(false);

    act(() => {
      link.focus();
    });
    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });

    expect(tip()).toBeNull();
  });

  it("stays open while the pointer moves onto it, and on a second move over the name", () => {
    const link = renderRow(true);
    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });
    fireEvent.pointerMove(link, { clientX: 60, clientY: 30 });

    const shown = tip();
    expect(shown).not.toBeNull();
    fireEvent.pointerMove(shown as Element, { clientX: 50, clientY: 40 });

    expect(tip()).toHaveTextContent("fact_sales_by_store_and_day");
  });

  it("stays open for the focused name when the pointer leaves it, and closes on a scroll", () => {
    const link = renderRow(true);
    act(() => {
      link.focus();
    });

    fireEvent.pointerMove(document.body, { clientX: 500, clientY: 500 });
    expect(tip()).not.toBeNull();

    fireEvent.scroll(window);
    expect(tip()).toBeNull();
  });

  it("ignores a move over no element, and focus on something that is not cut", () => {
    renderRow(true);
    fireEvent.pointerMove(document, { clientX: 50, clientY: 30 });
    expect(tip()).toBeNull();

    const button = document.createElement("button");
    document.body.append(button);
    act(() => {
      button.focus();
    });
    expect(tip()).toBeNull();
    button.remove();
  });

  it("stops listening when it goes", () => {
    const { unmount } = render(<TruncationTip />);
    unmount();

    expect(() => {
      fireEvent.keyDown(document, { key: "Escape" });
      fireEvent.scroll(window);
    }).not.toThrow();
  });

  it("makes a cut name without a page reachable by Tab, and takes that back once it fits", async () => {
    render(<TruncationTip />);
    const table = document.createElement("table");
    table.className = "list";
    const name = document.createElement("span");
    name.className = "row-name";
    name.textContent = "gold.sales.fact_returns_dropped_last_week";
    let wide = 400;
    Object.defineProperty(name, "scrollWidth", { get: () => wide });
    Object.defineProperty(name, "clientWidth", { value: 100 });
    name.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
    table.append(name);

    await act(async () => {
      document.body.append(table);
      await Promise.resolve();
    });
    expect(name).toHaveAttribute("tabindex", "0");
    act(() => {
      name.focus();
    });
    expect(tip()).toHaveTextContent("gold.sales.fact_returns_dropped_last_week");
    act(() => {
      name.blur();
    });

    wide = 100;
    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await Promise.resolve();
    });
    expect(name).not.toHaveAttribute("tabindex");
    table.remove();
  });

  it("leaves a name's own tabindex alone", async () => {
    render(<TruncationTip />);
    const table = document.createElement("table");
    table.className = "list";
    const name = document.createElement("span");
    name.className = "row-name";
    name.tabIndex = -1;
    Object.defineProperty(name, "scrollWidth", { value: 400 });
    Object.defineProperty(name, "clientWidth", { value: 100 });
    table.append(name);

    await act(async () => {
      document.body.append(table);
      await Promise.resolve();
    });

    expect(name).toHaveAttribute("tabindex", "-1");
    table.remove();
  });

  it("closes when its name leaves the page, as when a list is read again", async () => {
    const row = (
      <table className="list">
        <tbody>
          <tr>
            <td>
              <a className="row-name" href="/catalog/prod/gold.sales/fact_sales_by_store_and_day">
                fact_sales_by_store_and_day
              </a>
            </td>
          </tr>
        </tbody>
      </table>
    );
    const { rerender } = render(
      <>
        {row}
        <TruncationTip />
      </>,
    );
    const link = screen.getByRole("link");
    Object.defineProperty(link, "scrollWidth", { value: 400 });
    Object.defineProperty(link, "clientWidth", { value: 100 });
    link.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });
    expect(tip()).not.toBeNull();

    await act(async () => {
      rerender(<TruncationTip />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(tip()).toBeNull();
  });

  it("closes when a name it shows leaves the page outside React", async () => {
    render(<TruncationTip />);
    const table = document.createElement("table");
    table.className = "list";
    const name = document.createElement("span");
    name.className = "row-name";
    name.textContent = "gold.sales.fact_returns_dropped_last_week";
    Object.defineProperty(name, "scrollWidth", { value: 400 });
    Object.defineProperty(name, "clientWidth", { value: 100 });
    name.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
    table.append(name);
    document.body.append(table);
    fireEvent.pointerMove(name, { clientX: 50, clientY: 30 });
    expect(tip()).not.toBeNull();

    await act(async () => {
      table.remove();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(tip()).toBeNull();
  });

  it("keeps the stop of a name that fits again while it has the focus", async () => {
    render(<TruncationTip />);
    const table = document.createElement("table");
    table.className = "list";
    const name = document.createElement("span");
    name.className = "row-name";
    name.textContent = "gold.sales.fact_returns_dropped_last_week";
    let wide = 400;
    Object.defineProperty(name, "scrollWidth", { get: () => wide });
    Object.defineProperty(name, "clientWidth", { value: 100 });
    name.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
    table.append(name);
    await act(async () => {
      document.body.append(table);
      await Promise.resolve();
    });
    act(() => {
      name.focus();
    });

    wide = 100;
    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await Promise.resolve();
    });

    expect(name).toHaveAttribute("tabindex", "0");
    expect(name).toHaveFocus();
    // Once the focus leaves, the stop goes at once, with no other change to the page (the tip was
    // already closed with Escape, so closing it changes nothing either).
    fireEvent.keyDown(document, { key: "Escape" });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(name).toHaveAttribute("tabindex", "0");
    await act(async () => {
      name.blur();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(name).not.toHaveAttribute("tabindex");
    table.remove();
  });

  it("shows the sentence of an element with data-tip on hover and on focus, cut or not", () => {
    render(
      <>
        <span data-tip="Waits for run 1b2c3d4e of this project to finish.">Waiting</span>
        <TruncationTip />
      </>,
    );
    const pill = screen.getByText("Waiting");
    pill.getBoundingClientRect = () => new DOMRect(10, 20, 60, 20);

    fireEvent.pointerMove(pill, { clientX: 30, clientY: 30 });
    expect(tip()).toHaveTextContent("Waits for run 1b2c3d4e of this project to finish.");
    expect(tip()).toHaveStyle({ top: "40px", left: "10px" });
    // It is made a Tab stop for good, not as a cut name that may fit again.
    expect(pill).toHaveAttribute("tabindex", "0");
    expect(pill).not.toHaveAttribute("data-cut-stop");

    fireEvent.pointerMove(document.body, { clientX: 500, clientY: 500 });
    expect(tip()).toBeNull();

    act(() => {
      pill.focus();
    });
    expect(tip()).toHaveTextContent("Waits for run 1b2c3d4e");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(tip()).toBeNull();
  });

  it("shows nothing for an empty data-tip", () => {
    render(
      <>
        <span data-tip="">Waiting</span>
        <TruncationTip />
      </>,
    );
    const pill = screen.getByText("Waiting");
    pill.getBoundingClientRect = () => new DOMRect(10, 20, 60, 20);
    fireEvent.pointerMove(pill, { clientX: 30, clientY: 30 });
    expect(tip()).toBeNull();
    // With nothing to show, the keyboard gets no stop either.
    expect(pill).not.toHaveAttribute("tabindex");
    act(() => {
      pill.focus();
    });
    expect(tip()).toBeNull();
  });
});
